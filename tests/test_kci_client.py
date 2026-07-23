"""KCIClient(litdisc 어댑터) + PaperFetcher KCI 게이팅 단위 테스트.

Paper 변환 매핑, KCI_API_KEY 미설정 시 fail-soft, PaperFetcher의 kci_client
활성화 게이팅, 한글 제목 정규화/중복 제거 회귀, KCI 분기 한국어 쿼리 우선
공급을 검증한다. 원본: NERV literature_discovery 팀의
tests/test_kci_search_client.py — 네트워크 실호출 없이(mock 또는 무키
fail-soft 경로만) 전량 이식.
"""
from unittest.mock import MagicMock, patch

from modules.paper_fetcher import PaperFetcher
from utils.api_clients import KCIClient, OpenAlexClient
from utils.paper_models import Paper

SAMPLE_ARTICLE = {
    "article_id": "ART002744134",
    "title": "메타버스를 활용한 고등학생 진로체험 프로그램 사용자 경험 분석",
    "title_en": "An Analysis of User Experience of High School Career Program in Metaverse",
    "authors": [
        {"name": "임태형", "name_en": "Lim, Taehyeong"},
        {"name": "", "name_en": "Ryu, Jeeheon"},
    ],
    "year": 2021,
    "journal": "학습자중심교과교육연구",
    "doi": "",
    "url": "https://www.kci.go.kr/kciportal/ci/sereArticleSearch/ciSereArtiView.kci?sereArticleSearchBean.artiId=ART002744134",
    "abstract": "본 연구는 메타버스를 활용한 진로체험 프로그램의 사용자 경험을 분석하였다.",
    "categories": "사회과학 > 교육학",
    "citation_count_kci": 3,
    "citation_count_wos": 0,
}

_OTHERS_OFF = {
    "semantic_scholar": {"enabled": False},
    "arxiv": {"enabled": False},
    "eric": {"enabled": False},
}


# --- KCIClient._to_paper 매핑 ---

def test_to_paper_full_mapping():
    paper = KCIClient._to_paper(SAMPLE_ARTICLE)
    assert paper.paper_id == "kci:ART002744134"
    assert paper.source_db == "kci"
    assert paper.year == 2021
    assert paper.venue == "학습자중심교과교육연구"
    assert paper.citation_count == 3
    # "사회과학 > 교육학" 계층 분해
    assert paper.fields_of_study == ["사회과학", "교육학"]
    # 빈 DOI는 None (litdisc dedup이 falsy 체크에 의존)
    assert paper.doi is None
    assert paper.authors[0].name == "임태형"
    # 한국어 이름이 없으면 영문명 폴백
    assert paper.authors[1].name == "Ryu, Jeeheon"
    assert paper.abstract


def test_search_papers_maps_year_range():
    client = KCIClient.__new__(KCIClient)
    core = MagicMock()
    core.search_articles.return_value = [SAMPLE_ARTICLE]
    client._core = core

    papers = client.search_papers("메타버스", limit=5, year_range=(2021, 2024))

    core.search_articles.assert_called_once_with(
        title="메타버스",
        date_from="202101",
        date_to="202412",
        limit=5,
        force_refresh=False,
    )
    assert len(papers) == 1
    assert papers[0].source_db == "kci"


# --- fail-soft (KCI_API_KEY 없음 / 코어 비활성) ---

def test_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("KCI_API_KEY", raising=False)
    client = KCIClient()
    assert client.available is False
    assert client.search_papers("메타버스") == []


def test_inactive_core_returns_empty():
    client = KCIClient.__new__(KCIClient)
    client._core = None
    assert client.available is False
    assert client.search_papers("메타버스") == []


# --- PaperFetcher의 kci_client 활성화 게이팅 ---

def test_kci_skipped_without_key(monkeypatch):
    monkeypatch.delenv("KCI_API_KEY", raising=False)
    fetcher = PaperFetcher({"sources": {**_OTHERS_OFF, "kci": {"enabled": True}}})
    assert fetcher.kci_client is None


def test_kci_enabled_with_key(monkeypatch):
    mock_client = MagicMock()
    mock_client.available = True
    monkeypatch.setenv("KCI_API_KEY", "00000000")
    with patch("modules.paper_fetcher.KCIClient", return_value=mock_client):
        fetcher = PaperFetcher({"sources": {**_OTHERS_OFF, "kci": {"enabled": True}}})
    assert fetcher.kci_client is mock_client


def test_kci_respects_enabled_false(monkeypatch):
    monkeypatch.setenv("KCI_API_KEY", "00000000")
    fetcher = PaperFetcher({"sources": {**_OTHERS_OFF, "kci": {"enabled": False}}})
    assert fetcher.kci_client is None


# --- PaperFetcher의 openalex_client 활성화 게이팅 (KCI와 달리 무키도 생성) ---

def test_openalex_enabled_without_key_by_default(monkeypatch):
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    fetcher = PaperFetcher({"sources": {**_OTHERS_OFF}})
    assert isinstance(fetcher.openalex_client, OpenAlexClient)


