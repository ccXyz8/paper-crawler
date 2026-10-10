import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
import pytest
import requests
from paper_crawler.models import Paper, ConferenceConfig, RateLimitConfig, AppConfig, ZoteroConfig
from paper_crawler.pipeline import _download, valid_pdf, run
from paper_crawler.storage import StateStore
from paper_crawler.keywords import keyword_matches, matches_keywords
from paper_crawler.config import load_config
from paper_crawler.adapters.builtin import CvfAdapter, OpenReviewAdapter, GenericHtmlAdapter, PmlrAdapter, AaaiAdapter, AclAnthologyAdapter
from paper_crawler.zotero import ZoteroClient, ZoteroError

PDF = b"%PDF-1.7\ncontent\n%%EOF\n"

class Response:
    def __init__(self, text="", payload=None, data=PDF, fail=False, headers=None, status=200):
        self.text, self.payload, self.data, self.fail = text, payload, data, fail
        self.headers = headers or {}
        self.status_code = status
        self.closed = False
    def json(self): return self.payload
    def close(self): self.closed = True
    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def iter_content(self, **kwargs):
        yield self.data
        if self.fail: raise requests.ConnectionError("interrupted")
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


def paper(**kwargs):
    return replace(Paper("Deepfake detection", "CVPR", 2024, "https://test/p.pdf", source_id="source:1"), **kwargs)


def client(responses, conf=None):
    c = Mock()
    c.config = conf or ConferenceConfig("CVPR", "https://test", "cvf", [2024], rate_limit=RateLimitConfig(max_retries=1, backoff_seconds=0))
    c.get.side_effect = responses
    return c


def test_download_recovers_interrupted_stream(tmp_path):
    c = client([Response(data=b"%PDF-half", fail=True), Response()])
    target = tmp_path/"paper.pdf"
    assert _download(paper(), c, target)
    assert target.read_bytes() == PDF
    assert c.get.call_count == 2
    assert not target.with_suffix(".pdf.part").exists()
    assert not _download(paper(), c, target)


@pytest.mark.parametrize("data,headers", [(b"<html>error</html>", {}), (b"%PDF-half", {}), (PDF, {"Content-Length": "1000"}), (PDF, {"Content-Type":"text/html"})])
def test_invalid_download_never_committed(tmp_path, data, headers):
    c = client([Response(data=data, headers=headers), Response(data=data, headers=headers)])
    target = tmp_path/"p.pdf"
    with pytest.raises(ValueError): _download(paper(), c, target)
    assert not target.exists()
    assert not target.with_suffix(".pdf.part").exists()


def test_existing_truncated_file_is_replaced(tmp_path):
    target = tmp_path/"p.pdf"; target.write_bytes(b"%PDF-half")
    assert _download(paper(), client([Response()]), target)
    assert valid_pdf(target)


def test_keyword_groups_exclusions_and_evidence():
    p = paper(title="Deep-fake detection", abstract="image manipulation")
    matched, evidence = keyword_matches(p, [["deepfake", "forgery"], "image manipulation"], "all")
    assert matched and {x["field"] for x in evidence} == {"title", "abstract"}
    assert not keyword_matches(p, ["deepfake"], exclude=["manipulation"])[0]
    assert not matches_keywords(p, [""])
    assert not matches_keywords(paper(title="mailing", abstract=""), ["ai"])


def test_legacy_migration_and_checkpoints(tmp_path):
    path = tmp_path/"state.json"
    path.write_text(json.dumps({"papers":{"source:1":{"status":"downloaded", "pdf":"old.pdf"}}}))
    state = StateStore(path)
    assert state.get(paper())["status"] == "downloaded"
    state.mark(paper(), attachment_key="ABC")
    assert state.get(paper())["pdf"] == "old.pdf"
    state.close()
    state = StateStore(path)
    assert state.get(paper())["attachment_key"] == "ABC"
    assert path.exists()
    state.close()


