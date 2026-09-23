#!/usr/bin/env python3
"""零人工审批的影视资讯采集器。

只使用可追溯来源的原始标题、摘要和链接；不把单源报道升级成已确认事实。
输出可被渲染器和发布任务消费的 JSON，不负责调用语言模型或直接部署。
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from item_store import load_store, merge_records, save_store
from weibo_source import SourceUnavailable, fetch_weibo
from public_sources import Tree, fetch_public_source, safe_url


UTC = dt.timezone.utc
BEIJING = ZoneInfo("Asia/Shanghai")
USER_AGENT = "DailyFilmBriefBot/1.0 (+https://daily-film-brief.pages.dev/)"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def clean_text(value: str | None) -> str:
    # Some feeds escape their HTML description more than once. Decode a few
    # layers before stripping tags so cards never expose literal `<img ...>`.
    text = value or ""
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    text = re.sub(r"<[^>]+>", " ", text)
    # A few feeds truncate descriptions inside an HTML tag. Drop that dangling
    # tail rather than printing markup-like text in the card.
    text = re.sub(r"<[^>]*$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_url(value: str) -> str:
    parts = urlsplit(value)
    # Article IDs often live in the query. Only remove known tracking fields.
    query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), urlencode(query), ""))


def title_key(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value.lower())[:160]


def published_at(value: str) -> dt.datetime | None:
    """Normalise RSS/Atom dates without inventing a timestamp when absent."""
    raw = clean_text(value)
    if not raw:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def child_text(entry: ET.Element, names: set[str]) -> str:
    for child in entry.iter():
        if child is entry:
            continue
        if local_name(child.tag) in names and clean_text(child.text):
            return clean_text(child.text)
    return ""


def child_raw_text(entry: ET.Element, names: set[str]) -> str:
    for child in entry.iter():
        if child is entry:
            continue
        if local_name(child.tag) in names and (child.text or "").strip():
            return child.text or ""
    return ""


def decoded_markup(value: str) -> str:
    text = value or ""
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text


def media_urls(entry: ET.Element, raw_summary: str) -> tuple[str, str]:
    """Extract only source-provided media; never invent a poster or trailer."""
    image_url = ""
    video_url = ""
    for child in entry.iter():
        url = (child.attrib.get("url") or "").strip()
        media_type = (child.attrib.get("type") or "").lower()
        name = local_name(child.tag)
        if not url.startswith(("https://", "http://")):
            continue
        if not image_url and (name == "thumbnail" or media_type.startswith("image/")):
            image_url = url
        if not video_url and media_type.startswith("video/"):
            video_url = url

    markup = decoded_markup(raw_summary)
    urls = re.findall(r'(?:src|href|_src)=["\'](https?://[^"\']+)', markup, flags=re.I)
    for url in urls:
        clean_url = html.unescape(url)
        path = urlsplit(clean_url).path.lower()
        if not image_url and path.endswith((".jpg", ".jpeg", ".png", ".webp")):
            image_url = clean_url
        if not video_url and path.endswith((".mp4", ".m3u8", ".webm")):
            video_url = clean_url
    return image_url, video_url


def usable_news_image(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return url.startswith(("https://", "http://")) and not any(
        token in path for token in ("/skin/", "logo", "weixin", "weixing", "qrcode", "icon", "pic_fb.jpg", "line-ad",
                                    "cropped-header", "cropped-15181454", "u435p4t47d50136", "u719p4t47d50049")
    )


def page_media(url: str) -> tuple[str, str]:
    """Read publisher-declared Open Graph media from the original article."""
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    })
    with urllib.request.urlopen(request, timeout=12) as response:
        body = response.read(1_500_000)
        charset = response.headers.get_content_charset() or "utf-8"
    document = body.decode(charset, errors="replace")
    return parse_page_media(document, url)


def parse_page_media(document: str, url: str) -> tuple[str, str]:
    image_url = ""
    video_url = ""
    for tag in re.findall(r"<meta\b[^>]*>", document, flags=re.I):
        attrs = {
            key.lower(): html.unescape(value)
            for key, _, value in re.findall(r"([\w:-]+)\s*=\s*([\"\'])(.*?)\2", tag, flags=re.I | re.S)
        }
        field = (attrs.get("property") or attrs.get("name") or "").lower()
        content = (attrs.get("content") or "").strip()
        if not content.startswith(("https://", "http://")):
            continue
        if not image_url and field in {"og:image", "og:image:url", "twitter:image"} and usable_news_image(content):
            image_url = content
        if not video_url and field in {"og:video", "og:video:url", "og:video:secure_url"}:
            video_url = content
    root = Tree(document).root
    body_classes = {"entry-content", "article-text", "left_zw", "article-content", "articleContent"}
    scopes = [node for node in root.all() if body_classes.intersection(node.attrs.get("class", "").split())
              or node.attrs.get("id") in {"artibody", "articleContent"}]
    # Never fall back to the first image on the whole page: it may be a logo,
    # ad or a different story in the recommendations/sidebar.
    for scope in scopes:
        for node in scope.all("img"):
            candidate = safe_url(node.attrs.get("src") or node.attrs.get("data-src") or node.attrs.get("_src"), url)
            if usable_news_image(candidate):
                return candidate, video_url
    return image_url, video_url


def child_link(entry: ET.Element) -> str:
    for child in entry.iter():
        name = local_name(child.tag)
        if name == "link":
            href = (child.attrib.get("href") or "").strip()
            if href:
                return href
            if clean_text(child.text):
                return clean_text(child.text)
        if name in {"guid", "id"} and clean_text(child.text).startswith("http"):
            return clean_text(child.text)
    return ""


def parse_feed(body: bytes) -> list[dict[str, str]]:
    root = ET.fromstring(body)
    entries = [node for node in root.iter() if local_name(node.tag) in {"item", "entry"}]
    result: list[dict[str, str]] = []
    for entry in entries:
        title = child_text(entry, {"title"})
        link = child_link(entry)
        raw_summary = child_raw_text(entry, {"description", "summary", "content"})
        summary = clean_text(raw_summary)
        # WordPress feeds may put article images in content:encoded while the
        # description is only an excerpt. Extract media, not the full text.
        image_url, video_url = media_urls(entry, raw_summary + child_raw_text(entry, {"encoded"}))
        if image_url and not usable_news_image(image_url):
            image_url = ""
        published = child_text(entry, {"pubdate", "published", "updated", "date"})
        if title and link.startswith(("https://", "http://")):
            result.append({
                "title": title,
                "url": canonical_url(link),
                "summary": summary,
                "published": published,
                "image_url": image_url,
                "video_url": video_url,
            })
    return result


def fetch_bytes(url: str, accept: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            # Retry only transient server errors, never access restrictions.
            if attempt or exc.code not in {502, 503, 504}:
                raise
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt or isinstance(getattr(exc, "reason", None), ssl.SSLCertVerificationError):
                raise
        time.sleep(0.5)


def html_meta(document: str, name: str) -> str:
    match = re.search(
        r'<meta\b[^>]*\bname=["\']' + re.escape(name) + r'["\'][^>]*\bcontent=["\'](.*?)["\'][^>]*>',
        document,
        flags=re.I | re.S,
    )
    if not match:
        # Attribute order is not guaranteed by HTML.
        match = re.search(
            r'<meta\b[^>]*\bcontent=["\'](.*?)["\'][^>]*\bname=["\']' + re.escape(name) + r'["\'][^>]*>',
            document,
            flags=re.I | re.S,
        )
    return clean_text(match.group(1)) if match else ""


def table_cell_text(cell: str) -> str:
    """Read visible text plus values written into a cell by the source page's JS."""
    scripted = re.search(r"var\s+_(?:badw|biju)\s*=\s*'([^']*)'", cell, flags=re.S)
    if scripted:
        return clean_text(scripted.group(1))
    return clean_text(cell)


