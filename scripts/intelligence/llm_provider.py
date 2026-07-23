"""Provider-pluggable LLM 호출 — PAPER_SCOUT_LLM_CMD에 설정된 CLI로 위임.

prompt는 stdin으로 전달. 미설정/실행 실패/타임아웃/비정상 종료 전부 None
(호출측이 fail-soft 처리). 특정 벤더 하드코딩 금지.
"""
import logging
import os
import shlex
import subprocess

logger = logging.getLogger(__name__)


def call_llm(prompt: str, timeout: int = 120) -> "str | None":
    cmd = os.environ.get("PAPER_SCOUT_LLM_CMD", "").strip()
    if not cmd:
        return None
    try:
        result = subprocess.run(
            shlex.split(cmd), input=prompt,
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError) as e:
        logger.warning(f"LLM provider unavailable: {e}")
        return None
    if result.returncode != 0:
        logger.warning(f"LLM provider failed (exit {result.returncode}): {result.stderr[:200]}")
        return None
    return result.stdout
