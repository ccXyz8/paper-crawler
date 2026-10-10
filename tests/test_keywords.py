from paper_crawler.keywords import matches_keywords
from paper_crawler.models import Paper


def test_keyword_match_uses_title_and_abstract():
    paper = Paper(
        title="Robust image forgery detection",
        venue="CVPR",
        year=2024,
        pdf_url="https://example.test/paper.pdf",
        abstract="We localize manipulated regions.",
    )
    assert matches_keywords(paper, ["forgery"]) is True
    assert matches_keywords(paper, ["audio"]) is False


def test_all_mode_requires_every_keyword():
    paper = Paper(
        title="Video deepfake detection",
        venue="ICCV",
        year=2023,
        pdf_url="https://example.test/paper.pdf",
    )
    assert matches_keywords(paper, ["video", "deepfake"], "all") is True
    assert matches_keywords(paper, ["video", "audio"], "all") is False