def filing_category(filing_no: str, fallback: str = "电影项目") -> str:
    prefixes = {
        "影剧": "故事影片",
        "影动": "动画影片",
        "影纪": "纪录影片",
        "影科": "科教影片",
        "影特": "特种影片",
        "影虚": "虚拟现实影片",
        "影合": "合拍影片",
        "影协": "合拍影片",
    }
    return next((category for prefix, category in prefixes.items() if filing_no.startswith(prefix)), fallback)


def parse_chinafilm_filing_detail(body: bytes, url: str) -> dict:
    document = body.decode("utf-8", errors="replace")
    title = html_meta(document, "ArticleTitle")
    published = html_meta(document, "PubDate")
    if not title or not published:
        raise ValueError("国家电影局备案公示页缺少标题或发布日期")

    category_match = re.search(r'\bstr\s*=\s*["\']([^"\']+)["\']', document)
    categories = [clean_text(value) for value in category_match.group(1).split(",")] if category_match else []
    table_blocks = re.findall(r'<div class="hmc4Table">(.*?)</table>\s*</div>', document, flags=re.I | re.S)
    filings: list[dict[str, str]] = []
    category_count_map: dict[str, int] = {}
    for table_index, table in enumerate(table_blocks):
        category = categories[table_index] if table_index < len(categories) else "电影项目"
        for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, flags=re.I | re.S):
            cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, flags=re.I | re.S)
            values = [table_cell_text(cell) for cell in cells]
            if len(values) < 7 or not values[0].isdigit():
                continue
            row_category = filing_category(values[1], category)
            filings.append({
                "filing_no": values[1],
                "title": values[2],
                "company": values[3],
                "writer": values[4],
                "result": values[5],
                "region": values[6],
                "category": row_category,
            })
            category_count_map[row_category] = category_count_map.get(row_category, 0) + 1
    if not filings:
        raise ValueError("国家电影局备案公示页未解析到项目表格")

    period_match = re.search(r"关于(\d{4}年\d+月[上下]?)全国电影剧本", title)
    period = period_match.group(1) if period_match else published
    count_copy = "、".join(f"{name}{count}部" for name, count in category_count_map.items())
    sample_titles = "、".join(record["title"] for record in filings[:8])
    if len(filings) > 8:
        sample_titles += f"等{len(filings)}部"
    summary = (
        f"国家电影局公布{period}电影剧本（梗概）备案、立项结果，共{len(filings)}部：{count_copy}。"
        f"涉及《{sample_titles}》。备案立项仅代表项目获准拍摄，不代表已经定档或取得公映许可。"
    )
    return {
        "title": f"{period}电影备案立项公示：{len(filings)}部项目",
        "url": canonical_url(url),
        "summary": summary,
        "published": published + "T00:00:00+08:00",
        "image_url": "",
        "video_url": "",
        "filings": filings,
        "distribution": "cinema_filing",
    }


