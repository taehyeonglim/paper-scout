"""Semantic Scholar 무키(keyless) 429 fail-soft 계약 테스트 (Task 8 Fix 1).

배경: 클린 환경 스모크에서 `orchestrator.py related-papers`가 3분+ 정지.
원인: 무키 공유 풀은 429를 즉시 반환하는데, semanticscholar 라이브러리
클라이언트가 기본 `retry=True`(내부 tenacity: ConnectionRefusedError에
wait_exponential(min=5, max=60) + stop_after_attempt(10) — 최악 7분+)로
생성되어 있어, 이미 갖춘 AdaptiveRateLimiter + REST 폴백 + 빈 리스트
fail-soft 방어선 앞에서 라이브러리 자체가 장시간 블로킹된다.

이 테스트는 세 가지를 계약으로 고정한다:
1. SemanticScholarClient가 생성하는 라이브러리 requester의 retry가
   항상 False (무키/유키 무관 — 이중 방어 제거).
2. 무키 상태에서 라이브러리 429 + REST 폴백도 429인 최악의 경로가
   예외 없이 수 초 내 빈 리스트로 fail-soft 하는지.
3. retry=False 경로의 실제 예외 타입인 tenacity.RetryError(원 429가
   last_attempt 안에 숨는 랩 예외)도 관통하지 않고 fail-soft + 무키
   힌트 warning까지 발화하는지 (2차 스모크 uncaught traceback 회귀).
"""
import logging
import time
from unittest.mock import MagicMock, patch

import requests
import tenacity

from utils.api_clients import SemanticScholarClient


def _retry_error_429() -> tenacity.RetryError:
    """retry=False 경로에서 라이브러리가 실제로 던지는 예외를 충실히 재현.

    semanticscholar ApiRequester는 retry=False여도
    `_get_data_async.retry_with(stop=stop_after_attempt(1))` 경유라
    (reraise 미설정) 실패를 tenacity.RetryError로 감싸 던진다 — 원 429
    예외(ConnectionRefusedError)는 last_attempt 안에 있고,
    str(RetryError)에는 "429"가 드러나지 않는다 (실물 확인:
    'RetryError[<Future at 0x... raised ConnectionRefusedError>]').
    """
    attempt = tenacity.Future(1)
    attempt.set_exception(
        ConnectionRefusedError("HTTP status 429 Too Many Requests.")
    )
    return tenacity.RetryError(attempt)


def test_semanticscholar_client_disables_library_retry(monkeypatch):
    """라이브러리 requester의 retry가 False로 고정되는지 (키 유무 무관)."""
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    client = SemanticScholarClient()

    # 실물 확인된 내부 속성 경로: SemanticScholar -> _AsyncSemanticScholar -> _requester.retry
    assert client.client._AsyncSemanticScholar._requester.retry is False


def test_semanticscholar_client_disables_library_retry_with_api_key(monkeypatch):
    """API 키가 있는 생성 경로도 동일하게 retry=False여야 한다."""
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "dummy-key")
    client = SemanticScholarClient()

    assert client.client._AsyncSemanticScholar._requester.retry is False


def test_search_papers_keyless_429_fails_soft_quickly(monkeypatch):
    """라이브러리 429 + REST 폴백도 429인 최악의 경로가 수 초 내 []로 종료.

    - client.client.search_paper: 라이브러리 내부가 429에서 만드는 원 예외
      ConnectionRefusedError(OSError 서브클래스, _API_EXCEPTIONS 소속) mock.
    - requests.get (REST 폴백): status 429 → raise_for_status()가
      requests.exceptions.HTTPError(_API_EXCEPTIONS 소속)를 던지는 mock.
    """
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    client = SemanticScholarClient()

    client.client.search_paper = MagicMock(
        side_effect=ConnectionRefusedError("HTTP status 429 Too Many Requests.")
    )

    fake_resp = MagicMock()
    fake_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
        "429 Client Error: Too Many Requests"
    )

    with patch("utils.api_clients.requests.get", return_value=fake_resp):
        start = time.monotonic()
        result = client.search_papers(
            "test-s2-failsoft-query-task8", force_refresh=True
        )
        elapsed = time.monotonic() - start

    assert result == []
    assert elapsed < 5.0, f"fail-soft path took {elapsed:.2f}s (expected < 5s)"


def test_search_papers_keyless_429_retryerror_fails_soft(monkeypatch, caplog):
    """실물 예외 타입(tenacity.RetryError 랩)도 관통하지 않고 fail-soft.

    2차 스모크 회귀: retry=False 이후 실제 무키 429는 RetryError로 도착하는데
    _API_EXCEPTIONS에 없어 uncaught traceback으로 비정상 종료했다. 이 테스트는
    (1) 예외 없이 [] 반환, (2) 수 초 내 종료, (3) RetryError의 str()에 "429"가
    없어도 last_attempt 내부 예외 검사로 무키 힌트 warning이 발화함을 고정한다.
    """
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    client = SemanticScholarClient()

    client.client.search_paper = MagicMock(side_effect=_retry_error_429())

    fake_resp = MagicMock()
    fake_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
        "429 Client Error: Too Many Requests"
    )

    with patch("utils.api_clients.requests.get", return_value=fake_resp):
        with caplog.at_level(logging.WARNING, logger="utils.api_clients"):
            start = time.monotonic()
            result = client.search_papers(
                "test-s2-retryerror-query-task8", force_refresh=True
            )
            elapsed = time.monotonic() - start

    assert result == []
    assert elapsed < 5.0, f"fail-soft path took {elapsed:.2f}s (expected < 5s)"
    assert any(
        "SEMANTIC_SCHOLAR_API_KEY" in record.message for record in caplog.records
    ), "keyless 429 hint warning did not fire on the RetryError path"
