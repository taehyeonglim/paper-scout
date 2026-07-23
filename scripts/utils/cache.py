"""
Literature Discovery Team - Cache Manager

파일 기반 캐싱 유틸리티
"""

import json
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Optional, Dict
import logging

logger = logging.getLogger(__name__)


class CacheManager:
    """
    파일 기반 캐시 관리자

    API 응답을 로컬 파일로 캐싱하여 중복 호출을 방지합니다.
    """

    def __init__(
        self,
        namespace: str,
        cache_dir: str = "",
        ttl_days: int = 7
    ):
        """
        Args:
            namespace: 캐시 네임스페이스 (API별 구분)
            cache_dir: 캐시 디렉토리 경로 (기본: ~/.cache/paper-scout)
            ttl_days: 캐시 유효 기간 (일)
        """
        self.namespace = namespace
        resolved = Path(cache_dir) if cache_dir else Path.home() / ".cache" / "paper-scout"
        self.cache_dir = resolved / namespace
        self.ttl = timedelta(days=ttl_days)

        # 캐시 디렉토리 생성
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_cache_key(self, query: str) -> str:
        """쿼리를 캐시 키(파일명)로 변환"""
        # MD5 해시로 파일명 생성 (특수문자 방지)
        return hashlib.md5(query.encode("utf-8")).hexdigest()

    def _get_cache_path(self, query: str) -> Path:
        """캐시 파일 경로 반환"""
        key = self._get_cache_key(query)
        return self.cache_dir / f"{key}.json"

    def get(self, query: str) -> Optional[Any]:
        """
        캐시에서 데이터 조회

        Args:
            query: 검색 쿼리 또는 식별자

        Returns:
            캐시된 데이터 또는 None (캐시 미스 시)
        """
        cache_path = self._get_cache_path(query)

        if not cache_path.exists():
            logger.debug(f"Cache miss: {query[:50]}...")
            return None

        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache_data = json.load(f)

            # TTL 확인
            cached_at = datetime.fromisoformat(cache_data.get("cached_at", ""))
            if datetime.now() - cached_at > self.ttl:
                logger.debug(f"Cache expired: {query[:50]}...")
                cache_path.unlink()  # 만료된 캐시 삭제
                return None

            logger.debug(f"Cache hit: {query[:50]}...")
            return cache_data.get("data")

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Cache read error: {e}")
            return None

    def set(self, query: str, data: Any) -> bool:
        """
        캐시에 데이터 저장

        Args:
            query: 검색 쿼리 또는 식별자
            data: 저장할 데이터 (JSON 직렬화 가능해야 함)

        Returns:
            저장 성공 여부
        """
        cache_path = self._get_cache_path(query)

        try:
            cache_data = {
                "query": query,
                "cached_at": datetime.now().isoformat(),
                "data": data
            }

            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)

            logger.debug(f"Cached: {query[:50]}...")
            return True

        except (TypeError, IOError) as e:
            logger.warning(f"Cache write error: {e}")
            return False

    def invalidate(self, query: Optional[str] = None) -> int:
        """
        캐시 무효화

        Args:
            query: 특정 쿼리의 캐시 삭제 (None이면 전체 삭제)

        Returns:
            삭제된 캐시 파일 수
        """
        count = 0

        if query:
            # 특정 쿼리 캐시 삭제
            cache_path = self._get_cache_path(query)
            if cache_path.exists():
                cache_path.unlink()
                count = 1
                logger.info(f"Invalidated cache: {query[:50]}...")
        else:
            # 전체 캐시 삭제
            for cache_file in self.cache_dir.glob("*.json"):
                cache_file.unlink()
                count += 1
            logger.info(f"Invalidated all caches in {self.namespace}: {count} files")

        return count

    def cleanup_expired(self) -> int:
        """
        만료된 캐시 정리

        Returns:
            삭제된 캐시 파일 수
        """
        count = 0
        now = datetime.now()

        for cache_file in self.cache_dir.glob("*.json"):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cache_data = json.load(f)

                cached_at = datetime.fromisoformat(cache_data.get("cached_at", ""))
                if now - cached_at > self.ttl:
                    cache_file.unlink()
                    count += 1

            except (json.JSONDecodeError, KeyError, ValueError):
                # 손상된 캐시 파일도 삭제
                cache_file.unlink()
                count += 1

        if count > 0:
            logger.info(f"Cleaned up {count} expired caches in {self.namespace}")

        return count

    def get_stats(self) -> Dict[str, Any]:
        """캐시 통계 조회"""
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
                if now - cached_at > self.ttl:
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


class CachedAPIClient:
    """
    캐시 기능이 포함된 API 클라이언트 베이스 클래스

    상속받아 사용:
    ```python
    class MyAPIClient(CachedAPIClient):
        def __init__(self):
            super().__init__(namespace="my_api")

        def search(self, query):
            return self._cached_call(
                cache_key=f"search:{query}",
                api_call=lambda: self._actual_search(query)
            )
    ```
    """

    def __init__(self, namespace: str, cache_ttl_days: int = 7):
        self.cache = CacheManager(namespace=namespace, ttl_days=cache_ttl_days)

    def _cached_call(
        self,
        cache_key: str,
        api_call: callable,
        force_refresh: bool = False
    ) -> Any:
        """
        캐시를 활용한 API 호출

        Args:
            cache_key: 캐시 키
            api_call: 실제 API 호출 함수 (람다 또는 callable)
            force_refresh: True면 캐시 무시하고 새로 호출

        Returns:
            API 응답 데이터
        """
        # 캐시 확인 (강제 새로고침이 아닌 경우)
        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        # API 호출
        result = api_call()

        # 캐시 저장
        if result is not None:
            self.cache.set(cache_key, result)

        return result