def fetch_chinafilm_filings(source: dict) -> list[dict]:
    listing_url = source["url"]
    listing = fetch_bytes(listing_url, "text/html,application/xhtml+xml").decode("utf-8", errors="replace")
    links: list[str] = []
    for href in re.findall(r'<a\b[^>]*class=["\'][^"\']*m2r_a[^"\']*["\'][^>]*href=["\']([^"\']+)["\']', listing, flags=re.I):
        url = urljoin(listing_url, html.unescape(href.strip()))
        if url not in links:
            links.append(url)
        if len(links) >= int(source.get("max_items", 2)):
            break
    if not links:
        raise ValueError("国家电影局备案公示列表未解析到公告链接")
    return [parse_chinafilm_filing_detail(fetch_bytes(url, "text/html,application/xhtml+xml"), url) for url in links]


def fetch_source(source: dict) -> list[dict[str, str]]:
    source_type = source.get("type")
    if source_type in {"mtime_news", "bjnews_ent", "people_culture"}:
        return fetch_public_source(source, fetch_bytes)
    if source_type in {"weibo_search", "weibo_timeline"}:
        return fetch_weibo(source)
    if source_type == "chinafilm_filing":
        return fetch_chinafilm_filings(source)
    if source_type != "rss":
        raise ValueError(f"Unsupported source type: {source_type}")
    body = fetch_bytes(source["url"], "application/rss+xml, application/atom+xml, application/xml, text/xml")
    # The feed itself is bounded; preserve everything it currently exposes.
    return parse_feed(body)


