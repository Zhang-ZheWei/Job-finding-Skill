#!/usr/bin/env python3
"""Small deterministic helpers used by the S1 collector."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit


JOB_PATH = re.compile(r"^/job_detail/([A-Za-z0-9_~-]+)\.html$")


class S1Error(Exception):
    """Expected collection or validation failure."""

    def __init__(self, message: str, code: str = "s1_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def normalized_text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise S1Error(f"{field} must be a string", "invalid_type")
    result = unicodedata.normalize("NFKC", value).strip()
    if not allow_empty and not result:
        raise S1Error(f"{field} must not be empty", "empty_value")
    return result


def _reject_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise S1Error(f"duplicate JSON key: {key}", "duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise S1Error(f"invalid JSON number: {value}", "invalid_json_number")


def strict_json_loads(text: str) -> Any:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_pairs,
            parse_constant=_reject_constant,
        )
    except S1Error:
        raise
    except json.JSONDecodeError as exc:
        raise S1Error(f"invalid JSON: {exc}", "invalid_json") from exc
    validate_json_value(value)
    return value


def validate_json_value(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise S1Error(f"non-finite number at {path}", "invalid_json_number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise S1Error(f"non-string key at {path}", "invalid_json_key")
            validate_json_value(item, f"{path}.{key}")
        return
    raise S1Error(f"unsupported JSON value at {path}", "invalid_json_type")


def atomic_write_json(path: str | os.PathLike[str], value: Any) -> None:
    validate_json_value(value)
    payload = (json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n").encode("utf-8")
    if strict_json_loads(payload.decode("utf-8")) != value:
        raise S1Error("JSON round-trip changed the candidate", "json_roundtrip_failed")

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.is_symlink():
        raise S1Error(f"refusing to replace symlink: {destination}", "unsafe_path")

    fd = -1
    temporary = ""
    try:
        fd, temporary = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(payload):
            offset += os.write(fd, payload[offset:])
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, destination)
        temporary = ""
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def load_json(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise S1Error(f"cannot read JSON {path}: {exc}", "json_read_failed") from exc
    result = strict_json_loads(text)
    if not isinstance(result, dict):
        raise S1Error("top-level JSON must be an object", "invalid_top_level")
    return result


def validate_search_url(url: Any) -> tuple[str, str]:
    text = normalized_text(url, "search_url")
    try:
        parsed = urlsplit(text)
        _ = parsed.port
    except ValueError as exc:
        raise S1Error(f"invalid search URL: {exc}", "invalid_search_url") from exc
    if parsed.scheme != "https" or parsed.netloc != "www.zhipin.com":
        raise S1Error("search URL must use https://www.zhipin.com", "invalid_search_url")
    if parsed.path != "/web/geek/jobs":
        raise S1Error("search URL must use /web/geek/jobs", "invalid_search_url")
    query = parse_qs(parsed.query, keep_blank_values=True)
    city_values = query.get("city", [])
    if len(city_values) != 1 or not city_values[0]:
        raise S1Error("search URL must contain one non-empty city", "invalid_search_url")
    terms = query.get("query", [])
    term = terms[0].strip() if len(terms) == 1 else ""
    if not term:
        raise S1Error("search URL must contain one non-empty query", "invalid_search_url")
    return text.split("#", 1)[0], normalized_text(term, "query")


def normalize_job_url(url: Any) -> tuple[str, str]:
    raw = normalized_text(url, "boss_job_url")
    parsed = urlsplit(urljoin("https://www.zhipin.com", raw))
    if parsed.scheme != "https" or parsed.netloc != "www.zhipin.com":
        raise S1Error("job URL must use https://www.zhipin.com", "invalid_job_url")
    match = JOB_PATH.fullmatch(parsed.path)
    if not match:
        raise S1Error(f"invalid BOSS job detail URL: {raw}", "invalid_job_url")
    job_id = match.group(1)
    return f"https://www.zhipin.com/job_detail/{job_id}.html", job_id


def decode_salary(raw: Any) -> dict[str, str]:
    source = normalized_text(raw, "salary", allow_empty=True)
    digits = {chr(0xE031 + index): str(index) for index in range(10)}
    display = "".join(digits.get(character, character) for character in source)
    has_private = any(0xE000 <= ord(character) <= 0xF8FF for character in display)
    valid = bool(re.fullmatch(r"\d{1,3}-\d{1,3}K(?:·\d{1,2}薪)?", display, re.I))
    if source and not has_private and valid:
        return {"raw": source, "display": display, "parse_status": "已解析"}
    return {"raw": source, "display": source, "parse_status": "薪资待核实"}


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def combination_key(url: str, term: str) -> str:
    return sha256_text(f"{url}\n{term}")
