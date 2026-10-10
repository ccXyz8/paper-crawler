import json
import time
from pathlib import Path
from dataclasses import replace
from unittest.mock import Mock
import pytest
import requests
from test_reliability import Response, paper, client, config, limit_setup
from paper_crawler.models import ConferenceConfig, RateLimitConfig
from paper_crawler.pipeline import run
from paper_crawler.storage import StateStore
from paper_crawler.budget import RunBudget, RunStopped
from paper_crawler.adapters.builtin import CvfAdapter, NeuripsAdapter


def test_max_papers_limits_unmatched_scan_and_saves_partial(tmp_path, monkeypatch):
    limit_setup(monkeypatch, [paper(source_id=str(i)) for i in range(5)])
    cfg = replace(config(tmp_path), keywords=["absent"], max_papers=2)
    stats = run(cfg)
    assert stats["scanned"] == stats["seen"] == 2 and stats["attempted"] == 0
    report = json.loads(Path(cfg.report_file).read_text())
    assert report["stop_reason"] == "max_papers_reached" and stats["failed"] == 0
    store = StateStore(cfg.state_file)
    assert store.db.execute("SELECT COUNT(*) FROM metadata").fetchone()[0] == 2
    assert store.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
    store.close()


def test_round_robin_newest_first_moves_on_before_exhausting_source(tmp_path, monkeypatch):
    import paper_crawler.pipeline as pipeline
    sources = [ConferenceConfig("A", "", "cvf", [2023,2024]), ConferenceConfig("B", "", "cvf", [2024])]
    def factory(name):
        adapter = Mock(errors=[])
        adapter.iter_papers.side_effect = lambda source, c: iter([paper(source_id=f"{source.name}:{source.years[0]}:{i}") for i in range(10)])
        return adapter
    monkeypatch.setattr(pipeline, "build_adapter", factory)
    monkeypatch.setattr(pipeline, "HttpClient", Mock(return_value=client([])))
    cfg = replace(config(tmp_path, sources), scheduling="round_robin", source_batch_size=1,
                  newest_first=True, max_papers=4, metadata_only=True)
    stats = run(cfg)
    report = json.loads(Path(cfg.report_file).read_text())
    assert stats["scanned"] == 4
    assert [m["id"] for m in report["matches"]] == ["A:2024:0", "B:2024:0", "A:2024:1", "B:2024:1"]


def test_failed_detail_counts_towards_scan_limit(tmp_path, monkeypatch):
    import paper_crawler.pipeline as pipeline
    listing = '<dt class="ptitle"><a href="/bad">Bad</a></dt><dt class="ptitle"><a href="/good">Good</a></dt>'
    c = client([Response(listing), requests.HTTPError("404", response=Response(status=404))])
    monkeypatch.setattr(pipeline, "HttpClient", Mock(return_value=c))
    cfg = replace(config(tmp_path), max_papers=1, metadata_only=True)
    stats = run(cfg)
    assert stats["scanned"] == stats["failed"] == 1 and stats["seen"] == 0
    assert c.get.call_count == 2


def test_resume_reuses_index_and_details_and_rescreens_new_keywords(tmp_path, monkeypatch):
    import paper_crawler.pipeline as pipeline
    listing = '<dt class="ptitle"><a href="/a">First</a></dt><dt class="ptitle"><a href="/b">Second</a></dt>'
    detail = '<h1>Paper</h1><div id="abstract">deepfake</div><a href="/p.pdf">PDF</a>'
    c = client([Response(listing), Response(detail), Response(detail)])
    monkeypatch.setattr(pipeline, "HttpClient", Mock(return_value=c))
    cfg = replace(config(tmp_path), max_papers=1, metadata_only=True, keywords=["absent"])
    assert run(cfg)["matched"] == 0
    assert c.get.call_count == 2
    result = run(replace(cfg, max_papers=2, keywords=["deepfake"]))
    assert result["matched"] == 2 and c.get.call_count == 3
    report = json.loads(Path(cfg.report_file).read_text())
    assert report["sources"][0]["cache_hits"] >= 2
    result = run(replace(cfg, offline=True, max_papers=0, keywords=["deepfake"]))
    assert result["matched"] == 2 and c.get.call_count == 3
    report = json.loads(Path(cfg.report_file).read_text())
    assert report["sources"][0]["cache_mode"] == "partial_offline"
    assert report["sources"][0]["complete"] is False


