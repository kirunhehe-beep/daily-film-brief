"""Read back the public site after deployment; a green upload is not enough."""
import argparse
import hashlib
import html
import json
import time
import urllib.request
from pathlib import Path


def validate(expected, actual, page):
    if expected != actual:
        raise ValueError("线上数据与本次采集结果不一致")
    if f'每日影视简报 · {expected["edition_date"]}' not in page:
        raise ValueError("线上页面日期与本期不一致")
    for item in expected["items"]:
        if html.escape(item["title"], quote=True) not in page:
            raise ValueError("线上页面缺少本期新闻")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="generated/latest.json")
    parser.add_argument("--url", default="https://daily-film-brief.pages.dev")
    args = parser.parse_args()
    raw = Path(args.input).read_bytes()
    expected = json.loads(raw)
    marker = hashlib.sha256(raw).hexdigest()[:16]
    def read(path):
        request = urllib.request.Request(args.url.rstrip("/") + path + "?verify=" + marker,
                                         headers={"Cache-Control": "no-cache",
                                                  "User-Agent": "DailyFilmBriefDeploymentCheck/1.0 (+https://daily-film-brief.pages.dev/)"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8")
    for attempt in range(6):
        try:
            validate(expected, json.loads(read("/data/latest.json")), read("/"))
            print(f'线上核验通过：{expected["edition_date"]}，{len(expected["items"])} 条')
            return 0
        except Exception as exc:
            status = f" HTTP {exc.code}" if hasattr(exc, "code") else ""
            print(f"线上核验暂未通过（{type(exc).__name__}{status}），第 {attempt + 1}/6 次", flush=True)
            if attempt < 5:
                time.sleep(10)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
