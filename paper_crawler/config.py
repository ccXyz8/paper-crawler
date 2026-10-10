from __future__ import annotations

import json
import os
import re
import math
import warnings
from urllib.parse import urlparse
import soupsieve
from pathlib import Path
from typing import Any

from .models import (
    AppConfig,
    ConferenceConfig,
    LoginConfig,
    RateLimitConfig,
    ZoteroConfig,
)

ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return ENV_RE.sub(lambda match: os.getenv(match.group(1), ""), value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _rate_limit(raw: dict[str, Any] | None) -> RateLimitConfig:
    raw = raw or {}
    return RateLimitConfig(
        requests_per_minute=int(raw.get("requests_per_minute", 30)),
        min_delay_seconds=float(raw.get("min_delay_seconds", 1.0)),
        max_retries=int(raw.get("max_retries", 3)),
        backoff_seconds=float(raw.get("backoff_seconds", 2.0)),
        timeout_seconds=int(raw.get("timeout_seconds", 30)),
    )


def load_config(path: str | Path) -> AppConfig:
    raw = _expand_env(json.loads(Path(path).read_text(encoding="utf-8")))
    _validate(raw)
    base = Path(path).resolve().parent
    def local_path(value):
        return str((base / value).resolve())
    zotero_raw = raw.get("zotero", {})
    zotero = ZoteroConfig(
        enabled=bool(zotero_raw.get("enabled", True)),
        base_url=str(zotero_raw.get("base_url", "http://127.0.0.1:23119")).rstrip("/"),
        api_key=str(zotero_raw.get("api_key", "")),
        user_or_group=str(zotero_raw.get("user_or_group", "users/0")).strip("/"),
        root_collection=str(zotero_raw.get("root_collection", "TFD")),
        create_subcollections=bool(zotero_raw.get("create_subcollections", True)),
        fallback_to_root=bool(zotero_raw.get("fallback_to_root", True)),
    )
    conferences = []
    for item in raw.get("conferences", []):
        login_raw = item.get("login", {})
        conferences.append(
            ConferenceConfig(
                name=str(item["name"]),
                website=str(item.get("website", "")),
                adapter=str(item["adapter"]),
                years=[int(year) for year in item.get("years", [])],
                login=LoginConfig(
                    username=str(login_raw.get("username", "")),
                    password=str(login_raw.get("password", "")),
                    login_url=str(login_raw.get("login_url", "")),
                    username_field=str(login_raw.get("username_field", "username")),
                    password_field=str(login_raw.get("password_field", "password")),
                ),
                rate_limit=_rate_limit(item.get("rate_limit")),
                keywords=item.get("keywords"),
                enabled=bool(item.get("enabled", True)),
                options=item.get("options", {}),
                fallbacks=item.get("fallbacks", []),
                meeting_dates=item.get("meeting_dates", {}),
            )
        )
    return AppConfig(
        keywords=raw.get("keywords", []),
        keyword_mode=str(raw.get("keyword_mode", "any")).lower(),
        download_dir=local_path(raw.get("download_dir", "downloads")),
        state_file=local_path(raw.get("state_file", "state.json")),
        user_agent=str(raw.get("user_agent", "paper-crawler/0.1")),
        dry_run=bool(raw.get("dry_run", False)),
        zotero=zotero,
        conferences=conferences,
        download_only=raw.get("download_only", raw.get("dry_run", False)),
        exclude_keywords=raw.get("exclude_keywords", []),
        keyword_word_boundary=raw.get("keyword_word_boundary", True),
        cache_ttl_hours=float(raw.get("cache_ttl_hours", 24)),
        offline=raw.get("offline", False),
        report_file=local_path(raw.get("report_file", "run_report.json")),
        max_matches=raw.get("max_matches"),
        max_papers=raw.get("max_papers"),
        max_seconds=raw.get("max_seconds"),
        scheduling=raw.get("scheduling", "round_robin"),
        source_batch_size=raw.get("source_batch_size", 5),
        newest_first=raw.get("newest_first", True),
        metadata_only=raw.get("metadata_only", False),
    )


def _validate(raw):
    from .adapters.builtin import ADAPTERS
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be an object")
    validate_runtime_options(raw)
    if raw.get("keyword_mode", "any") not in {"any", "all"}:
        raise ValueError("keyword_mode must be any or all")

    def words(value, groups=True):
        if not isinstance(value, list):
            raise ValueError("Keywords must be a list")
        for entry in value:
            terms = entry if groups and isinstance(entry, list) else [entry]
            if not terms or any(not isinstance(t, str) or not t.strip() for t in terms):
                raise ValueError("Keywords and synonym groups must contain nonempty strings")

    def booleans(obj, names):
        for name in names:
            if name in obj and not isinstance(obj[name], bool):
                raise ValueError(f"{name} must be a JSON boolean")

    words(raw.get("keywords", []))
    words(raw.get("exclude_keywords", []), False)
    booleans(raw, ["dry_run", "download_only", "offline", "keyword_word_boundary"])
    if "dry_run" in raw and "download_only" in raw and raw["dry_run"] != raw["download_only"]:
        raise ValueError("dry_run and download_only conflict; remove dry_run")
    for key in ("download_dir", "state_file", "report_file"):
        if key in raw and (not isinstance(raw[key], str) or not raw[key].strip()):
            raise ValueError(f"{key} must be a nonempty path")
    if "dry_run" in raw:
        warnings.warn("dry_run is deprecated; use download_only (downloads are still performed)", FutureWarning)
    ttl = raw.get("cache_ttl_hours", 24)
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl < 0:
        raise ValueError("cache_ttl_hours must be finite and nonnegative")
    zotero = raw.get("zotero", {})
    if not isinstance(zotero, dict):
        raise ValueError("zotero must be an object")
    booleans(zotero, ["enabled", "create_subcollections", "fallback_to_root"])
    if urlparse(zotero.get("base_url", "http://127.0.0.1:23119")).scheme not in {"http", "https"}:
        raise ValueError("Invalid Zotero base_url")
    conferences = raw.get("conferences", [])
    if not isinstance(conferences, list) or not conferences:
        raise ValueError("At least one conference/source is required")
    for source in conferences:
        if not isinstance(source, dict):
            raise ValueError("Each source must be an object")
        name = source.get("name", "")
        if not isinstance(name, str) or not name.strip() or name in {".", ".."} or any(c in name for c in '/\\:<>|?*"') or name.endswith((' ', '.')):
            raise ValueError("Source name must be a safe directory name")
        if source.get("adapter") not in ADAPTERS:
            raise ValueError(f"Unknown adapter: {source.get('adapter')}")
        years = source.get("years", [])
        if not isinstance(years, list) or not years or any(type(y) is not int or not 1900 <= y <= 2200 for y in years):
            raise ValueError(f"{name}: years must be a nonempty list of integer years")
        booleans(source, ["enabled"])
        from .schedule import validate_meeting_dates
        validate_meeting_dates(source.get("meeting_dates", {}))
        if source.get("keywords") is not None:
            words(source["keywords"])
        rate = source.get("rate_limit", {})
        if not isinstance(rate, dict):
            raise ValueError("rate_limit must be an object")
        for key, value in rate.items():
            if key not in {"requests_per_minute", "min_delay_seconds", "max_retries", "backoff_seconds", "timeout_seconds"}:
                raise ValueError(f"Unknown rate_limit field: {key}")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Invalid rate_limit.{key}")
            if key in {"requests_per_minute", "timeout_seconds"} and value <= 0:
                raise ValueError(f"{key} must be positive")
            if key in {"requests_per_minute", "max_retries", "timeout_seconds"} and type(value) is not int:
                raise ValueError(f"{key} must be an integer")
        fallbacks = source.get("fallbacks", [])
        if not isinstance(fallbacks, list):
            raise ValueError("fallbacks must be a list")
        for fallback in fallbacks:
            if not isinstance(fallback, dict) or fallback.get("adapter") not in ADAPTERS:
                raise ValueError("Each fallback needs a known adapter")
            if set(fallback) - {"adapter", "website", "options"}:
                raise ValueError("Fallback only accepts adapter, website and options")
            _validate({"conferences": [{**source, **fallback, "fallbacks": []}]})
        options = source.get("options", {})
        if not isinstance(options, dict):
            raise ValueError("options must be an object")
        booleans(options, ["accepted_only", "detail_pages", "prioritize_title_matches"])
        replacements = options.get("detail_url_map", {})
        if not isinstance(replacements, dict) or any(
                not isinstance(u, str) or urlparse(u).scheme not in {"http", "https"} or not urlparse(u).netloc
                for pair in replacements.items() for u in pair):
            raise ValueError("detail_url_map must map absolute HTTP(S) URLs to replacements")
        if options.get("item_type", "conferencePaper") not in {"conferencePaper", "journalArticle"}:
            raise ValueError("item_type must be conferencePaper or journalArticle")
        if source["adapter"] == "generic_html":
            selectors = options.get("selectors", {})
            if not isinstance(selectors, dict):
                raise ValueError("selectors must be an object")
            required = ["item", "title"] + ([] if options.get("detail_pages") else ["pdf"])
            if not options.get("year_url_template") or any(not selectors.get(k) for k in required):
                raise ValueError("generic_html needs year_url_template and item/title/pdf selectors (or detail_pages)")
            for selector in selectors.values():
                if not isinstance(selector, str) or not selector.strip():
                    raise ValueError("CSS selectors must be nonempty strings")
                soupsieve.compile(selector)


def validate_max_matches(value):
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("max_matches must be a nonnegative integer or null (0/null = unlimited)")


def validate_runtime_options(raw):
    validate_max_matches(raw.get("max_matches"))
    value = raw.get("max_papers")
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("max_papers must be a nonnegative integer or null")
    value = raw.get("max_seconds")
    if value is not None and (type(value) not in {int, float} or not math.isfinite(value) or value < 0):
        raise ValueError("max_seconds must be finite and nonnegative or null")
    if raw.get("scheduling", "round_robin") not in {"round_robin", "sequential"}:
        raise ValueError("scheduling must be round_robin or sequential")
    value = raw.get("source_batch_size", 5)
    if type(value) is not int or value <= 0:
        raise ValueError("source_batch_size must be a positive integer")
    for key in ("newest_first", "metadata_only"):
        if key in raw and type(raw[key]) is not bool:
            raise ValueError(f"{key} must be a boolean")
