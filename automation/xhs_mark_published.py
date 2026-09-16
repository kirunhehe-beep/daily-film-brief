#!/usr/bin/env python3
"""记录小红书发布结果，供定时任务去重。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--note-url", required=True)
    parser.add_argument("--state", default="output/xhs/published.json")
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    state_path = Path(args.state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {"published": []}
    digest = manifest["digest"]
    if not any(entry.get("digest") == digest for entry in state["published"]):
        state["published"].append({
            "digest": digest,
            "publish_date": manifest["publish_date"],
            "note_url": args.note_url,
            "published_at": dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        })
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