def test_deadline_interrupts_rate_limit_wait():
    from paper_crawler.http_client import HttpClient
    budget = RunBudget(0.03)
    c = HttpClient("test", ConferenceConfig("X", "", "cvf", [2024], rate_limit=RateLimitConfig(min_delay_seconds=30)), budget=budget)
    c.session = Mock()
    c.limiter._last_request = time.monotonic()
    started = time.monotonic()
    with pytest.raises(RunStopped, match="max_seconds_reached"):
        c.get("https://test")
    assert time.monotonic()-started < 0.5
    c.session.request.assert_not_called()


def test_deadline_stops_run_normally_and_preserves_metadata(tmp_path, monkeypatch):
    import paper_crawler.pipeline as pipeline
    def generate(*args):
        yield paper()
        time.sleep(0.04)
        yield paper(source_id="second")
    factory, _ = limit_setup(monkeypatch, [])
    factory.return_value.iter_papers.side_effect = generate
    cfg = replace(config(tmp_path), metadata_only=True, max_seconds=0.03)
    result = run(cfg)
    assert result["seen"] == 1 and result["failed"] == 0
    assert json.loads(Path(cfg.report_file).read_text())["stop_reason"] == "max_seconds_reached"
    store = StateStore(cfg.state_file)
    assert store.db.execute("SELECT COUNT(*) FROM metadata").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
    store.close()


def test_keyboard_interrupt_keeps_partial_cache_and_report(tmp_path, monkeypatch):
    factory, _ = limit_setup(monkeypatch, [])
    def generate(*args):
        yield paper()
        raise KeyboardInterrupt
    factory.return_value.iter_papers.side_effect = generate
    cfg = replace(config(tmp_path), metadata_only=True)
    assert run(cfg)["seen"] == 1
    assert json.loads(Path(cfg.report_file).read_text())["stop_reason"] == "interrupted"
    store = StateStore(cfg.state_file)
    assert store.db.execute("SELECT COUNT(*) FROM metadata").fetchone()[0] == 1
    store.close()


def test_configured_fallback_recovers_source_and_deduplicates(tmp_path, monkeypatch):
    import paper_crawler.pipeline as pipeline
    primary, backup = Mock(errors=[]), Mock(errors=[])
    def fail_after_one(*args):
        yield paper()
        raise requests.HTTPError("denied", response=Response(status=403))
    primary.iter_papers.side_effect = fail_after_one
    backup.iter_papers.return_value = iter([paper(), paper(source_id="second")])
    monkeypatch.setattr(pipeline, "build_adapter", Mock(side_effect=[primary,backup]))
    monkeypatch.setattr(pipeline, "HttpClient", Mock(return_value=client([])))
    source = ConferenceConfig("NeurIPS", "https://primary", "openreview", [2024], fallbacks=[{"adapter":"neurips","website":"https://papers.nips.cc"}])
    cfg = replace(config(tmp_path,[source]), metadata_only=True)
    stats = run(cfg)
    assert stats["seen"] == 2 and stats["failed"] == 1
    report = json.loads(Path(cfg.report_file).read_text())
    assert report["sources"][0]["fallback_used"] and report["sources"][0]["complete"]
    assert report["failures"][0]["kind"] == "access_denied"


def test_exact_detail_url_map_recovers_404_and_resolves_relative_pdf():
    source = ConferenceConfig("CVPR", "https://test", "cvf", [2024], options={"detail_url_map":{"https://test/old":"https://mirror/new/paper.html"}})
    c = client([requests.HTTPError("404",response=Response(status=404)),Response('<h1>Paper</h1><a href="p.pdf">PDF</a>')])
    result = CvfAdapter().detail(source,c,"https://test/old",2024)
    assert result.pdf_url == "https://mirror/new/p.pdf" and result.landing_url == "https://test/old"


def test_neurips_preserves_main_track_and_abstract():
    listing = '<a href="/paper_files/paper/2024/hash/a-Abstract-Conference.html">Main</a><a href="/paper_files/paper/2024/hash/b-Abstract-Datasets_and_Benchmarks.html">Dataset</a>'
    detail = '<meta name="citation_title" content="Main"><h4>Abstract</h4><p>Real abstract</p><a href="/p.pdf">Paper</a>'
    source = ConferenceConfig("NeurIPS","","neurips",[2024])
    papers = list(NeuripsAdapter().iter_papers(source,client([Response(listing),Response(detail)])))
    assert len(papers) == 1 and papers[0].abstract == "Real abstract" and papers[0].track == "main"