def confidence_for(source: dict) -> str:
    if source.get("source_class") == "industry_blog":
        return "industry_commentary"
    if source.get("source_class") == "social":
        return "social_lead"
    return "official" if source.get("source_class") == "official" else "single_source"


def matches_source_filter(source: dict, searchable_text: str) -> bool:
    """Apply optional per-feed filters before an item enters the evidence set."""
    text = search_text(searchable_text)
    included = tuple(search_text(str(value)) for value in source.get("include_keywords", []))
    excluded = tuple(search_text(str(value)) for value in source.get("exclude_keywords", []))
    if included and not any(keyword in text for keyword in included):
        return False
    if excluded and any(keyword in text for keyword in excluded):
        return False
    return True


def search_text(value: str) -> str:
    """Small matching-only variant map; never rewrite original Chinese copy."""
    return value.casefold().translate(str.maketrans({
        "電": "电", "視": "视", "劇": "剧", "網": "网", "紀": "纪", "錄": "录",
        "導": "导", "發": "发", "臺": "台", "灣": "湾", "國": "国", "藝": "艺",
        "獎": "奖", "開": "开", "預": "预", "戲": "戏", "線": "线", "華": "华",
        "長": "长", "陸": "陆", "韓": "韩", "萊": "莱", "塢": "坞", "歐": "欧", "動": "动",
    }))


def excluded_item(source: dict, title: str, summary: str, policy: dict) -> bool:
    if source.get("entry_type") in policy.get("excluded_entry_types", []):
        return True
    heading, whole = search_text(title), search_text(title + " " + summary)
    for keyword in policy.get("excluded_keywords", []):
        keyword = search_text(keyword)
        if keyword in {"短剧", "微短剧"}:
            # A long-series or film report comparing itself with short dramas is
            # still in scope. Do not delete it for an incidental summary mention.
            if keyword in heading and not any(word in heading for word in ("长剧", "电影", "电视剧", "院线")):
                return True
        elif keyword in whole:
            return True
    return False


def item_market(source: dict, item: dict) -> str:
    if not source.get("infer_market"):
        return source.get("market", "m-obs")
    title, lead = search_text(item["title"]), search_text(item.get("summary", "")[:100])
    if any(word in title for word in ("中国大陆", "内地", "国产", "全国公映")):
        return "m-cn"
    if any(word in title for word in ("香港", "澳门", "台湾", "金马奖", "金像奖")):
        return "m-hmt"
    if any(word in title for word in ("好莱坞", "北美", "奥斯卡", "戛纳", "威尼斯", "艾美奖")):
        return "m-intl"
    if re.search(r"(?:中央社|中新社|中新网).*?(?:洛杉磯|纽约|紐約|伦敦|倫敦|巴黎|首尔|首爾|东京|東京|柏林|釜山).*?(?:电|電|外電)", lead):
        return "m-intl"
    return source.get("market", "m-obs")


def classify_distribution(source: dict, title: str, summary: str) -> str:
    """Classify release channel only when the source text provides a clear signal."""
    configured = source.get("distribution")
    if configured:
        return str(configured)
    text = search_text(title + " " + summary)
    if re.search(r"网络(?:公益)?电影|网络故事片", text) or any(keyword in text for keyword in ("网大", "云影院")):
        return "network"
    if source.get("entry_type") in {"剧集动态", "电视剧动态", "网络剧动态"} or re.search(r"电视剧|剧集|网剧", text):
        return "series"
    if any(keyword in text for keyword in ("线上首映", "全网首播")) and any(keyword in text for keyword in ("电影", "影片")):
        return "network"
    if any(keyword in text for keyword in ("院线电影", "全国公映", "影院上映", "院线上映", "正式上映", "定档", "预售", "点映", "票房")):
        return "cinema"
    if "电影" in source.get("entry_type", "") or any(keyword in text for keyword in ("电影", "影片")):
        return "film_unspecified"
    return "other"


