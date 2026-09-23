"""Small synthetic fixtures matching public publisher structures; no network."""
import json
import unittest
import ssl
import urllib.error
from unittest.mock import MagicMock, patch

import brief_pipeline as pipeline
import public_sources as public


class PublicSourceTests(unittest.TestCase):
    def test_mtime_relative_clock_requires_original_date(self):
        source = {"type": "mtime_news", "url": "https://news.mtime.com/"}
        body = '<li><div><h4><a href="https://content.mtime.com/article/1">电影预告</a></h4><img src="https://img.mtime.cn/poster.jpg"><p>片方发布预告。</p><span class="news-time">2小时前</span></div></li>'
        row = public.parse_listing(source, body.encode())[0]
        self.assertEqual(row["published"], "")
        state = {"content": {"userCreateTime": {"show": "2026-09-23 10:10:00"},
                             "body": '<p>新闻内容</p><video src="https://vfx.mtime.cn/trailer.mp4"></video>'}}
        detail = ('<script>window.__INITIAL_STATE__=' + json.dumps(state) + ';</script>').encode()
        item = public.enrich_detail(source, row, detail)
        self.assertEqual(item["published"], "2026-09-23T10:10:00+08:00")
        self.assertEqual(item["video_url"], "https://vfx.mtime.cn/trailer.mp4")
        self.assertEqual(item["summary"], "片方发布预告。")

    def test_bjnews_keeps_article_excerpt_not_site_description(self):
        source = {"type": "bjnews_ent", "url": "https://www.bjnews.com.cn/entertainment"}
        listing = '<div class="pin_demo"><a href="https://www.bjnews.com.cn/detail/123.html"><div>新剧开机</div></a><img src="https://media.bjnews.com.cn/poster.jpg"></div>'
        row = public.parse_listing(source, listing.encode())[0]
        detail = '<meta name="description" content="网站简介"><span class="timer">2026-09-23 11:30</span><div class="article-text"><p>新京报讯，这是一部新剧开机的新闻摘要，演员们在发布会上介绍了角色和创作情况。</p><video src="https://media.bjnews.com.cn/clip.m3u8"></video></div>'
        item = public.enrich_detail(source, row, detail.encode())
        self.assertIn("新京报讯", item["summary"])
        self.assertNotIn("网站简介", item["summary"])
        self.assertTrue(item["video_url"].endswith("m3u8"))
        self.assertTrue(item["published"].endswith("+08:00"))

    def test_people_duplicate_teaser_does_not_replace_headline(self):
        source = {"type": "people_culture", "url": "http://ent.people.com.cn/"}
        path = '/n1/2026/0923/c1012-123.html'
        listing = f'<a href="{path}">电影市场观察</a><a href="{path}">电影市场观察的很长很长的一段摘要内容</a>'
        rows = public.parse_listing(source, listing.encode())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "电影市场观察")
        detail = '<h1>电影市场观察</h1><b id="newstime">2026年09月23日08:08</b><meta name="description" content="原文摘要">'
        self.assertEqual(public.enrich_detail(source, rows[0], detail.encode())["published"], "2026-09-23T08:08:00+08:00")

    def test_failed_detail_is_preserved_undated_and_visible(self):
        source = {"type": "bjnews_ent", "url": "https://www.bjnews.com.cn/entertainment"}
        body = b'<div class="pin_demo"><a href="https://www.bjnews.com.cn/detail/123.html">Film</a></div>'
        def fetch(url, accept):
            if url == source["url"]:
                return body
            raise TimeoutError()
        result = public.fetch_public_source(source, fetch)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result[0]["published"], "")
        self.assertEqual(result.checks[0]["status"], "detail_unavailable")

    def test_unknown_listing_is_not_silent_empty_success(self):
        with self.assertRaises(ValueError):
            public.parse_listing({"type": "mtime_news", "url": "https://news.mtime.com/"}, b'<html>Access denied</html>')

    def test_traditional_chinese_matching_preserves_original_copy(self):
        title = "電影開機與劇集消息"
        self.assertTrue(pipeline.matches_source_filter({"include_keywords": ["电影"]}, title))
        self.assertEqual(title, "電影開機與劇集消息")
        self.assertEqual(pipeline.classify_distribution({}, "新劇集正式開機", ""), "series")

    def test_short_drama_comparison_does_not_delete_long_series(self):
        policy = {"excluded_keywords": ["短剧", "微短剧"], "excluded_entry_types": ["短剧动态"]}
        self.assertFalse(pipeline.excluded_item({}, "长剧的出路不在短剧化", "", policy))
        self.assertFalse(pipeline.excluded_item({}, "电影市场年度报告", "也讨论了短剧", policy))
        self.assertTrue(pipeline.excluded_item({}, "短劇新作開機", "", policy))
        self.assertTrue(pipeline.excluded_item({"entry_type": "短剧动态"}, "新作开机", "", policy))

    def test_source_country_is_not_always_story_country(self):
        source = {"market": "m-hmt", "infer_market": True}
        self.assertEqual(pipeline.item_market(source, {"title": "電影博物館開幕", "summary": "（中央社洛杉磯22日綜合外電報導）"}), "m-intl")
        self.assertEqual(pipeline.item_market(source, {"title": "淺談中國大陸的十一檔期", "summary": ""}), "m-cn")

    def test_industry_blog_has_observation_label(self):
        self.assertEqual(pipeline.confidence_for({"source_class": "industry_blog"}), "industry_commentary")

    def test_rss_encoded_media_is_kept_without_copying_full_article(self):
        feed = b'''<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>
        <title>Film</title><link>https://example.com/film</link><description>Short excerpt</description>
        <content:encoded><![CDATA[<p>Full article</p><img src="https://example.com/poster.jpg">]]></content:encoded>
        </item></channel></rss>'''
        row = pipeline.parse_feed(feed)[0]
        self.assertEqual(row["summary"], "Short excerpt")
        self.assertEqual(row["image_url"], "https://example.com/poster.jpg")

    def test_site_share_logo_is_not_article_artwork(self):
        self.assertFalse(pipeline.usable_news_image("https://imgcdn.cna.com.tw/www/images/pic_fb.jpg"))
        self.assertTrue(pipeline.usable_news_image("https://imgcdn.cna.com.tw/www/webphotos/WebOg/600/story.jpg"))

    def test_only_article_body_images_are_used_as_fallback(self):
        document = '<img src="https://example.com/sidebar.jpg"><div class="entry-content"><img src="//example.com/poster.jpg"></div>'
        self.assertEqual(pipeline.parse_page_media(document, "https://example.com/story"), ("https://example.com/poster.jpg", ""))
        self.assertEqual(pipeline.parse_page_media('<img src="https://example.com/unrelated.jpg">', "https://example.com/story"), ("", ""))

    def test_body_image_wins_over_site_og_header(self):
        document = '<meta property="og:image" content="https://example.com/header.jpg"><div class="article-text"><img src="/poster.jpg"></div>'
        self.assertEqual(pipeline.parse_page_media(document, "https://example.com/story")[0], "https://example.com/poster.jpg")

    def test_transient_connection_failure_retries_once(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"feed"
        with patch.object(pipeline.urllib.request, "urlopen", side_effect=[urllib.error.URLError("temporary"), response]) as request, patch.object(pipeline.time, "sleep"):
            self.assertEqual(pipeline.fetch_bytes("https://example.com/feed", "application/xml"), b"feed")
        self.assertEqual(request.call_count, 2)

    def test_certificate_failure_is_not_bypassed_or_retried(self):
        error = urllib.error.URLError(ssl.SSLCertVerificationError("untrusted certificate"))
        with patch.object(pipeline.urllib.request, "urlopen", side_effect=error) as request:
            with self.assertRaises(urllib.error.URLError):
                pipeline.fetch_bytes("https://example.com/feed", "application/xml")
        self.assertEqual(request.call_count, 1)

    def test_forbidden_source_is_not_retried(self):
        error = urllib.error.HTTPError("https://example.com/", 403, "Forbidden", {}, None)
        with patch.object(pipeline.urllib.request, "urlopen", side_effect=error) as request:
            with self.assertRaises(urllib.error.HTTPError):
                pipeline.fetch_bytes("https://example.com/", "text/html")
        self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
