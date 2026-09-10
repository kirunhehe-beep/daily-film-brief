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


def esc(value: str) -> str:
    return html.escape(value or "", quote=True)


def display_date(now: dt.datetime) -> tuple[str, str, str]:
    weekdays = "一二三四五六日"
    return now.strftime("%Y-%m-%d"), now.strftime("%Y 年 %-m 月 %-d 日"), "周" + weekdays[now.weekday()]


def evidence(item: dict, labels: dict) -> tuple[str, str]:
    level = item.get("confidence", "single_source")
    return ("ok" if level in {"official", "multi_source"} else "pending", labels.get(level, "单源待确认"))


def render_item(item: dict, labels: dict) -> str:
    cred_class, cred_label = evidence(item, labels)
    source_rows = []
    for source in item.get("sources", []):
        source_rows.append(
            '<a class="sb-i sb-main" href="{url}" target="_blank" rel="noopener">'
            '<span class="sb-b">{name}</span>{name}<span class="sb-a">&#8599;</span></a>'.format(
                url=esc(source.get("url", "")), name=esc(source.get("name", "来源"))
            )
        )
    source_names = " / ".join(source.get("name", "来源") for source in item.get("sources", [])) or "来源待补充"
    return (
        '<details class="row" data-cred="{cred_class}"><summary>'
        '<div class="r-line"><span class="r-time">——</span><span class="r-kind">{kind}</span>'
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
    if not payload.get("items"):
        raise SystemExit("Refusing to replace the briefing with an empty result")
    site = Path(args.site)
    index = site / "index.html"
    old_html = index.read_text(encoding="utf-8")
    now = dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    date_iso, date_cn, weekday = display_date(now)
    title_match = re.search(r"<title>每日影视简报 · (\d{4}-\d{2}-\d{2})</title>", old_html)
    old_date = title_match.group(1) if title_match else date_iso
    if old_date != date_iso:
        archive_previous(site, old_html, old_date, int(payload["stats"]["items"]))

    grouped = {market: [] for market in MARKETS}
    for item in payload["items"]:
        grouped.get(item.get("market"), grouped["m-obs"]).append(item)
    missing = [market for market in payload["policy"].get("required_markets", []) if not grouped.get(market)]
    if missing:
        names = ", ".join(MARKETS.get(market, market) for market in missing)
        raise SystemExit("Refusing production render: required markets have no traceable items: " + names)
    result = old_html
    labels = payload["policy"]["labels"]
    for market, name in MARKETS.items():
        items = grouped[market]
        feed = "".join(render_item(item, labels) for item in items)
        if not feed:
            feed = '<div class="mk-empty">本次自动采集未发现可追溯的' + name + '条目。</div>'
        result = replace_feed(result, market, feed)

    total = len(payload["items"])
    result = re.sub(r"<title>每日影视简报 · \d{4}-\d{2}-\d{2}</title>", f"<title>每日影视简报 · {date_iso}</title>", result, count=1)
    result = re.sub(r'(<div class="tb-date">).*?(</div>)', r"\1" + date_iso + " · " + weekday + r"\2", result, count=1)
    result = re.sub(r'(<span class="st-date">).*?(</span>)', r"\1" + date_cn + r"\2", result, count=1)
    result = re.sub(r'(<span class="st-upd">).*?(</span>)', r"\1系统自动更新于 " + now.strftime("%H:%M") + r"\2", result, count=1)
    result = re.sub(r'(<span class="st-new">今日新增 <em>)\d+(</em> 条</span>)', r"\1" + str(total) + r"\2", result, count=1)
    result = re.sub(r'(<div class="st-sub2">).*?(</div>)', r"\1自动采集 · " + str(payload["stats"]["configured_sources"]) + " 个信源 · " + str(payload["stats"]["failed_sources"]) + r" 个异常\2", result, count=1)
    index.write_text(result, encoding="utf-8")
    (site / "data").mkdir(exist_ok=True)
    (site / "data" / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"date": date_iso, "items": total, "archived": old_date if old_date != date_iso else None}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
