"""
Unified Cache Manager

통합 캐시 관리 시스템
- 다중 API 네임스페이스 지원
- 통일된 TTL 및 정리 정책
- 캐시 통계 및 모니터링
"""

import json
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Optional, Dict, List
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)


@dataclass
class CacheConfig:
    """캐시 설정 — 기본 base_dir은 ~/.cache/paper-scout/kci.

    litdisc 쪽 utils/cache.py CacheManager(~/.cache/paper-scout 루트)와 디렉토리
    수준에서 격리한다: ttl_overrides에 crossref/semantic_scholar 등 litdisc와 겹치는
    네임스페이스 키가 선언돼 있어, 루트를 공유하면 같은 디렉토리에 서로 다른 JSON
    스키마(CacheManager vs UnifiedCacheManager)가 쓰일 수 있기 때문.
    UnifiedCacheManager가 namespace를 하위 폴더로 덧붙인다 (예: .../kci/kci)."""
    base_dir: str = field(default_factory=lambda: str(Path.home() / ".cache" / "paper-scout" / "kci"))
    default_ttl_days: int = 7
    max_size_mb: int = 500  # 최대 캐시 크기

    # API별 TTL 설정
    ttl_overrides: Dict[str, int] = None

    def __post_init__(self):
        if self.ttl_overrides is None:
            self.ttl_overrides = {
                "crossref": 7,
                "kci": 7,
                "datacite": 7,
                "semantic_scholar": 7,
                "arxiv": 3,  # arXiv는 더 자주 업데이트
                "eric": 14,  # ERIC은 변경 적음
                "opencitations": 7,
            }


