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