def test_cache_expiry_and_offline(tmp_path):
    state = StateStore(tmp_path/"s.db")
    state.cache("source", [paper()])
    assert state.cached("source", 24)[0] == paper()
    assert state.cached("source", 0) is None
    assert state.cached("source", 0, True)[0] == paper()
    state.close()


def test_cvf_day_pages_real_abstract_and_failure_isolation():
    conf = ConferenceConfig("CVPR", "https://test", "cvf", [2024])
    index = '<a href="?day=all">All</a>'
    listing = '<dt class="ptitle"><a href="/bad.html">bad</a></dt><dt class="ptitle"><a href="/good.html">good</a></dt>'
    detail = '<meta name="citation_title" content="Good"><meta name="citation_author" content="Alice"><div id="abstract">Real abstract</div><a href="/real.pdf">pdf</a>'
    adapter = CvfAdapter()
    result = list(adapter.iter_papers(conf, client([Response(index), Response(listing), requests.ConnectionError("bad"), Response(detail)], conf)))
    assert len(result) == 1 and result[0].abstract == "Real abstract"
    assert result[0].authors == ["Alice"] and result[0].pdf_url == "https://test/real.pdf"
    assert len(adapter.errors) == 1


def test_openreview_accepted_and_server_page_cap():
    conf = ConferenceConfig("ICLR", "", "openreview", [2025])
    def note(id, venue): return {"id": id, "content": {"title":{"value":"Paper"}, "pdf":{"value":"/pdf/file.pdf"}, "venueid":{"value":venue}}}
    c = client([Response(payload={"notes":[note("a", "ICLR.cc/2025/Conference")]}),
                Response(payload={"notes":[note("b", "rejected")]}), Response(payload={"notes":[]})], conf)
    result = list(OpenReviewAdapter().iter_papers(conf, c))
    assert len(result) == 1 and result[0].source_id == "openreview:a"
    assert c.get.call_args_list[0].kwargs["params"]["content.venueid"] == "ICLR.cc/2025/Conference"
    assert c.get.call_count == 3


def test_generic_optional_fields():
    conf = ConferenceConfig("Journal", "", "generic_html", [2024], options={"year_url_template":"https://test/{year}", "item_type":"journalArticle", "selectors":{"item":"article", "title":"h2", "pdf":"a", "landing":".missing"}})
    result = list(GenericHtmlAdapter().iter_papers(conf, client([Response('<article><h2>Title</h2><a href="/p.pdf">pdf</a></article>')], conf)))
    assert result[0].abstract == "" and result[0].item_type == "journalArticle"
    assert result[0].landing_url == "https://test/p.pdf"


def test_pmlr_ignores_navigation_and_deduplicates():
    conf = ConferenceConfig("ICML", "", "pmlr", [2024])
    c = client([Response('<a href="/about.html">About</a><div class="paper"><a href="p.html">paper</a><a href="p.html">abs</a></div>'),
                Response('<h1>Title</h1><div class="abstract">Abstract</div><a href="p.pdf">PDF</a>')], conf)
    assert len(list(PmlrAdapter().iter_papers(conf, c))) == 1
    assert c.get.call_count == 2


def test_aaai_archive_pagination_and_pdf_meta():
    conf = ConferenceConfig("AAAI", "", "aaai", [2024])
    c = client([Response('<a class="next" href="archive/2">next</a>'),
                Response('<a href="/issue/view/1">2024</a>'),
                Response('<h3><a href="/article/view/2">Paper</a></h3>'),
                Response('<meta name="citation_title" content="Paper"><meta name="citation_pdf_url" content="/article/download/2/3">')], conf)
    result = list(AaaiAdapter().iter_papers(conf, c))
    assert len(result) == 1 and "/article/download/2/3" in result[0].pdf_url


def test_acl_only_main_volumes():
    conf = ConferenceConfig("ACL", "", "acl_anthology", [2024])
    c = client([Response('<a href="/2024.acl-long.0/">front matter</a><a href="/2024.acl-long.1/">main</a><a href="/2024.findings-acl.1/">findings</a><a href="/2024.acl-long/">volume</a>'),
                Response('<h1>Main</h1><a href="p.pdf">pdf</a>')], conf)
    result = list(AclAnthologyAdapter().iter_papers(conf, c))
    assert len(result) == 1 and result[0].track == "2024.acl-long"


