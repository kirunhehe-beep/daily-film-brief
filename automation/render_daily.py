#!/usr/bin/env python3
"""将可追溯资讯 JSON 渲染为现有 Pages 静态站点，并在跨日时自动归档旧版。"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo


MARKETS = {
    "m-cn": "中国内地",
    "m-hmt": "中国港澳台",
    "m-intl": "海外",
    "m-obs": "行业观察",
}

EMPTY_MARKET_COPY = {
    "m-cn": ("今日暂无新的中国内地影视动态", "下一次更新会继续检索新增消息。"),
    "m-hmt": ("本轮暂未发现中国港澳台的新进展", "有新的可收录资讯时会在下一版出现。"),
    "m-intl": ("海外市场本轮暂无新增消息", "下次自动更新将继续留意。"),
    "m-obs": ("行业观察暂未有新的收录项", "市场没有新节点时，留白也值得保留。"),
}


def esc(value: str) -> str:
    return html.escape(value or "", quote=True)


def display_date(now: dt.datetime) -> tuple[str, str, str]:
    weekdays = "一二三四五六日"
    return now.strftime("%Y-%m-%d"), now.strftime("%Y 年 %-m 月 %-d 日"), "周" + weekdays[now.weekday()]


def evidence(item: dict, labels: dict) -> tuple[str, str]:
    level = item.get("confidence", "single_source")
    return ("ok" if level in {"official", "multi_source"} else "pending", labels.get(level, "单源待确认"))


def item_time(item: dict) -> str:
    """Show the source timestamp in Beijing time; never use generation time instead."""
    raw = item.get("published_at", "")
    try:
        timestamp = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return timestamp.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%m/%d %H:%M")
    except (ValueError, TypeError):
        return "——"


def render_empty_market(market: str, name: str) -> str:
    headline, detail = EMPTY_MARKET_COPY.get(
        market,
        (f"{name}本轮暂无新增消息", "下次自动更新将继续留意。"),
    )
    return '<div class="mk-empty"><strong>{headline}</strong><span>{detail}</span></div>'.format(
        headline=esc(headline), detail=esc(detail)
    )


def render_item(item: dict, labels: dict) -> str:
    cred_class, cred_label = evidence(item, labels)
    source_rows = []
    for source in item.get("sources", []):
        source_rows.append(
            '<a class="sb-i sb-main" href="{url}" target="_blank" rel="noopener">'
            '<span class="sb-b">{name}</span><span class="sb-name">{name}</span><span class="sb-a">&#8599;</span></a>'.format(
                url=esc(source.get("url", "")), name=esc(source.get("name", "来源"))
            )
        )
    source_names = " / ".join(source.get("name", "来源") for source in item.get("sources", [])) or "来源待补充"
    return (
        '<details class="row" data-cred="{cred_class}"><summary>'
        '<div class="r-line"><span class="r-time">{source_time}</span><span class="r-kind">{kind}</span>'
        '<span class="r-cred {cred_class}">{cred_label} · {source_count} 来源</span>'
        '<svg class="chev" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg></div>'
        '<div class="r-title">{title}</div><div class="r-lead">{summary}</div>'
        '<div class="r-src"><span class="dot"></span>来源 · {source_names}</div></summary>'
        '<div class="r-det"><p class="d-desc">{summary}</p><div class="srcbox"><div class="sb-h">信源</div>'
        '<div class="sb-list">{source_rows}</div></div></div></details>'
    ).format(
        cred_class=cred_class,
        cred_label=esc(cred_label),
        source_count=len(item.get("sources", [])),
        source_time=esc(item_time(item)),
        kind=esc(item.get("entry_type", "影视动态")),
        title=esc(item.get("title", "未命名资讯")),
        summary=esc(item.get("summary") or "原始来源未提供摘要，请展开查看信源。"),
        source_names=esc(source_names),
        source_rows="".join(source_rows),
    )


def replace_feed(document: str, market: str, body: str) -> str:
    pattern = re.compile(
        r'(<section id="' + re.escape(market) + r'" class="market"[^>]*>.*?<div class="feed">)(.*?)(</div>\s*</section>)',
        re.S,
    )
    replaced, count = pattern.subn(lambda match: match.group(1) + body + match.group(3), document, count=1)
    if count != 1:
        raise RuntimeError(f"Could not locate feed for {market}")
    return replaced


def replace_group(document: str, pattern: str, value: str) -> str:
    """Use a callback so a value beginning with a digit cannot corrupt \\1."""
    result, count = re.subn(pattern, lambda match: match.group(1) + value + match.group(2), document, count=1)
    if count != 1:
        raise RuntimeError(f"Could not locate dynamic field: {pattern}")
    return result


def replace_market_count(document: str, market: str, count_value: int) -> str:
    pattern = re.compile(
        r'(<section id="' + re.escape(market) + r'" class="market"[^>]*>.*?<span class="n">)\d+(</span>)',
        re.S,
    )
    result, count = pattern.subn(lambda match: match.group(1) + str(count_value) + match.group(2), document, count=1)
    if count != 1:
        raise RuntimeError(f"Could not locate market count for {market}")
    return result


def extract_feed(document: str, market: str) -> str:
    pattern = re.compile(
        r'<section id="' + re.escape(market) + r'" class="market"[^>]*>.*?<div class="feed">(.*?)(?:</div>\s*</section>)',
        re.S,
    )
    match = pattern.search(document)
    return match.group(1) if match else ""


def recent_chinese_feed(site: Path, market: str) -> str:
    """Reuse the most recent Chinese, source-linked cards when this round is empty."""
    archive_dir = site / "archive"
    for page in sorted(archive_dir.glob("*.html"), reverse=True):
        try:
            feed = extract_feed(page.read_text(encoding="utf-8"), market)
        except OSError:
            continue
        titles = re.findall(r'<div class="r-title">(.*?)</div>', feed, re.S)
        if not any(re.search(r"[\u4e00-\u9fff]", re.sub(r"<[^>]+>", "", title)) for title in titles):
            continue
        # Archive pages are one level deeper than index.html.
        return feed.replace('src="../posters/', 'src="posters/').replace('href="../posters/', 'href="posters/')
    return ""


def fallback_feed(site: Path, market: str) -> str:
    feed = recent_chinese_feed(site, market)
    if not feed:
        return ""
    return '<div class="feed-keep"><div class="feed-keep-label">最近已核验 · 本轮暂无新增</div>' + feed + '</div>'


def archive_previous(site: Path, old_html: str, old_date: str, count: int) -> None:
    archive_dir = site / "archive"
    archive_dir.mkdir(exist_ok=True)
    target = archive_dir / f"{old_date}.html"
    if target.exists():
        return
    archived = old_html
    archived = archived.replace('href="manifest.json"', 'href="../manifest.json"')
    archived = archived.replace('href="film-ticket-192.png"', 'href="../film-ticket-192.png"')
    archived = archived.replace('href="film-ticket-180.png"', 'href="../film-ticket-180.png"')
    archived = archived.replace('src="posters/', 'src="../posters/')
    archived = archived.replace('href="archive.html"', 'href="../archive.html"')
    archived = archived.replace(
        '<header class="topbar">',
        '<header class="topbar"><a href="../archive.html" aria-label="返回历史归档" style="display:inline-flex;align-items:center;min-height:36px;margin:0 16px 2px;color:var(--paper);font:800 11px/1 var(--mono);letter-spacing:.05em">← 历史归档</a>',
        1,
    )
    target.write_text(archived, encoding="utf-8")

    archive_index = site / "archive.html"
    index_html = archive_index.read_text(encoding="utf-8")
    if old_date not in index_html:
        day = dt.date.fromisoformat(old_date)
        weekday = "周" + "一二三四五六日"[day.weekday()]
        entry = f'  {{d:"{old_date}", w:"{weekday}", n:{count}}},\n'
        index_html = re.sub(r"(const ARCH = \[\n)", r"\1" + entry, index_html, count=1)
        archive_index.write_text(index_html, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="generated/latest.json")
    parser.add_argument("--site", default="app")
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    site = Path(args.site)
    index = site / "index.html"
    old_html = index.read_text(encoding="utf-8")
    now = dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    date_iso, date_cn, weekday = display_date(now)
    title_match = re.search(r"<title>每日影视简报 · (\d{4}-\d{2}-\d{2})</title>", old_html)
    old_date = title_match.group(1) if title_match else date_iso
    grouped = {market: [] for market in MARKETS}
    for item in payload["items"]:
        grouped.get(item.get("market"), grouped["m-obs"]).append(item)
    # A market without eligible items is rendered as an explicit empty state;
    # other markets must keep updating instead of leaving the whole edition stale.
    if old_date != date_iso:
        archive_previous(site, old_html, old_date, int(payload["stats"]["items"]))
    result = old_html
    labels = payload["policy"]["labels"]
    for market, name in MARKETS.items():
        items = grouped[market]
        feed = "".join(render_item(item, labels) for item in items)
        display_count = len(items)
        if not feed:
            feed = fallback_feed(site, market)
            if feed:
                display_count = feed.count("<details")
            else:
                feed = render_empty_market(market, name)
        result = replace_feed(result, market, feed)
        result = replace_market_count(result, market, display_count)

    total = len(payload["items"])
    result = re.sub(r"<title>每日影视简报 · \d{4}-\d{2}-\d{2}</title>", f"<title>每日影视简报 · {date_iso}</title>", result, count=1)
    result = replace_group(result, r'(<div class="tb-date">).*?(</div>)', date_iso + " · " + weekday)
    result = replace_group(result, r'(<span class="st-date">).*?(</span>)', date_cn)
    result = replace_group(result, r'(<span class="st-upd">).*?(</span>)', "系统自动更新于 " + now.strftime("%H:%M"))
    result = replace_group(result, r'(<span class="st-new">今日新增 <em>)\d+(</em> 条</span>)', str(total))
    result = replace_group(result, r'(<div class="st-sub2">).*?(</div>)', "自动采集 · " + str(payload["stats"]["configured_sources"]) + " 个信源 · " + str(payload["stats"]["failed_sources"]) + " 个异常")
    index.write_text(result, encoding="utf-8")
    (site / "data").mkdir(exist_ok=True)
    (site / "data" / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"date": date_iso, "items": total, "archived": old_date if old_date != date_iso else None}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