def test_repeated_denial_stops_source_before_hundreds_of_details(tmp_path, monkeypatch):
    import paper_crawler.pipeline as pipeline
    listing = ''.join(f'<dt class="ptitle"><a href="/{i}">Paper</a></dt>' for i in range(20))
    denied = requests.HTTPError("403",response=Response(status=403))
    c = client([Response(listing),denied,denied,denied])
    monkeypatch.setattr(pipeline,"HttpClient",Mock(return_value=c))
    cfg=replace(config(tmp_path),metadata_only=True)
    stats=run(cfg)
    assert c.get.call_count == 4 and stats["failed"] > 0


@pytest.mark.parametrize("change",[{"max_papers":True},{"max_papers":-1},{"max_seconds":float('inf')},{"source_batch_size":0},{"scheduling":"bad"},{"newest_first":"true"}])
def test_runtime_config_validation(tmp_path,change):
    from paper_crawler.config import load_config
    path=tmp_path/"c.json"
    path.write_text(json.dumps({"conferences":[{"name":"CVPR","adapter":"cvf","years":[2024]}],**change}))
    with pytest.raises(ValueError):load_config(path)


def test_cli_forwards_combined_limits(monkeypatch):
    import paper_crawler.cli as cli
    mock=Mock(return_value={"failed":0})
    monkeypatch.setattr(cli,"run_from_file",mock)
    assert cli.main(["--max-matches","10","--max-papers","100","--max-seconds","30","--metadata-only"]) == 0
    mock.assert_called_once_with("config.json",max_matches=10,max_papers=100,max_seconds=30.0,metadata_only=True)


def test_fallback_alias_with_matching_author_survives_restart(tmp_path, monkeypatch):
    import paper_crawler.pipeline as pipeline
    primary = paper(source_id="openreview:abc", authors=["Alice"])
    backup = paper(source_id="proceedings:abc", authors=["Alice"])
    a, b = Mock(errors=[]), Mock(errors=[])
    def first(*args):
        yield primary
        raise requests.HTTPError("403",response=Response(status=403))
    a.iter_papers.side_effect = first
    b.iter_papers.return_value = iter([backup])
    monkeypatch.setattr(pipeline,"build_adapter",Mock(side_effect=[a,b]))
    monkeypatch.setattr(pipeline,"HttpClient",Mock(return_value=client([])))
    source=ConferenceConfig("CVPR","https://primary","cvf",[2024],fallbacks=[{"adapter":"neurips"}])
    cfg=replace(config(tmp_path,[source]),metadata_only=True)
    assert run(cfg)["seen"] == 1
    store=StateStore(cfg.state_file)
    store.mark(primary,downloaded=True,pdf="already.pdf")
    assert store.get(backup)["pdf"] == "already.pdf"
    store.close()


def test_failed_parsing_does_not_poison_cached_detail(tmp_path):
    from paper_crawler.pipeline import source_key
    store=StateStore(tmp_path/"state.db")
    source=ConferenceConfig("CVPR","https://test","cvf",[2024])
    adapter=CvfAdapter();adapter.store=store;adapter.cache_scope=source_key(source)
    c=client([Response('<html>login page</html>'),Response('<h1>Fixed</h1><a href="/p.pdf">pdf</a>')])
    assert adapter.detail(source,c,"https://test/paper",2024) is None
    assert adapter.detail(source,c,"https://test/paper",2024).title == "Fixed"
    assert c.get.call_count == 2
    store.close()


def test_expired_partial_metadata_is_refetched(tmp_path,monkeypatch):
    import paper_crawler.pipeline as pipeline
    listing='<dt class="ptitle"><a href="/a">First</a></dt>'
    detail='<h1>Paper</h1><div id="abstract">deepfake</div><a href="/p.pdf">PDF</a>'
    c=client([Response(listing),Response(detail),Response(listing),Response(detail)])
    monkeypatch.setattr(pipeline,"HttpClient",Mock(return_value=c))
    cfg=replace(config(tmp_path),metadata_only=True,max_papers=1,cache_ttl_hours=0)
    run(cfg);run(cfg)
    assert c.get.call_count == 4


def test_deadline_cleans_partial_download(tmp_path):
    from paper_crawler.pipeline import _download
    class SlowResponse(Response):
        def iter_content(self,**kwargs):
            yield b"%PDF-partial"
            time.sleep(0.04)
            yield b"more"
    c=client([SlowResponse()]);c.budget=RunBudget(0.02)
    target=tmp_path/"p.pdf"
    with pytest.raises(RunStopped):_download(paper(),c,target)
    assert not target.exists() and not target.with_suffix('.pdf.part').exists()


def test_cli_interrupt_exit_code(monkeypatch):
    import paper_crawler.cli as cli
    monkeypatch.setattr(cli,"run_from_file",lambda *args,**kwargs:{"failed":0,"interrupted":1})
    assert cli.main([]) == 130
