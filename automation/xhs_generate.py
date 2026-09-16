#!/usr/bin/env python3
"""从每日影视简报 JSON 生成可发布的小红书图文包。

输出包括 3:4 PNG 多图、caption.txt 和 manifest.json。程序只消费来源中
已经存在的信息，不补写演员、档期或数据；证据状态在视觉上仅显示为
“已确认”或“待确认”。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import re
import sys
import unicodedata
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont, ImageOps


BEIJING = ZoneInfo("Asia/Shanghai")
SIZE = (1080, 1440)
PAPER = (248, 247, 243)
INK = (24, 24, 22)
MUTED = (119, 119, 112)
RED = (190, 53, 45)
DIVIDER = (216, 214, 207)
MARKET_NAMES = {
    "m-cn": "中国内地",
    "m-hmt": "中国港澳台",
    "m-intl": "海外",
    "m-obs": "行业观察",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="generated/latest.json")
    parser.add_argument("--output-root", default="output/xhs")
    parser.add_argument("--publish-date", help="笔记发布日期，YYYY-MM-DD；默认北京时间今天")
    parser.add_argument("--max-items", type=int, default=5)
    parser.add_argument("--site-posters", default="app/posters")
    parser.add_argument("--allow-no-yesterday", action="store_true", help="没有昨日条目时从本轮最新条目生成，仅用于预览")
    return parser.parse_args()


def pick_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "/System/Library/Fonts/STHeiti Medium.ttc" if bold else "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size, index=0)
    raise RuntimeError("未找到中文字体；Linux 请安装 fonts-noto-cjk")


def published_beijing(item: dict) -> dt.datetime | None:
    raw = item.get("published_at", "")
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(BEIJING)
    except (TypeError, ValueError):
        return None


def project_names(text: str) -> list[str]:
    names = []
    for name in re.findall(r"《([^》]{1,24})》", text or ""):
        name = re.sub(r"\s+", "", name)
        if name and name not in names:
            names.append(name)
    return names


def actor_names(text: str) -> list[str]:
    """只从明确的演员字段附近抽取中文姓名；宁缺毋滥。"""
    found: list[str] = []
    normalized = unicodedata.normalize("NFKC", text or "")
    stop = {
        "电影", "电视剧", "影片", "剧集", "导演", "编剧", "监制", "出品", "联合", "正式",
        "全国", "上线", "首播", "主演", "领衔", "以及", "等人", "按照", "姓氏", "笔画",
        "阵容", "主创", "儿童电影", "国产电影", "经典电影",
    }
    fragments: list[str] = []
    # Explicit cast lists before the role marker are the most reliable form.
    fragments.extend(match.group(1) for match in re.finditer(
        r"([\u4e00-\u9fff·]{2,4}?(?:、[\u4e00-\u9fff·]{2,4}?){1,8})(?=领衔主演|共同主演|主演)",
        normalized,
    ))
    # Also accept fields that explicitly introduce a following cast list.
    fragments.extend(match.group(1) for match in re.finditer(
        r"(?:主演包括|主演有|演员包括|演员有)[：:\s]*([^。；…]{2,90})",
        normalized,
    ))
    # Role annotations and a name placed directly before a project title are
    # useful when the feed omits a formal cast field.
    fragments.extend(re.findall(r"[（(]([\u4e00-\u9fff·]{2,4})\s*饰", normalized))
    fragments.extend(re.findall(r"(?:^|[！!。；，,\s])([\u4e00-\u9fff·]{2,4})《", normalized))
    for fragment in fragments:
        fragment = re.sub(r"（[^）]*）|\([^)]*\)", "", fragment)
        for token in re.split(r"[、，,／/和与×\s]+", fragment):
            token = re.sub(r"[^\u4e00-\u9fff·]", "", token).strip("·")
            if 2 <= len(token) <= 4 and token not in stop and token not in found:
                found.append(token)
    return found[:8]


def card_title_and_event(item: dict) -> tuple[str, str]:
    """优先用片名做主标题，用原始标题剩余部分描述动态。"""
    raw = re.sub(r"\s+", " ", item.get("title", "影视动态")).strip()
    projects = project_names(raw)
    if not projects:
        return raw, ""
    project = projects[0]
    title = f"《{project}》"
    event = raw.replace(title, "", 1).strip(" ，,·｜|-—")
    event = re.sub(r"^(电影|电视剧|剧集|网剧|优酷|爱奇艺|腾讯视频)", "", event).strip(" ，,·｜|-—")
    return title, event


def first_sentence(text: str, limit: int = 76) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    sentence = re.split(r"(?<=[。！？])", compact, maxsplit=1)[0]
    if len(sentence) > limit:
        sentence = sentence[:limit].rstrip("，、； ") + "…"
    return sentence or "来源未提供可用摘要"


def status_label(item: dict) -> str:
    return "已确认" if item.get("confidence") in {"official", "multi_source"} else "待确认"


def select_items(payload: dict, publish_day: dt.date, max_items: int, allow_fallback: bool) -> tuple[list[dict], dt.date]:
    source_day = publish_day - dt.timedelta(days=1)
    source_items = payload.get("xhs_items", payload.get("items", []))
    items = [item for item in source_items if (published_beijing(item) or dt.datetime.min.replace(tzinfo=BEIJING)).date() == source_day]
    if not items and allow_fallback:
        dated = [(published_beijing(item), item) for item in source_items]
        dated = [(stamp, item) for stamp, item in dated if stamp]
        if dated:
            source_day = max(stamp.date() for stamp, _ in dated)
            items = [item for stamp, item in dated if stamp.date() == source_day]
    rank = {"official": 0, "multi_source": 1, "single_source": 2}
    items.sort(key=lambda item: (rank.get(item.get("confidence"), 3), int(item.get("content_priority", 9)), -(published_beijing(item) or dt.datetime.min.replace(tzinfo=BEIJING)).timestamp()))
    return items[:max_items], source_day


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int, max_lines: int | None = None) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > width:
            lines.append(current)
            current = char
            if max_lines and len(lines) == max_lines:
                lines[-1] = lines[-1][:-1].rstrip() + "…"
                return lines
        else:
            current = candidate
    if current and (not max_lines or len(lines) < max_lines):
        lines.append(current)
    return lines


def draw_lines(draw: ImageDraw.ImageDraw, xy: tuple[int, int], lines: list[str], font: ImageFont.FreeTypeFont, fill: tuple[int, int, int], spacing: int) -> int:
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += font.size + spacing
    return y


def local_poster(item: dict, poster_dir: Path) -> Path | None:
    for project in project_names(item.get("title", "") + item.get("summary", "")):
        for candidate in poster_dir.glob("*"):
            if project in candidate.stem and candidate.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                return candidate
    return None


def fetch_image(item: dict, poster_dir: Path) -> Image.Image | None:
    candidate = local_poster(item, poster_dir)
    if candidate:
        try:
            return Image.open(candidate).convert("RGB")
        except OSError:
            pass
    url = item.get("image_url", "")
    if not url.startswith(("https://", "http://")):
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 DailyFilmBrief/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return Image.open(io.BytesIO(response.read(8_000_000))).convert("RGB")
    except Exception:
        return None


def render_cover(path: Path, publish_day: dt.date, source_day: dt.date, count: int) -> None:
    image = Image.new("RGB", SIZE, PAPER)
    draw = ImageDraw.Draw(image)
    left = 78
    draw.text((left, 102), publish_day.strftime("%Y年%-m月%-d日"), font=pick_font(37, True), fill=INK)
    draw.line((left, 186, SIZE[0] - left, 186), fill=RED, width=5)
    y = 260
    for line in ("一觉醒来", "影视圈", "发生了什么？"):
        draw.text((left, y), line, font=pick_font(112, True), fill=INK)
        y += 145
    draw.rounded_rectangle((left, 775, left + 196, 838), radius=5, fill=RED)
    draw.text((left + 21, 786), "昨日简报", font=pick_font(38, True), fill=(255, 255, 255))
    draw.line((left, 888, SIZE[0] - left, 888), fill=DIVIDER, width=2)
    draw.text((left, 930), f"{count}条动态｜{source_day.strftime('%-m月%-d日')}", font=pick_font(47, True), fill=INK)
    draw.text((left, 1002), "电影打工人的宣发观察", font=pick_font(34), fill=MUTED)
    draw.text((left, 1310), "每日影视简报", font=pick_font(30, True), fill=INK)
    image.save(path, quality=95)


def render_card(path: Path, item: dict, index: int, poster_dir: Path) -> None:
    image = Image.new("RGB", SIZE, PAPER)
    draw = ImageDraw.Draw(image)
    left, right = 70, SIZE[0] - 70
    market = MARKET_NAMES.get(item.get("market"), "影视动态")
    draw.text((left, 54), f"{index:02d}｜{market}", font=pick_font(36, True), fill=INK)
    draw.line((left + 235, 80, right, 80), fill=RED, width=4)
    y = 142
    card_title, event = card_title_and_event(item)
    title_font = pick_font(70, True)
    y = draw_lines(draw, (left, y), wrap_text(draw, card_title, title_font, right - left, 2), title_font, INK, 16)
    event_font = pick_font(38, True)
    detail_font = pick_font(31)
    y += 18
    if event:
        y = draw_lines(draw, (left, y), wrap_text(draw, event, event_font, right - left, 3), event_font, (48, 48, 44), 12)
    else:
        y = draw_lines(draw, (left, y), wrap_text(draw, first_sentence(item.get("summary", "")), detail_font, right - left, 3), detail_font, (55, 55, 51), 11)
    y += 28
    draw.line((left, y, right, y), fill=DIVIDER, width=2)
    image_top = min(max(y + 30, 490), 650)
    source_image = fetch_image(item, poster_dir)
    box = (left, image_top, right, 1245)
    if source_image:
        fitted = ImageOps.fit(source_image, (box[2] - box[0], box[3] - box[1]), method=Image.Resampling.LANCZOS, centering=(0.5, 0.45))
        image.paste(fitted, (box[0], box[1]))
    else:
        draw.rectangle(box, fill=(232, 230, 224))
        draw.text((left + 36, image_top + 42), "FILM", font=pick_font(92, True), fill=(198, 195, 187))
    draw.line((left, 1285, right, 1285), fill=DIVIDER, width=2)
    sources = " / ".join(source.get("name", "来源") for source in item.get("sources", [])) or "来源待补充"
    draw.text((left, 1310), sources[:34], font=pick_font(24), fill=MUTED)
    status = status_label(item)
    status_font = pick_font(26)
    status_width = draw.textbbox((0, 0), status, font=status_font)[2]
    draw.text((right - status_width, 1310), status, font=status_font, fill=MUTED)
    image.save(path, quality=95)


def build_caption(items: list[dict]) -> tuple[str, str, list[str]]:
    title = "一觉醒来，影视圈发生了什么？"
    paragraphs = ["把昨天值得留意的影视动态整理成一组图。"]
    tags = ["影视圈", "每日影视简报"]
    for index, item in enumerate(items, 1):
        label = status_label(item)
        paragraphs.append(f"{index:02d} {item.get('title', '影视动态')}。{first_sentence(item.get('summary', ''), 92)}（{label}）")
        combined = item.get("title", "") + " " + item.get("summary", "")
        tags.extend(project_names(combined))
        tags.extend(actor_names(combined))
    tags.append("电影宣发")
    unique_tags = []
    for tag in tags:
        tag = re.sub(r"[\s#]+", "", tag)
        if tag and tag not in unique_tags:
            unique_tags.append(tag)
    tag_line = " ".join("#" + tag for tag in unique_tags[:18])
    return title, "\n\n".join(paragraphs) + "\n\n" + tag_line, unique_tags


def main() -> int:
    args = parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    publish_day = dt.date.fromisoformat(args.publish_date) if args.publish_date else dt.datetime.now(BEIJING).date()
    items, source_day = select_items(payload, publish_day, args.max_items, args.allow_no_yesterday)
    if not items:
        print(json.dumps({"status": "skipped", "reason": "no_yesterday_items", "source_date": source_day.isoformat()}, ensure_ascii=False))
        return 2

    output = Path(args.output_root) / publish_day.isoformat()
    output.mkdir(parents=True, exist_ok=True)
    images = []
    cover = output / "01-cover.png"
    render_cover(cover, publish_day, source_day, len(items))
    images.append(cover.name)
    poster_dir = Path(args.site_posters)
    for index, item in enumerate(items, 1):
        target = output / f"{index + 1:02d}-item.png"
        render_card(target, item, index, poster_dir)
        images.append(target.name)

    title, caption, tags = build_caption(items)
    (output / "caption.txt").write_text(title + "\n\n" + caption + "\n", encoding="utf-8")
    digest_source = json.dumps([item.get("id") for item in items], ensure_ascii=False, separators=(",", ":"))
    manifest = {
        "schema_version": 1,
        "status": "ready",
        "publish_date": publish_day.isoformat(),
        "source_date": source_day.isoformat(),
        "digest": hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:20],
        "title": title,
        "caption": caption,
        "tags": tags,
        "images": images,
        "items": [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "status": status_label(item),
                "source_urls": [source.get("url") for source in item.get("sources", [])],
            }
            for item in items
        ],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "ready", "output": str(output), "images": len(images), "digest": manifest["digest"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
