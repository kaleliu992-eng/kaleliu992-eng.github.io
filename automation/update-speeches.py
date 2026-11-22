"""Build a weekly ReadFlow selection from Federal Reserve Board speeches."""

import argparse
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
from io import BytesIO
import json
import os
from pathlib import Path
import re
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo


HOST = "www.federalreserve.gov"
BASE = f"https://{HOST}"
FEED = f"{BASE}/feeds/speeches.xml"
LICENSE = f"{BASE}/disclaimer.htm"
AGENT = "ReadFlowSpeeches/1.0 (weekly Federal Reserve Board public feed reader)"
PUBLISHER = "Board of Governors of the Federal Reserve System"
CATALOG = "catalog-v1.json"
BATCH_SIZE = 7
ARCHIVE_YEARS = 10
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
SPEECH_PATH = re.compile(r"/newsevents/speech/([a-z]+(\d{8})[a-z])\.htm$")
BOARD_ROLE = re.compile(r"^(?:Governor|Chair(?:man|woman)?|Vice Chair(?:man|woman)?(?: for Supervision)?) .+")
BOARD_BIO = re.compile(r"/aboutthefed/bios/board/[a-z0-9_-]+\.htm$")
RESTRICTED = re.compile(
    r"©|\ball rights reserved\b|\bcopyright\b|\b(?:reprinted|reproduced|used) with permission\b"
    r"|\b(?:appear(?:s|ed)|published) by permission\b|\b(?:photo|image|chart|figure) (?:credit|courtesy)\b",
    re.I,
)
VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
EXCLUDED = frozenset("script style noscript iframe video audio figure img table svg".split())
MONTHS = "January February March April May June July August September October November December".split()
XHTML = "http://www.w3.org/1999/xhtml"


class SourceError(Exception):
    """An unavailable or changed source prevents a reliable extraction."""


class BoardRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ensure_board_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def ensure_board_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != HOST:
        raise SourceError(f"Unexpected source host: {url}")


class BoardSource:
    def __init__(self):
        self.client = build_opener(BoardRedirects())
        self.last_request = 0.0
        self.robots = RobotFileParser()
        self.delay = 0.3
        robots_url = f"{BASE}/robots.txt"
        try:
            self.robots.parse(self._download(robots_url).decode("utf-8-sig").splitlines())
        except HTTPError as error:
            if error.code not in (404, 410):
                raise
            self.robots.allow_all = True
        self.delay = max(self.delay, self.robots.crawl_delay("ReadFlowSpeeches") or 0)

    def _download(self, url):
        ensure_board_url(url)
        remaining = self.last_request + self.delay - monotonic()
        if remaining > 0:
            sleep(remaining)
        self.last_request = monotonic()
        req = Request(url, headers={"User-Agent": AGENT, "Accept": "text/html, application/rss+xml, text/xml, text/plain"})
        with self.client.open(req, timeout=20) as response:
            ensure_board_url(response.geturl())
            data = response.read(4_000_001)
            if len(data) > 4_000_000:
                raise SourceError(f"Unexpectedly large response from {url}")
            return data

    def read(self, url):
        if not self.robots.can_fetch("ReadFlowSpeeches", url):
            raise SourceError(f"Board robots.txt does not permit {url}")
        return self._download(url)