@pytest.mark.parametrize("change", [{"keywords":[""]}, {"keyword_mode":"typo"}, {"download_only":"false"}, {"cache_ttl_hours":-1}])
def test_config_rejects_invalid_values(tmp_path, change):
    raw = {"conferences":[{"name":"CVPR", "adapter":"cvf", "years":[2024]}], **change}
    path = tmp_path/"c.json"; path.write_text(json.dumps(raw))
    with pytest.raises(ValueError): load_config(path)


def test_config_paths_relative_to_config(tmp_path):
    path = tmp_path/"c.json"
    path.write_text(json.dumps({"conferences":[{"name":"CVPR", "adapter":"cvf", "years":[2024]}]}))
    assert load_config(path).download_dir == str(tmp_path/"downloads")


def config(tmp_path, conferences=None):
    return AppConfig([], "any", str(tmp_path/"pdf"), str(tmp_path/"state.json"), "test", False,
                     ZoteroConfig(enabled=False), conferences or [ConferenceConfig("CVPR", "", "cvf", [2024])],
                     report_file=str(tmp_path/"report.json"), scheduling="sequential", newest_first=False)


def test_pipeline_download_then_import_and_metadata_cache(tmp_path, monkeypatch):
    import paper_crawler.pipeline as pipeline
    cfg = config(tmp_path)
    adapter = Mock(errors=[]); adapter.iter_papers.return_value = iter([paper()])
    monkeypatch.setattr(pipeline, "build_adapter", Mock(return_value=adapter))
    c = client([Response()]); monkeypatch.setattr(pipeline, "HttpClient", Mock(return_value=c))
    first = run(cfg)
    assert first["downloaded"] == 1
    z = Mock(scope="server/users/0/")
    def do_import(p, target, collection, progress, checkpoint): checkpoint(item_key="ITEM", status="imported")
    z.import_paper.side_effect = do_import
    monkeypatch.setattr(pipeline, "ZoteroClient", Mock(return_value=z))
    second = run(replace(cfg, zotero=ZoteroConfig(enabled=True)))
    assert second["imported"] == 1 and second["downloaded"] == 0
    third = run(replace(cfg, zotero=ZoteroConfig(enabled=True)))
    assert third["skipped"] == 1 and z.import_paper.call_count == 1
    assert pipeline.build_adapter.call_count == 1


def test_pipeline_year_failure_does_not_stop_later_year(tmp_path, monkeypatch):
    import paper_crawler.pipeline as pipeline
    cfg = config(tmp_path, [ConferenceConfig("CVPR", "", "cvf", [2023,2024])])
    broken = Mock(errors=[]); broken.iter_papers.side_effect = RuntimeError("index failure")
    good = Mock(errors=[]); good.iter_papers.return_value = iter([paper()])
    monkeypatch.setattr(pipeline, "build_adapter", Mock(side_effect=[broken,good]))
    monkeypatch.setattr(pipeline, "HttpClient", Mock(return_value=client([Response()])))
    stats = run(cfg)
    assert stats["failed"] == 1 and stats["downloaded"] == 1
    report = json.loads(Path(cfg.report_file).read_text())
    assert len(report["sources"]) == 2 and report["failures"][0]["year"] == 2023


def test_zotero_handshake_authorization_and_server_header():
    z = ZoteroClient(ZoteroConfig())
    session = Mock(headers={})
    session.get.return_value = Response(headers={"Zotero-Server-ID":"server"})
    session.request.side_effect = [Response(status=401), Response(payload={"ok":True})]
    session.post.return_value = Response(payload={"key":"local-key"})
    z.session = session
    assert z._json("POST", "users/0/items", json=[]) == {"ok":True}
    assert session.headers["Zotero-Server-ID"] == "server"
    assert session.headers["Zotero-API-Key"] == "local-key"
    assert z.scope == "server/users/0/"
    z.close()


