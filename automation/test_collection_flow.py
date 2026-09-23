"""End-to-end local pipeline regression tests with synthetic source responses."""
import datetime as dt
import json
import tempfile
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import brief_pipeline as pipeline
import item_store
import render_daily
from weibo_source import SourceUnavailable


NOW = dt.datetime(2026, 9, 23, 3, 0, tzinfo=dt.timezone.utc)


def item(ident="a", **fields):
    return {"title": "电影测试 " + ident, "summary": "电影发布海报", "published": NOW.isoformat(),
            "url": "https://example.com/article?id=" + ident, "image_url": "", "video_url": "", **fields}


def source(ident="news", **fields):
    return {"id": ident, "name": "测试来源", "enabled": True, "type": "rss", "market": "m-cn",
            "source_class": "trade_media", "language": "zh", **fields}


def config(*sources):
    return {"policy": {"required_markets": ["m-cn", "m-hmt"], "max_age_hours": 120,
                       "max_items_per_market": {}, "allow_carry_forward": False}, "sources": list(sources or [source()])}


class CollectionTests(unittest.TestCase):
    def test_distinct_query_ids_are_not_lost(self):
        self.assertNotEqual(pipeline.canonical_url("https://example.com/article?id=1"), pipeline.canonical_url("https://example.com/article?id=2"))
        self.assertEqual(pipeline.canonical_url("https://example.com/article?id=1&utm_source=x#top"), "https://example.com/article?id=1")

    def test_multiple_runs_accumulate_when_feed_rolls_over(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.json"
            with patch.object(pipeline, "fetch_source", return_value=[item("a")]):
                pipeline.run(config(), store_path=path, now=NOW)
            with patch.object(pipeline, "fetch_source", return_value=[item("b")]):
                result = pipeline.run(config(), store_path=path, now=NOW + dt.timedelta(hours=3))
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["stats"]["new_records"], 1)

    def test_auth_failure_does_not_block_news_and_is_visible(self):
        cfg = config(source("weibo-film-search", type="weibo_search"), source())
        with patch.object(pipeline, "fetch_source", side_effect=[SourceUnavailable("authorization_required", "微博待授权"), [item()]]):
            result = pipeline.run(cfg, now=NOW)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["stats"]["successful_sources"], 1)
        self.assertEqual(result["source_statuses"][0]["status"], "authorization_required")

    def test_known_items_remain_when_source_temporarily_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.json"
            with patch.object(pipeline, "fetch_source", return_value=[item()]):
                pipeline.run(config(), store_path=path, now=NOW)
            with patch.object(pipeline, "fetch_source", side_effect=TimeoutError("failure")):
                result = pipeline.run(config(), store_path=path, now=NOW + dt.timedelta(hours=1))
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["stats"]["successful_sources"], 0)

    def test_no_undated_or_yesterday_items_masquerade_as_today(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.json"
            rows = [item("undated", published=""), item("yesterday", published=(NOW - dt.timedelta(days=1)).isoformat())]
            with patch.object(pipeline, "fetch_source", return_value=rows):
                result = pipeline.run(config(source(carry_forward_days=30)), store_path=path, now=NOW)
            self.assertEqual(len(item_store.load_store(path)["records"]), 2)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["edition_date"], "2026-09-23")

    def test_two_social_discoveries_do_not_promote_credibility(self):
        cfg = config(source("search", source_class="social"), source("timeline", source_class="social"))
        row = item(author="测试账号", publisher_id="weibo:888", is_repost=True)
        with patch.object(pipeline, "fetch_source", return_value=[row]):
            result = pipeline.run(cfg, now=NOW)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["confidence"], "social_lead")
        self.assertEqual(len(result["items"][0]["sources"]), 1)

    def test_unlimited_market_and_film_before_series(self):
        rows = [item(str(i), title=f"电视剧第{i}项") for i in range(20)] + [item("film", title="电影发布预告")]
        with patch.object(pipeline, "fetch_source", return_value=rows):
            result = pipeline.run(config(), now=NOW)
        self.assertEqual(len(result["items"]), 21)
        self.assertEqual(result["items"][0]["distribution"], "film_unspecified")

    def test_same_headline_different_days_is_not_merged(self):
        rows = [item("old", title="电影预售开启", published=(NOW - dt.timedelta(days=1)).isoformat()), item("new", title="电影预售开启")]
        with patch.object(pipeline, "fetch_source", return_value=rows):
            result = pipeline.run(config(), now=NOW)
        self.assertEqual(len(result["items"]), 1)
        self.assertIn("id=new", result["items"][0]["url"])

    def test_dedup_uses_beijing_day_not_utc_day(self):
        midnight = dt.datetime(2026, 9, 22, 17, tzinfo=dt.timezone.utc)
        rows = [item("old", title="电影预售开启", published="2026-09-22T14:00:00Z"),
                item("new", title="电影预售开启", published="2026-09-22T17:00:00Z")]
        with patch.object(pipeline, "fetch_source", return_value=rows):
            result = pipeline.run(config(), now=midnight)
        self.assertEqual(len(result["items"]), 1)
        self.assertIn("id=new", result["items"][0]["url"])

    def test_media_budget_does_not_limit_news_items(self):
        rows = [item(str(i)) for i in range(12)]
        cfg = config(source(enrich_media=True))
        cfg["policy"]["max_media_enrichments"] = 2
        with patch.object(pipeline, "fetch_source", return_value=rows), patch.object(pipeline, "page_media", return_value=("https://example.com/a.jpg", "")) as media:
            result = pipeline.run(cfg, now=NOW)
        self.assertEqual(media.call_count, 2)
        self.assertEqual(len(result["items"]), 12)

    def test_rss_is_not_truncated_before_processing(self):
        rows = [item(str(i)) for i in range(30)]
        with patch.object(pipeline, "fetch_bytes", return_value=b"test"), patch.object(pipeline, "parse_feed", return_value=rows):
            result = pipeline.fetch_source(source(url="https://example.com/feed", max_items=2))
        self.assertEqual(len(result), 30)

    def test_social_rendering_explains_reposts_and_has_media(self):
        row = {**item(), "published_at": NOW.isoformat(), "confidence": "social_lead", "is_repost": True,
               "text_complete": False, "video_page_url": "https://weibo.com/tv/show/test",
               "sources": [{"name": "微博 · 测试账号", "url": "https://weibo.com/888/test"}]}
        rendered = render_daily.render_item(row, {"social_lead": "社媒线索"})
        self.assertIn("社媒线索", rendered)
        self.assertIn("转发动态", rendered)
        self.assertIn("查看原帖视频", rendered)
        self.assertIn("正文可能被平台截断", rendered)
        self.assertNotIn("预告片暂未收录", rendered)

    def test_market_without_source_is_not_reported_as_no_news(self):
        rendered = render_daily.render_empty_market("m-hmt", "中国港澳台", {"configured_sources": 0})
        self.assertIn("来源尚待补充", rendered)

    def test_real_config_has_no_xhs_input_and_no_market_caps(self):
        current = json.loads((Path(__file__).parent / "sources.json").read_text())
        self.assertEqual(current["policy"]["max_items_per_market"], {})
        self.assertFalse(current["policy"]["allow_carry_forward"])
        self.assertFalse(any("xiaohongshu" in s["type"] for s in current["sources"]))
        self.assertFalse(any(s["type"].startswith("weibo_") and s["enabled"] for s in current["sources"]))

    def test_paused_weibo_is_not_called_or_counted_as_missing_auth(self):
        current = json.loads((Path(__file__).parent / "sources.json").read_text())
        with patch.object(pipeline, "fetch_source", return_value=[]) as fetch:
            result = pipeline.run(current, now=NOW)
        called_sources = [call.args[0] for call in fetch.call_args_list]
        self.assertTrue(called_sources)
        self.assertFalse(any(s["type"].startswith("weibo_") for s in called_sources))
        self.assertEqual(result["errors"], [])
        self.assertFalse(any(s["source_id"].startswith("weibo-") for s in result["source_statuses"]))

    def test_complete_render_keeps_edition_date_and_auth_status(self):
        cfg = config(source("weibo-film-search", type="weibo_search"), source())
        cfg["policy"]["labels"] = {"single_source": "媒体报道"}
        with patch.object(pipeline, "fetch_source", side_effect=[SourceUnavailable("authorization_required", "微博待授权"), [item()]]):
            payload = pipeline.run(cfg, now=NOW)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            site = path / "site"
            site.mkdir()
            app = Path(__file__).resolve().parents[1] / "app"
            shutil.copy2(app / "index.html", site / "index.html")
            shutil.copy2(app / "archive.html", site / "archive.html")
            data_path = path / "latest.json"
            data_path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(sys, "argv", ["render_daily", "--input", str(data_path), "--site", str(site)]):
                self.assertEqual(render_daily.main(), 0)
            page = (site / "index.html").read_text()
            self.assertIn("每日影视简报 · 2026-09-23", page)
            self.assertIn("微博待授权", page)
            self.assertIn("电影测试 a", page)
            self.assertIn("本轮接通 1/2", page)
            self.assertIn("采集来源尚待补充", page)


if __name__ == "__main__":
    unittest.main()
