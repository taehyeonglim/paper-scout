"""소스 귀속(source_db) 팩토리 근본 부여 테스트.

배경 (Task 4 실측): coverage_manifest.counts_per_source가 전량 'unknown'으로
잡히던 결함 — S2/arXiv/ERIC 팩토리가 source_db를 설정하지 않았고,
related_paper_finder는 paper_fetcher와 달리 외부 태깅도 하지 않았다.
팩토리에서 직접 설정하면 전 호출자(캐시 복원 경로 포함)에 일괄 적용된다.
KCI(_to_paper)/OpenAlex(from_openalex)는 이미 설정하고 있어 대상 아님.
"""
from datetime import datetime
from types import SimpleNamespace

from utils.api_clients import ERICClient
from utils.paper_models import Paper


def test_from_semantic_scholar_sets_source_db():
    p = Paper.from_semantic_scholar({"paperId": "abc123", "title": "T"})
    assert p.source_db == "semantic_scholar"


def test_from_arxiv_sets_source_db():
    result = SimpleNamespace(
        authors=[SimpleNamespace(name="A. Author")],
        entry_id="http://arxiv.org/abs/2401.00001v1",
        title="T",
        published=datetime(2024, 1, 1),
        summary="S",
        doi=None,
        pdf_url=None,
        categories=["cs.AI"],
    )
    assert Paper.from_arxiv(result).source_db == "arxiv"


def test_eric_parse_sets_source_db():
    client = ERICClient.__new__(ERICClient)
    p = client._parse_eric_doc({"id": "EJ1", "title": "T"})
    assert p.source_db == "eric"


def test_eric_cache_restore_sets_source_db():
    # 팩토리 fix 이전에 기록된 stale 캐시(dict에 source_db 부재)도 eric으로 복원
    client = ERICClient.__new__(ERICClient)
    p = client._dict_to_paper({"paper_id": "eric:EJ1", "title": "T"})
    assert p.source_db == "eric"
