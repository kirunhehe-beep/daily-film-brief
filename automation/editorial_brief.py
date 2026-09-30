#!/usr/bin/env python3
"""Turn a complete source feed into an event-based, traceable daily brief.

This is deliberately conservative: no model-generated facts, no cross-day
merges, and no assertion that two matching posts independently verify a claim.
Every collected item remains in ``source_items`` for inspection.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import difflib
import json
import re
import unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo


EVENT_RULES = (
    ("filing", r"备案|立项|filing"),
    ("run_end", r"下映|结束公映|密钥到期"),
    ("award", r"入围|提名|获奖|斩获|喜夺|特别表扬|费穆荣誉|获最佳|最佳.{0,8}奖|nomine|award|wins?\b"),
    ("box_office", r"票房|预售|box.office|grosses?\b"),
    ("release_date", r"定档|上映日期|公映|上映|首映|release.date|premiere"),
    ("music_asset", r"\bMV\b|歌词海报|主题曲|歌曲|音乐特辑|music.video"),
    ("trailer", r"预告|先导片|特别视频|片花|trailer|teaser"),
    ("poster", r"海报|剧照|poster|first.look"),
    ("renewal", r"续订|第二季|第三季|renew"),
    ("production", r"开机|开拍|拍摄启动|杀青|production.begins|filming"),
    ("casting", r"加盟|主演|选角|cast|star in"),
    ("rights", r"发行|购得|版权|收购|rights|distribution|acquir"),
    ("review", r"影评|评价|评论|review|critics?"),
    ("campaign", r"广告|营销|宣发|campaign|new.ad"),
)
FACT_WORDS = re.compile(
    r"公布|发布|定档|上映|下映|公映|入围|提名|获奖|票房|预售|备案|立项|开机|开拍|杀青|"
    r"续订|发行|上线|播出|签约|购得|收购|启动|首映|刷新|突破|推出|宣布|释出|曝出|"
    r"release|premiere|acquir|renew|cast|box.office|gross|释出|提档", re.I
)
LOW_SIGNAL = (
    ("anniversary", re.compile(r"上映.{0,3}(?:\d+|[一二三四五六七八九十]+)周年|上映.{0,3}(?:\d+|[一二三四五六七八九十]+)年|周年回顾")),
    ("advance_teaser", re.compile(r"即将公布|期待入围名单|敬请期待|明日揭晓")),
    ("promotion", re.compile(r"饮品优惠|购票福利|抽奖送|优惠券")),
)
SOCIAL_IDS = {"taopiaopiao-official-posts", "dengta-official-posts"}
SOURCE_RANK = {"official": 0, "trade_media": 1, "established_media": 1,
               "social": 3, "industry_blog": 4}
BEIJING = ZoneInfo("Asia/Shanghai")
EDITORIAL_BUDGET = {"m-cn": 18, "m-hmt": 8, "m-intl": 22, "m-obs": 5}
EVENT_RANK = {"release_date": 0, "box_office": 1, "award": 1, "production": 2,
              "trailer": 2, "music_asset": 2, "renewal": 2, "rights": 2,
              "filing": 3, "casting": 3, "poster": 3, "campaign": 4,
              "review": 4, "run_end": 2, "other": 5}

EVENT_FACTS = {
    "filing": r"备案|立项",
    "run_end": r"下映|结束公映|密钥到期|同步网播|上线网播",
    "award": r"入围|提名|获奖|斩获|获得.{0,15}奖",
    "box_office": r"票房|预售|突破",
    "release_date": r"定档|上映|公映|首映",
    "music_asset": r"发布|释出|MV|歌词海报|歌曲",
    "trailer": r"发布|释出|预告|先导片|特别视频",
    "poster": r"发布|释出|海报|剧照",
    "renewal": r"续订|renew",
    "production": r"开机|开拍|杀青|拍摄",
    "casting": r"主演|加盟|选角|cast",
    "rights": r"发行|购得|版权|收购",
}


def compact(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"[\u200b-\u200d\ufeff]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def visible_copy(value: str) -> str:
    value = compact(value)
    value = re.sub(r"https?://\S+", "", value)
    value = re.sub(r"#([^#\n]{2,80})#", "", value)
    value = re.sub(r"@[\w\u4e00-\u9fff-]+", "", value)
    value = re.split(r"你观看过|一起来评论区|欢迎评论|点击链接|敬请期待|数据统计范围[:：]", value, maxsplit=1)[0]
    return compact(value).strip("，。；、# ")


def normalized(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", compact(value).lower())


def source_ids(item: dict) -> set[str]:
    return {source.get("id", "") for source in item.get("sources", [])}


def event_kind(item: dict) -> str:
    title = compact(item.get("original_title") or item.get("title", ""))
    for kind, expression in EVENT_RULES:
        if re.search(expression, title, re.I):
            return kind
    # A headline may only name the work, especially on an official account.
    summary = compact(item.get("original_summary") or item.get("summary", ""))[:180]
    for kind, expression in EVENT_RULES:
        if re.search(expression, summary, re.I):
            return kind
    return "other"


def work_name(item: dict) -> str:
    title = compact(item.get("title", ""))
    original = compact(item.get("original_title", ""))
    for candidate in (title, original, compact(item.get("summary", ""))[:170]):
        match = re.search(r"《([^》]{2,65})》", candidate)
        if match:
            return normalized(match.group(1))
    if original:
        match = re.search(r"[‘'“\"]([^’'”\"]{3,65})[’'”\"]", original)
        if match and len(match.group(1).split()) <= 8:
            return normalized(match.group(1))
    return ""


def milestone(item: dict, kind: str) -> str:
    text = compact(item.get("title", "") + " " + item.get("summary", "")[:140])
    if kind == "box_office":
        amount = re.search(r"\d+(?:\.\d+)?\s*(?:亿|万|billion|million)", text, re.I)
        scope = "advance" if re.search(r"预售|点映|presale", text, re.I) else "total"
        return scope + ":" + (normalized(amount.group()) if amount else normalized(item.get("title", "")))
    if kind == "release_date":
        date = re.search(r"\d{1,2}月\d{1,2}日|\d{4}-\d{2}-\d{2}", text)
        return normalized(date.group()) if date else ""
    if kind == "music_asset":
        song = re.search(r"《([^》]{2,35})》\s*(?:MV|歌曲|歌词海报|主题曲)", text, re.I)
        return normalized(song.group(1)) if song else ""
    if kind in {"trailer", "poster"}:
        material = re.search(r"[“「]([^”」]{2,35})[”」]", item.get("title", ""))
        return normalized(material.group(1)) if material else ""
    return ""


def event_key(item: dict) -> str:
    title = compact(item.get("title", ""))
    kind = event_kind(item)
    try:
        day = dt.datetime.fromisoformat(item.get("published_at", "").replace("Z", "+00:00")).astimezone(BEIJING).date().isoformat()
    except ValueError:
        day = item.get("published_at", "")[:10]
    market = editorial_market(item)
    # The full nominees list and individual nominee posts describe one award announcement.
    if market == "m-cn" and "飞天奖" in title and re.search(r"入围|提名|名单", title):
        edition = re.search(r"第\d+届", title + " " + item.get("summary", ""))
        return f"{day}:{market}:飞天奖:{edition.group() if edition else '本届'}:nominees"
    if kind == "box_office" and "文牧野" in title:
        return f"{day}:{market}:文牧野:box_office:{milestone(item, kind)}"
    work = work_name(item)
    if work and kind != "other":
        return f"{day}:{market}:{work}:{kind}:{milestone(item, kind)}"
    return f"{day}:{market}:{kind}:headline:{normalized(title)}"


def editorial_market(item: dict) -> str:
    """Resolve only unmistakable mainland awards classified as observations."""
    market = item.get("market", "m-obs")
    title = compact(item.get("title", ""))
    if market == "m-obs" and re.search(r"中国电视金鹰奖|平遥国际电影展|飞天奖", title):
        return "m-cn"
    return market


def low_signal_reason(item: dict) -> str:
    title = compact(item.get("title", ""))
    summary = compact(item.get("summary", ""))
    if source_ids(item) & SOCIAL_IDS and ("飞天奖" in title and
            re.search(r"即将|期待", summary) and not re.search(r"作品名单公布|正式公布", summary)):
        return "advance_teaser"
    if re.search(r"我们目前对.{0,60}了解的内容|你需要知道的|What We Know", title, re.I):
        return "background_roundup"
    if re.search(r"美国偶像.{0,25}谋杀|American Idol.{0,25}murder", title, re.I):
        return "outside_film_tv"
    if re.search(r"饮品优惠|飲品優惠|購票優惠|购票福利|抽奖送|优惠券", title):
        return "promotion"
    if re.search(r"温馨提示|溫馨提示", title) and not re.search(r"《[^》]+》", title):
        return "insufficient_detail"
    has_specific_fact = bool(re.search(r"20\d{2}年|\d{1,2}月\d{1,2}日", title) or
                             re.search(r"执导|编剧|主演|定档|发布|获得", summary))
    if (len(summary) < 25 and event_kind(item) == "other"
            and not FACT_WORDS.search(title) and not has_specific_fact):
        return "insufficient_detail"
    for reason, pattern in LOW_SIGNAL:
        if pattern.search(title) and source_ids(item) & SOCIAL_IDS:
            # A new trailer/release attached to an anniversary is a new event.
            if reason != "anniversary" or not re.search(r"新预告|新海报|定档|今日上映", summary):
                return reason
    return ""


def short_headline(item: dict) -> str:
    title = visible_copy(item.get("title", "")) or compact(item.get("title", ""))
    if len(title) > 100 and "金鹰奖" in title and "颁奖典礼" in title:
        award = re.search(r"第\d+届[^，。]{2,20}金鹰奖", title)
        if award:
            return award.group() + "获奖结果公布"
    title = re.sub(r"^(?:国庆档|重磅|独家[:：]?|全球现象级)\s*", "", title)
    title = compact(title)
    if len(title) > 72:
        title = title[:72].rstrip("，。、：: -") + "…"
    return title


def core_sentence(item: dict) -> str:
    summary = visible_copy(item.get("summary", ""))
    summary = re.sub(r"^(?:中新网|人民网|新京报讯)[^。]{0,35}?(?:电|讯)\s*(?:[（(][^）)]{1,18}[）)])?", "", summary)
    summary = re.sub(r"^[（(]中央社記者[^）)]{2,45}[）)]\s*", "", summary)
    headline = short_headline(item)
    if not summary:
        return headline
    sentences = [compact(part) for part in re.split(r"[。！？；;]", summary) if compact(part)]
    if not sentences:
        return headline
    kind = event_kind(item)
    if "industry_blog" in {source.get("class") for source in item.get("sources", [])}:
        hong_kong_release = re.search(r"《([^》]+)》將於([^，,。]{2,25})上映", summary)
        if hong_kong_release:
            return f"《{hong_kong_release.group(1)}》將於{hong_kong_release.group(2)}上映"
        return headline
    if kind == "award" and "金鹰奖" in headline:
        winners = re.findall(
            r"([\u4e00-\u9fff]{2,4})凭借《([^》]+)》获(最佳(?:男|女)主角)",
            item.get("title", "") + "。" + item.get("summary", ""),
        )
        if winners:
            unique = list(dict.fromkeys(winners))[:2]
            return "，".join(f"{name}凭《{work}》获{honor}" for name, work, honor in unique)
    work = re.search(r"《([^》]+)》", headline)
    if work and kind in {"trailer", "poster", "music_asset"}:
        material = re.search(r"[“「]([^”」]{2,35})[”」]\s*(特别视频|预告片?|先导片|海报|剧照|MV)", headline, re.I)
        if material:
            return f"《{work.group(1)}》发布“{material.group(1)}”{material.group(2)}"
        material = re.search(r"(?:发布|释出|曝出?|推出)([^，。]{0,28}?(?:特别视频|预告片?|先导片|海报|剧照|MV))", headline, re.I)
        if material:
            return f"《{work.group(1)}》发布{material.group(1).strip()}"
    if kind == "filing" and source_ids(item) & SOCIAL_IDS:
        company = re.search(r"备案单位[^。；，,]{3,60}", summary)
        return headline + ("，" + company.group() if company else "")
    if kind == "box_office" and source_ids(item) & SOCIAL_IDS and FACT_WORDS.search(headline):
        return headline
    if item.get("distribution") == "series" and "播放量冠军" in headline:
        plays = re.search(r"(?:正片|全网)?播放量\s*\d+(?:\.\d+)?\s*[亿万]", summary)
        return plays.group() if plays else headline
    if kind == "award":
        if work:
            honor = re.search(r"[“「]([^”」]{3,45}(?:荣誉|奖|表扬|提名))(?=[”」])", summary)
            if honor:
                fact = f"《{work.group(1)}》获“{honor.group(1)}”"
                if re.search(r"先导片|预告片?", headline):
                    fact += "，并发布先导片" if "先导片" in headline else "，并发布预告"
                return fact
        awarded = re.search(r"导演([\u4e00-\u9fff]{2,4})凭借(?:动画)?电影《([^》]+)》(?:斩获|获得)([^。！!]{3,65})", summary)
        if awarded:
            return f"《{awarded.group(2)}》导演{awarded.group(1)}获得{awarded.group(3)}"
    if "征片" in headline:
        figures = re.search(r"收到\d+部影片报名[^。]{0,20}?覆盖\d+个国家和地区", summary)
        if figures:
            return figures.group()
    cue = re.compile(EVENT_FACTS.get(kind, r"$^"), re.I)
    def score(sentence: str) -> tuple[int, int]:
        value = 0
        if cue.search(sentence):
            value += 6
        if FACT_WORDS.search(sentence):
            value += 3
        if re.search(r"\d|[一二三四五六七八九十]月", sentence):
            value += 2
        if re.search(r"讲述|故事发生|饰演|期待|燃爽|震撼|一起走进|评论区", sentence):
            value -= 5
        return (value, -len(sentence))
    fact = max(sentences[:5], key=score)
    if len(fact) > 165:
        clauses = [compact(part) for part in re.split(r"[，,]", fact) if compact(part)]
        scored = [clause for clause in clauses if cue.search(clause)] or [
            clause for clause in clauses if FACT_WORDS.search(clause)]
        if scored:
            fact = max(scored, key=score)
            work = re.search(r"《([^》]+)》", summary)
            if work and work.group(1) not in fact:
                fact = "《" + work.group(1) + "》" + re.sub(r"^(?:影片|电影|该剧)", "", fact)
    if kind in {"music_asset", "trailer", "poster", "award"} and len(fact) > 115:
        return headline
    if len(fact) > 105 and FACT_WORDS.search(headline) and kind != "filing":
        return headline
    if len(fact) > 105:
        fact = fact[:105].rstrip("，、：: ") + "…"
    if len(fact) < 12 or normalized(fact) == normalized(headline):
        return headline
    return fact


def primary_rank(item: dict) -> tuple[int, int, int, int, int]:
    classes = [source.get("class", "") for source in item.get("sources", [])]
    rank = min((SOURCE_RANK.get(value, 5) for value in classes), default=5)
    general_award = 0 if "名单" in item.get("title", "") else 1
    media = 0 if item.get("image_url") or item.get("video_url") else 1
    specific_market = 0 if item.get("market") != "m-obs" else 1
    return rank, specific_market, general_award, media, -len(visible_copy(item.get("summary", "")))


def factual_overlap(left: str, right: str) -> float:
    a, b = normalized(left), normalized(right)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def assemble(group: list[dict]) -> dict:
    primary = min(group, key=primary_rank)
    result = copy.deepcopy(primary)
    specific_markets = {editorial_market(item) for item in group if editorial_market(item) != "m-obs"}
    if len(specific_markets) == 1:
        result["market"] = specific_markets.pop()
    kind = event_kind(primary)
    result["title"] = short_headline(primary)
    primary_fact = core_sentence(primary)
    if kind in {"award", "trailer", "poster", "music_asset"} and primary_fact.startswith("《") and len(primary_fact) <= 68:
        result["title"] = primary_fact
    facts = [primary_fact]
    if kind == "run_end":
        all_text = " ".join(item.get("summary", "") for item in group)
        stream = "并同步网播" if re.search(r"同步网播|同步上线网播", all_text) else ""
        box_office = re.search(r"(?:最终票房|票房)[^。；，,]{0,18}?\d+(?:\.\d+)?亿", all_text)
        work = re.search(r"《([^》]+)》", all_text)
        if work:
            result["title"] = f"《{work.group(1)}》今日下映"
        details = []
        if stream:
            details.append("同步网播")
        if box_office:
            details.append(box_office.group())
        facts = ["，".join(details)] if details else []
    elif kind == "award" and "飞天奖" in result["title"] and len(group) > 1:
        count = re.search(r"\d+部作品入围", " ".join(item.get("summary", "") for item in group))
        if count:
            facts = ["第35届电视剧飞天奖提名名单公布，" + count.group()]
    elif kind == "music_asset" and len(group) > 1:
        all_text = " ".join(item.get("title", "") + item.get("summary", "") for item in group)
        if "MV" in all_text and "歌词海报" in all_text:
            facts = ["同一歌曲的MV及歌词海报已发布"]
    else:
        for item in sorted(group, key=primary_rank):
            if item is primary:
                continue
            fact = core_sentence(item)
            if (fact and len(facts) < 2 and FACT_WORDS.search(fact)
                    and all(factual_overlap(fact, existing) < .45 for existing in facts)
                    and event_kind(item) != kind):
                facts.append(fact)
    # Headlines are not repeated as a second line; expanded sources still expose
    # every original article for readers who need the full detail.
    facts = [fact for fact in facts if factual_overlap(fact, result["title"]) < .78]
    result["summary"] = "；".join(facts)[:190] if facts else ""
    result["key_points"] = facts
    result["group_size"] = len(group)
    result["event_key"] = event_key(primary)
    result["source_items"] = [{"title": item.get("title", ""), "url": item.get("url", "")}
                              for item in group]
    sources = {}
    for item in group:
        for source in item.get("sources", []):
            if source.get("url"):
                value = copy.deepcopy(source)
                value["headline"] = short_headline(item)
                sources[source["url"]] = value
        if not result.get("image_url") and item.get("image_url"):
            result["image_url"] = item["image_url"]
        if not result.get("video_url") and item.get("video_url"):
            result["video_url"] = item["video_url"]
    result["sources"] = list(sources.values())
    # Grouping is a reading aid, not an independent-verification signal.
    result["confidence"] = primary.get("confidence", "single_source")
    result["editorial"] = True
    return result


def relevance_key(item: dict) -> tuple[int, int, int, float]:
    kind = event_kind(item)
    # A project filing from a social account is useful, but its official status
    # is not established by that post and it should not crowd out official news.
    social_filing = 2 if kind == "filing" and source_ids(item) & SOCIAL_IDS else 0
    rank = min((SOURCE_RANK.get(source.get("class", ""), 5)
                for source in item.get("sources", [])), default=5)
    try:
        time = dt.datetime.fromisoformat(item.get("published_at", "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        time = 0
    return EVENT_RANK.get(kind, 5) + social_filing, rank, -int(bool(item.get("image_url"))), -time


def choose_events(items: list[dict], budgets: dict[str, int]) -> tuple[list[dict], list[dict]]:
    chosen, more = [], []
    for market in {item.get("market", "m-obs") for item in items}:
        market_items = sorted((item for item in items if item.get("market") == market), key=relevance_key)
        budget = max(0, int(budgets.get(market, len(market_items))))
        selection = market_items[:budget]
        # Keep series visible beside films when this market has a series update.
        if budget >= 6 and not any(item.get("distribution") == "series" for item in selection):
            series = next((item for item in market_items[budget:] if item.get("distribution") == "series"), None)
            if series:
                selection[-1] = series
        selected_ids = {item["id"] for item in selection}
        chosen.extend(selection)
        more.extend(item for item in market_items if item["id"] not in selected_ids)
    return chosen, more


def make_brief(payload: dict) -> dict:
    raw = copy.deepcopy(payload.get("items", []))
    groups: dict[str, list[dict]] = {}
    filtered = []
    for item in raw:
        reason = low_signal_reason(item)
        if reason:
            filtered.append({"id": item.get("id"), "reason": reason, "title": item.get("title"),
                             "url": item.get("url")})
            continue
        groups.setdefault(event_key(item), []).append(item)
    # "Industry observation" is sometimes a fallback classification, not the
    # location of the news. Merge it into a single matching specific market.
    for key in list(groups):
        if ":m-obs:" not in key:
            continue
        matches = [key.replace(":m-obs:", f":{market}:", 1)
                   for market in ("m-cn", "m-hmt", "m-intl")]
        matches = [candidate for candidate in matches if candidate in groups]
        if len(matches) == 1:
            groups[matches[0]].extend(groups.pop(key))
    event_items = [assemble(group) for group in groups.values()]
    budget = payload.get("policy", {}).get("editorial_budget", EDITORIAL_BUDGET)
    items, more_items = choose_events(event_items, budget)
    payload["source_items"] = raw
    payload["filtered_items"] = filtered
    payload["more_items"] = more_items
    payload["items"] = items
    stats = payload.setdefault("stats", {})
    stats["source_items"] = len(raw)
    stats["brief_events"] = len(items)
    stats["available_events"] = len(event_items)
    stats["more_events"] = len(more_items)
    stats["merged_source_items"] = sum(len(group) - 1 for group in groups.values())
    stats["filtered_low_signal"] = len(filtered)
    stats["items"] = len(items)
    for distribution in stats.get("distribution_counts", {}):
        stats["distribution_counts"][distribution] = sum(
            item.get("distribution") == distribution for item in items)
    if "translation" in stats:
        stats["translation"]["translated_home_items"] = sum(
            item.get("translation_status") == "machine_translated" for item in items)
        stats["translation"]["original_english_home_items"] = sum(
            item.get("translation_status") == "original_english" for item in items)
    for market, coverage in stats.get("market_coverage", {}).items():
        coverage["items"] = sum(item.get("market") == market for item in items)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="generated/latest.json")
    args = parser.parse_args()
    path = Path(args.input)
    payload = json.loads(path.read_text(encoding="utf-8"))
    make_brief(payload)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["stats"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
