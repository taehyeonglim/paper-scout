"""citation-network 파이프라인 PageRank/Betweenness 크래시 회귀 테스트.

배경 (final-review C1): `orchestrator.py citation-network` → CitationNetworkExplorer.
analyze_network() → nx.pagerank()가 networkx>=3.0에서 항상 scipy 경유
(_pagerank_scipy, 실물 확인: pagerank_alg.py:110)로 디스패치되는데
requirements.txt에 scipy/numpy가 없어 클린 venv에서 ModuleNotFoundError로
크래시(exit 1)한다. `_calculate_pagerank`/`_calculate_betweenness`의 except
튜플 (NetworkXException, ValueError, ZeroDivisionError)도 ImportError 서브클래스인
ModuleNotFoundError를 잡지 못해 fail-soft 없이 그대로 전파된다.

네트워크 API(SemanticScholarClient/OpenCitationsClient) 호출 없이, 실제
`explorer.graph`/`explorer.papers`에 소형 그래프를 직접 주입해
`analyze_network()`가 소비하는 최소 형상으로 재현한다 — build_network()는
외부 HTTP 호출을 하므로 이 테스트 범위 밖.
"""
import networkx as nx

from citation_network_explorer import CitationNetworkExplorer
from config import Config
from utils.paper_models import Paper


def _make_explorer() -> CitationNetworkExplorer:
    config = Config.load()
    return CitationNetworkExplorer(config)


def _small_graph_explorer() -> CitationNetworkExplorer:
    """5-노드 인용 네트워크(순환 + 코드 엣지)를 API 호출 없이 직접 주입."""
    explorer = _make_explorer()

    graph = nx.DiGraph()
    node_ids = ["p1", "p2", "p3", "p4", "p5"]
    papers = {
        pid: Paper(paper_id=pid, title=f"Test Paper {pid}", year=2020 + i, citation_count=i)
        for i, pid in enumerate(node_ids)
    }
    for pid, paper in papers.items():
        graph.add_node(pid, title=paper.title, year=paper.year, citations=paper.citation_count)

    # 순환 엣지 + 코드(chord) 1개 — betweenness 편차를 만들기 위함
    graph.add_edge("p1", "p2")
    graph.add_edge("p2", "p3")
    graph.add_edge("p3", "p4")
    graph.add_edge("p4", "p5")
    graph.add_edge("p5", "p1")
    graph.add_edge("p1", "p3")

    explorer.graph = graph
    explorer.papers = papers
    return explorer


def test_analyze_network_pagerank_smoke():
    """analyze_network()가 예외 없이 PageRank/Betweenness를 포함해 결과를 낸다.

    C1 재현: networkx>=3.0 pagerank()는 scipy 경유로만 동작 —
    scipy(및 동반 numpy) 부재 시 ModuleNotFoundError로 여기서 죽는다.
    """
    explorer = _small_graph_explorer()

    result = explorer.analyze_network()

    assert set(result.keys()) == {"statistics", "pagerank", "betweenness", "clusters"}

    pagerank = result["pagerank"]
    assert pagerank != {}, "PageRank가 비어있다 — fail-soft 강등(except 미스매치) 의심"
    assert len(pagerank) == 5
    assert abs(sum(pagerank.values()) - 1.0) < 1e-6

    betweenness = result["betweenness"]
    assert betweenness != {}
    assert len(betweenness) == 5


def test_get_key_papers_smoke():
    """get_key_papers()가 PageRank 계산 경로를 재사용하므로 함께 크래시 검증."""
    explorer = _small_graph_explorer()

    key_papers = explorer.get_key_papers(top_n=3)

    assert len(key_papers) == 3
    for paper in key_papers:
        assert paper.pagerank is not None
        assert paper.pagerank > 0


def test_pagerank_fails_soft_on_import_error(monkeypatch):
    """벨트-서스펜더: nx.pagerank가 (미래에) ImportError를 던져도 fail-soft로 강등.

    :192 except 튜플에 ImportError 추가 여부를 직접 고정 — scipy 설치
    유무와 무관하게 회귀를 잡는다 (monkeypatch로 원인 자체를 시뮬레이션).
    """
    explorer = _small_graph_explorer()

    def _boom(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'numpy'")

    monkeypatch.setattr(nx, "pagerank", _boom)

    assert explorer._calculate_pagerank() == {}


def test_betweenness_fails_soft_on_import_error(monkeypatch):
    """:199 except 튜플도 동일하게 ImportError를 fail-soft 처리해야 한다."""
    explorer = _small_graph_explorer()

    def _boom(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'numpy'")

    monkeypatch.setattr(nx, "betweenness_centrality", _boom)

    assert explorer._calculate_betweenness() == {}
