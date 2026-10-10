from bs4 import BeautifulSoup

from paper_crawler.adapters.builtin import GenericHtmlAdapter
from paper_crawler.keywords import keyword_matches
from paper_crawler.models import ConferenceConfig


def test_iclr_proceedings_abstract_supports_foundation_model_filter(monkeypatch):
    html = '''<meta name="citation_title" content="Aurora: Multimodal Time Series Forecasting">
    <meta name="citation_pdf_url" content="/paper.pdf">
    <meta name="citation_author" content="Wu, Xingjian">
    <p class="paper-abstract"><p>A time series foundation model for zero-shot forecasting.</p></p>'''
    adapter = GenericHtmlAdapter()
    monkeypatch.setattr(adapter, "soup", lambda client, url: BeautifulSoup(html, "html.parser"))
    source = ConferenceConfig("ICLR", "https://proceedings.iclr.cc", "generic_html", [2026])
    paper = adapter.detail(source, None, "https://proceedings.iclr.cc/abstract.html", 2026, "conference")
    assert paper is not None
    assert paper.pdf_url == "https://proceedings.iclr.cc/paper.pdf"
    matched, evidence = keyword_matches(paper, [["time series"], ["forecasting"], ["foundation model"]], "all")
    assert matched
    assert any(entry["field"] == "abstract" for entry in evidence)
