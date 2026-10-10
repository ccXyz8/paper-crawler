from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Paper:
    title: str
    venue: str
    year: int
    pdf_url: str
    landing_url: str = ""
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    doi: str = ""
    source_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    item_type: str = "conferencePaper"
    track: str = "main"

    @property
    def stable_id(self) -> str:
        doi = self.doi.strip().lower().removeprefix("https://doi.org/").removeprefix("http://doi.org/").removeprefix("doi:").strip()
        return f"doi:{doi}" if doi else self.source_id or self.pdf_url


@dataclass(slots=True)
class LoginConfig:
    username: str = ""
    password: str = ""
    login_url: str = ""
    username_field: str = "username"
    password_field: str = "password"


@dataclass(slots=True)
class RateLimitConfig:
    requests_per_minute: int = 30
    min_delay_seconds: float = 1.0
    max_retries: int = 3
    backoff_seconds: float = 2.0
    timeout_seconds: int = 30


@dataclass(slots=True)
class ConferenceConfig:
    name: str
    website: str
    adapter: str
    years: list[int]
    login: LoginConfig = field(default_factory=LoginConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    keywords: list[str | list[str]] | None = None
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)
    fallbacks: list[dict[str, Any]] = field(default_factory=list)
    meeting_dates: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass(slots=True)
class ZoteroConfig:
    enabled: bool = True
    base_url: str = "http://127.0.0.1:23119"
    api_key: str = ""
    user_or_group: str = "users/0"
    root_collection: str = "TFD"
    create_subcollections: bool = True
    fallback_to_root: bool = True


@dataclass(slots=True)
class AppConfig:
    keywords: list[str | list[str]]
    keyword_mode: str
    download_dir: str
    state_file: str
    user_agent: str
    dry_run: bool
    zotero: ZoteroConfig
    conferences: list[ConferenceConfig]

    download_only: bool = False
    exclude_keywords: list[str] = field(default_factory=list)
    keyword_word_boundary: bool = True
    cache_ttl_hours: float = 24.0
    offline: bool = False
    report_file: str = "run_report.json"
    max_matches: int | None = None
    max_papers: int | None = None
    max_seconds: float | None = None
    scheduling: str = "round_robin"
    source_batch_size: int = 5
    newest_first: bool = True
    metadata_only: bool = False