DISTRIBUTION_PRIORITY = {
    "cinema": 0,
    "cinema_filing": 1,
    "film_unspecified": 2,
    "network": 3,
    "series": 4,
    "other": 5,
}


def collect_records(config: dict, generated_at: str, store_path: Path | None) -> tuple:
    """Persist before eligibility checks; an unavailable source never erases history."""
    previous = load_store(store_path) if store_path else {"schema_version": 1, "records": []}
    incoming, errors, reports = [], [], []
    successful_sources = set()
    for source in config.get("sources", []):
        if not source.get("enabled"):
            continue
        report = {"source_id": source["id"], "name": source["name"],
                  "market": source.get("market", "m-obs"), "fetched": 0}
        try:
            items = fetch_source(source)
            report.update(status=getattr(items, "status", "ok"), fetched=len(items))
            report["checks"] = getattr(items, "checks", [])
            dates = [stamp for item in items if (stamp := published_at(item.get("published", ""))) is not None]
            report["latest_published_at"] = max(dates).isoformat() if dates else None
            collection_day = published_at(generated_at).astimezone(BEIJING).date()
            report["today_fetched"] = sum(stamp.astimezone(BEIJING).date() == collection_day for stamp in dates)
            report["undated_items"] = len(items) - len(dates)
            if report["status"] == "partial":
                errors.append({"source_id": source["id"], "message": "部分查询未完成，已保留成功结果"})
            successful_sources.add(source["id"])
            for item in items:
                incoming.append({**item, "source_id": source["id"]})
        except SourceUnavailable as exc:
            report.update(status=exc.status, message=str(exc))
            errors.append({"source_id": source["id"], "message": str(exc)})
        except Exception as exc:
            # Never log credential-bearing HTTP responses or arbitrary adapter text.
            message = f"采集失败（{type(exc).__name__}），请检查来源连接或格式"
            report.update(status="error", message=message)
            errors.append({"source_id": source["id"], "message": message})
        reports.append(report)
    store = merge_records(previous, incoming, generated_at)
    if store_path:
        save_store(store_path, store)
    return store["records"], successful_sources, errors, reports


