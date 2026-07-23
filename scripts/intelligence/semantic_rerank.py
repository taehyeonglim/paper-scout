"""의미 관련성 재채점 — 검색 순위/키워드 매칭을 LLM 의미 판단으로 교체.

PAPER_SCOUT_LLM_CMD로 설정한 LLM CLI에 위임. 실패 시 기존 점수로 fail-soft,
단 mode="heuristic_fallback"로 표면화한다 (조용한 강등 금지).
할루시네이션 가드: 반환 id가 입력 집합의 부분집합인지 검증, 신규 논문 추가 차단.
"""
import json
import logging
from dataclasses import dataclass, field
from typing import List

from .llm_provider import call_llm
from utils.paper_models import Paper

logger = logging.getLogger(__name__)


@dataclass
class RerankResult:
    papers: List[Paper]   # relevance_score/_reason 갱신, 내림차순 정렬, off-topic 제거
    mode: str             # "llm" | "heuristic_fallback"
    dropped: int          # off-topic 제거 수 (fallback이면 0)
    near_matches: List[Paper] = field(default_factory=list)  # off-topic이지만 LLM 평가를 거친 근접 후보, 점수 내림차순


def _build_prompt(query_intent: str, papers: List[Paper]) -> str:
    lines = []
    for i, p in enumerate(papers, 1):
        pid = p.doi or p.paper_id
        lines.append(
            f"[{i}] id={pid}\n"
            f"    title: {p.title or '(제목없음)'}\n"
            f"    year: {p.year or 'n.d.'}\n"
            f"    abstract: {(p.abstract or '')[:300]}"
        )
    candidates_text = "\n\n".join(lines)
    return f"""당신은 학술 문헌 탐색 보조 AI다. 아래 검색 의도와 후보 논문들을 보고, 각 논문이 **검색 의도의 실제 주제에 부합하는지** 의미적으로 판단하라. 키워드만 겹치고 주제가 다른 논문(예: 'esports' 검색에 일반 체육교육 논문)은 on_topic=false로 표시하라. 출력은 질의와 같은 언어로 작성하라.

## 검색 의도
{query_intent}

## 후보 논문
{candidates_text}

## 출력 (반드시 유효한 JSON만, 코드블록 없이)
{{
  "rankings": [
    {{"id": "위 후보의 id를 그대로 복사", "relevance": 0.0~1.0, "on_topic": true/false, "reason": "1문장"}}
  ]
}}
주의: 위 후보 목록에 없는 논문을 새로 만들지 마라. id는 반드시 위에서 그대로 복사하라."""


def rerank(query_intent: str, papers: List[Paper], top_k: int, timeout: int = 90) -> RerankResult:
    """의미 관련성으로 재채점하고 off-topic을 제거한 상위 top_k를 반환한다.

    주의: input papers 리스트의 Paper 객체가 in-place로 mutate됨 (relevance_score/_reason 갱신).
    이는 related_paper_finder 등 기존 호출자 관례와 동일.
    """
    if not papers:
        return RerankResult(papers=[], mode="heuristic_fallback", dropped=0)

    parsed = _call_llm_json(query_intent, papers, timeout)
    if parsed is None:
        ranked = sorted(papers, key=lambda p: p.relevance_score, reverse=True)
        return RerankResult(papers=ranked[:top_k], mode="heuristic_fallback", dropped=0)

    by_id = {(p.doi or p.paper_id): p for p in papers}
    valid_ids = set(by_id.keys())
    kept: List[Paper] = []
    near: List[Paper] = []
    dropped = 0
    for r in parsed.get("rankings", []):
        rid = r.get("id")
        if rid not in valid_ids:      # 할루시네이션 가드: 입력에 없는 id 무시
            continue
        paper = by_id[rid]
        paper.relevance_score = max(0.0, min(1.0, float(r.get("relevance", 0.0))))
        paper.relevance_reason = r.get("reason") or "semantic_match"
        if not r.get("on_topic", True):
            dropped += 1
            near.append(paper)        # off-topic이지만 LLM 평가 보존
            continue
        kept.append(paper)

    kept.sort(key=lambda p: p.relevance_score, reverse=True)
    near.sort(key=lambda p: p.relevance_score, reverse=True)
    return RerankResult(papers=kept[:top_k], mode="llm", dropped=dropped, near_matches=near[:5])


def _call_llm_json(query_intent: str, papers: List[Paper], timeout: int):
    """LLM 호출 + JSON 파싱. 실패 시 None (호출자가 fallback)."""
    prompt = _build_prompt(query_intent, papers)
    text = call_llm(prompt, timeout=timeout)
    if text is None:
        return None
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        logger.warning("LLM rerank: JSON not found in output")
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        logger.warning("LLM rerank: JSON parse failed")
        return None
