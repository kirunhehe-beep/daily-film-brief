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


DISTRIBUTION_LABELS = {
    "cinema": "院线电影",
    "cinema_filing": "电影备案项目",
    "network": "网络电影",
    "film_unspecified": "电影·渠道待确认",
    "series": "剧集",
    "other": "影视动态",
}

DISTRIBUTION_PRIORITY = {
    "cinema": 0,
    "cinema_filing": 1,
    "film_unspecified": 2,
    "network": 3,
    "series": 4,
    "other": 5,
}


def item_time(item: dict) -> str:
    """Show the source timestamp in Beijing time; never use generation time instead."""
    raw = item.get("published_at", "")
    try:
        timestamp = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return timestamp.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%m/%d %H:%M")
    except (ValueError, TypeError):
        return "——"


def excerpt(value: str, limit: int = 180) -> str:
    """Keep the closed card skimmable; the complete captured summary is on expand."""
    compact = re.sub(r"\s+", " ", value or "").strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip("，。；、 ") + "…"


def render_empty_market(market: str, name: str, coverage: dict | None = None) -> str:
    headline, detail = EMPTY_MARKET_COPY.get(
        market,
        (f"{name}本轮暂无新增消息", "下次自动更新将继续留意。"),
    )
    if coverage is not None:
        headline = f"暂未收录今天的{name}消息"
        if not coverage.get("configured_sources"):
            detail = "本区域采集来源尚待补充，不代表没有新消息。"
        elif not coverage.get("successful_sources"):
            detail = "本轮未取得来源数据，恢复采集后继续补充。"
        elif coverage.get("unavailable_sources"):
            detail = "部分来源尚未接通或采集受限，已接通的来源仍会更新。"
        else:
            detail = "当前来源暂未收录新增内容，可查看历史归档。"
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
    summary = item.get("summary") or "原始来源未提供摘要，请展开查看信源。"
    image_url = item.get("image_url", "")
    video_url = item.get("video_url", "")
    video_page_url = item.get("video_page_url", "")
    primary_url = (item.get("sources") or [{}])[0].get("url", item.get("url", ""))
    if image_url:
        thumbnail = '<img class="r-thumb" src="{url}" alt="{title}" loading="lazy" referrerpolicy="no-referrer">'.format(
            url=esc(image_url), title=esc(item.get("title", "影视物料"))
        )
    else:
        thumbnail = '<span class="poster-placeholder" aria-label="暂无来源图片">FILM</span>'

    media_parts = []
    if video_url:
        media_parts.append(
            '<div class="v-wrap"><video controls preload="metadata" playsinline src="{url}"></video></div>'
            '<div class="v-src">来源页面提供的视频物料</div>'.format(url=esc(video_url))
        )
    if image_url:
        media_parts.append(
            '<figure class="source-media"><a href="{source}" target="_blank" rel="noopener">'
            '<img src="{image}" alt="{title}" loading="lazy" referrerpolicy="no-referrer"></a>'
            '<figcaption>来源图片 · 点击查看原始报道</figcaption></figure>'.format(
                source=esc(primary_url), image=esc(image_url), title=esc(item.get("title", "影视物料"))
            )
        )
    if video_page_url and not video_url:
        media_parts.append('<div class="v-src"><a href="{url}" target="_blank" rel="noopener">查看原帖视频 ↗</a></div>'.format(url=esc(video_page_url)))
    if not video_url and not video_page_url:
        media_parts.append(
            '<div class="media-empty"><strong>预告片暂未收录</strong>'
            '<span>只有找到可追溯的官方或平台视频时才会加入播放器。</span></div>'
        )

    verify_mark = "✓" if cred_class == "ok" else "?"
    verify_text = (
        "来源信息已达到当前标注条件，仍建议通过原文核对具体口径。"
        if cred_class == "ok"
        else "当前为单一媒体线索，标题、摘要和发布时间均来自原始报道，尚未升级为已确认事实。"
    )
    if item.get("confidence") == "industry_commentary":
        verify_mark = "i"
        verify_text = "来自行业博客，包含作者观察与观点；请结合原文判断，不代表官方结论。"
    if item.get("confidence") == "social_lead":
        verify_mark = "i"
        verify_text = "来自微博原帖，保留账号与发布时间；账号认证或转发数量不代表内容已核实。"
        if item.get("is_repost"):
            verify_text += " 本条为转发动态，不计作独立信源确认。"
        if not item.get("text_complete", True):
            verify_text += " 当前正文可能被平台截断，请查看原帖全文。"
    material_label = "含图片 / 视频 / 信源" if video_url else ("含来源图片 / 信源" if image_url else "查看物料与信源")
    distribution_label = DISTRIBUTION_LABELS.get(item.get("distribution", "other"), "影视动态")
    kind_label = distribution_label + " · " + item.get("entry_type", "影视动态")
    if item.get("is_carried_forward"):
        kind_label += " · 最新一期"
    filing_rows = []
    for filing in item.get("filings", []):
        filing_rows.append(
            '<tr><td>{title}</td><td>{category}</td><td>{filing_no}</td><td>{company}</td><td>{writer}</td><td>{result}</td></tr>'.format(
                title=esc(filing.get("title", "")),
                category=esc(filing.get("category", "")),
                filing_no=esc(filing.get("filing_no", "")),
                company=esc(filing.get("company", "")),
                writer=esc(filing.get("writer", "")),
                result=esc(filing.get("result", "")),
            )
        )
    filing_table = ""
    if filing_rows:
        filing_table = (
            '<div class="filing-block"><div class="vp-h">备案项目明细</div>'
            '<div class="filing-scroll"><table class="filing-table"><thead><tr>'
            '<th>片名</th><th>类别</th><th>备案立项号</th><th>备案单位</th><th>编剧</th><th>结果</th>'
            '</tr></thead><tbody>' + "".join(filing_rows) + '</tbody></table></div>'
            '<div class="vp-note">备案立项不等于定档，也不代表已取得公映许可。</div></div>'
        )
    return (
        '<details class="row row-media" data-cred="{cred_class}"><summary>'
        '<div class="r-line"><span class="r-time">{source_time}</span><span class="r-kind">{kind}</span>'
        '<span class="r-cred {cred_class}">{cred_label} · {source_count} 来源</span>'
        '<svg class="chev" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg></div>'
        '<div class="r-media-body">{thumbnail}<div class="r-media-txt"><div class="r-title">{title}</div>'
        '<div class="r-lead">{excerpt}</div><div class="r-src"><span class="dot"></span>来源 · {source_names}</div>'
        '</div></div><div class="r-mat">{material_label}</div></summary>'
        '<div class="r-det"><p class="d-desc">{summary}</p>{filing_table}{media_parts}'
        '<div class="vpanel {cred_class}"><div class="vp-h">来源说明</div>'
        '<div class="vp-row {cred_class}"><span class="vpdot">{verify_mark}</span>{verify_text}</div>'
        '<div class="vp-note">来源标注：{cred_label}。收录不等于事实确认。</div></div>'
        '<div class="srcbox"><div class="sb-h">原始信源</div>'
        '<div class="sb-list">{source_rows}</div></div></div></details>'
    ).format(
        cred_class=cred_class,
        cred_label=esc(cred_label),
        source_count=len(item.get("sources", [])),
        source_time=esc(item_time(item)),
        kind=esc(kind_label),
        title=esc(item.get("title", "未命名资讯")),
        summary=esc(summary),
        excerpt=esc(excerpt(summary)),
        thumbnail=thumbnail,
        media_parts="".join(media_parts),
        filing_table=filing_table,
        material_label=esc(material_label),
        verify_mark=verify_mark,
        verify_text=esc(verify_text),
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


def item_sort_key(item: dict) -> tuple[int, int, float]:
    try:
        timestamp = dt.datetime.fromisoformat(item.get("published_at", "").replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        timestamp = 0
    return (
        DISTRIBUTION_PRIORITY.get(item.get("distribution", "other"), 5),
        int(item.get("content_priority", 3)),
        -timestamp,
    )


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
    collected_at = dt.datetime.fromisoformat(payload["generated_at"].replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai"))
    edition_date = dt.date.fromisoformat(payload.get("edition_date", collected_at.date().isoformat()))
    now = dt.datetime.combine(edition_date, dt.time(), tzinfo=ZoneInfo("Asia/Shanghai"))
    date_iso, date_cn, weekday = display_date(now)
    title_match = re.search(r"<title>每日影视简报 · (\d{4}-\d{2}-\d{2})</title>", old_html)
    old_date = title_match.group(1) if title_match else date_iso
    grouped = {market: [] for market in MARKETS}
    for item in payload["items"]:
        grouped.get(item.get("market"), grouped["m-obs"]).append(item)
    # A market without eligible items is rendered as an explicit empty state;
    # other markets must keep updating instead of leaving the whole edition stale.
    if old_date != date_iso:
        previous_count = len(re.findall(r'<details\b[^>]*class="row(?:\s|\")', old_html))
        archive_previous(site, old_html, old_date, previous_count)
    result = old_html
    labels = payload["policy"]["labels"]
    for market, name in MARKETS.items():
        items = sorted(grouped[market], key=item_sort_key)
        feed = "".join(render_item(item, labels) for item in items)
        display_count = len(items)
        if not feed:
            feed = render_empty_market(market, name, payload["stats"].get("market_coverage", {}).get(market))
        result = replace_feed(result, market, feed)
        result = replace_market_count(result, market, display_count)

    total = len(payload["items"])
    result = re.sub(r"<title>每日影视简报 · \d{4}-\d{2}-\d{2}</title>", f"<title>每日影视简报 · {date_iso}</title>", result, count=1)
    result = replace_group(result, r'(<div class="tb-date">).*?(</div>)', date_iso + " · " + weekday)
    result = replace_group(result, r'(<span class="st-date">).*?(</span>)', date_cn)
    result = replace_group(result, r'(<span class="st-upd">).*?(</span>)', "最近采集 " + collected_at.strftime("%m/%d %H:%M"))
    result = replace_group(result, r'(<span class="st-new">).*?(</span>)', "今日收录 <em>" + str(total) + "</em> 条")
    source_copy = "本轮接通 " + str(payload["stats"]["successful_sources"]) + "/" + str(payload["stats"]["configured_sources"]) + " 个采集入口"
    awaiting_weibo = any(report["source_id"].startswith("weibo-") and report["status"] == "authorization_required" for report in payload.get("source_statuses", []))
    if awaiting_weibo:
        source_copy += " · 微博待授权"
    elif payload["stats"]["failed_sources"]:
        source_copy += " · 部分来源采集受限"
    result = replace_group(result, r'(<div class="st-sub2">).*?(</div>)', esc(source_copy))
    index.write_text(result, encoding="utf-8")
    (site / "data").mkdir(exist_ok=True)
    (site / "data" / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"date": date_iso, "items": total, "archived": old_date if old_date != date_iso else None}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