def test_openalex_respects_enabled_false(monkeypatch):
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    fetcher = PaperFetcher({"sources": {**_OTHERS_OFF, "openalex": {"enabled": False}}})
    assert fetcher.openalex_client is None


# --- 한글 제목 정규화/중복 제거 회귀 ---
# 순한글 제목이 ASCII 전용 정규화로 ""가 되어 중복 오판되던 회귀 방지
# (\w는 유니코드 단어문자라 한글 제목이 빈 문자열로 붕괴하지 않아야 한다).

def test_normalize_preserves_korean():
    n1 = PaperFetcher._normalize_title("메타버스 학습환경 연구")
    n2 = PaperFetcher._normalize_title("아바타 상호작용 효과")
    assert n1
    assert n2
    assert n1 != n2


def test_distinct_korean_papers_not_collapsed():
    fetcher = PaperFetcher.__new__(PaperFetcher)
    papers = [
        Paper(paper_id=f"kci:A{i}", title=t)
        for i, t in enumerate([
            "메타버스 학습환경 연구",
            "아바타 상호작용 효과",
            "가상현실 실재감 분석",
        ])
    ]
    assert len(fetcher._deduplicate_candidates(papers)) == 3


def test_same_korean_title_still_deduped():
    fetcher = PaperFetcher.__new__(PaperFetcher)
    papers = [
        Paper(paper_id="kci:A", title="메타버스 학습환경 연구"),
        Paper(paper_id="kci:B", title="메타버스 학습환경 연구!"),
    ]
    assert len(fetcher._deduplicate_candidates(papers)) == 1


# --- KCI 분기 한국어 쿼리 우선 공급 (영어 쿼리는 KCI 리콜 0 실측) ---

def _fetcher_with_mock_kci():
    mock_kci = MagicMock()
    mock_kci.search_papers.return_value = []
    fetcher = PaperFetcher.__new__(PaperFetcher)
    fetcher.config = {}
    fetcher.s2_client = None
    fetcher.arxiv_client = None
    fetcher.eric_client = None
    fetcher.kci_client = mock_kci
    fetcher.openalex_client = None
    fetcher.s2_config = {}
    fetcher.arxiv_config = {}
    fetcher.eric_config = {}
    fetcher.kci_config = {"queries_per_project": 2, "results_per_query": 5}
    fetcher.openalex_config = {}
    return fetcher, mock_kci


def test_ko_queries_preferred_for_kci():
    fetcher, mock_kci = _fetcher_with_mock_kci()
    fetcher.fetch_for_project({
        "project_id": "t",
        "custom_queries": ["english query one", "english query two"],
        "custom_queries_ko": ["디지털 휴먼", "AI 아바타", "가상 교사"],
    })
    called = [c.kwargs["query"] for c in mock_kci.search_papers.call_args_list]
    # ko 쿼리만 사용 + queries_per_project=2 절단
    assert called == ["디지털 휴먼", "AI 아바타"]


def test_fallback_to_default_queries_without_ko():
    fetcher, mock_kci = _fetcher_with_mock_kci()
    fetcher.fetch_for_project({
        "project_id": "t",
        "custom_queries": ["english query one"],
    })
    called = [c.kwargs["query"] for c in mock_kci.search_papers.call_args_list]
    assert called == ["english query one"]


# --- fetch_for_project의 OpenAlex 검색 루프 (ERIC 미러 패턴) ---

def _fetcher_with_mock_openalex(openalex_result=None):
    mock_openalex = MagicMock()
    mock_openalex.search_papers.return_value = openalex_result or []
    fetcher = PaperFetcher.__new__(PaperFetcher)
    fetcher.config = {}
    fetcher.s2_client = None
    fetcher.arxiv_client = None
    fetcher.eric_client = None
    fetcher.kci_client = None
    fetcher.openalex_client = mock_openalex
    fetcher.s2_config = {}
    fetcher.arxiv_config = {}
    fetcher.eric_config = {}
    fetcher.kci_config = {}
    fetcher.openalex_config = {"queries_per_project": 2, "results_per_query": 5}
    return fetcher, mock_openalex


def test_openalex_queried_with_configured_limit_and_query_cap():
    fetcher, mock_openalex = _fetcher_with_mock_openalex()
    fetcher.fetch_for_project({
        "project_id": "t",
        "custom_queries": ["query one", "query two", "query three"],
    })
    calls = mock_openalex.search_papers.call_args_list
    # queries_per_project=2 절단
    assert [c.kwargs["query"] for c in calls] == ["query one", "query two"]
    assert all(c.kwargs["limit"] == 5 for c in calls)


def test_openalex_results_tagged_and_included():
    result = [Paper(paper_id="openalex:W1", title="OpenAlex hit")]
    fetcher, mock_openalex = _fetcher_with_mock_openalex(result)
    papers = fetcher.fetch_for_project({
        "project_id": "t",
        "custom_queries": ["query one"],
    })
    assert len(papers) == 1
    assert papers[0].source_db == "openalex"
