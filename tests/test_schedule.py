import json
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from paper_crawler.models import ConferenceConfig
from paper_crawler.schedule import eligibility, validate_meeting_dates
from test_reliability import config, client, paper


def source(name="A", years=None, end="2026-07-11"):
    return ConferenceConfig(name, "", "cvf", years or [2026], meeting_dates={
        "2026": {"end_date": end, "source_url": "https://official.example/dates"}})


@pytest.mark.parametrize("timestamp,reason", [
    ("2026-07-10T12:00:00+00:00", "conference_not_finished"),
    ("2026-07-11T23:59:59+00:00", "conference_not_finished"),
    ("2026-07-12T11:59:59+00:00", "conference_not_finished"),
    ("2026-07-12T12:00:00+00:00", None),
])
def test_end_date_boundary(timestamp, reason):
    assert eligibility(source(), 2026, datetime.fromisoformat(timestamp)) == reason


def test_unknown_future_and_journal():
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)
    unknown = replace(source(), meeting_dates={})
    assert eligibility(unknown, 2026, now) == "unknown_meeting_end_date"
    assert eligibility(unknown, 2027, now) == "future_year"
    assert eligibility(unknown, 2025, now) is None
    assert eligibility(replace(unknown, options={"item_type": "journalArticle"}), 2026, now) is None


@pytest.mark.parametrize("entry", [[], {"2026": {}}, {"2026": {"end_date": "2026-02-30"}},
    {"2026": {"end_date": "2025-07-11", "source_url": "https://official.example"}},
    {"2026": {"end_date": "2026-07-11", "source_url": "file:///bad"}}])
def test_calendar_validation(entry):
    with pytest.raises(ValueError):
        validate_meeting_dates(entry)


def test_global_year_priority_and_skips_before_network(tmp_path, monkeypatch):
    import paper_crawler.pipeline as pipeline
    monkeypatch.setattr(pipeline, "utc_now", lambda: datetime(2026, 9, 22, tzinfo=timezone.utc))
    order = []
    def factory(name):
        adapter = Mock(errors=[])
        def generate(s, c):
            order.append((s.name, s.years[0]))
            yield paper(source_id=f"{s.name}:{s.years[0]}", year=s.years[0])
        adapter.iter_papers.side_effect = generate
        return adapter
    monkeypatch.setattr(pipeline, "build_adapter", factory)
    monkeypatch.setattr(pipeline, "HttpClient", Mock(return_value=client([])))
    sources = [source("Old", [2024]), source("A", [2025, 2026]), source("B"),
               source("December", end="2026-12-13"), replace(source("Unknown"), meeting_dates={}),
               source("Future", [2027])]
    cfg = replace(config(tmp_path, sources), newest_first=True, scheduling="round_robin",
                  source_batch_size=1, metadata_only=True)
    result = pipeline.run(cfg)
    assert result["failed"] == 0
    assert order == [("A", 2026), ("B", 2026), ("A", 2025), ("Old", 2024)]
    report = json.loads(Path(cfg.report_file).read_text())
    assert [s["reason"] for s in report["skipped_sources"]] == [
        "conference_not_finished", "unknown_meeting_end_date", "future_year"]
