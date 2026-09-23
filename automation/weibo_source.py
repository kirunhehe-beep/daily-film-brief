"""Read-only Weibo CLI gateway adapter (no login, posting or purchase actions).

Protocol: official @weibo-ai/weibo-cli 0.9.8 and open.weibo.com/cli.
Only q (search) and uid (external timeline) are assumed. Optional flags are
checked against authenticated command metadata, never guessed from other APIs.
Response compatibility is fixture-tested; a real authorized sample is still
required before claiming live coverage. Tokens must only come from environment.
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit


BASE_URL = "https://open.weibo.com/cli/api"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward a Bearer credential to a redirected destination."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect())


class SourceUnavailable(RuntimeError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


class FetchedItems(list):
    def __init__(self, items: list, checks: list):
        super().__init__(items)
        self.checks = checks
        self.status = "ok" if all(c["status"] == "ok" for c in checks) else "partial"


def clean(value: object) -> str:
    text = html.unescape(value) if isinstance(value, str) else ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def http_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username:
            return value
    except ValueError:
        pass
    return ""


def post_url(post: dict) -> str:
    user = post.get("user") or {}
    uid = str(user.get("idstr") or user.get("id") or "")
    bid = str(post.get("mblogid") or post.get("bid") or "")
    if uid.isdigit() and re.fullmatch(r"[A-Za-z0-9]+", bid):
        return f"https://weibo.com/{uid}/{bid}"
    ident = str(post.get("idstr") or post.get("id") or "")
    return f"https://m.weibo.cn/detail/{ident}" if ident.isdigit() else ""


def normalize_post(post: dict) -> dict:
    """Keep the posting account/time; a repost is never an independent confirmation."""
    if not isinstance(post, dict):
        raise ValueError("微博条目不是对象")
    long_text = post.get("long_text") or post.get("longText") or {}
    if isinstance(long_text, dict):
        long_text = long_text.get("longTextContent") or long_text.get("content") or long_text.get("text")
    text = clean(long_text or post.get("text_raw") or post.get("text"))
    url = post_url(post)
    if not text or not url:
        raise ValueError("微博条目缺少正文或可定位的原帖 ID")
    user = post.get("user") or {}
    user_id = str(user.get("idstr") or user.get("id") or "")
    author = clean(user.get("screen_name")) or "微博账号"
    # Account verification badges do not prove an individual statement true.
    image = http_url(post.get("original_pic")) or http_url(post.get("bmiddle_pic"))
    if not image:
        for pic in post.get("pics") or []:
            if not isinstance(pic, dict):
                continue
            large = pic.get("large") or {}
            image = http_url(large.get("url")) if isinstance(large, dict) else ""
            image = image or http_url(pic.get("url"))
            if image:
                break
    page = post.get("page_info") or {}
    media = page.get("media_info") or {}
    video = http_url(media.get("stream_url_hd")) or http_url(media.get("stream_url"))
    video_page = http_url(page.get("page_url")) if str(page.get("type", "")) == "video" else ""
    original = post.get("retweeted_status") or {}
    return {
        "title": text[:100] + ("…" if len(text) > 100 else ""),
        "summary": text,
        "url": url,
        "published": str(post.get("created_at") or ""),
        "image_url": image,
        "video_url": video,
        "video_page_url": video_page,
        "author": author,
        "author_id": user_id,
        "post_id": str(post.get("idstr") or post.get("id")),
        "publisher_id": "weibo:" + (user_id or author),
        "platform": "weibo",
        "is_repost": bool(original),
        "original_post_url": post_url(original) if isinstance(original, dict) else "",
        "text_complete": not bool(post.get("is_long_text")) or bool(long_text),
    }


def request_json(path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    request = urllib.request.Request(BASE_URL + path, data=data, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/json", "Content-Type": "application/json",
        "User-Agent": "DailyFilmBrief/WeiboReadOnly/1.0",
    })
    try:
        with OPENER.open(request, timeout=25) as response:
            raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise SourceUnavailable("response_error", "微博响应过大，本轮已停止读取")
        payload = json.loads(raw)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        if code == 401:
            raise SourceUnavailable("authorization_required", "微博授权缺失或已过期，需要重新授权") from None
        if code in {402, 403}:
            raise SourceUnavailable("service_required", "微博服务权限或额度不可用，请在开放平台检查") from None
        if code == 429:
            raise SourceUnavailable("rate_limited", "微博接口限流，本轮停止请求，下次再试") from None
        raise SourceUnavailable("network_error", f"微博接口暂不可用（HTTP {code}）") from None
    except (OSError, ValueError):
        # Never echo the response or exception: they may contain access tokens.
        raise SourceUnavailable("response_error", "微博网络或响应格式异常") from None
    if not isinstance(payload, dict) or payload.get("error") or payload.get("error_code"):
        raise SourceUnavailable("response_error", "微博接口返回错误，未计为采集成功")
    return payload


def flag_names(metadata: dict) -> set[str]:
    command = metadata.get("command", metadata.get("result", metadata))
    if not isinstance(command, dict):
        raise SourceUnavailable("schema_changed", "微博接口参数格式待核对")
    flags = command.get("flags")
    if isinstance(flags, dict):
        return set(flags)
    if isinstance(flags, list):
        return {flag.get("name", "").lstrip("-") for flag in flags if isinstance(flag, dict)}
    raise SourceUnavailable("schema_changed", "微博接口参数格式待核对，未调用内容接口")


def extract_posts(payload: dict) -> tuple[list, bool]:
    """Fail visibly on unknown envelopes, instead of silently treating them as no news."""
    result = payload.get("result")
    if isinstance(result, dict) and (result.get("error") or result.get("error_code")):
        raise SourceUnavailable("response_error", "微博内容接口未成功返回资讯")
    posts = result.get("statuses") if isinstance(result, dict) else result
    if not isinstance(posts, list):
        raise SourceUnavailable("schema_changed", "微博内容结构待核对，不能判定为暂无消息")
    meta = payload.get("meta") or {}
    # The public SDK doesn't define outcome enums. Surface any outcome for
    # operator verification; retain well-formed items without claiming full success.
    needs_review = isinstance(meta, dict) and meta.get("outcome") is not None
    if not posts and needs_review:
        raise SourceUnavailable("outcome_unverified", "微博返回附加执行状态，空结果需核对")
    return posts, needs_review


def fetch_weibo(source: dict) -> FetchedItems:
    token = os.environ.get("WEIBO_CLI_TOKEN") or os.environ.get("WEIBO_TOKEN")
    if not token:
        raise SourceUnavailable("authorization_required", "微博待授权，尚未接通实际采集")
    timeline = source.get("type") == "weibo_timeline"
    targets = source.get("uids", []) if timeline else source.get("queries", [])
    if not isinstance(targets, list) or not targets or any(not str(t).strip() for t in targets):
        raise SourceUnavailable("configuration_required", "微博采集目标未配置")
    if timeline and any(not str(uid).isdigit() for uid in targets):
        raise SourceUnavailable("configuration_required", "微博账号请填写核实后的数字 UID")
    group, action, target_flag = ("statuses", "user_timeline/other", "uid") if timeline else (
        "search", "statuses/limited", "q")
    flags = flag_names(request_json(f"/cli/commands/{group}/{quote(action, safe='')}", token))
    if target_flag not in flags:
        raise SourceUnavailable("schema_changed", "微博目标参数与官方接口不匹配，未调用内容接口")
    max_pages = max(1, min(int(source.get("max_pages", 1)), 10))
    # A ceiling controls usage, not item count. Hitting it is visible in checks.
    max_requests = max(1, min(int(source.get("max_requests_per_run", 2)), 50))
    if max_requests < len(targets):
        raise SourceUnavailable("configuration_required", "微博调用预算小于目标数，请拆分来源或调整预算")
    calls, result, checks, seen = 0, [], [], set()
    base_pages, extra_pages = divmod(max_requests, len(targets))
    for target_index, target in enumerate(targets):
        check = {"target": str(target), "status": "ok", "requests": 0, "items": 0}
        if calls >= max_requests:
            check.update(status="budget_limited", message="已到本轮调用上限，此目标尚未采集")
            checks.append(check)
            continue
        target_pages = min(max_pages, base_pages + int(target_index < extra_pages))
        seen_pages = set()
        for page in range(1, target_pages + 1):
            if calls >= max_requests:
                check.update(status="budget_limited", message="已到本轮调用上限，尚有后续页待采集")
                break
            if page > 1 and "page" not in flags:
                check.update(status="pagination_unavailable", message="当前接口不支持已验证的页码参数")
                break
            arguments = {target_flag: str(target)}
            if "page" in flags:
                arguments["page"] = page
            if "count" in flags:
                arguments["count"] = int(source.get("page_size", 20))
            if "isGetLongText" in flags:
                arguments["isGetLongText"] = 1
            calls += 1
            check["requests"] += 1
            try:
                response = request_json("/cli/invoke", token, {"group": group, "action": action, "args": arguments})
                posts, outcome_unverified = extract_posts(response)
                if outcome_unverified:
                    check.update(status="partial", message="已保留条目，接口附加执行状态待核对")
                page_ids = []
                for post in posts:
                    try:
                        item = normalize_post(post)
                    except (ValueError, TypeError, AttributeError):
                        check.update(status="partial", message="部分微博条目格式待核对")
                        continue
                    page_ids.append(item["post_id"])
                    if item["post_id"] not in seen:
                        seen.add(item["post_id"])
                        result.append(item)
                        check["items"] += 1
                if not posts:
                    break
                signature = tuple(sorted(page_ids))
                if signature and signature in seen_pages:
                    check.update(status="window_limited", message="接口重复返回当前结果，无法确认后续覆盖")
                    break
                seen_pages.add(signature)
                if page == target_pages and check["status"] == "ok":
                    check.update(status="window_limited", message="已抓取本轮查询窗口，不代表微博全量覆盖")
            except SourceUnavailable as exc:
                check.update(status=exc.status, message=str(exc))
                break
        checks.append(check)
        if check["status"] in {"authorization_required", "service_required", "rate_limited"}:
            for remaining in targets[len(checks):]:
                checks.append({"target": str(remaining), "status": "not_attempted", "items": 0, "requests": 0})
            break
    if not result and all(c["status"] != "ok" for c in checks):
        failed = next((c for c in checks if c["status"] != "not_attempted"), checks[0])
        raise SourceUnavailable(failed["status"], failed.get("message", "微博本轮未完成采集"))
    return FetchedItems(result, checks)