class UnifiedCacheManager:
    """
    통합 캐시 관리자

    모든 API 클라이언트가 공유하는 중앙 캐시 시스템
    """

    def __init__(
        self,
        namespace: str,
        ttl_days: Optional[int] = None,
        config: Optional[CacheConfig] = None
    ):
        """
        Args:
            namespace: 캐시 네임스페이스 (API 이름)
            ttl_days: TTL 일 수 (None이면 config에서 결정)
            config: 캐시 설정
        """
        self.namespace = namespace
        self.config = config or CacheConfig()

        # TTL 결정: 명시적 지정 > config override > default
        if ttl_days is not None:
            self.ttl = timedelta(days=ttl_days)
        elif namespace in self.config.ttl_overrides:
            self.ttl = timedelta(days=self.config.ttl_overrides[namespace])
        else:
            self.ttl = timedelta(days=self.config.default_ttl_days)

        # 캐시 디렉토리 설정
        self.cache_dir = Path(self.config.base_dir) / namespace
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        logger.debug(f"UnifiedCacheManager initialized: {namespace} (TTL: {self.ttl.days}d)")

    def _get_cache_key(self, query: str) -> str:
        """쿼리를 캐시 키(파일명)로 변환"""
        return hashlib.md5(query.encode("utf-8")).hexdigest()

    def _get_cache_path(self, query: str) -> Path:
        """캐시 파일 경로 반환"""
        key = self._get_cache_key(query)
        return self.cache_dir / f"{key}.json"

    def get(self, query: str) -> Optional[Any]:
        """
        캐시에서 데이터 조회

        Args:
            query: 캐시 키

        Returns:
            캐시된 데이터 또는 None
        """
        cache_path = self._get_cache_path(query)

        if not cache_path.exists():
            return None

        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache_data = json.load(f)

            # TTL 확인
            cached_at = datetime.fromisoformat(cache_data.get("cached_at", ""))
            if datetime.now() - cached_at > self.ttl:
                cache_path.unlink()
                return None

            return cache_data.get("data")

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Cache read error ({self.namespace}): {e}")
            return None

    def set(self, query: str, data: Any) -> bool:
        """
        캐시에 데이터 저장

        Args:
            query: 캐시 키
            data: 저장할 데이터

        Returns:
            저장 성공 여부
        """
        cache_path = self._get_cache_path(query)

        try:
            cache_data = {
                "namespace": self.namespace,
                "query": query[:200],  # 쿼리는 일부만 저장
                "cached_at": datetime.now().isoformat(),
                "ttl_days": self.ttl.days,
                "data": data
            }

            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)

            return True

        except (TypeError, IOError) as e:
            logger.warning(f"Cache write error ({self.namespace}): {e}")
            return False

    def invalidate(self, query: Optional[str] = None) -> int:
        """
        캐시 무효화

        Args:
            query: 특정 쿼리 (None이면 전체)

        Returns:
            삭제된 파일 수
        """
        count = 0

        if query:
            cache_path = self._get_cache_path(query)
            if cache_path.exists():
                cache_path.unlink()
                count = 1
        else:
            for cache_file in self.cache_dir.glob("*.json"):
                cache_file.unlink()
                count += 1

        logger.info(f"Invalidated {count} cache files in {self.namespace}")
        return count

    def cleanup_expired(self) -> int:
        """만료된 캐시 정리"""
        count = 0
        now = datetime.now()

        for cache_file in self.cache_dir.glob("*.json"):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cache_data = json.load(f)

                cached_at = datetime.fromisoformat(cache_data.get("cached_at", ""))
                ttl_days = cache_data.get("ttl_days", self.ttl.days)

                if now - cached_at > timedelta(days=ttl_days):
                    cache_file.unlink()
                    count += 1

            except (json.JSONDecodeError, KeyError, ValueError):
                cache_file.unlink()
                count += 1

        if count > 0:
            logger.info(f"Cleaned up {count} expired caches in {self.namespace}")

        return count

    def get_stats(self) -> Dict[str, Any]:
        """캐시 통계"""
        total_files = 0
        total_size = 0
        expired_count = 0
        now = datetime.now()

        for cache_file in self.cache_dir.glob("*.json"):
            total_files += 1
            total_size += cache_file.stat().st_size

            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cache_data = json.load(f)
                cached_at = datetime.fromisoformat(cache_data.get("cached_at", ""))
                ttl_days = cache_data.get("ttl_days", self.ttl.days)
                if now - cached_at > timedelta(days=ttl_days):
                    expired_count += 1
            except (json.JSONDecodeError, KeyError, ValueError, OSError):
                expired_count += 1

        return {
            "namespace": self.namespace,
            "total_files": total_files,
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "expired_count": expired_count,
            "ttl_days": self.ttl.days,
        }


class CacheRegistry:
    """
    캐시 레지스트리

    모든 네임스페이스의 캐시를 중앙에서 관리
    """

    _instances: Dict[str, UnifiedCacheManager] = {}
    _config: CacheConfig = None

    @classmethod
    def configure(cls, config: CacheConfig):
        """전역 설정"""
        cls._config = config

    @classmethod
    def get(cls, namespace: str) -> UnifiedCacheManager:
        """네임스페이스별 캐시 매니저 획득"""
        if namespace not in cls._instances:
            cls._instances[namespace] = UnifiedCacheManager(
                namespace=namespace,
                config=cls._config
            )
        return cls._instances[namespace]

    @classmethod
    def cleanup_all(cls) -> Dict[str, int]:
        """모든 캐시 정리"""
        results = {}
        for namespace, manager in cls._instances.items():
            results[namespace] = manager.cleanup_expired()
        return results

    @classmethod
    def get_all_stats(cls) -> List[Dict[str, Any]]:
        """모든 캐시 통계"""
        return [manager.get_stats() for manager in cls._instances.values()]

    @classmethod
    def total_size_mb(cls) -> float:
        """전체 캐시 크기 (MB)"""
        total = 0
        for manager in cls._instances.values():
            stats = manager.get_stats()
            total += stats["total_size_bytes"]
        return round(total / (1024 * 1024), 2)