def test_zotero_resume_after_upload_failure(tmp_path, monkeypatch):
    z = ZoteroClient(ZoteroConfig())
    created = set(); progress = {}
    monkeypatch.setattr(z, "_existing", lambda key: {"key":key} if key in created else None)
    z._find_item = Mock(return_value=None)
    z._find_attachment = Mock(return_value=None)
    def create_item(p, collection): created.add("ITEM"); return "ITEM"
    def create_attachment(parent, path): created.add("ATTACH"); return "ATTACH"
    z.create_item = Mock(side_effect=create_item)
    z.create_attachment = Mock(side_effect=create_attachment)
    z.upload_attachment = Mock(side_effect=[requests.ConnectionError("upload failed"), None])
    def save(**values): progress.update(values)
    with pytest.raises(requests.ConnectionError):
        z.import_paper(paper(), tmp_path/"p.pdf", "collection", progress, save)
    assert progress["status"] == "attachment_created"
    z.import_paper(paper(), tmp_path/"p.pdf", "collection", progress, save)
    assert z.create_item.call_count == 1 and z.create_attachment.call_count == 1
    assert progress["status"] == "imported"
    z.close()


def test_zotero_collection_custom_error_falls_back(monkeypatch):
    z = ZoteroClient(ZoteroConfig())
    z.ensure_collection = Mock(side_effect=["ROOT", ZoteroError("bad child")])
    assert z.target_collection("CVPR") == "ROOT"
    z.close()


def test_http_does_not_retry_permanent_error(monkeypatch):
    from paper_crawler.http_client import HttpClient
    c = HttpClient("test", ConferenceConfig("Test", "", "cvf", [2024]))
    c.limiter = Mock()
    response = Response(status=404)
    c.session = Mock(); c.session.request.return_value = response
    with pytest.raises(requests.HTTPError): c.get("https://test/missing")
    assert c.session.request.call_count == 1 and response.closed


def test_http_retries_transient_error_and_closes_response(monkeypatch):
    from paper_crawler.http_client import HttpClient
    monkeypatch.setattr("paper_crawler.http_client.time.sleep", lambda _: None)
    c = HttpClient("test", ConferenceConfig("Test", "", "cvf", [2024]))
    c.limiter = Mock()
    response = Response(status=429, headers={"Retry-After":"Wed, 21 Oct 2015 07:28:00 GMT"})
    c.session = Mock(); c.session.request.side_effect = [response, Response()]
    assert c.get("https://test").status_code == 200
    assert response.closed and c.session.request.call_count == 2


def test_cli_reports_partial_failure(monkeypatch):
    from paper_crawler.cli import main
    monkeypatch.setattr("paper_crawler.cli.run_from_file", lambda _: {"failed":1})
    assert main([]) == 2


def test_zotero_lost_create_response_reuses_reserved_key(tmp_path, monkeypatch):
    z = ZoteroClient(ZoteroConfig())
    progress = {}
    monkeypatch.setattr(z, "_existing", lambda key: None)
    z._find_item = Mock(side_effect=[None, "ITEM"])
    z._find_attachment = Mock(return_value=None)
    def create(p, collection):
        raise requests.ConnectionError("response lost after server commit")
    z.create_item = Mock(side_effect=create)
    z.create_attachment = Mock(return_value="ATTACH")
    z.upload_attachment = Mock()
    def checkpoint(**values): progress.update(values)
    with pytest.raises(requests.ConnectionError): z.import_paper(paper(),tmp_path/"p.pdf","C",progress,checkpoint)
    z.import_paper(paper(),tmp_path/"p.pdf","C",progress,checkpoint)
    assert z.create_item.call_count == 1 and progress["item_key"] == "ITEM" and progress["status"] == "imported"
    z.close()


