"""Offline protocol/normalization fixtures, not proof of live Weibo coverage."""
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

import weibo_source as wb


def post(ident=12345, **extra):
    return {"idstr": str(ident), "text": "电影《测试》发布海报", "created_at": "Tue Sep 22 10:10:00 +0800 2026",
            "user": {"idstr": "888", "screen_name": "测试片方", "verified": True}, **extra}


def metadata(*flags):
    # Official CLI wraps command metadata in `command`, not `result`.
    return {"command": {"flags": [{"name": flag} for flag in flags]}}


class WeiboTests(unittest.TestCase):
    def setUp(self):
        self.source = {"type": "weibo_search", "queries": ["电影"], "max_pages": 1}

    def test_missing_token_never_sends_request(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(wb, "request_json") as request:
            with self.assertRaises(wb.SourceUnavailable) as error:
                wb.fetch_weibo(self.source)
        self.assertEqual(error.exception.status, "authorization_required")
        request.assert_not_called()

    def test_auth_error_does_not_echo_secret(self):
        error = urllib.error.HTTPError("https://example.com?token=TOPSECRET", 401, "TOPSECRET", {}, None)
        with patch.object(wb.OPENER, "open", side_effect=error):
            with self.assertRaises(wb.SourceUnavailable) as context:
                wb.request_json("/cli/invoke", "TOPSECRET")
        self.assertEqual(context.exception.status, "authorization_required")
        self.assertNotIn("TOPSECRET", str(context.exception))

    def test_official_metadata_envelope(self):
        self.assertEqual(wb.flag_names(metadata("q", "page")), {"q", "page"})

    def test_unknown_metadata_fails_before_paid_invoke(self):
        with patch.dict(os.environ, {"WEIBO_CLI_TOKEN": "fixture"}), patch.object(wb, "request_json", return_value={}) as request:
            with self.assertRaises(wb.SourceUnavailable):
                wb.fetch_weibo(self.source)
        self.assertEqual(request.call_count, 1)

    def test_only_advertised_flags_are_sent(self):
        responses = [metadata("q"), {"result": {"statuses": [post()]}}]
        with patch.dict(os.environ, {"WEIBO_CLI_TOKEN": "fixture"}), patch.object(wb, "request_json", side_effect=responses) as request:
            result = wb.fetch_weibo(self.source)
        self.assertEqual(request.call_args.args[2], {"group": "search", "action": "statuses/limited", "args": {"q": "电影"}})
        self.assertEqual(len(result), 1)
        self.assertEqual(result.checks[0]["status"], "window_limited")

    def test_other_account_not_my_account_endpoint(self):
        source = {"type": "weibo_timeline", "uids": ["888"]}
        with patch.dict(os.environ, {"WEIBO_CLI_TOKEN": "fixture"}), patch.object(wb, "request_json", side_effect=[metadata("uid", "count"), {"result": {"statuses": []}}]) as request:
            result = wb.fetch_weibo(source)
        self.assertEqual(request.call_args.args[2]["action"], "user_timeline/other")
        self.assertEqual(request.call_args.args[2]["args"], {"uid": "888", "count": 20})
        self.assertEqual(result.status, "ok")

    def test_budget_allocated_to_each_target(self):
        source = {**self.source, "queries": ["电影", "电视剧"], "max_pages": 5, "max_requests_per_run": 2}
        with patch.dict(os.environ, {"WEIBO_CLI_TOKEN": "fixture"}), patch.object(wb, "request_json", side_effect=[metadata("q", "page"), {"result": {"statuses": [post(1)]}}, {"result": {"statuses": [post(2)]}}]) as request:
            result = wb.fetch_weibo(source)
        self.assertEqual([call.args[2]["args"]["q"] for call in request.call_args_list[1:]], ["电影", "电视剧"])
        self.assertEqual(len(result), 2)

    def test_global_dedup_does_not_end_another_targets_pages(self):
        source = {**self.source, "queries": ["电影", "电视剧"], "max_pages": 3, "max_requests_per_run": 6}
        responses = [metadata("q", "page")]
        responses += [{"result": {"statuses": [post(n)]}} for n in [1, 2, 3, 4, 2, 5]]
        with patch.dict(os.environ, {"WEIBO_CLI_TOKEN": "fixture"}), patch.object(wb, "request_json", side_effect=responses) as request:
            result = wb.fetch_weibo(source)
        self.assertEqual(request.call_count, 7)
        self.assertEqual({item["post_id"] for item in result}, {"1", "2", "3", "4", "5"})

    def test_rate_limit_keeps_prior_items_and_stops_more_targets(self):
        source = {**self.source, "queries": ["电影", "电视剧", "影视"], "max_requests_per_run": 3}
        responses = [metadata("q"), {"result": {"statuses": [post()]}}, wb.SourceUnavailable("rate_limited", "限流")]
        with patch.dict(os.environ, {"WEIBO_CLI_TOKEN": "fixture"}), patch.object(wb, "request_json", side_effect=responses) as request:
            result = wb.fetch_weibo(source)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.checks[-1]["status"], "not_attempted")
        self.assertEqual(request.call_count, 3)

    def test_unknown_result_is_not_empty_success(self):
        for response in [{"result": {"unexpected": []}}, {"result": {"error_code": 123}}, {"result": {"statuses": []}, "meta": {"outcome": "UNKNOWN"}}]:
            with self.subTest(response=response), self.assertRaises(wb.SourceUnavailable):
                wb.extract_posts(response)

    def test_repost_preserves_posting_author_and_original_link(self):
        item = wb.normalize_post(post(retweeted_status=post(1)))
        self.assertTrue(item["is_repost"])
        self.assertEqual(item["author"], "测试片方")
        self.assertEqual(item["original_post_url"], "https://m.weibo.cn/detail/1")
        self.assertNotIn("official", json.dumps(item))

    def test_media_and_long_text_compatible_fixture(self):
        item = wb.normalize_post(post(is_long_text=True, long_text={"longTextContent": "<b>电影新预告</b>"},
                                      pics=[{"large": {"url": "https://example.com/poster.jpg"}}],
                                      page_info={"type": "video", "page_url": "https://weibo.com/tv/show/abc",
                                                 "media_info": {"stream_url": "https://example.com/trailer.mp4"}}))
        self.assertEqual(item["summary"], "电影新预告")
        self.assertTrue(item["text_complete"])
        self.assertTrue(item["image_url"].endswith("poster.jpg"))
        self.assertTrue(item["video_url"].endswith("trailer.mp4"))

    def test_missing_date_is_not_replaced_by_collection_time(self):
        item = wb.normalize_post(post(created_at="", is_long_text=True))
        self.assertEqual(item["published"], "")
        self.assertFalse(item["text_complete"])

    def test_post_text_is_not_executable_html(self):
        item = wb.normalize_post(post(text='<a href="javascript:alert(1)">电影海报</a>', original_pic="javascript:alert(1)"))
        self.assertEqual(item["summary"], "电影海报")
        self.assertEqual(item["image_url"], "")


if __name__ == "__main__":
    unittest.main()
