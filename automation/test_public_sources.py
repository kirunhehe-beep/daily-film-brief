"""Small synthetic fixtures matching public publisher structures; no network."""
import json
import datetime as dt
import unittest
import ssl
import urllib.error
from unittest.mock import MagicMock, patch

import brief_pipeline as pipeline
import public_sources as public


class PublicSourceTests(unittest.TestCase):
    def test_official_account_public_posts_keep_date_permalink_and_social_status(self):
        source = {"type": "sina_official_posts", "url": "https://www.sina.cn/media/2095820504",
                  "expected_author": "淘票票", "source_class": "social"}
        listing = '''<div class="feed">
        <a class="post-link" href="/news/detail/5348886970633241.html"><article class="post">
          <div class="uname">淘票票</div><div class="time">2026-09-30 16:38<span class="src">来自微博网页版</span></div>
          <div class="post-text">电影《新片》今日发布预告。</div>
        </article></a>
        <a class="post-link" href="/news/detail/2.html"><article class="post">
          <div class="uname">其他账号</div><div class="time">2026-09-30 16:38</div>
          <div class="post-text">电影消息</div>
        </article></a></div>'''
        rows = public.parse_listing(source, listing.encode())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://www.sina.cn/news/detail/5348886970633241.html")
        self.assertEqual(rows[0]["published"], "2026-09-30T16:38:00+08:00")
        self.assertFalse(rows[0]["text_complete"])
        self.assertEqual(pipeline.confidence_for(source), "social_lead")

    def test_official_post_headline_uses_hashtag_not_full_caption(self):
        source = {"type": "sina_official_posts", "url": "https://www.sina.cn/media/2095820504",
                  "expected_author": "淘票票", "source_class": "social"}
        listing = '''<a class="post-link" href="/news/detail/123.html"><article class="post">
          <div class="uname">淘票票</div><div class="time">2026-09-30 16:38</div>
          <div class="post-text">#电影新片今日发布预告# 片方公开了首支预告片。</div>
        </article></a>'''
        row = public.parse_listing(source, listing.encode())[0]
        self.assertEqual(row["title"], "电影新片今日发布预告")
        self.assertIn("片方公开", row["summary"])

    def test_identical_ticketing_captions_keep_both_permalinks(self):
        sources = [{"id": source_id, "enabled": True, "name": name,
                    "type": "sina_official_posts", "source_class": "social",
                    "market": "m-cn", "language": "zh"}
                   for source_id, name in (("taopiaopiao-official-posts", "淘票票"),
                                           ("dengta-official-posts", "灯塔专业版"))]
        records = [{"source_id": source["id"], "title": "新片预告发布",
                    "summary": "#新片预告发布# 片方公开了首支预告片。",
                    "url": f"https://www.sina.cn/news/detail/{number}.html",
                    "published": "2026-09-30T17:00:00+08:00"}
                   for number, source in enumerate(sources, start=1)]
        reports = [{"market": "m-cn", "status": "ok", "fetched": 1} for _ in sources]
        config = {"policy": {"max_age_hours": 48, "required_markets": ["m-cn"],
                             "render_languages": ["zh"], "allow_carry_forward": False},
                  "sources": sources}
        with patch.object(pipeline, "collect_records", return_value=(records, set(s["id"] for s in sources), [], reports)):
            result = pipeline.run(config, now=dt.datetime(2026, 9, 30, 10, tzinfo=dt.timezone.utc))
        self.assertEqual(result["stats"]["items"], 1)
        self.assertEqual(result["items"][0]["confidence"], "social_lead")
        self.assertEqual(len(result["items"][0]["sources"]), 2)

    def test_maoyan_news_uses_original_online_time_and_detail_excerpt(self):
        source = {"type": "maoyan_news", "url": "https://i.maoyan.com/asgard/news", "read_details": True}
        stamp = 1790753746000
        listing = ('<script>var AppData = ' + json.dumps({"newsList": [{
            "contentId": 20243040, "title": "电影发布预告", "onlineTime": stamp,
            "images": [{"url": "https://p0.pipi.cn/poster.jpg"}]
        }]}, ensure_ascii=False) + ';</script>').encode()
        detail = ('<script>var AppData = ' + json.dumps({"news": {
            "id": 20243040, "created": stamp, "text": json.dumps("<p>片方今日发布新预告。</p>")
        }}, ensure_ascii=False) + ';</script>').encode()
        def fetch(url, accept):
            return listing if url == source["url"] else detail
        items = public.fetch_public_source(source, fetch)
        self.assertEqual(items.status, "ok")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["published"], "2026-09-30T07:35:46+00:00")
        self.assertEqual(items[0]["summary"], "片方今日发布新预告。")
        self.assertEqual(items[0]["image_url"], "https://p0.pipi.cn/poster.jpg")
        self.assertEqual(items[0]["url"], "https://i.maoyan.com/asgard/information/20243040?_v_=yes")

    def test_maoyan_detail_failure_preserves_dated_listing(self):
        source = {"type": "maoyan_news", "url": "https://i.maoyan.com/asgard/news", "read_details": True}
        listing = ('<script>var AppData = ' + json.dumps({"newsList": [{
            "contentId": 1, "title": "电影消息", "onlineTime": 1790753746000
        }]}, ensure_ascii=False) + ';</script>').encode()
        def fetch(url, accept):
            if url == source["url"]:
                return listing
            raise TimeoutError()
        items = public.fetch_public_source(source, fetch)
        self.assertEqual(items.status, "partial")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["published"])
        self.assertEqual(items.checks[0]["status"], "detail_unavailable")

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
