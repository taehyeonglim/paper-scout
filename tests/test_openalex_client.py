"""OpenAlexClient 단위 테스트 — 전부 mock, 네트워크 무의존."""
import json
from unittest.mock import MagicMock

import pytest
import requests

from utils.api_clients import OpenAlexClient
from utils.paper_models import Paper, reconstruct_openalex_abstract

OA_WORK = {
    "id": "https://openalex.org/W2741809807",
    "display_name": "The state of OA",
    "publication_year": 2018,
    "doi": "https://doi.org/10.7717/peerj.4375",
    "cited_by_count": 1330,
    "authorships": [
        {"author": {"display_name": "Heather Piwowar"}},
        {"author": {"display_name": "Jason Priem"}},
    ],
    "primary_location": {"source": {"display_name": "PeerJ"}},
    "abstract_inverted_index": {"Despite": [0], "growth": [2], "the": [1]},
}


def _resp(status=200, results=None):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = {"results": results if results is not None else [OA_WORK]}
    m.raise_for_status.side_effect = (
        requests.HTTPError(f"{status}") if status >= 400 else None
    )
    return m


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("utils.cache.Path.home", lambda: tmp_path)  # 캐시 홈 격리
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    return OpenAlexClient()


def test_reconstruct_abstract_orders_by_position():
    assert reconstruct_openalex_abstract(
        {"Despite": [0], "growth": [2], "the": [1]}
    ) == "Despite the growth"


def test_reconstruct_abstract_none_and_empty():
    assert reconstruct_openalex_abstract(None) is None
    assert reconstruct_openalex_abstract({}) is None


def test_from_openalex_mapping():
    p = Paper.from_openalex(OA_WORK)
    assert p.paper_id == "openalex:W2741809807"
    assert p.doi == "10.7717/peerj.4375"          # https://doi.org/ 접두 제거
    assert p.title == "The state of OA"
    assert p.author_names == ["Heather Piwowar", "Jason Priem"]
    assert p.year == 2018 and p.venue == "PeerJ"
    assert p.citation_count == 1330
    assert p.abstract == "Despite the growth"
    assert p.source_db == "openalex"


def test_search_returns_papers(client, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _resp())
    papers = client.search_papers("open access")
    assert len(papers) == 1 and papers[0].doi == "10.7717/peerj.4375"


def test_keyless_still_calls_without_key_param(client, monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["params"] = params
        return _resp()

    monkeypatch.setattr(requests, "get", fake_get)
    client.search_papers("q", force_refresh=True)
    assert "api_key" not in captured["params"]     # 무키: 파라미터 없이 호출은 함


def test_keyed_passes_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr("utils.cache.Path.home", lambda: tmp_path)
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["params"] = params
        return _resp()

    monkeypatch.setattr(requests, "get", fake_get)
    OpenAlexClient(api_key="K123").search_papers("q", force_refresh=True)
    assert captured["params"]["api_key"] == "K123"


def test_quota_429_failsoft_with_one_hint(client, monkeypatch, caplog):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _resp(status=429))
    with caplog.at_level("WARNING", logger="utils.api_clients"):
        assert client.search_papers("q", force_refresh=True) == []
        assert client.search_papers("q2", force_refresh=True) == []
    hints = [r for r in caplog.records if "openalex.org/settings/api" in r.message]
    assert len(hints) == 1                          # 힌트는 1회만


def test_timeout_failsoft(client, monkeypatch):
    def boom(*a, **k):
        raise requests.Timeout("t")

    monkeypatch.setattr(requests, "get", boom)
    assert client.search_papers("q", force_refresh=True) == []


def test_malformed_json_failsoft(client, monkeypatch):
    m = _resp()
    m.json.side_effect = json.JSONDecodeError("x", "y", 0)
    monkeypatch.setattr(requests, "get", lambda *a, **k: m)
    assert client.search_papers("q", force_refresh=True) == []
