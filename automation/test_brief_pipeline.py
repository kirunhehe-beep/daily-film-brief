import datetime as dt
import unittest
from unittest.mock import patch

import brief_pipeline
import render_daily


class DistributionTests(unittest.TestCase):
    def test_network_film_overrides_misfiled_series_feed(self):
        source = {"entry_type": "剧集动态"}
        self.assertEqual(
            brief_pipeline.classify_distribution(source, "网络故事片《生死尽头》定档", ""),
            "network",
        )

    def test_series_is_not_misclassified_by_release_words(self):
        source = {"entry_type": "剧集动态"}
        self.assertEqual(
            brief_pipeline.classify_distribution(source, "新剧定档并全网首播", ""),
            "series",
        )

    def test_explicit_cinema_and_unknown_film_are_distinct(self):
        source = {"entry_type": "电影动态"}
        self.assertEqual(brief_pipeline.classify_distribution(source, "影片全国公映", ""), "cinema")
        self.assertEqual(brief_pipeline.classify_distribution(source, "电影项目启动", ""), "film_unspecified")

    def test_cinema_survives_market_capacity_before_network_films(self):
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        feed_items = [
            {
                "title": f"网络电影项目{i}",
                "url": f"https://example.com/network-{i}",
                "summary": "网络电影上线",
                "published": now,
                "image_url": "",
                "video_url": "",
            }
            for i in range(3)
        ] + [{
            "title": "重点影片全国公映",
            "url": "https://example.com/cinema",
            "summary": "院线电影正式上映",
            "published": now,
            "image_url": "",
            "video_url": "",
        }]
        config = {
            "policy": {
                "max_age_hours": 72,
                "max_items_per_market": {"m-cn": 2},
                "render_languages": ["zh"],
                "required_markets": ["m-cn"],
                "excluded_entry_types": [],
                "excluded_keywords": [],
            },
            "sources": [{
                "id": "test",
                "name": "测试源",
                "publisher_id": "test",
                "type": "rss",
                "source_class": "established_media",
                "market": "m-cn",
                "entry_type": "电影动态",
                "content_priority": 0,
                "language": "zh",
                "enabled": True,
            }],
        }
        with patch.object(brief_pipeline, "fetch_source", return_value=feed_items):
            payload = brief_pipeline.run(config)
        self.assertEqual(len(payload["items"]), 2)
        self.assertEqual(payload["items"][0]["distribution"], "cinema")
        self.assertEqual(payload["stats"]["dropped_capacity"], 2)

    def test_periodic_filing_is_carried_forward_with_original_date(self):
        published = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).replace(microsecond=0).isoformat()
        filing_item = {
            "title": "最新一期电影备案",
            "url": "https://example.com/filing",
            "summary": "官方备案公示",
            "published": published,
            "image_url": "",
            "video_url": "",
            "distribution": "cinema_filing",
            "filings": [],
        }
        config = {
            "policy": {
                "max_age_hours": 120,
                "max_items_per_market": {"m-cn": 10},
                "render_languages": ["zh"],
                "required_markets": ["m-cn"],
                "excluded_entry_types": [],
                "excluded_keywords": [],
            },
            "sources": [{
                "id": "filing",
                "name": "国家电影局",
                "publisher_id": "chinafilm",
                "type": "chinafilm_filing",
                "source_class": "official",
                "market": "m-cn",
                "entry_type": "项目备案",
                "distribution": "cinema_filing",
                "content_priority": -1,
                "language": "zh",
                "max_age_hours": 1080,
                "carry_forward_days": 30,
                "enabled": True,
            }],
        }
        with patch.object(brief_pipeline, "fetch_source", return_value=[filing_item]):
            payload = brief_pipeline.run(config)
        self.assertEqual(len(payload["items"]), 1)
        self.assertTrue(payload["items"][0]["is_carried_forward"])
        self.assertEqual(payload["stats"]["carried_forward"], 1)


class FilingParserTests(unittest.TestCase):
    def test_official_filing_table_is_structured(self):
        document = """
        <meta name="ArticleTitle" content="国家电影局关于2026年8月上全国电影剧本（梗概）备案、立项公示的通知">
        <meta name="PubDate" content="2026-09-14">
        <script>str="故事影片,合拍影片";</script>
        <div class="hmc4Table"><table><tr><td>序号</td></tr><tr>
          <td>1</td><td>影剧备字〔2026〕第1号</td><td>测试影片</td>
          <td><script>var _badw = '测试影业有限公司';</script></td>
          <td><script>var _biju = '测试编剧';</script></td><td>同意拍摄</td><td>北京市</td>
        </tr></table></div>
        <div class="hmc4Table"><table><tr>
          <td>1</td><td>影合立字〔2026〕第2号</td><td>合拍测试片</td>
          <td>合拍公司</td><td>编剧乙</td><td>同意立项</td><td>直备</td>
        </tr></table></div>
        """
        item = brief_pipeline.parse_chinafilm_filing_detail(document.encode(), "https://example.com/notice")
        self.assertEqual(len(item["filings"]), 2)
        self.assertEqual(item["filings"][0]["company"], "测试影业有限公司")
        self.assertEqual(item["filings"][1]["category"], "合拍影片")
        self.assertEqual(item["distribution"], "cinema_filing")

    def test_filing_item_renders_channel_label_and_detail_table(self):
        item = {
            "title": "电影备案立项公示",
            "summary": "官方公示摘要",
            "published_at": "2026-09-14T00:00:00+08:00",
            "entry_type": "项目备案",
            "distribution": "cinema_filing",
            "is_carried_forward": True,
            "confidence": "official",
            "sources": [{"name": "国家电影局", "url": "https://example.com"}],
            "filings": [{
                "title": "测试影片",
                "category": "故事影片",
                "filing_no": "影剧备字〔2026〕第1号",
                "company": "测试影业有限公司",
                "writer": "测试编剧",
                "result": "同意拍摄",
            }],
        }
        output = render_daily.render_item(item, {"official": "官方确认"})
        self.assertIn("电影备案项目 · 项目备案 · 最新一期", output)
        self.assertIn('class="filing-table"', output)
        self.assertIn("备案立项不等于定档", output)


if __name__ == "__main__":
    unittest.main()
