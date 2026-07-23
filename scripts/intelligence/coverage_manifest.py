"""커버리지 매니페스트 — 검색·제외 범위와 신뢰도를 정직하게 노출하는 결정론 신호.

LLM 호출 0. '이게 전부인가?'에 항상 신뢰 가능한 메타로 답한다.
"""
from typing import Dict, List, Optional

# 통합하지 않은 국내 소스 (KCI는 2026-07-22 인증키 발급으로 활성 —
# 미가용 런에서는 orchestrator가 excluded_sources로 동적 추가)
DEFAULT_EXCLUDED = ["RISS", "DBpia"]


def build_manifest(
    *,
    search_query: str,
    sources_queried: List[str],
    counts_per_source: Dict[str, int],
    year_range: Optional[str],
    total_retrieved: int,
    returned: int,
    rerank_mode: str,            # "llm" | "heuristic_fallback"
    rerank_dropped: int,
    influential_threshold: int,
    influential_count: int,
    excluded_sources: Optional[List[str]] = None,
) -> Dict:
    """검색 커버리지 매니페스트를 생성한다."""
    excluded = list(excluded_sources) if excluded_sources is not None else list(DEFAULT_EXCLUDED)

    # confidence: LLM rerank + 결과 충분 = high, fallback이면 강등
    if rerank_mode == "llm" and returned > 0:
        confidence = "high"
    elif returned > 0:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "search_query": search_query,
        "sources_queried": list(sources_queried),
        "counts_per_source": dict(counts_per_source),
        "year_range": year_range,
        "total_retrieved": total_retrieved,
        "returned": returned,
        "rerank_mode": rerank_mode,
        "rerank_dropped": rerank_dropped,
        "influential_threshold": influential_threshold,
        "influential_count": influential_count,
        "excluded_sources": excluded,
        "confidence": confidence,
    }