def run(config: dict, *, store_path: Path | None = None, now: dt.datetime | None = None) -> dict:
    policy = config["policy"]
    now = (now or dt.datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    generated_at = now.isoformat().replace("+00:00", "Z")
    max_age = dt.timedelta(hours=float(policy.get("max_age_hours", 72)))
    candidates: list[dict] = []
    dropped_stale = 0
    dropped_undated = 0
    dropped_language = 0
    dropped_not_today = 0
    dropped_excluded = 0
    dropped_source_filter = 0
    dropped_capacity = 0
    media_requests = 0
    media_budget = max(0, int(policy.get("max_media_enrichments", 8)))
    records, successful_sources, errors, source_reports = collect_records(config, generated_at, store_path)
    records_by_source: dict[str, list] = {}
    for record in records:
        records_by_source.setdefault(record["source_id"], []).append(record)

    for source in config.get("sources", []):
        if not source.get("enabled"):
            continue
        items = records_by_source.get(source["id"], [])
        for item in items:
            if item.get("image_url") and not usable_news_image(item["image_url"]):
                item["image_url"] = ""
            searchable_text = item["title"] + " " + item["summary"]
            if excluded_item(source, item["title"], item["summary"], policy):
                dropped_excluded += 1
                continue
            source_filter_text = item["title"] if source.get("filter_scope") == "title" else searchable_text
            if not matches_source_filter(source, source_filter_text):
                dropped_source_filter += 1
                continue
            timestamp = published_at(item["published"])
            if timestamp is None:
                dropped_undated += 1
                continue
            # Periodic official filing notices may remain useful between two
            # publication dates. Their source can opt into a wider evidence
            # window without loosening the global freshness rule for news.
            source_max_age = dt.timedelta(hours=float(source.get("max_age_hours", max_age.total_seconds() / 3600)))
            # A future date is also suspicious: it cannot truthfully be a current item.
            if timestamp > now + dt.timedelta(hours=1) or now - timestamp > source_max_age:
                dropped_stale += 1
                continue
            if (source.get("enrich_media") and item.get("last_seen_at") == generated_at
                    and timestamp.astimezone(BEIJING).date() == now.astimezone(BEIJING).date()
                    and media_requests < media_budget
                    and (not item.get("image_url") or (not item.get("video_url")
                         and any(word in search_text(item["title"]) for word in ("预告", "片花"))))):
                media_requests += 1
                try:
                    page_image, page_video = page_media(item["url"])
                    item["image_url"] = item.get("image_url") or page_image
                    item["video_url"] = item.get("video_url") or page_video
                except Exception:
                    # Media enrichment is optional; the source item remains valid
                    # and its traceable article link must still be published.
                    pass
            summary = item["summary"][:800]
            distribution = item.get("distribution") or classify_distribution(source, item["title"], summary)
            candidates.append({
                "id": hashlib.sha256((source["id"] + item["url"]).encode("utf-8")).hexdigest()[:16],
                "title": item["title"],
                "summary": summary,
                "url": item["url"],
                "published": item["published"],
                "published_at": timestamp.isoformat().replace("+00:00", "Z"),
                "first_seen_at": item.get("first_seen_at", generated_at),
                "last_seen_at": item.get("last_seen_at", generated_at),
                "market": item_market(source, item),
                "entry_type": source.get("entry_type", "影视动态"),
                "language": source.get("language", "und"),
                "content_priority": int(source.get("content_priority", 3)),
                "distribution": distribution,
                "carry_forward_days": int(source.get("carry_forward_days", 0)),
                "confidence": confidence_for(source),
                "image_url": item.get("image_url", ""),
                "video_url": item.get("video_url", ""),
                "video_page_url": item.get("video_page_url", ""),
                "author": item.get("author", ""),
                "author_id": item.get("author_id", ""),
                "is_repost": item.get("is_repost", False),
                "original_post_url": item.get("original_post_url", ""),
                "text_complete": item.get("text_complete", True),
                "filings": item.get("filings", []),
                "sources": [{
                    "id": source["id"],
                    "publisher_id": item.get("publisher_id") or source.get("publisher_id", source["id"]),
                    "name": ("微博 · " + item["author"]) if item.get("author") else source["name"],
                    "url": item["url"],
                    "class": source.get("source_class"),
                }]
            })

    # Merge duplicate discoveries, but neither reposts nor matching headlines
    # establish truth. Social posts are grouped by permalink, not truncated title.
    by_title: dict[str, list[dict]] = {}
    for candidate in candidates:
        publication_day = dt.datetime.fromisoformat(candidate["published_at"].replace("Z", "+00:00")).astimezone(BEIJING).date().isoformat()
        key = candidate["url"] if candidate["confidence"] == "social_lead" else (
            title_key(candidate["title"]) + publication_day)
        by_title.setdefault(key, []).append(candidate)
    merged: list[dict] = []
    for group in by_title.values():
        primary = min(
            group,
            key=lambda item: (
                DISTRIBUTION_PRIORITY.get(item.get("distribution", "other"), 5),
                int(item.get("content_priority", 3)),
            ),
        )
        all_sources = {(source["publisher_id"], source["url"]): source
                       for item in group for source in item["sources"]}
        primary["sources"] = list(all_sources.values())
        merged.append(primary)

    allowed_languages = set(policy.get("render_languages", []))
    if allowed_languages:
        eligible = []
        for item in merged:
            if item["language"] in allowed_languages:
                eligible.append(item)
            else:
                dropped_language += 1
        merged = eligible
    # Keep a wider, uncapped evidence pool for the previous-day Xiaohongshu
    # package. The web brief itself is a Beijing-calendar-day product and must
    # never mix yesterday's items into today's page.
    merged.sort(key=lambda item: item["published_at"], reverse=True)
    xhs_items = list(merged)
    beijing_today = now.astimezone(BEIJING).date()
    today_items = []
    for item in merged:
        item_date = dt.datetime.fromisoformat(item["published_at"].replace("Z", "+00:00")).astimezone(BEIJING).date()
        age_days = (beijing_today - item_date).days
        if item_date == beijing_today or (policy.get("allow_carry_forward", True)
                                         and 0 <= age_days <= int(item.get("carry_forward_days", 0))):
            item["is_carried_forward"] = item_date != beijing_today
            today_items.append(item)
        else:
            dropped_not_today += 1
    merged = today_items

    # Apply the reading budget only after the strict same-day filter. Sort first
    # so cinema releases and film filings cannot be displaced by lower-priority
    # network-film, series, or general-industry items from the same market.
    merged.sort(key=lambda item: (
        DISTRIBUTION_PRIORITY.get(item.get("distribution", "other"), 5),
        int(item.get("content_priority", 3)),
        -dt.datetime.fromisoformat(item["published_at"].replace("Z", "+00:00")).timestamp(),
    ))
    limits = policy.get("max_items_per_market", {})
    kept_per_market: dict[str, int] = {}
    capped: list[dict] = []
    for item in merged:
        market = item.get("market", "m-obs")
        limit = limits.get(market)
        if limit is not None and kept_per_market.get(market, 0) >= int(limit):
            dropped_capacity += 1
            continue
        kept_per_market[market] = kept_per_market.get(market, 0) + 1
        capped.append(item)
    merged = capped
    merged.sort(key=lambda item: (
        item.get("market", "m-obs"),
        DISTRIBUTION_PRIORITY.get(item.get("distribution", "other"), 5),
        int(item.get("content_priority", 3)),
        -dt.datetime.fromisoformat(item["published_at"].replace("Z", "+00:00")).timestamp(),
    ))
    market_coverage = {
        market: {
            "items": sum(1 for item in merged if item["market"] == market),
            "successful_sources": sum(
                1 for source in config.get("sources", [])
                if source.get("enabled") and source.get("market") == market and source.get("id") in successful_sources
            ),
            "configured_sources": sum(1 for source in config.get("sources", [])
                                      if source.get("enabled") and source.get("market") == market),
            "unavailable_sources": sum(1 for report in source_reports if report["market"] == market
                                       and report["status"] != "ok"),
        }
        for market in policy.get("required_markets", [])
    }
    if store_path:
        # Include optional media found during enrichment without dropping old rows.
        save_store(store_path, {"schema_version": 1, "updated_at": generated_at, "records": records})
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "edition_date": beijing_today.isoformat(),
        "edition_mode": "today",
        "policy": policy,
        "items": merged,
        "xhs_items": xhs_items,
        "errors": errors,
        "source_statuses": source_reports,
        "stats": {
            "items": len(merged),
            "stored_records": len(records),
            "media_enrichment_requests": media_requests,
            "fetched_items": sum(report["fetched"] for report in source_reports),
            "new_records": sum(record.get("first_seen_at") == generated_at for record in records),
            "xhs_items": len(xhs_items),
            "failed_sources": len(errors),
            "configured_sources": len([s for s in config.get("sources", []) if s.get("enabled")]),
            "dropped_stale": dropped_stale,
            "dropped_undated": dropped_undated,
            "dropped_language": dropped_language,
            "dropped_not_today": dropped_not_today,
            "dropped_excluded": dropped_excluded,
            "dropped_source_filter": dropped_source_filter,
            "dropped_capacity": dropped_capacity,
            "successful_sources": len(successful_sources),
            "carried_forward": sum(1 for item in merged if item.get("is_carried_forward")),
            "distribution_counts": {
                distribution: sum(1 for item in merged if item.get("distribution") == distribution)
                for distribution in DISTRIBUTION_PRIORITY
            },
            "market_coverage": market_coverage,
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", default="automation/sources.json")
    parser.add_argument("--output", default="generated/latest.json")
    parser.add_argument("--store", default="generated/collected_items.json")
    args = parser.parse_args()
    config = json.loads(Path(args.sources).read_text(encoding="utf-8"))
    payload = run(config, store_path=Path(args.store))
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["stats"], ensure_ascii=False))
    # An empty eligible set is valid (for example, only non-Chinese source
    # entries arrived). A complete source-fetch failure is not.
    return 0 if payload["stats"]["successful_sources"] else 2


if __name__ == "__main__":
    sys.exit(main())
