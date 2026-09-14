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
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


UTC = dt.timezone.utc
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
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


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
        summary = child_text(entry, {"description", "summary", "content"})
        published = child_text(entry, {"pubdate", "published", "updated", "date"})
        if title and link.startswith(("https://", "http://")):
            result.append({"title": title, "url": canonical_url(link), "summary": summary, "published": published})
    return result


def fetch_source(source: dict) -> list[dict[str, str]]:
    if source.get("type") != "rss":
        raise ValueError("Only RSS/Atom sources are supported by this runner")
    request = urllib.request.Request(source["url"], headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
    with urllib.request.urlopen(request, timeout=25) as response:
        return parse_feed(response.read())[: int(source.get("max_items", 20))]


def confidence_for(source: dict) -> str:
    return "official" if source.get("source_class") == "official" else "single_source"


def run(config: dict) -> dict:
    policy = config["policy"]
    now = dt.datetime.now(UTC).replace(microsecond=0)
    generated_at = now.isoformat().replace("+00:00", "Z")
    max_age = dt.timedelta(hours=float(policy.get("max_age_hours", 72)))
    candidates: list[dict] = []
    errors: list[dict] = []
    seen_links: set[str] = set()
    dropped_stale = 0
    dropped_undated = 0
    dropped_language = 0
    dropped_capacity = 0
    successful_sources: set[str] = set()

    for source in config.get("sources", []):
        if not source.get("enabled"):
            continue
        try:
            items = fetch_source(source)
        except Exception as exc:  # A failed source should be observable, not silently omitted.
            errors.append({"source_id": source.get("id"), "message": str(exc)})
            continue
        successful_sources.add(source["id"])
        for item in items:
            if item["url"] in seen_links:
                continue
            timestamp = published_at(item["published"])
            if timestamp is None:
                dropped_undated += 1
                continue
            # A future date is also suspicious: it cannot truthfully be a current item.
            if timestamp > now + dt.timedelta(hours=1) or now - timestamp > max_age:
                dropped_stale += 1
                continue
            seen_links.add(item["url"])
            summary = item["summary"][:800]
            candidates.append({
                "id": hashlib.sha256((source["id"] + item["url"]).encode("utf-8")).hexdigest()[:16],
                "title": item["title"],
                "summary": summary,
                "url": item["url"],
                "published": item["published"],
                "published_at": timestamp.isoformat().replace("+00:00", "Z"),
                "market": source.get("market", "m-obs"),
                "entry_type": source.get("entry_type", "影视动态"),
                "language": source.get("language", "und"),
                "confidence": confidence_for(source),
                "sources": [{"id": source["id"], "name": source["name"], "url": item["url"], "class": source.get("source_class")}]
            })

    # Conservative multi-source promotion: exact normalized title agreement only.
    by_title: dict[str, list[dict]] = {}
    for candidate in candidates:
        by_title.setdefault(title_key(candidate["title"]), []).append(candidate)
    merged: list[dict] = []
    for group in by_title.values():
        primary = group[0]
        all_sources = [source for item in group for source in item["sources"]]
        distinct_sources = {source["id"] for source in all_sources}
        if len(distinct_sources) >= 2:
            primary["confidence"] = "multi_source"
            primary["sources"] = all_sources
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
    # Keep the newest traceable items first, then apply an explicit per-market
    # reading budget. Coverage should grow without turning the brief into an
    # unscannable stream or silently favouring an alphabetically early title.
    merged.sort(key=lambda item: item["published_at"], reverse=True)
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
    market_coverage = {
        market: {
            "items": sum(1 for item in merged if item["market"] == market),
            "successful_sources": sum(
                1 for source in config.get("sources", [])
                if source.get("enabled") and source.get("market") == market and source.get("id") in successful_sources
            ),
        }
        for market in policy.get("required_markets", [])
    }
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "policy": policy,
        "items": merged,
        "errors": errors,
        "stats": {
            "items": len(merged),
            "failed_sources": len(errors),
            "configured_sources": len([s for s in config.get("sources", []) if s.get("enabled")]),
            "dropped_stale": dropped_stale,
            "dropped_undated": dropped_undated,
            "dropped_language": dropped_language,
            "dropped_capacity": dropped_capacity,
            "successful_sources": len(successful_sources),
            "market_coverage": market_coverage,
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", default="automation/sources.json")
    parser.add_argument("--output", default="generated/latest.json")
    args = parser.parse_args()
    config = json.loads(Path(args.sources).read_text(encoding="utf-8"))
    payload = run(config)
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["stats"], ensure_ascii=False))
    # An empty eligible set is valid (for example, only non-Chinese source
    # entries arrived). A complete source-fetch failure is not.
    return 0 if payload["stats"]["successful_sources"] else 2


if __name__ == "__main__":
    sys.exit(main())