def test_zotero_collection_paginates():
    z = ZoteroClient(ZoteroConfig())
    z._json = Mock(side_effect=[[{"key":str(i),"data":{"name":"other","parentCollection":False}} for i in range(100)],
                               [{"key":"ROOT","data":{"name":"TFD","parentCollection":False}}]])
    assert z.ensure_collection("TFD") == "ROOT"
    assert z._json.call_args.kwargs["params"]["start"] == 100
    z.close()


def test_journal_zotero_metadata():
    z = ZoteroClient(ZoteroConfig())
    z._json = Mock(return_value={"successful":{"0":{"key":"ITEM"}}})
    p = paper(item_type="journalArticle", metadata={"volume":"12","issue":"3","ISSN":"1234-5678","date":"2024-03-01"})
    assert z.create_item(p,"C") == "ITEM"
    data = z._json.call_args.kwargs["json"][0]
    assert data["itemType"] == "journalArticle" and data["volume"] == "12"
    assert "conferenceName" not in data and data["publicationTitle"] == "CVPR"
    z.close()


def test_zotero_keyed_create_uses_library_version_precondition():
    z = ZoteroClient(ZoteroConfig())
    z._library_version = Mock(return_value="42")
    z._json = Mock(return_value={"successful":{"0":{"key":"ABCD2345"}}})
    assert z.create_item(paper(), "C", "ABCD2345") == "ABCD2345"
    kwargs = z._json.call_args.kwargs
    assert kwargs["headers"] == {"If-Unmodified-Since-Version": "42"}
    assert kwargs["json"][0]["key"] == "ABCD2345"
    assert "version" not in kwargs["json"][0]
    z.close()


def test_incomplete_source_is_not_cached(tmp_path,monkeypatch):
    import paper_crawler.pipeline as pipeline
    cfg=config(tmp_path)
    adapter=Mock(errors=[{"url":"https://test/bad","error":"failed"}])
    adapter.iter_papers.side_effect=lambda *args: iter([paper()])
    monkeypatch.setattr(pipeline,"build_adapter",Mock(return_value=adapter))
    monkeypatch.setattr(pipeline,"HttpClient",Mock(return_value=client([Response()])))
    assert run(cfg)["failed"] == 1
    assert run(cfg)["failed"] == 1
    assert pipeline.build_adapter.call_count == 2


def test_pipeline_adopts_legacy_import_only_after_identity_check(tmp_path,monkeypatch):
    import paper_crawler.pipeline as pipeline
    cfg=config(tmp_path)
    pdf=tmp_path/"old.pdf";pdf.write_bytes(PDF)
    Path(cfg.state_file).write_text(json.dumps({"papers":{"source:1":{"status":"imported","zotero_key":"OLD","pdf":str(pdf)}}}))
    adapter=Mock(errors=[]);adapter.iter_papers.return_value=iter([paper()])
    monkeypatch.setattr(pipeline,"build_adapter",Mock(return_value=adapter))
    monkeypatch.setattr(pipeline,"HttpClient",Mock(return_value=client([])))
    z=Mock(scope="server/users/0/");z.legacy_item_matches.return_value=True
    monkeypatch.setattr(pipeline,"ZoteroClient",Mock(return_value=z))
    result=run(replace(cfg,zotero=ZoteroConfig(enabled=True)))
    assert result["skipped"] == 1
    z.import_paper.assert_not_called()
    z.legacy_item_matches.assert_called_once_with("OLD",paper())


def test_icml_2025_volume():
    assert PmlrAdapter.DEFAULT_VOLUMES[2025] == 267


def limit_setup(monkeypatch, papers):
    import paper_crawler.pipeline as pipeline
    adapter = Mock(errors=[])
    adapter.iter_papers.side_effect = lambda *args: iter(papers)
    factory = Mock(return_value=adapter)
    monkeypatch.setattr(pipeline, "build_adapter", factory)
    c = client([Response() for _ in papers])
    monkeypatch.setattr(pipeline, "HttpClient", Mock(return_value=c))
    return factory, c


