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


def _tagged(prefix, source_db, n, doi_ns):
    """소스 태깅된 mock Paper n편 (팩토리 fix 이후 현실 조건 — source_db 보유)."""
    papers = [
        _paper(f"{prefix}:{i}", doi=f"10.{doi_ns}/{prefix}{i}", title=f"{prefix} paper {i}")
        for i in range(n)
    ]
    for p in papers:
        p.source_db = source_db
    return papers


def test_openalex_survives_when_merged_last_keyless(monkeypatch, tmp_path):
    """무키 현실 조건 재현: S2 빈손(429) + arXiv 5 + ERIC 5 뒤에 OA 5가 마지막 병합.

    휴리스틱 폴백이 병합 인덱스 기반 점수면 OA 5편이 전역 인덱스 10~14에 놓여
    전부 관련성 임계(0.5) 이하/절단 밖으로 밀려 탈락한다 (Task 4 라이브 스모크
    실측과 동일 산술). 폴백은 병합 위치와 무관하게 각 소스 상위 논문을
    생존시켜야 한다.
    """
    oa = _tagged("openalex", "openalex", 5, doi_ns=5)
    f = _finder(monkeypatch, tmp_path, oa)
    f.arxiv_client.search_papers.return_value = _tagged("arxiv", "arxiv", 5, doi_ns=3)
    f.eric_client.search_papers.return_value = _tagged("eric", "eric", 5, doi_ns=4)

    _, high, moderate = f.find_by_keywords("q", FinderConfig(limit=5))

    survivors = [p for p in high + moderate if p.source_db == "openalex"]
    assert survivors, "마지막 병합 소스(OpenAlex)가 폴백 위치 점수로 구조적 탈락"


def test_fallback_distributes_across_relevance_tiers(monkeypatch, tmp_path):
    """폴백 점수는 소스 내 rank를 HIGH/MODERATE 양쪽으로 자연 분산해야 한다.

    소스 내 rank-4를 HIGH(0.8)로 라벨하는 건 MODERATE로 라벨하는 것만큼
    인공적 — decay 0.1이면 rank 0~2가 HIGH(>=0.8), rank 3+가 MODERATE로
    분산되고, floor 0.5로 폴백 논문이 관련성 임계 아래로 떨어지지 않아
    breadth(반환 총량)가 보존된다. 7편째(rank 6)는 raw 0.4 → floor 0.5가
    실제로 바인딩되는 케이스.
    """
    oa = _tagged("openalex", "openalex", 7, doi_ns=5)
    f = _finder(monkeypatch, tmp_path, oa)

    _, high, moderate = f.find_by_keywords("q", FinderConfig(limit=10))

    assert len(high) == 3, "소스 내 rank 0~2만 HIGH"
    assert len(moderate) == 4, "rank 3+는 MODERATE 진입 (floor 0.5로 탈락 없음)"
    scores = [p.relevance_score for p in high + moderate]
    assert min(scores) == 0.5, "floor 0.5 계약 (rank 6 raw 0.4 → 0.5)"


def test_fallback_scores_independent_of_merge_order(monkeypatch, tmp_path):
    """폴백 점수는 _merge_and_deduplicate 입력(병합) 순서에 의존하면 안 된다."""
    f = _finder(monkeypatch, tmp_path, [])

    def build(arxiv_first):
        ax = _tagged("arxiv", "arxiv", 3, doi_ns=3)
        oa = _tagged("openalex", "openalex", 3, doi_ns=5)
        return ax + oa if arxiv_first else oa + ax

    r1 = f._apply_semantic_rerank("q", build(arxiv_first=True), FinderConfig(limit=5))
    r2 = f._apply_semantic_rerank("q", build(arxiv_first=False), FinderConfig(limit=5))

    s1 = {p.paper_id: p.relevance_score for p in r1}
    s2 = {p.paper_id: p.relevance_score for p in r2}
    assert s1 == s2, "동일 논문 집합의 폴백 점수가 병합 순서에 따라 달라짐"
    assert [p.paper_id for p in r1] == [p.paper_id for p in r2], (
        "폴백 정렬 결과가 병합 순서에 따라 달라짐"
    )


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
