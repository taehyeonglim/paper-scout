"""종합/통독 — 초록 나열 대신 주제별 종합·합의/불일치·연구 갭을 서술.

PAPER_SCOUT_LLM_CMD로 설정한 LLM CLI에 위임. 할루시네이션 가드: 인용된 id가 입력 검증셋에 없으면
flagged_uncited로 분리(ANTI_HALLUCINATION_POLICY 동일 적용). LLM 비가용 시 mode="unavailable" + 빈 결과.
"""
import json
import logging
from dataclasses import dataclass, field
from typing import List

from .llm_provider import call_llm
from utils.paper_models import Paper

logger = logging.getLogger(__name__)


@dataclass
class SynthesisResult:
    themes: List[str] = field(default_factory=list)
    consensus: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    cited_ids: List[str] = field(default_factory=list)        # 입력 검증셋에 존재하는 인용
    flagged_uncited: List[str] = field(default_factory=list)  # 환각 의심 (입력에 없음)
    mode: str = "unavailable"                                 # "llm" | "unavailable"


def _build_prompt(query: str, papers: List[Paper]) -> str:
    lines = []
    for p in papers:
        pid = p.doi or p.paper_id
        lines.append(f"- id={pid} | {p.title} ({p.year or 'n.d.'})\n    {(p.abstract or '')[:400]}")
    papers_text = "\n".join(lines)
    return f"""당신은 학술 문헌 종합 보조 AI다. 아래 논문들(이미 주제 적합성 검증됨)을 통독하고 종합하라. 초록을 나열하지 말고, 주제별로 묶어 합의·불일치·연구 갭을 서술하라. 출력은 질의와 같은 언어로 작성하라.

## 검색 주제
{query}

## 논문 (id | 제목 | 초록)
{papers_text}

## 출력 (반드시 유효한 JSON만, 코드블록 없이)
{{
  "themes": ["주제 묶음 3-6개"],
  "consensus": ["문헌의 합의점 1-3개"],
  "conflicts": ["불일치/긴장 1-3개"],
  "gaps": ["미탐구 연구 갭 2-4개"],
  "limitations": ["이 문헌집합의 한계 1-3개"],
  "cited_ids": ["위 논문 목록에서 실제로 근거로 삼은 id만 (그대로 복사)"]
}}
주의: 위 목록에 없는 논문/DOI를 새로 만들지 마라."""


def synthesize(query: str, papers: List[Paper], timeout: int = 120) -> SynthesisResult:
    """on-topic 논문들을 종합한다. 실패 시 빈 결과(mode=unavailable)."""
    if not papers:
        return SynthesisResult(mode="unavailable")

    parsed = _call_llm_json(query, papers, timeout)
    if parsed is None:
        return SynthesisResult(mode="unavailable")

    valid_ids = {(p.doi or p.paper_id) for p in papers}
    cited = parsed.get("cited_ids", []) or []
    cited_ok = [c for c in cited if c in valid_ids]
    flagged = [c for c in cited if c not in valid_ids]  # 할루시네이션 가드

    return SynthesisResult(
        themes=list(parsed.get("themes", []) or []),
        consensus=list(parsed.get("consensus", []) or []),
        conflicts=list(parsed.get("conflicts", []) or []),
        gaps=list(parsed.get("gaps", []) or []),
        limitations=list(parsed.get("limitations", []) or []),
        cited_ids=cited_ok,
        flagged_uncited=flagged,
        mode="llm",
    )


def _call_llm_json(query: str, papers: List[Paper], timeout: int):
    prompt = _build_prompt(query, papers)
    text = call_llm(prompt, timeout=timeout)
    if text is None:
        return None
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
