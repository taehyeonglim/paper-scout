"""
Literature Discovery Team - Configuration Module

환경변수 및 설정 로드
"""

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv


@dataclass
class Config:
    """애플리케이션 설정"""

    # API Keys
    semantic_scholar_api_key: Optional[str] = None
    opencitations_api_token: Optional[str] = None
    kci_api_key: Optional[str] = None

    # Paths — 사용자 작업 디렉토리 기준. PAPER_SCOUT_OUTPUT_DIR로 오버라이드 가능.
    base_dir: Path = field(default_factory=Path.cwd)
    output_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("PAPER_SCOUT_OUTPUT_DIR") or Path.cwd() / "literature-discovery"))
    cache_dir: Path = field(default_factory=lambda: Path.home() / ".cache" / "paper-scout")

    # Cache Settings
    cache_ttl_days: int = 7

    # Logging
    log_level: str = "INFO"

    @classmethod
    def load(cls, env_path: Optional[str] = None) -> "Config":
        """
        설정 로드

        Args:
            env_path: .env 파일 경로 (기본: 현재 작업 디렉토리의 .env)

        Returns:
            Config 인스턴스
        """
        # .env 파일 로드
        if env_path:
            load_dotenv(env_path)
        else:
            # 사용자 작업 디렉토리 기준 .env 하나만 탐색
            default_env = Path(".env")
            if default_env.exists():
                load_dotenv(default_env)

        config = cls(
            # API Keys
            semantic_scholar_api_key=os.getenv("SEMANTIC_SCHOLAR_API_KEY") or None,
            opencitations_api_token=os.getenv("OPENCITATIONS_API_TOKEN") or None,
            kci_api_key=os.getenv("KCI_API_KEY") or None,

            # Cache Settings
            cache_ttl_days=int(os.getenv("CACHE_TTL_DAYS", "7")),

            # Logging
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )

        # 출력 디렉토리 생성
        config.output_dir.mkdir(parents=True, exist_ok=True)
        config.cache_dir.mkdir(parents=True, exist_ok=True)

        return config

    @property
    def has_semantic_scholar_key(self) -> bool:
        """Semantic Scholar API 키 보유 여부"""
        return bool(self.semantic_scholar_api_key)

    @property
    def has_opencitations_token(self) -> bool:
        """OpenCitations API 토큰 보유 여부"""
        return bool(self.opencitations_api_token)

    def get_rate_limit(self, api: str) -> float:
        """
        API별 Rate Limit 반환 (requests per second)

        Args:
            api: API 이름 (semantic_scholar, arxiv, opencitations)
        """
        if api == "semantic_scholar":
            # API 키 있으면 100/sec, 없으면 1/sec
            return 100.0 if self.has_semantic_scholar_key else 1.0
        elif api == "arxiv":
            return 3.0  # arXiv 권장
        elif api == "opencitations":
            return 10.0
        else:
            return 1.0  # 기본값
