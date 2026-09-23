import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import item_store


FIRST = "2026-09-22T02:10:00Z"
SECOND = "2026-09-22T10:00:00Z"
THIRD = "2026-09-23T02:10:00Z"


def empty_store():
    return {"schema_version": 1, "records": []}


def article(source="film-news", url="https://example.test/article?id=1", **fields):
    return {"source_id": source, "url": url, "title": "电影新消息", **fields}


class MergeRecordsTests(unittest.TestCase):
    def test_multiple_runs_accumulate_even_when_old_items_leave_feed(self):
        morning = item_store.merge_records(empty_store(), [article()], FIRST)
        evening = item_store.merge_records(morning, [article(url="https://example.test/article?id=2")], SECOND)
        next_day = item_store.merge_records(evening, [], THIRD)
        self.assertEqual(len(next_day["records"]), 2)
        self.assertEqual(next_day["records"][0]["first_seen_at"], FIRST)
        self.assertEqual(next_day["records"][0]["last_seen_at"], FIRST)
        self.assertEqual(next_day["records"][1]["first_seen_at"], SECOND)

    def test_identity_keeps_query_strings_and_distinct_publishers(self):
        incoming = [article(), article(url="https://example.test/article?id=2"), article(source="other")]
        merged = item_store.merge_records(empty_store(), incoming, FIRST)
        self.assertEqual(len(merged["records"]), 3)

    def test_updates_nonempty_fields_without_erasing_media_or_publication_time(self):
        old = item_store.merge_records(empty_store(), [article(
            published_at="2026-09-22T01:00:00Z", image_url="https://example.test/poster.jpg",
            video_url="https://example.test/trailer.mp4", summary="早间摘要", content_priority=3,
        )], FIRST)
        incoming = [article(summary="晚间补充", published_at=None, image_url="", video_url="  ", content_priority=0)]
        result = item_store.merge_records(old, incoming, SECOND)["records"][0]
        self.assertEqual(result["summary"], "晚间补充")
        self.assertEqual(result["content_priority"], 0)
        self.assertEqual(result["image_url"], "https://example.test/poster.jpg")
        self.assertEqual(result["video_url"], "https://example.test/trailer.mp4")
        self.assertEqual(result["published_at"], "2026-09-22T01:00:00Z")
        self.assertEqual(result["first_seen_at"], FIRST)
        self.assertEqual(result["last_seen_at"], SECOND)

    def test_undated_record_does_not_borrow_collection_date(self):
        result = item_store.merge_records(empty_store(), [article(published="", published_at=None)], FIRST)
        row = result["records"][0]
        self.assertEqual(row["published"], "")
        self.assertIsNone(row["published_at"])
        self.assertEqual(row["first_seen_at"], FIRST)
        self.assertEqual(row["last_seen_at"], FIRST)

    def test_duplicate_in_one_fetch_merges_and_later_date_can_be_added(self):
        rows = [article(), article(summary="补充摘要"), article(published_at="2026-09-21T10:00:00Z")]
        merged = item_store.merge_records(empty_store(), rows, FIRST)
        self.assertEqual(len(merged["records"]), 1)
        self.assertEqual(merged["records"][0]["summary"], "补充摘要")
        self.assertEqual(merged["records"][0]["published_at"], "2026-09-21T10:00:00Z")

    def test_credentials_and_unknown_connector_fields_are_not_persisted(self):
        incoming = article(api_key="secret", cookies="secret", headers={"Authorization": "secret"},
                           author="片方账号", author_id="public-user-123", post_id="public-post-456",
                           is_repost=True, original_post_url="https://example.test/original",
                           sources=[{"id": "film-news", "name": "媒体", "token": "secret"}],
                           filings=[{"title": "电影", "password": "secret"}])
        store = item_store.merge_records(empty_store(), [incoming], FIRST)
        serialized = json.dumps(store)
        self.assertNotIn("secret", serialized)
        self.assertEqual(store["records"][0]["sources"][0]["name"], "媒体")
        self.assertEqual(store["records"][0]["author_id"], "public-user-123")
        self.assertEqual(store["records"][0]["post_id"], "public-post-456")
        self.assertTrue(store["records"][0]["is_repost"])

    def test_inputs_are_not_mutated_and_nested_output_is_independent(self):
        incoming = [article(sources=[{"id": "film-news", "name": "媒体"}])]
        previous = item_store.merge_records(empty_store(), incoming, FIRST)
        originals = copy.deepcopy((previous, incoming))
        result = item_store.merge_records(previous, incoming, SECOND)
        result["records"][0]["sources"][0]["name"] = "修改"
        self.assertEqual((previous, incoming), originals)

    def test_invalid_store_and_identity_raise_instead_of_resetting(self):
        for payload in ({}, {"schema_version": 2, "records": []}, {"schema_version": 1, "records": {}}):
            with self.subTest(payload=payload), self.assertRaises(item_store.StoreFormatError):
                item_store.merge_records(payload, [], FIRST)
        for row in ({"url": "https://example.test/"}, article(url="https://name:password@example.test/")):
            with self.subTest(row=row), self.assertRaises(item_store.StoreFormatError):
                item_store.merge_records(empty_store(), [row], FIRST)

    def test_old_replay_does_not_move_last_seen_backwards(self):
        previous = item_store.merge_records(empty_store(), [article()], SECOND)
        result = item_store.merge_records(previous, [article()], FIRST)
        self.assertEqual(result["records"][0]["last_seen_at"], SECOND)
        self.assertEqual(result["updated_at"], SECOND)


class StoreFilesTests(unittest.TestCase):
    def test_missing_file_and_save_load_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "records.json"
            self.assertEqual(item_store.load_store(path), empty_store())
            store = item_store.merge_records(empty_store(), [article()], FIRST)
            item_store.save_store(path, store)
            self.assertEqual(item_store.load_store(path), store)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_damaged_existing_file_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.json"
            for damaged in ("not json", '{"schema_version":1,"records":[{}]}'):
                path.write_text(damaged, encoding="utf-8")
                with self.assertRaises(item_store.StoreFormatError):
                    item_store.load_store(path)
                with self.assertRaises(item_store.StoreFormatError):
                    item_store.save_store(path, empty_store())
                self.assertEqual(path.read_text(encoding="utf-8"), damaged)

    def test_failed_atomic_replace_preserves_old_file_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.json"
            original = item_store.merge_records(empty_store(), [article()], FIRST)
            item_store.save_store(path, original)
            updated = item_store.merge_records(original, [article(summary="更新")], SECOND)
            with patch.object(item_store.os, "replace", side_effect=OSError("simulated disk failure")):
                with self.assertRaises(OSError):
                    item_store.save_store(path, updated)
            self.assertEqual(item_store.load_store(path), original)
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
