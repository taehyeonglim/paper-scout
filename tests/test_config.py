import importlib
import os
from pathlib import Path

import pytest

from config import Config


def test_default_output_dir_is_cwd_based(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PAPER_SCOUT_OUTPUT_DIR", raising=False)
    cfg = Config.load()
    assert cfg.output_dir == tmp_path / "literature-discovery"
    assert "NERV" not in str(cfg.output_dir)


def test_env_override_output_dir(tmp_path, monkeypatch):
    custom = tmp_path / "custom-out"
    monkeypatch.setenv("PAPER_SCOUT_OUTPUT_DIR", str(custom))
    cfg = Config.load()
    assert cfg.output_dir == custom


def test_cache_dir_under_home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Config.load()
    assert cfg.cache_dir == Path.home() / ".cache" / "paper-scout"


def test_missing_keys_are_none(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for var in ("SEMANTIC_SCHOLAR_API_KEY", "OPENCITATIONS_API_TOKEN", "KCI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    cfg = Config.load()
    assert cfg.semantic_scholar_api_key is None
    assert cfg.kci_api_key is None


def test_kci_cache_manager_default_under_home_cache(monkeypatch, tmp_path):
    """kci/cache_manager.py의 CacheConfig 기본 base_dir은 NERV VAULT_ROOT가 아닌
    ~/.cache/paper-scout/kci — litdisc CacheManager 루트(~/.cache/paper-scout)와
    디렉토리 수준에서 격리 (리뷰 Fix 1)."""
    monkeypatch.delenv("VAULT_ROOT", raising=False)
    from kci.cache_manager import CacheConfig

    config = CacheConfig()
    assert Path(config.base_dir) == Path.home() / ".cache" / "paper-scout" / "kci"
    assert "NERV" not in config.base_dir


def test_litdisc_cache_manager_default_under_home_cache():
    """utils/cache.py CacheManager의 기본 base가 ~/.cache/paper-scout 하위인지
    직접 검증 (리뷰 Fix 2 — 확장 파일 커버리지 갭)."""
    from utils.cache import CacheManager

    cm = CacheManager("arxiv")
    assert cm.cache_dir == Path.home() / ".cache" / "paper-scout" / "arxiv"


def test_two_cache_systems_are_directory_isolated():
    """리뷰 Fix 1 격리 단언: 같은 namespace 문자열을 써도 litdisc CacheManager와
    kci UnifiedCacheManager의 실제 캐시 디렉토리가 달라야 한다. CacheConfig의
    ttl_overrides에 crossref/semantic_scholar 등 겹치는 키가 이미 선언돼 있어,
    네임스페이스 문자열 관례에만 의존하면 향후 확장 시 같은 디렉토리에 서로 다른
    JSON 스키마가 쓰이는 클래스를 디렉토리 수준에서 구조적으로 차단."""
    from utils.cache import CacheManager
    from kci.cache_manager import UnifiedCacheManager

    litdisc = CacheManager("semantic_scholar")
    kci_side = UnifiedCacheManager("semantic_scholar")

    assert litdisc.cache_dir != kci_side.cache_dir
    kci_base = Path.home() / ".cache" / "paper-scout" / "kci"
    assert kci_base in kci_side.cache_dir.parents


def test_kci_unified_cache_default_dir_under_kci_base():
    """kci UnifiedCacheManager("kci")의 실제 cache_dir이
    ~/.cache/paper-scout/kci 하위인지 (리뷰 Fix 2)."""
    from kci.cache_manager import UnifiedCacheManager

    ucm = UnifiedCacheManager("kci")
    assert ucm.cache_dir == Path.home() / ".cache" / "paper-scout" / "kci" / "kci"


@pytest.mark.parametrize("module_name", ["kci.kci_journal", "kci.kci_cited_by"])
def test_load_env_reads_cwd_dotenv(module_name, tmp_path, monkeypatch):
    """kci CLI 2종의 _load_env()가 (구 VAULT_ROOT/.env가 아닌) cwd의 .env에서
    KCI_API_KEY를 실제로 로드하는지 — mock 없이 실동작 검증 (리뷰 Fix 2)."""
    monkeypatch.chdir(tmp_path)
    # setenv로 원래 상태를 monkeypatch 복원 스택에 기록한 뒤 delenv — _load_env가
    # os.environ을 직접 변경해도 테스트 종료 시 원상복구 보장.
    monkeypatch.setenv("KCI_API_KEY", "to-be-removed")
    monkeypatch.delenv("KCI_API_KEY")
    (tmp_path / ".env").write_text("KCI_API_KEY=from-cwd-env\n", encoding="utf-8")

    mod = importlib.import_module(module_name)
    mod._load_env()

    assert os.environ.get("KCI_API_KEY") == "from-cwd-env"


def test_discovery_packet_default_path_is_not_vault_shaped(tmp_path, monkeypatch):
    """related_paper_finder.generate_discovery_packet()의 미지정 시 기본 경로가
    (config.base_dir이 VAULT_ROOT였을 때의 잔재인) "Research/.shared/discovery_packet"가
    아니라 config.output_dir 하위(cwd 기반)여야 한다. grep VAULT_ROOT로는 안 잡히는
    NERV-vault-shaped 하드코딩 문자열 잔재 회귀 방지용."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PAPER_SCOUT_OUTPUT_DIR", raising=False)

    from related_paper_finder import RelatedPaperFinder
    from utils.paper_models import Paper

    cfg = Config.load()
    finder = RelatedPaperFinder(cfg)
    seed = Paper(paper_id="seed1", title="Seed Paper")

    packet_path = finder.generate_discovery_packet(
        seed_paper=seed,
        highly_relevant=[],
        moderately_relevant=[],
        search_query="test query",
    )

    assert "Research" not in str(packet_path)
    assert str(packet_path).startswith(str(cfg.output_dir))
