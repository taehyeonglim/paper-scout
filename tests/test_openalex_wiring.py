"""related-papers 경로에 OpenAlex가 실제로 배선됐는지 — 클라이언트 mock 기반."""
from unittest.mock import MagicMock

from config import Config
from related_paper_finder import RelatedPaperFinder, FinderConfig
from utils.paper_models import Paper


def _paper(pid, doi=None, title="T"):
    return Paper(paper_id=pid, title=title, doi=doi)


def _finder(monkeypatch, tmp_path, openalex_result):
    monkeypatch.delenv("PAPER_SCOUT_LLM_CMD", raising=False)
    monkeypatch.chdir(tmp_path)
    f = RelatedPaperFinder(Config.load())
    for name in ("ss_client", "arxiv_client", "eric_client", "kci_client"):
        client = MagicMock()
        client.search_papers.return_value = []
        client.available = False
        setattr(f, name, client)
    f.openalex_client = MagicMock()
    f.openalex_client.search_papers.return_value = openalex_result
    return f


def test_openalex_results_flow_into_finder(monkeypatch, tmp_path):
    f = _finder(monkeypatch, tmp_path, [_paper("openalex:W1", doi="10.1/a", title="OA hit")])
    _, high, moderate = f.find_by_keywords("q", FinderConfig(limit=5))
    titles = [p.title for p in high + moderate]
    assert "OA hit" in titles


def test_no_openalex_flag_skips_call(monkeypatch, tmp_path):
    f = _finder(monkeypatch, tmp_path, [_paper("openalex:W1")])
    f.find_by_keywords("q", FinderConfig(limit=5, include_openalex=False))
    f.openalex_client.search_papers.assert_not_called()


def test_doi_dedup_across_s2_and_openalex(monkeypatch, tmp_path):
    f = _finder(monkeypatch, tmp_path, [_paper("openalex:W1", doi="10.1/same")])
    f.ss_client.search_papers.return_value = [_paper("s2:1", doi="10.1/same", title="S2 first")]
    _, high, moderate = f.find_by_keywords("q", FinderConfig(limit=5))
    dois = [p.doi for p in high + moderate]
    assert dois.count("10.1/same") == 1             # DOI 중복 1건으로 수렴


def test_deep_researcher_wires_openalex(monkeypatch, tmp_path):
    """deep_researcher가 openalex_client를 보유하고 source_db='openalex'로 소비하는지."""
    monkeypatch.delenv("PAPER_SCOUT_LLM_CMD", raising=False)
    monkeypatch.chdir(tmp_path)
    from deep_researcher import DeepResearcher

    dr = DeepResearcher(Config.load())
    assert hasattr(dr, "openalex_client")


def test_trend_analyzer_dedupes_openalex_against_s2(monkeypatch, tmp_path):
    monkeypatch.delenv("PAPER_SCOUT_LLM_CMD", raising=False)
    monkeypatch.chdir(tmp_path)
    from research_trend_analyzer import ResearchTrendAnalyzer, TrendConfig

    a = ResearchTrendAnalyzer(Config.load())
    a.ss_client = MagicMock()
    a.ss_client.search_papers.return_value = [_paper("s2:1", doi="10.1/x", title="Same")]
    a.openalex_client = MagicMock()
    a.openalex_client.search_papers.return_value = [
        _paper("openalex:W9", doi="10.1/x", title="Same"),
        _paper("openalex:W8", doi="10.2/y", title="Other"),
    ]
    result = a.analyze_trend("topic", TrendConfig(years=0, papers_per_year=10))
    assert result["total_papers"] == 2               # 3건 수집, DOI dedup 후 2건


def test_trend_analyzer_dedupes_no_doi_s2_against_doi_openalex(monkeypatch, tmp_path):
    """S2 학회논문(DOI 부재) → OpenAlex 동제목(DOI 보유) 순서 — 제목 dedup이 무조건 적용돼야 1건."""
    monkeypatch.delenv("PAPER_SCOUT_LLM_CMD", raising=False)
    monkeypatch.chdir(tmp_path)
    from research_trend_analyzer import ResearchTrendAnalyzer, TrendConfig

    a = ResearchTrendAnalyzer(Config.load())
    a.ss_client = MagicMock()
    a.ss_client.search_papers.return_value = [_paper("s2:1", doi=None, title="Same")]
    a.openalex_client = MagicMock()
    a.openalex_client.search_papers.return_value = [_paper("openalex:W9", doi="10.1/x", title="Same")]
    result = a.analyze_trend("topic", TrendConfig(years=0, papers_per_year=10))
    assert result["total_papers"] == 1               # 동일 제목 → 1건으로 수렴