def test_limit_stops_without_fetching_extra_paper_and_closes_generator(tmp_path, monkeypatch):
    import paper_crawler.pipeline as pipeline
    closed = []
    def generate(*args):
        try:
            yield paper()
            pytest.fail("Should not request the next detail page after reaching limit")
        finally:
            closed.append(True)
    factory, c = limit_setup(monkeypatch, [])
    factory.return_value.iter_papers.side_effect = generate
    c.get.side_effect = [Response()]
    cfg = replace(config(tmp_path), max_matches=1)
    result = run(cfg)
    assert result["attempted"] == result["downloaded"] == 1
    assert result["failed"] == 0 and closed == [True]
    report = json.loads(Path(cfg.report_file).read_text())
    assert report["stop_reason"] == "max_matches_reached"
    assert report["sources"][0]["limited"] is True
    store = StateStore(cfg.state_file)
    assert store.db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0
    store.close()


def test_limit_continues_next_batch_skipping_completed_downloads(tmp_path, monkeypatch):
    papers = [paper(source_id=f"source:{i}") for i in range(3)]
    factory, c = limit_setup(monkeypatch, papers)
    cfg = replace(config(tmp_path), max_matches=1)
    for expected_seen in (1, 2, 3):
        result = run(cfg)
        assert result["attempted"] == result["downloaded"] == 1
        assert result["seen"] == expected_seen
    result = run(cfg)
    assert result["attempted"] == 0 and result["skipped"] == 3
    assert json.loads(Path(cfg.report_file).read_text())["stop_reason"] == "completed"
    assert c.get.call_count == 3


def test_limit_failed_download_consumes_slot(tmp_path, monkeypatch):
    import paper_crawler.pipeline as pipeline
    limit_setup(monkeypatch, [paper(), paper(source_id="second")])
    download = Mock(side_effect=ValueError("bad PDF"))
    monkeypatch.setattr(pipeline, "_download", download)
    result = run(replace(config(tmp_path), max_matches=1))
    assert result["attempted"] == result["failed"] == 1
    assert result["seen"] == download.call_count == 1


def test_limit_is_global_across_years_and_sources(tmp_path, monkeypatch):
    import paper_crawler.pipeline as pipeline
    sources = [ConferenceConfig("A", "", "cvf", [2023,2024]), ConferenceConfig("B", "", "cvf", [2024]), ConferenceConfig("C", "", "cvf", [2024])]
    factory, _ = limit_setup(monkeypatch, [paper(), paper(), paper()])
    factory.return_value.iter_papers.side_effect = lambda source, client: iter([paper(source_id=f"{source.name}:{source.years[0]}")])
    cfg = replace(config(tmp_path, sources), max_matches=3)
    result = run(cfg)
    assert result["attempted"] == result["downloaded"] == 3
    report = json.loads(Path(cfg.report_file).read_text())
    assert [(s["venue"], s["year"]) for s in report["sources"]] == [("A",2023),("A",2024),("B",2024)]
    assert pipeline.HttpClient.call_count == 2


def test_limit_reuses_complete_cache_without_truncating_it(tmp_path, monkeypatch):
    import paper_crawler.pipeline as pipeline
    papers = [paper(source_id=f"id:{i}") for i in range(3)]
    factory, c = limit_setup(monkeypatch, papers)
    cfg = config(tmp_path)
    run(replace(cfg, keywords=["nonmatching"]))  # Save all metadata without downloading.
    result = run(replace(cfg, max_matches=1))
    assert result["attempted"] == 1
    assert factory.call_count == 1
    store = StateStore(cfg.state_file)
    cached = json.loads(store.db.execute("SELECT data FROM snapshots").fetchone()[0])
    assert len(cached) == 3
    store.close()


