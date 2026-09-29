import io
import json
import unittest
import urllib.error

import render_daily
import translate_brief as translation


def english_item():
    return {
        "id": "news-1", "title": "Tokyo Film Festival Announces Awards",
        "summary": "The festival announced two awards today.",
        "language": "en", "entry_type": "电影动态", "distribution": "film_unspecified",
        "published_at": "2026-09-29T02:00:00Z", "confidence": "single_source",
        "sources": [{"name": "Example Film", "url": "https://example.com/film"}],
    }


class FakeTranslator:
    available = True
    failure = ""

    def __init__(self):
        self.calls = 0

    def translate(self, title, summary):
        self.calls += 1
        return {"title": "东京电影节公布奖项", "summary": "电影节今天公布两项奖项。"}


class TranslationTests(unittest.TestCase):
    def test_english_is_translated_once_for_home_and_xhs(self):
        payload = {"items": [english_item()], "xhs_items": [english_item()], "stats": {}}
        client = FakeTranslator()
        cache = {}
        translation.translate_payload(payload, client, cache)
        self.assertEqual(client.calls, 1)
        self.assertEqual(payload["items"][0]["title"], "东京电影节公布奖项")
        self.assertEqual(payload["items"][0]["original_title"], "Tokyo Film Festival Announces Awards")
        self.assertEqual(payload["xhs_items"][0]["translation_status"], "machine_translated")
        self.assertEqual(payload["stats"]["translation"]["translated_home_items"], 1)
        self.assertEqual(len(cache), 1)

    def test_missing_credentials_keeps_english_item_visible(self):
        payload = {"items": [english_item()], "xhs_items": [], "stats": {}}
        translation.translate_payload(payload, translation.CloudflareTranslator("", ""), {})
        self.assertEqual(payload["items"][0]["title"], "Tokyo Film Festival Announces Awards")
        self.assertEqual(payload["items"][0]["translation_status"], "original_english")
        self.assertEqual(payload["stats"]["translation"]["original_english_home_items"], 1)

    def test_api_rejection_does_not_log_or_drop_article(self):
        calls = []

        def deny(request, timeout):
            calls.append(request)
            raise urllib.error.HTTPError(request.full_url, 403, "denied", {}, io.BytesIO(b"secret"))

        client = translation.CloudflareTranslator("account", "private-token", open_url=deny)
        payload = {"items": [english_item(), {**english_item(), "id": "news-2", "title": "Another Film"}], "stats": {}}
        translation.translate_payload(payload, client, {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(payload["stats"]["translation"]["provider_status"], "http_403")
        self.assertEqual(payload["stats"]["translation"]["original_english_home_items"], 2)
        self.assertNotIn("private-token", json.dumps(payload))

    def test_structured_cloudflare_response_is_accepted(self):
        calls = []

        def reply(request, timeout):
            calls.append(json.loads(request.data))
            return io.BytesIO(json.dumps({"success": True, "result": {"response": {
                "title": "东京电影节公布奖项", "summary": "电影节今天公布两项奖项。"
            }}}).encode())

        client = translation.CloudflareTranslator("account", "token", open_url=reply)
        result = client.translate("Tokyo Film Festival Announces Awards", "The festival announced two awards today.")
        self.assertEqual(result["title"], "东京电影节公布奖项")
        self.assertEqual(calls[0]["response_format"]["type"], "json_schema")

    def test_untranslated_or_translated_label_is_explicit(self):
        item = english_item()
        item["translation_status"] = "original_english"
        self.assertIn("英文原文", render_daily.render_item(item, {}))
        item.update(title="东京电影节公布奖项", summary="电影节今天公布两项奖项。",
                    original_title="Tokyo Film Festival Announces Awards",
                    translation_status="machine_translated")
        rendered = render_daily.render_item(item, {})
        self.assertIn("机器翻译", rendered)
        self.assertIn("Tokyo Film Festival Announces Awards", rendered)

    def test_invalid_translation_is_not_accepted(self):
        self.assertFalse(translation.valid_translation("English film", "A film", {"title": "English film", "summary": "A film"}))
        self.assertFalse(translation.valid_translation("English film", "A film", {"title": "中文标题", "summary": ""}))


if __name__ == "__main__":
    unittest.main()
