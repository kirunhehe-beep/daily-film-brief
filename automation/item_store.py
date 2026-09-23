"""Persistent, source-level news records; collection time is not publication time.

The exact (source_id, url) pair identifies a record, including URL query strings.
Records absent from a later fetch remain in the store. This module neither
selects a daily edition nor assesses credibility or discards undated stories.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


SCHEMA_VERSION = 1

# Only normalized news data is persisted, never connector configuration, request
# headers, cookies, or arbitrary metadata returned by a collector.
TEXT_FIELDS = frozenset({
    "id", "source_id", "source_name", "source_class", "source_type",
    "publisher_id", "title", "summary", "url", "published", "published_at",
    "market", "entry_type", "language", "distribution", "confidence",
    "image_url", "video_url", "video_page_url", "author", "author_id", "author_url", "platform",
    "post_id", "original_post_url",
    "date_status", "timestamp_status", "original_title", "original_summary",
    "translated_title", "translated_summary",
})
INTEGER_FIELDS = frozenset({"content_priority", "carry_forward_days"})
BOOLEAN_FIELDS = frozenset({"is_carried_forward", "is_repost", "text_complete"})
SOURCE_FIELDS = frozenset({
    "id", "source_id", "publisher_id", "name", "url", "class", "language",
})
FILING_FIELDS = frozenset({
    "filing_no", "title", "company", "writer", "result", "region", "category",
})


class StoreFormatError(ValueError):
    """A store cannot be interpreted safely; leave the existing file untouched."""


def _timestamp(value: object, field: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise StoreFormatError(f"{field} must be a timezone-aware ISO timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StoreFormatError(f"Invalid timestamp for {field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StoreFormatError(f"{field} must include a timezone")
    return parsed


def _identity(record: dict) -> tuple[str, str]:
    source_id, url = record.get("source_id"), record.get("url")
    if not isinstance(source_id, str) or not source_id.strip():
        raise StoreFormatError("Every record requires a nonempty source_id")
    if not isinstance(url, str) or not url.strip():
        raise StoreFormatError("Every record requires an original url")
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("not an HTTP article URL")
        if parts.username is not None or parts.password is not None:
            raise ValueError("embedded credentials are not article data")
    except ValueError as exc:
        raise StoreFormatError("Invalid article url") from exc
    return source_id, url


def _text_map(value: object, allowed: frozenset[str], field: str) -> dict:
    if not isinstance(value, dict):
        raise StoreFormatError(f"{field} must be an object")
    result = {}
    for key in sorted(allowed):
        if key in value:
            if value[key] is not None and not isinstance(value[key], str):
                raise StoreFormatError(f"{field}.{key} must be text or null")
            result[key] = value[key]
    return result


def _news_fields(value: object) -> dict:
    result = _text_map(value, TEXT_FIELDS, "record")
    _identity(result)
    for key in sorted(INTEGER_FIELDS | BOOLEAN_FIELDS):
        if key not in value:
            continue
        expected = bool if key in BOOLEAN_FIELDS else int
        if value[key] is not None and type(value[key]) is not expected:
            raise StoreFormatError(f"record.{key} has an invalid type")
        result[key] = value[key]
    for key, allowed in (("sources", SOURCE_FIELDS), ("filings", FILING_FIELDS)):
        if key not in value:
            continue
        nested = value[key]
        if nested is None:
            result[key] = None
        elif isinstance(nested, list):
            result[key] = [_text_map(item, allowed, key) for item in nested]
        else:
            raise StoreFormatError(f"record.{key} must be a list or null")
    return result


def _validate_store(payload: object) -> dict:
    if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int:
        raise StoreFormatError("Store must contain an integer schema_version")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise StoreFormatError("Unsupported item store schema_version")
    if not isinstance(payload.get("records"), list):
        raise StoreFormatError("Store records must be a list")
    clean = {"schema_version": SCHEMA_VERSION, "records": []}
    if "updated_at" in payload:
        _timestamp(payload["updated_at"], "updated_at")
        clean["updated_at"] = payload["updated_at"]
    seen = set()
    for raw in payload["records"]:
        record = _news_fields(raw)
        identity = _identity(record)
        if identity in seen:
            raise StoreFormatError("Store contains duplicate source/url identities")
        seen.add(identity)
        first = _timestamp(raw.get("first_seen_at"), "first_seen_at")
        last = _timestamp(raw.get("last_seen_at"), "last_seen_at")
        if last < first:
            raise StoreFormatError("last_seen_at precedes first_seen_at")
        record["first_seen_at"] = raw["first_seen_at"]
        record["last_seen_at"] = raw["last_seen_at"]
        clean["records"].append(record)
    return clean


def _has_value(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None and value != [] and value != {}


def merge_records(previous: dict, incoming: list[dict], now: str) -> dict:
    """Merge a fetch without losing older records, dates, or available media.

    An undated incoming record stays undated; first_seen_at is only collection
    evidence. Empty fields do not erase known values. Inputs are not mutated.
    """
    current_time = _timestamp(now, "now")
    store = _validate_store(previous)
    if not isinstance(incoming, list):
        raise StoreFormatError("incoming must be a list")
    records = {_identity(record): record for record in store["records"]}
    for raw in incoming:
        clean = _news_fields(raw)
        key = _identity(clean)
        if key not in records:
            records[key] = clean
            records[key]["first_seen_at"] = now
            records[key]["last_seen_at"] = now
            continue
        record = records[key]
        for field, value in clean.items():
            if _has_value(value):
                record[field] = value
        # Older replay jobs must not move the most recent observation backwards.
        if current_time >= _timestamp(record["last_seen_at"], "last_seen_at"):
            record["last_seen_at"] = now
    store["records"] = list(records.values())
    if "updated_at" not in store or current_time >= _timestamp(store["updated_at"], "updated_at"):
        store["updated_at"] = now
    return store


def load_store(path: Path) -> dict:
    """Return an empty store only for a missing file, never for a damaged file."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"schema_version": SCHEMA_VERSION, "records": []}
    try:
        payload = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise StoreFormatError(f"Malformed item store: {path}") from exc
    return _validate_store(payload)


def save_store(path: Path, payload: dict) -> None:
    """Atomically replace a valid store; a damaged old store is never overwritten."""
    path = Path(path)
    clean = _validate_store(payload)
    if path.exists():
        load_store(path)
    serialized = json.dumps(clean, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