def test_limit_skips_imported_but_counts_pending_import(tmp_path, monkeypatch):
    import paper_crawler.pipeline as pipeline
    papers = [paper(source_id=f"id:{i}") for i in range(3)]
    limit_setup(monkeypatch, papers)
    cfg = config(tmp_path)
    run(cfg)
    store = StateStore(cfg.state_file)
    store.mark(papers[0], imports={"server/users/0/":{"status":"imported"}})
    store.close()
    z = Mock(scope="server/users/0/")
    monkeypatch.setattr(pipeline, "ZoteroClient", Mock(return_value=z))
    result = run(replace(cfg, max_matches=1, zotero=ZoteroConfig(enabled=True)))
    assert result["attempted"] == result["imported"] == 1
    assert result["downloaded"] == 0 and result["seen"] == 2
    assert z.import_paper.call_args.args[0].stable_id == papers[1].stable_id


def test_limit_duplicate_failures_only_attempted_once_globally(tmp_path, monkeypatch):
    import paper_crawler.pipeline as pipeline
    sources = [ConferenceConfig("A", "", "cvf", [2024]), ConferenceConfig("B", "", "cvf", [2024])]
    limit_setup(monkeypatch, [paper()])
    monkeypatch.setattr(pipeline, "_download", Mock(side_effect=ValueError("failed")))
    result = run(replace(config(tmp_path, sources), max_matches=2))
    assert result["attempted"] == result["failed"] == 1
    assert pipeline._download.call_count == 1


@pytest.mark.parametrize("value", [-1, True, 1.5, "10"])
def test_invalid_max_matches_config(tmp_path, value):
    path = tmp_path/"config.json"
    path.write_text(json.dumps({"max_matches":value,"conferences":[{"name":"CVPR","adapter":"cvf","years":[2024]}]}))
    with pytest.raises(ValueError, match="max_matches"):
        load_config(path)


@pytest.mark.parametrize("override,expected", [(None, 2), (1, 1), (0, 0)])
def test_limit_cli_override_config(tmp_path, monkeypatch, override, expected):
    import paper_crawler.pipeline as pipeline
    path = tmp_path/"config.json"
    path.write_text(json.dumps({"max_matches":2,"conferences":[{"name":"CVPR","adapter":"cvf","years":[2024]}]}))
    mock = Mock(return_value={"failed":0})
    monkeypatch.setattr(pipeline, "run", mock)
    pipeline.run_from_file(str(path), max_matches=override)
    assert mock.call_args.args[0].max_matches == expected


def test_limit_cli_normal_exit_and_argument_forwarding(monkeypatch):
    from paper_crawler.cli import main
    mock = Mock(return_value={"failed":0,"attempted":10})
    monkeypatch.setattr("paper_crawler.cli.run_from_file", mock)
    assert main(["--config","x.json","--max-matches","10"]) == 0
    mock.assert_called_once_with("x.json", max_matches=10)


@pytest.mark.parametrize("value", ["-1", "1.5", "invalid"])
def test_limit_cli_invalid_argument(value):
    from paper_crawler.cli import main
    with pytest.raises(SystemExit) as exc:
        main(["--max-matches",value])
    assert exc.value.code == 2


def test_zero_limit_is_unlimited(tmp_path,monkeypatch):
    limit_setup(monkeypatch,[paper(source_id=str(i)) for i in range(3)])
    cfg = replace(config(tmp_path),max_matches=0)
    assert run(cfg)["attempted"] == 3
    assert json.loads(Path(cfg.report_file).read_text())["stop_reason"] == "completed"


def test_cvf_title_priority_keeps_abstract_only_candidates():
    conf = ConferenceConfig("CVPR", "https://test", "cvf", [2024], keywords=["deepfake"], options={"prioritize_title_matches":True})
    listing = '<dt class="ptitle"><a href="/a.html">Other title</a></dt><dt class="ptitle"><a href="/b.html">Deepfake detection</a></dt>'
    detail = '<h1>Paper</h1><div id="abstract">deepfake abstract</div><a href="/p.pdf">pdf</a>'
    c = client([Response(listing),Response(detail),Response(detail)],conf)
    papers = list(CvfAdapter().iter_papers(conf,c))
    assert [p.landing_url for p in papers] == ["https://test/b.html","https://test/a.html"]
    assert all(p.abstract == "deepfake abstract" for p in papers)