class Node:
    def __init__(self, tag, attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def classes(self):
        return self.attrs.get("class", "").split()

    def descendants(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.descendants()

    def text(self):
        if self.tag in EXCLUDED:
            return ""
        if self.tag == "br":
            return " "
        if self.tag == "a" and self.attrs.get("href", "").startswith("#fn"):
            return ""
        return "".join(child.text() if isinstance(child, Node) else child for child in self.children)


class PageParser(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]
        self.feed(html.decode("utf-8-sig"))
        self.close()

    def handle_starttag(self, tag, attrs):
        self._add(tag, attrs, tag not in VOID)

    def handle_startendtag(self, tag, attrs):
        self._add(tag, attrs, False)

    def _add(self, tag, attrs, push):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if push:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def clean(text):
    return " ".join(text.split())


def field(nodes, class_name):
    return next((node for node in nodes if class_name in node.classes()), None)


def date_in_slug(value):
    return date(int(value[:4]), int(value[4:6]), int(value[6:]))


def candidate(link):
    url = urljoin(BASE, link)
    parsed = urlsplit(url)
    match = SPEECH_PATH.fullmatch(parsed.path)
    if parsed.scheme == "https" and parsed.netloc == HOST and match:
        return f"fed-{match[1]}", f"{BASE}{parsed.path}", date_in_slug(match[2])
    return None


def rss_links(data):
    root = ET.fromstring(data)
    return [item.findtext("link", "").strip() for item in root.findall("./channel/item") if item.findtext("category") == "Speech"]


def archive_links(data):
    root = PageParser(data).root
    return [link.attrs.get("href", "") for row in root.descendants() if "eventlist__event" in row.classes()
            for link in row.descendants() if link.tag == "a"]


def parse_speech(data, speech_id, url, source_date):
    root = PageParser(data).root
    article = next((node for node in root.descendants() if node.attrs.get("id") == "article"), None)
    if not article:
        raise SourceError("Missing official speech article")
    nodes = list(article.descendants())
    title, speaker, time = (field(nodes, name) for name in ("title", "speaker", "article__time"))
    if not all((title, speaker, time)):
        raise SourceError("Missing title, speaker, or date")
    speaker_name = clean(speaker.text())
    if not BOARD_ROLE.fullmatch(speaker_name):
        raise SourceError("Speaker is not identified as a member of the Board")
    bio = [child for child in speaker.descendants() if child.tag == "a"]
    if len(bio) != 1 or not BOARD_BIO.fullmatch(bio[0].attrs.get("href", "")) or clean(bio[0].text()) != speaker_name:
        raise SourceError("Speaker is not linked to a single official Board member biography")
    month, day, year = clean(time.text()).replace(",", "").split()
    source_header_date = date(int(year), MONTHS.index(month) + 1, int(day))
    if source_header_date != source_date:
        raise SourceError("Speech date does not match its official link")

    body = next((node for node in article.children if isinstance(node, Node) and "col-md-8" in node.classes()
                 and "heading" not in node.classes()), None)
    if body is None:
        raise SourceError("Missing official speech text container")
    if RESTRICTED.search(clean(body.text())):
        raise SourceError("A copyright or third-party attribution appeared in the speech text")
    if any(node.tag in ("blockquote", "q") for node in body.descendants()):
        raise SourceError("A separately marked quotation requires rights review")

    blocks = []
    for block in body_blocks(body):
        value = clean(block.text())
        if value:
            blocks.append(("p" if block.tag in ("p", "li") else "h2", value))
    if sum(tag == "p" for tag, _ in blocks) < 3 or sum(len(text) for _, text in blocks) < 500:
        raise SourceError("Speech page does not have enough identifiable original prose")
    title_text = clean(title.text())
    if not title_text:
        raise SourceError("Speech title is empty")
    return {"id": speech_id, "title": title_text, "speaker": speaker_name, "date": source_date.isoformat(),
            "sourceUrl": url, "epub": f"{speech_id}.epub"}, blocks


def body_blocks(node):
    for child in node.children:
        if not isinstance(child, Node) or child.tag in EXCLUDED:
            continue
        if child.tag == "hr" or any(link.tag == "a" and link.attrs.get("name", "").startswith("fn")
                                    for link in child.descendants()):
            return
        if child.tag in ("p", "li", "h2", "h3", "h4", "h5", "h6"):
            yield child
        else:
            yield from body_blocks(child)


def build_epub(item, blocks):
    title, speaker, speech_id = (escape(item[key]) for key in ("title", "speaker", "id"))
    url = escape(item["sourceUrl"], quote=True)
    published = item["date"]
    prose = "\n".join(f"<{tag}>{escape(text)}</{tag}>" for tag, text in blocks)
    files = {
        "META-INF/container.xml": """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>
<rootfile full-path="EPUB/book.opf" media-type="application/oebps-package+xml"/>
</rootfiles></container>""",
        "EPUB/book.opf": f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="publication-id" version="3.0" xml:lang="en">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="publication-id">urn:readflow:speech:{speech_id}</dc:identifier>
<dc:title>{title}</dc:title><dc:creator>{speaker}</dc:creator><dc:language>en</dc:language>
<dc:source>{url}</dc:source><dc:date>{published}</dc:date><dc:publisher>{PUBLISHER}</dc:publisher>
<dc:rights>Public domain under the Federal Reserve Board website disclaimer.</dc:rights>
<meta property="dcterms:modified">{published}T00:00:00Z</meta></metadata>
<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="speech" href="speech.xhtml" media-type="application/xhtml+xml"/></manifest>
<spine><itemref idref="speech"/></spine></package>""",
        "EPUB/nav.xhtml": f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="{XHTML}" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en">
<head><title>Contents</title></head><body><nav epub:type="toc" id="toc"><h1>Contents</h1>
<ol><li><a href="speech.xhtml#main">{title}</a></li></ol></nav></body></html>""",
        "EPUB/speech.xhtml": f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="{XHTML}" lang="en" xml:lang="en"><head><title>{title}</title></head>
<body><article id="main"><header><h1>{title}</h1><p>{speaker}</p><p>{published}</p></header>
<div id="speech-text">{prose}</div>
<footer><p>Source: {PUBLISHER}. <a href="{url}">Original speech</a>.
<a href="{LICENSE}">Federal Reserve Board reuse policy</a>.</p></footer></article></body></html>""",
    }
    buffer = BytesIO()
    stamp = date.fromisoformat(published)
    timestamp = (stamp.year, stamp.month, stamp.day, 0, 0, 0)
    with ZipFile(buffer, "w") as epub:
        epub.writestr(ZipInfo("mimetype", timestamp), b"application/epub+zip", compress_type=ZIP_STORED)
        for name, content in files.items():
            ET.fromstring(content)
            epub.writestr(ZipInfo(name, timestamp), content.encode("utf-8"), compress_type=ZIP_DEFLATED)
    return buffer.getvalue()


def body_digest(texts):
    return sha256("\n".join(clean(text).casefold() for text in texts).encode("utf-8")).hexdigest()


def saved_body_digest(path):
    with ZipFile(path) as epub:
        root = ET.fromstring(epub.read("EPUB/speech.xhtml"))
    body = root.find(f".//{{{XHTML}}}div[@id='speech-text']")
    if body is None:
        raise SourceError(f"Saved speech is missing its original text: {path.name}")
    return body_digest("".join(block.itertext()) for block in body)


def saved_catalog(output):
    path = output / CATALOG
    previous = path.read_bytes() if path.is_file() else None
    catalog = json.loads(previous) if previous is not None else {"version": 1, "weeks": []}
    if catalog["version"] != 1 or not isinstance(catalog["weeks"], list):
        raise SourceError("Unsupported speech catalog")
    return previous, catalog


def collect_batch(source, cutoff, saved_ids, digests):
    found = []
    attempted = set(saved_ids)
    get_lists = [(FEED, rss_links)]
    get_lists.extend((f"{BASE}/newsevents/speech/{year}-speeches.htm", archive_links)
                     for year in range(cutoff.year, cutoff.year - ARCHIVE_YEARS, -1))
    for url, list_links in get_lists:
        try:
            links = [entry for link in list_links(source.read(url)) if (entry := candidate(link))]
        except (SourceError, URLError, ET.ParseError, ValueError) as error:
            print(f"Skipped source list {url}: {error}")
            continue
        for speech_id, speech_url, published in sorted(links, key=lambda entry: entry[2], reverse=True):
            if speech_id in attempted or published > cutoff:
                continue
            attempted.add(speech_id)
            try:
                item, blocks = parse_speech(source.read(speech_url), speech_id, speech_url, published)
            except (SourceError, URLError, ValueError) as error:
                print(f"Skipped {speech_id}: {error}")
                continue
            digest = body_digest(text for _, text in blocks)
            if digest in digests:
                continue
            digests.add(digest)
            found.append((item, build_epub(item, blocks)))
            if len(found) == BATCH_SIZE:
                return sorted(found, key=lambda pair: pair[0]["date"], reverse=True)
    raise SourceError(f"Found {len(found)} verified new speeches. A batch requires {BATCH_SIZE}. The site was preserved.")


def publish(output, previous, catalog, week, batch):
    output.mkdir(parents=True, exist_ok=True)
    next_weeks = [{"week": week, "speeches": [item for item, _ in batch]}, *catalog["weeks"]]
    new_catalog = {"version": 1, "weeks": sorted(next_weeks, key=lambda item: item["week"], reverse=True)}
    # The staging directory must inherit the output ACL. Python's private
    # TemporaryDirectory permissions follow files moved or hard-linked out.
    staging = output / f".readflow-speeches-{uuid4().hex}"
    staging.mkdir()
    try:
        for item, epub in batch:
            (staging / item["epub"]).write_bytes(epub)
        (staging / CATALOG).write_text(json.dumps(new_catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if saved_catalog(output)[0] != previous:
            raise SourceError("Another process modified the speech catalog during this run. Retry the update.")
        for item, epub in batch:
            target = output / item["epub"]
            try:
                os.link(staging / item["epub"], target)
            except FileExistsError:
                if target.read_bytes() != epub:
                    raise SourceError(f"Existing EPUB content differs: {target.name}")
        os.replace(staging / CATALOG, output / CATALOG)
    finally:
        for item, _ in batch:
            (staging / item["epub"]).unlink(missing_ok=True)
        (staging / CATALOG).unlink(missing_ok=True)
        staging.rmdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--week", type=date.fromisoformat, help="Monday in Asia/Shanghai, YYYY-MM-DD.")
    args = parser.parse_args()
    today = datetime.now(SHANGHAI).date()
    monday = args.week or today - timedelta(days=today.weekday())
    if monday.weekday() != 0 or monday > today:
        parser.error("--week must be a Monday no later than today in Asia/Shanghai")
    week = monday.isoformat()
    cutoff = min(monday + timedelta(days=6), today)
    previous, catalog = saved_catalog(args.output)
    saved_ids = set()
    digests = set()
    for group in catalog["weeks"]:
        if len(group["speeches"]) != BATCH_SIZE:
            raise SourceError(f"Existing speech batch is incomplete: {group['week']}")
        for item in group["speeches"]:
            speech_id = item["id"]
            if not re.fullmatch(r"fed-[a-z]+\d{8}[a-z]", speech_id) or item["epub"] != f"{speech_id}.epub":
                raise SourceError("Existing catalog has an invalid speech identifier")
            if speech_id in saved_ids:
                raise SourceError(f"Existing catalog repeats {speech_id}")
            saved_ids.add(speech_id)
            digests.add(saved_body_digest(args.output / item["epub"]))
    if any(group["week"] == week for group in catalog["weeks"]):
        print(f"ReadFlow already has a batch for {week}.")
        return
    batch = collect_batch(BoardSource(), cutoff, saved_ids, digests)
    publish(args.output, previous, catalog, week, batch)
    for item, _ in batch:
        print(f"{item['date']} {item['sourceUrl']} {item['title']}")
    print(f"Added {len(batch)} speeches for {week}.")


if __name__ == "__main__":
    try:
        main()
    except (SourceError, OSError, ValueError) as error:
        raise SystemExit(str(error)) from error
