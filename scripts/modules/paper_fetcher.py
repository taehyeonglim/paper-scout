"""
Paper Fetcher - 다중 소스 논문 검색

Semantic Scholar, arXiv, ERIC, OpenAlex, KCI에서 프로젝트 프로파일 기반
논문을 검색하고 통합합니다.
"""

import os
import logging
from typing import Dict, List, Any, Optional
from datetime import datetime

import sys
from pathlib import Path

# 상위 디렉토리의 utils 모듈 접근
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.api_clients import SemanticScholarClient, ArxivClient, ERICClient, KCIClient, OpenAlexClient
from utils.paper_models import Paper

logger = logging.getLogger(__name__)


class PaperFetcher:
    """다중 소스 논문 검색기"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        sources = config.get("sources", {})

        # API 클라이언트 초기화
        self.s2_client = None
        self.arxiv_client = None
        self.eric_client = None
        self.kci_client = None
        self.openalex_client = None

        if sources.get("semantic_scholar", {}).get("enabled", True):
            self.s2_client = SemanticScholarClient()

        if sources.get("arxiv", {}).get("enabled", True):
            self.arxiv_client = ArxivClient()

        if sources.get("eric", {}).get("enabled", True):
            self.eric_client = ERICClient()

        # KCI는 인증키 필수 — 미설정 시 조용히 생략 (fail-soft)
        if sources.get("kci", {}).get("enabled", True) and os.getenv("KCI_API_KEY"):
            kci_client = KCIClient()
            if kci_client.available:
                self.kci_client = kci_client

        # OpenAlex는 KCI와 달리 무키도 usage-based 쿼터로 동작 (fail-soft)
        if sources.get("openalex", {}).get("enabled", True):
            self.openalex_client = OpenAlexClient()

        self.s2_config = sources.get("semantic_scholar", {})
        self.arxiv_config = sources.get("arxiv", {})
        self.eric_config = sources.get("eric", {})
        self.kci_config = sources.get("kci", {})
        self.openalex_config = sources.get("openalex", {})

    def fetch_for_project(
        self,
        profile: Dict[str, Any],
        force_refresh: bool = False,
    ) -> List[Paper]:
        """
        프로젝트 프로파일 기반 다중 소스 논문 검색

        Args:
            profile: ProjectProfiler가 생성한 프로파일
            force_refresh: 캐시 무시

        Returns:
            검색된 Paper 리스트 (중복 제거 전)
        """
        queries = profile.get("custom_queries")
        if queries:
            logger.info(f"Using {len(queries)} custom queries for {profile.get('project_id')}")
        else:
            from .project_profiler import ProjectProfiler
            profiler = ProjectProfiler(self.config)
            queries = profiler.build_search_queries(profile)

        if not queries:
            logger.warning(f"No queries for project {profile.get('project_id')}")
            return []

        all_papers = []
        current_year = datetime.now().year
        year_range = (current_year - 2, current_year)  # 최근 2년

        # Semantic Scholar 검색
        if self.s2_client:
            limit = self.s2_config.get("results_per_query", 20)
            max_queries = self.s2_config.get("queries_per_project", 3)
            for query in queries[:max_queries]:
                papers = self.s2_client.search_papers(
                    query=query,
                    limit=limit,
                    year_range=year_range,
                    force_refresh=force_refresh,
                )
                for p in papers:
                    p.source_db = "semantic_scholar"
                all_papers.extend(papers)

        # arXiv 검색
        if self.arxiv_client:
            limit = self.arxiv_config.get("results_per_query", 15)
            max_queries = self.arxiv_config.get("queries_per_project", 2)
            categories = self.arxiv_config.get("categories", ["cs.AI", "cs.CL", "cs.HC"])
            for query in queries[:max_queries]:
                papers = self.arxiv_client.search_papers(
                    query=query,
                    limit=limit,
                    categories=categories,
                    year_range=year_range,
                    force_refresh=force_refresh,
                )
                for p in papers:
                    p.source_db = "arxiv"
                all_papers.extend(papers)

        # ERIC 검색
        if self.eric_client:
            limit = self.eric_config.get("results_per_query", 10)
            max_queries = self.eric_config.get("queries_per_project", 2)
            for query in queries[:max_queries]:
                papers = self.eric_client.search_papers(
                    query=query,
                    limit=limit,
                    year_range=year_range,
                    peer_reviewed=True,
                    force_refresh=force_refresh,
                )
                for p in papers:
                    p.source_db = "eric"
                all_papers.extend(papers)

        # OpenAlex 검색 (usage-based free tier, 무키도 동작)
        if self.openalex_client:
            limit = self.openalex_config.get("results_per_query", 10)
            max_queries = self.openalex_config.get("queries_per_project", 2)
            for query in queries[:max_queries]:
                papers = self.openalex_client.search_papers(
                    query=query,
                    limit=limit,
                    year_range=year_range,
                    force_refresh=force_refresh,
                )
                for p in papers:
                    p.source_db = "openalex"
                all_papers.extend(papers)

        # KCI 검색 (국내 학술지) — 한국어 쿼리 우선 공급
        # (영어 쿼리는 KCI 리콜 0 실측, 2026-07-22 — custom_queries_ko가 있으면
        #  그것만 사용, 없으면 기본 쿼리로 폴백)
        if self.kci_client:
            limit = self.kci_config.get("results_per_query", 10)
            max_queries = self.kci_config.get("queries_per_project", 3)
            kci_queries = profile.get("custom_queries_ko") or queries
            for query in kci_queries[:max_queries]:
                papers = self.kci_client.search_papers(
                    query=query,
                    limit=limit,
                    year_range=year_range,
                    force_refresh=force_refresh,
                )
                for p in papers:
                    p.source_db = "kci"
                all_papers.extend(papers)

        # 후보 내 자체 중복 제거 (DOI 기반)
        all_papers = self._deduplicate_candidates(all_papers)

        logger.info(
            f"Fetched {len(all_papers)} papers for {profile.get('project_id')} "
            f"from {len(queries)} queries"
        )
        return all_papers

    def _deduplicate_candidates(self, papers: List[Paper]) -> List[Paper]:
        """후보 내 DOI/제목 기반 자체 중복 제거"""
        seen_dois = set()
        seen_titles = set()
        unique = []

        for paper in papers:
            # DOI 기반 중복 체크
            if paper.doi:
                doi_lower = paper.doi.lower()
                if doi_lower in seen_dois:
                    continue
                seen_dois.add(doi_lower)

            # 제목 기반 중복 체크 (DOI가 없는 경우 — 빈 정규화 결과는 대조 불가로 스킵)
            title_normalized = self._normalize_title(paper.title)
            if title_normalized and title_normalized in seen_titles:
                continue
            if title_normalized:
                seen_titles.add(title_normalized)

            unique.append(paper)

        return unique

    @staticmethod
    def _normalize_title(title: str) -> str:
        """제목 정규화 (비교용)"""
        if not title:
            return ""
        import re
        # 소문자 변환, 기호 제거, 공백 정규화 — \w는 유니코드 단어문자라
        # 한글 제목이 빈 문자열로 붕괴하지 않음 (ASCII 전용 패턴이면 순한글
        # 제목이 전부 ""로 정규화되어 서로 중복 오판, 2026-07-22 라이브 발견)
        normalized = title.lower()
        normalized = re.sub(r"[^\w\s]", "", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized
