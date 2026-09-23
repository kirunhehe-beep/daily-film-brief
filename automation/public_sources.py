"""Public publisher pages: no login, private API, generated facts or invented dates.

Only store headline, a short publisher excerpt, original date/link and declared
media. Selectors were checked against publisher pages on 2026-09-23.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


class Node:
    def __init__(self, tag="root", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def all(self, tag=None, cls=None):
        for child in self.children:
            if isinstance(child, Node):
                if (tag is None or child.tag == tag) and (cls is None or cls in child.attrs.get("class", "").split()):
                    yield child
                yield from child.all(tag, cls)

    def text(self):
        return " ".join(child.text() if isinstance(child, Node) else child for child in self.children
                        if not isinstance(child, Node) or child.tag not in {"script", "style"})


class Tree(HTMLParser):
    VOID = {"img", "meta", "link", "input", "br", "hr", "source", "area", "base", "embed", "wbr"}

    def __init__(self, document):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, [(key, value or "") for key, value in attrs])
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def text(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def first(node, tag=None, cls=None):
    return next(node.all(tag, cls), Node())


def safe_url(value, base=""):
    if not value:
        return ""
    url = urljoin(base, html.unescape(value))
    parts = urlsplit(url)
    return url if parts.scheme in {"http", "https"} and parts.hostname and not parts.username else ""


def local_date(value):
    """These Chinese publisher pages explicitly use local +08:00 wall times."""
    match = re.search(r"(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})(?:日)?(?:\s*)(\d{1,2}):(\d{2})(?::(\d{2}))?", value or "")
    if not match:
        return ""
    parts = [int(v or 0) for v in match.groups()]
    try:
        return dt.datetime(*parts, tzinfo=dt.timezone(dt.timedelta(hours=8))).isoformat()
    except ValueError:
        return ""


class PublicItems(list):
    def __init__(self, rows, errors):
        super().__init__(rows)
        self.status = "partial" if errors else "ok"
        self.checks = errors


def parse_listing(source, body):
    root = Tree(body.decode("utf-8", errors="replace")).root
    kind, base = source["type"], source["url"]
    rows = {}
    if kind == "mtime_news":
        for card in root.all("li"):
            clock = first(card, "span", "news-time")
            if not clock.children:
                continue
            heading = first(card, "h4")
            anchor = first(heading, "a")
            url = safe_url(anchor.attrs.get("href"), base)
            if urlsplit(url).hostname != "content.mtime.com":
                continue
            rows[url] = {"title": text(anchor.text()), "url": url,
                         "summary": text(first(card, "p").text())[:500],
                         "published": local_date(clock.text()),
                         "image_url": safe_url(first(card, "img").attrs.get("src"), base), "video_url": ""}
    elif kind == "bjnews_ent":
        for card in root.all("div", "pin_demo"):
            anchor = first(card, "a")
            url = safe_url(anchor.attrs.get("href"), base)
            if not (urlsplit(url).hostname or "").endswith(".bjnews.com.cn") or "/detail/" not in url:
                continue
            rows[url] = {"title": text(anchor.text()), "url": url, "summary": "", "published": "",
                         "image_url": safe_url(first(card, "img").attrs.get("src"), base), "video_url": ""}
    elif kind == "people_culture":
        keywords = source.get("discovery_keywords", ["电影", "影视", "剧集", "票房", "电视剧", "影片", "院线"])
        for anchor in root.all("a"):
            title = text(anchor.text())
            url = safe_url(anchor.attrs.get("href"), base)
            if urlsplit(url).hostname != urlsplit(base).hostname or not re.search(r"/n1/\d{4}/\d{4}/", url):
                continue
            if not any(word in title for word in keywords):
                continue
            if url in rows and len(rows[url]["title"]) <= len(title):
                continue
            rows[url] = {"title": title, "url": url, "summary": "", "published": "", "image_url": "", "video_url": ""}
    else:
        raise ValueError("Unsupported public page type")
    if not rows:
        raise ValueError("公开列表没有解析到目标新闻，需检查页面结构或主题覆盖")
    return list(rows.values())


def enrich_detail(source, item, body):
    document = body.decode("utf-8", errors="replace")
    root = Tree(document).root
    updated = dict(item)
    if source["type"] == "mtime_news":
        marker = re.search(r"window\.__INITIAL_STATE__\s*=\s*", document)
        if not marker:
            raise ValueError("时光网原文缺少结构化时间")
        state, _ = json.JSONDecoder().raw_decode(document[marker.end():])
        content = state["content"]
        updated["published"] = local_date(content.get("userCreateTime", {}).get("show", ""))
        article = Tree(content.get("body") or "").root
        # Prefer list teaser; never replicate an entire article.
        updated["summary"] = item["summary"] or text(first(article, "p").text())[:500]
        updated["video_url"] = safe_url(first(article, "video").attrs.get("src"))
        updated["image_url"] = item["image_url"] or safe_url(first(article, "img").attrs.get("src"))
    elif source["type"] == "bjnews_ent":
        updated["published"] = local_date(first(root, "span", "timer").text())
        article = first(root, "div", "article-text")
        updated["summary"] = next((text(p.text())[:500] for p in article.all("p") if len(text(p.text())) > 30), "")
        updated["video_url"] = safe_url(first(article, "video").attrs.get("src"))
    else:
        clocks = [n for n in root.all() if n.attrs.get("id") == "newstime"]
        updated["published"] = local_date(clocks[0].text()) if clocks else ""
        meta = {n.attrs.get("name", ""): n.attrs.get("content", "") for n in root.all("meta")}
        updated["summary"] = text(meta.get("description", ""))[:500]
        headline = text(first(root, "h1").text())
        if headline:
            updated["title"] = headline
    if not updated["published"]:
        raise ValueError("原文发布时间未解析，不能使用抓取时间代替")
    return updated


def fetch_public_source(source, fetch_bytes):
    rows = parse_listing(source, fetch_bytes(source["url"], "text/html"))
    # Site-specific pages are already bounded. Date-only parsing needs no detail;
    # undated/relative-date cards must read the original timestamp.
    def read(item):
        if item["published"] and not source.get("read_details", False):
            return item, None
        try:
            return enrich_detail(source, item, fetch_bytes(item["url"], "text/html")), None
        except Exception as exc:
            # Keep the list entry in the persistent store, visibly undated.
            return item, {"url": item["url"], "status": "detail_unavailable", "message": type(exc).__name__}
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(read, rows))
    return PublicItems([row for row, _ in results], [error for _, error in results if error])
