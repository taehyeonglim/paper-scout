import sys
from pathlib import Path

from intelligence.llm_provider import call_llm
from intelligence.synthesize import synthesize
from intelligence.semantic_rerank import rerank
from utils.paper_models import Paper

FAKE = f"{sys.executable} {Path(__file__).parent / 'fake_llm.py'}"
FAKE_RERANK = f"{sys.executable} {Path(__file__).parent / 'fake_llm_rerank.py'}"


def _paper():
    return Paper(paper_id="x", title="T", doi="10.1234/x")


def test_call_llm_none_when_unset(monkeypatch):
    monkeypatch.delenv("PAPER_SCOUT_LLM_CMD", raising=False)
    assert call_llm("hi") is None


def test_call_llm_none_when_cmd_missing(monkeypatch):
    monkeypatch.setenv("PAPER_SCOUT_LLM_CMD", "no-such-binary-xyz")
    assert call_llm("hi") is None


def test_call_llm_none_when_cmd_malformed(monkeypatch):
    monkeypatch.setenv("PAPER_SCOUT_LLM_CMD", "echo 'unbalanced")
    assert call_llm("hi") is None


def test_synthesize_unavailable_without_llm(monkeypatch):
    monkeypatch.delenv("PAPER_SCOUT_LLM_CMD", raising=False)
    assert synthesize("q", [_paper()]).mode == "unavailable"


def test_synthesize_with_fake_llm(monkeypatch):
    monkeypatch.setenv("PAPER_SCOUT_LLM_CMD", FAKE)
    result = synthesize("q", [_paper()])
    assert result.mode == "llm"
    assert result.themes == ["t1"]
    assert result.cited_ids == ["10.1234/x"]      # 검증셋에 존재
    assert result.flagged_uncited == []


def test_hallucination_guard(monkeypatch):
    monkeypatch.setenv("PAPER_SCOUT_LLM_CMD", FAKE)
    other = Paper(paper_id="y", title="T2", doi="10.9999/other")
    result = synthesize("q", [other])
    assert result.cited_ids == []
    assert result.flagged_uncited == ["10.1234/x"]  # 입력에 없는 인용 → 격리


def test_rerank_heuristic_fallback_without_llm(monkeypatch):
    monkeypatch.delenv("PAPER_SCOUT_LLM_CMD", raising=False)
    result = rerank("q", [_paper()], top_k=5)
    assert result.mode == "heuristic_fallback"
    assert result.dropped == 0


def test_rerank_with_fake_llm(monkeypatch):
    monkeypatch.setenv("PAPER_SCOUT_LLM_CMD", FAKE_RERANK)
    result = rerank("q", [_paper()], top_k=5)
    assert result.mode == "llm"
    assert len(result.papers) == 1
    assert result.papers[0].relevance_score == 0.9
    assert result.dropped == 0
