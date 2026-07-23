"""
Literature Discovery Team - API Clients

외부 학술 API 클라이언트 모듈
- Semantic Scholar
- arXiv
- OpenCitations
- ERIC (Education Resources Information Center)
"""

import os
import logging
from typing import List, Optional, Dict, Any

import requests
import tenacity  # semanticscholar의 하드 의존 — 항상 함께 설치됨
from semanticscholar import SemanticScholar
import arxiv

from .paper_models import Paper, Author, CitationEdge
from .rate_limiter import RateLimiter, AdaptiveRateLimiter
from .cache import CacheManager

logger = logging.getLogger(__name__)

_API_EXCEPTIONS = (
    requests.RequestException,
    OSError,
    ValueError,
    TypeError,
    AttributeError,
    KeyError,
    RuntimeError,
    arxiv.HTTPError,
    # semanticscholar는 retry=False여도 retry_with(stop_after_attempt(1)) 경유라
    # (reraise 미설정) 실패를 tenacity.RetryError로 감싸 던진다 — 원 예외
    # (예: 429 → ConnectionRefusedError)는 last_attempt 안에 숨는다. 이 랩
    # 예외가 fail-soft catch를 관통하지 않도록 튜플에 포함한다.
    tenacity.RetryError,
)


def _is_429(exc: BaseException) -> bool:
    """예외가 rate limit(HTTP 429)인지 판정.

    tenacity.RetryError는 str()에 원 메시지("429")가 드러나지 않으므로
    last_attempt 내부 예외까지 검사한다. 내부 예외를 못 꺼내면 False
    (판정 불가 시 힌트 없이 fail-soft만 수행).
    """
    if isinstance(exc, tenacity.RetryError):
        try:
            inner = exc.last_attempt.exception()
        except Exception:
            return False
        return inner is not None and "429" in str(inner)
    return "429" in str(exc)


class SemanticScholarClient:
    """
    Semantic Scholar API 클라이언트

    주요 기능:
    - 논문 검색 (키워드 기반)
    - 논문 상세 조회 (ID/DOI 기반)
    - 인용/참조 논문 조회
    - 추천 논문 조회
    """

    # API에서 요청할 필드 목록
    PAPER_FIELDS = [
        "paperId", "title", "abstract", "year", "venue",
        "authors", "citationCount", "referenceCount",
        "influentialCitationCount", "externalIds", "url",
        "openAccessPdf", "s2FieldsOfStudy"
    ]

    def __init__(
        self,
        api_key: Optional[str] = None,
        cache_ttl_days: int = 7
    ):
        """
        Args:
            api_key: Semantic Scholar API 키 (선택)
            cache_ttl_days: 캐시 유효 기간
        """
        self.api_key = api_key or os.getenv("SEMANTIC_SCHOLAR_API_KEY")
        # retry=False: 라이브러리 기본값(retry=True)은 내부 tenacity가 429(ConnectionRefusedError)에서
        # wait_exponential(min=5, max=60) + stop_after_attempt(10)로 최악 7분+ 블로킹한다. 우리는 이미
        # AdaptiveRateLimiter + REST 폴백 + 빈 리스트 fail-soft를 갖추고 있어 라이브러리 재시도는
        # 이중 방어이자 무키 공유 풀 429 시 정지 원인이므로 명시적으로 끈다.
        self.client = (
            SemanticScholar(api_key=self.api_key, retry=False)
            if self.api_key
            else SemanticScholar(retry=False)
        )
        self._library_broken = False  # 라이브러리 호환성 문제 감지 시 REST만 사용
        self._library_fail_count = 0  # 연속 실패 3회 시 _library_broken 활성화
        self._keyless_429_warned = False  # 무키 429 힌트 1회성 억제

        # Rate Limiter 설정
        rate = 100.0 if self.api_key else 1.0
        self.rate_limiter = AdaptiveRateLimiter(initial_calls_per_second=rate)

        # Cache 설정
        self.cache = CacheManager("semantic_scholar", ttl_days=cache_ttl_days)

        logger.info(f"SemanticScholarClient initialized (API key: {'Yes' if self.api_key else 'No'}, Rate: {rate}/s)")

    def _warn_keyless_429_once(self):
        """무키 상태에서 429를 맞으면 1회성으로 키 발급을 안내한다."""
        if not self.api_key and not self._keyless_429_warned:
            self._keyless_429_warned = True
            logger.warning(
                "Semantic Scholar keyless pool is heavily throttled; "
                "set SEMANTIC_SCHOLAR_API_KEY (free) for reliable access."
            )

    def search_papers(
        self,
        query: str,
        limit: int = 10,
        year_range: Optional[tuple] = None,
        fields_of_study: Optional[List[str]] = None,
        force_refresh: bool = False
    ) -> List[Paper]:
        """
        키워드 기반 논문 검색

        Args:
            query: 검색 쿼리
            limit: 결과 수 제한
            year_range: (시작년도, 종료년도) 튜플
            fields_of_study: 분야 필터 (예: ["Computer Science"])
            force_refresh: 캐시 무시

        Returns:
            Paper 리스트
        """
        cache_key = f"search:{query}:{limit}:{year_range}:{fields_of_study}"

        # 캐시 확인
        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached:
                return [Paper.from_semantic_scholar(p) for p in cached]

        # 라이브러리가 연속 3회 이상 깨진 경우에만 REST만 사용
        if not self._library_broken:
            try:
                papers_data, papers = self._search_via_library(query, limit, year_range, fields_of_study)
                self.cache.set(cache_key, papers_data)
                self._library_fail_count = 0  # 성공 시 카운터 리셋
                logger.info(f"Search '{query}': {len(papers)} papers found")
                return papers
            except _API_EXCEPTIONS as e:
                if _is_429(e):
                    self.rate_limiter.on_rate_limit_error()
                    self._warn_keyless_429_once()
                else:
                    self._library_fail_count += 1
                    if self._library_fail_count >= 3:
                        self._library_broken = True
                        logger.warning(f"Library broken after {self._library_fail_count} failures (switching to REST-only): {e}")
                    else:
                        logger.warning(f"Library error ({self._library_fail_count}/3, falling back to REST): {e}")

        try:
            papers_data, papers = self._search_via_rest(query, limit, year_range, fields_of_study)
            self.cache.set(cache_key, papers_data)
            logger.info(f"Search '{query}' (REST): {len(papers)} papers found")
            return papers
        except _API_EXCEPTIONS as e2:
            if _is_429(e2):
                self.rate_limiter.on_rate_limit_error()
                self._warn_keyless_429_once()
            logger.error(f"Search failed: {e2}")
            return []

    def _search_via_library(self, query, limit, year_range, fields_of_study):
        """semanticscholar 라이브러리를 통한 검색"""
        self.rate_limiter.wait()

        year_filter = None
        if year_range:
            year_filter = f"{year_range[0]}-{year_range[1]}"

        results = self.client.search_paper(
            query,
            limit=limit,
            year=year_filter,
            fields_of_study=fields_of_study,
            fields=self.PAPER_FIELDS
        )

        papers_data = []
        papers = []
        for result in results:
            paper_dict = result.raw_data if hasattr(result, 'raw_data') else result.__dict__
            papers_data.append(paper_dict)
            papers.append(Paper.from_semantic_scholar(paper_dict))
            # 라이브러리가 전체 결과를 순회하므로 수동으로 제한
            if len(papers) >= limit:
                break

        self.rate_limiter.on_success()
        return papers_data, papers

    def _search_via_rest(self, query, limit, year_range, fields_of_study):
        """Semantic Scholar REST API 직접 호출 (라이브러리 호환성 문제 시 폴백)"""
        self.rate_limiter.wait()

        url = "https://api.semanticscholar.org/graph/v1/paper/search"
        params = {
            "query": query,
            "limit": min(limit, 100),
            "fields": ",".join(self.PAPER_FIELDS),
        }
        if year_range:
            params["year"] = f"{year_range[0]}-{year_range[1]}"
        if fields_of_study:
            params["fieldsOfStudy"] = ",".join(fields_of_study)

        headers = {}
        if self.api_key:
            headers["x-api-key"] = self.api_key

        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        papers_data = []
        papers = []
        for item in data.get("data", []):
            papers_data.append(item)
            papers.append(Paper.from_semantic_scholar(item))

        self.rate_limiter.on_success()
        return papers_data, papers

    def get_paper(
        self,
        paper_id: str,
        force_refresh: bool = False
    ) -> Optional[Paper]:
        """
        논문 ID로 상세 정보 조회 (Sprint F 2026-05-21: REST fallback 추가).

        Args:
            paper_id: Semantic Scholar Paper ID 또는 DOI (DOI:xxx 형식)
            force_refresh: 캐시 무시

        Returns:
            Paper 또는 None
        """
        cache_key = f"paper:{paper_id}"

        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached:
                return Paper.from_semantic_scholar(cached)

        if not self._library_broken:
            try:
                paper_dict, paper = self._get_paper_via_library(paper_id)
                if paper_dict:
                    self.cache.set(cache_key, paper_dict)
                self._library_fail_count = 0
                return paper
            except _API_EXCEPTIONS as e:
                if _is_429(e):
                    self.rate_limiter.on_rate_limit_error()
                else:
                    self._library_fail_count += 1
                    if self._library_fail_count >= 3:
                        self._library_broken = True
                        logger.warning(f"Library broken after {self._library_fail_count} failures (switching to REST-only): {e}")
                    else:
                        logger.warning(f"get_paper library error ({self._library_fail_count}/3, falling back to REST): {e}")

        try:
            paper_dict, paper = self._get_paper_via_rest(paper_id)
            if paper_dict:
                self.cache.set(cache_key, paper_dict)
            return paper
        except _API_EXCEPTIONS as e2:
            if _is_429(e2):
                self.rate_limiter.on_rate_limit_error()
            logger.error(f"Get paper failed (REST): {e2}")
            return None

    def _get_paper_via_library(self, paper_id):
        """semanticscholar 라이브러리를 통한 단일 논문 조회"""
        self.rate_limiter.wait()
        result = self.client.get_paper(paper_id, fields=self.PAPER_FIELDS)
        if not result:
            self.rate_limiter.on_success()
            return None, None
        paper_dict = result.raw_data if hasattr(result, 'raw_data') else result.__dict__
        self.rate_limiter.on_success()
        return paper_dict, Paper.from_semantic_scholar(paper_dict)

    def _get_paper_via_rest(self, paper_id):
        """REST 직접 호출 (Sprint F 2026-05-21: get_paper fallback).

        S2 API는 paper_id에 'DOI:10.xxx', 'ARXIV:1234.5678' 같은 prefix를
        직접 지원. URL path에 그대로 사용 가능 (urlencoding은 requests가 처리).
        """
        self.rate_limiter.wait()
        url = f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}"
        params = {"fields": ",".join(self.PAPER_FIELDS)}
        headers = {}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code == 404:
            self.rate_limiter.on_success()
            return None, None
        resp.raise_for_status()
        data = resp.json()
        self.rate_limiter.on_success()
        return data, Paper.from_semantic_scholar(data)

    def get_paper_citations(
        self,
        paper_id: str,
        limit: int = 100,
        force_refresh: bool = False
    ) -> List[Paper]:
        """
        논문을 인용한 논문 목록 조회 (Sprint F 2026-05-21: REST fallback 추가).
        """
        cache_key = f"citations:{paper_id}:{limit}"

        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached:
                return [Paper.from_semantic_scholar(p) for p in cached]

        if not self._library_broken:
            try:
                papers_data, papers = self._get_paper_citations_via_library(paper_id, limit)
                self.cache.set(cache_key, papers_data)
                self._library_fail_count = 0
                logger.info(f"Citations for {paper_id}: {len(papers)} papers")
                return papers
            except _API_EXCEPTIONS as e:
                if _is_429(e):
                    self.rate_limiter.on_rate_limit_error()
                else:
                    self._library_fail_count += 1
                    if self._library_fail_count >= 3:
                        self._library_broken = True
                        logger.warning(f"Library broken after {self._library_fail_count} failures (switching to REST-only): {e}")
                    else:
                        logger.warning(f"get_paper_citations library error ({self._library_fail_count}/3, falling back to REST): {e}")

        try:
            papers_data, papers = self._get_paper_citations_via_rest(paper_id, limit)
            self.cache.set(cache_key, papers_data)
            logger.info(f"Citations for {paper_id} (REST): {len(papers)} papers")
            return papers
        except _API_EXCEPTIONS as e2:
            if _is_429(e2):
                self.rate_limiter.on_rate_limit_error()
            logger.error(f"Get citations failed (REST): {e2}")
            return []

    def _get_paper_citations_via_library(self, paper_id, limit):
        """semanticscholar 라이브러리를 통한 인용 조회"""
        self.rate_limiter.wait()
        result = self.client.get_paper_citations(
            paper_id, limit=limit, fields=self.PAPER_FIELDS
        )
        papers_data, papers = [], []
        for citation in result:
            if citation.citingPaper:
                paper_dict = citation.citingPaper.raw_data if hasattr(citation.citingPaper, 'raw_data') else citation.citingPaper.__dict__
                papers_data.append(paper_dict)
                papers.append(Paper.from_semantic_scholar(paper_dict))
            if len(papers) >= limit:
                break
        self.rate_limiter.on_success()
        return papers_data, papers

    def _get_paper_citations_via_rest(self, paper_id, limit):
        """REST 직접 호출 (Sprint F 2026-05-21).

        S2 API: /graph/v1/paper/{paper_id}/citations
        response.data[].citingPaper.{paperId, title, ...} 형식.
        nested field 명시 필요.
        """
        self.rate_limiter.wait()
        url = f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}/citations"
        nested = ",".join(f"citingPaper.{f}" for f in self.PAPER_FIELDS)
        params = {"limit": min(limit, 1000), "fields": nested}
        headers = {}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        papers_data, papers = [], []
        for item in data.get("data", []):
            cp = item.get("citingPaper")
            if cp:
                papers_data.append(cp)
                papers.append(Paper.from_semantic_scholar(cp))
        self.rate_limiter.on_success()
        return papers_data, papers

    def get_paper_references(
        self,
        paper_id: str,
        limit: int = 100,
        force_refresh: bool = False
    ) -> List[Paper]:
        """
        논문이 인용한 논문 목록 조회 (Sprint F 2026-05-21: REST fallback 추가).
        """
        cache_key = f"references:{paper_id}:{limit}"

        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached:
                return [Paper.from_semantic_scholar(p) for p in cached]

        if not self._library_broken:
            try:
                papers_data, papers = self._get_paper_references_via_library(paper_id, limit)
                self.cache.set(cache_key, papers_data)
                self._library_fail_count = 0
                logger.info(f"References for {paper_id}: {len(papers)} papers")
                return papers
            except _API_EXCEPTIONS as e:
                if _is_429(e):
                    self.rate_limiter.on_rate_limit_error()
                else:
                    self._library_fail_count += 1
                    if self._library_fail_count >= 3:
                        self._library_broken = True
                        logger.warning(f"Library broken after {self._library_fail_count} failures (switching to REST-only): {e}")
                    else:
                        logger.warning(f"get_paper_references library error ({self._library_fail_count}/3, falling back to REST): {e}")

        try:
            papers_data, papers = self._get_paper_references_via_rest(paper_id, limit)
            self.cache.set(cache_key, papers_data)
            logger.info(f"References for {paper_id} (REST): {len(papers)} papers")
            return papers
        except _API_EXCEPTIONS as e2:
            if _is_429(e2):
                self.rate_limiter.on_rate_limit_error()
            logger.error(f"Get references failed (REST): {e2}")
            return []

    def _get_paper_references_via_library(self, paper_id, limit):
        """semanticscholar 라이브러리를 통한 참조 조회"""
        self.rate_limiter.wait()
        result = self.client.get_paper_references(
            paper_id, limit=limit, fields=self.PAPER_FIELDS
        )
        papers_data, papers = [], []
        for ref in result:
            if ref.citedPaper:
                paper_dict = ref.citedPaper.raw_data if hasattr(ref.citedPaper, 'raw_data') else ref.citedPaper.__dict__
                papers_data.append(paper_dict)
                papers.append(Paper.from_semantic_scholar(paper_dict))
            if len(papers) >= limit:
                break
        self.rate_limiter.on_success()
        return papers_data, papers

    def _get_paper_references_via_rest(self, paper_id, limit):
        """REST 직접 호출 (Sprint F 2026-05-21)."""
        self.rate_limiter.wait()
        url = f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}/references"
        nested = ",".join(f"citedPaper.{f}" for f in self.PAPER_FIELDS)
        params = {"limit": min(limit, 1000), "fields": nested}
        headers = {}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        papers_data, papers = [], []
        for item in data.get("data", []):
            cp = item.get("citedPaper")
            if cp:
                papers_data.append(cp)
                papers.append(Paper.from_semantic_scholar(cp))
        self.rate_limiter.on_success()
        return papers_data, papers

    def get_recommendations(
        self,
        paper_id: str,
        limit: int = 10,
        force_refresh: bool = False
    ) -> List[Paper]:
        """
        추천 논문 조회 (시맨틱 유사도 기반) — Sprint F 2026-05-21: REST fallback 추가.
        """
        cache_key = f"recommendations:{paper_id}:{limit}"

        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached:
                return [Paper.from_semantic_scholar(p) for p in cached]

        if not self._library_broken:
            try:
                papers_data, papers = self._get_recommendations_via_library(paper_id, limit)
                self.cache.set(cache_key, papers_data)
                self._library_fail_count = 0
                logger.info(f"Recommendations for {paper_id}: {len(papers)} papers")
                return papers
            except _API_EXCEPTIONS as e:
                if _is_429(e):
                    self.rate_limiter.on_rate_limit_error()
                else:
                    self._library_fail_count += 1
                    if self._library_fail_count >= 3:
                        self._library_broken = True
                        logger.warning(f"Library broken after {self._library_fail_count} failures (switching to REST-only): {e}")
                    else:
                        logger.warning(f"get_recommendations library error ({self._library_fail_count}/3, falling back to REST): {e}")

        try:
            papers_data, papers = self._get_recommendations_via_rest(paper_id, limit)
            self.cache.set(cache_key, papers_data)
            logger.info(f"Recommendations for {paper_id} (REST): {len(papers)} papers")
            return papers
        except _API_EXCEPTIONS as e2:
            if _is_429(e2):
                self.rate_limiter.on_rate_limit_error()
            logger.error(f"Get recommendations failed (REST): {e2}")
            return []

    def _get_recommendations_via_library(self, paper_id, limit):
        """semanticscholar 라이브러리를 통한 추천 조회"""
        self.rate_limiter.wait()
        result = self.client.get_recommended_papers(
            paper_id, limit=limit, fields=self.PAPER_FIELDS
        )
        papers_data, papers = [], []
        for rec in result:
            paper_dict = rec.raw_data if hasattr(rec, 'raw_data') else rec.__dict__
            papers_data.append(paper_dict)
            paper = Paper.from_semantic_scholar(paper_dict)
            paper.relevance_reason = "semantic_similarity"
            papers.append(paper)
            if len(papers) >= limit:
                break
        self.rate_limiter.on_success()
        return papers_data, papers

    def _get_recommendations_via_rest(self, paper_id, limit):
        """REST 직접 호출 (Sprint F 2026-05-21).

        S2 API: /recommendations/v1/papers/forpaper/{paper_id}
        (graph/v1과 다른 endpoint prefix 주의)
        """
        self.rate_limiter.wait()
        url = f"https://api.semanticscholar.org/recommendations/v1/papers/forpaper/{paper_id}"
        params = {"limit": min(limit, 100), "fields": ",".join(self.PAPER_FIELDS)}
        headers = {}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        papers_data, papers = [], []
        for item in data.get("recommendedPapers", []):
            papers_data.append(item)
            paper = Paper.from_semantic_scholar(item)
            paper.relevance_reason = "semantic_similarity"
            papers.append(paper)
        self.rate_limiter.on_success()
        return papers_data, papers


class ArxivClient:
    """
    arXiv API 클라이언트

    주요 기능:
    - 논문 검색 (키워드, 카테고리)
    - 논문 조회 (arXiv ID)
    """

    def __init__(self, cache_ttl_days: int = 7):
        self.client = arxiv.Client()
        self.rate_limiter = RateLimiter(calls_per_second=3.0)  # arXiv 권장
        self.cache = CacheManager("arxiv", ttl_days=cache_ttl_days)

        logger.info("ArxivClient initialized")

    def search_papers(
        self,
        query: str,
        limit: int = 10,
        categories: Optional[List[str]] = None,
        year_range: Optional[tuple] = None,
        force_refresh: bool = False
    ) -> List[Paper]:
        """
        arXiv 논문 검색

        Args:
            query: 검색 쿼리
            limit: 결과 수 제한
            categories: 카테고리 필터 (예: ["cs.AI", "cs.CL"])
            year_range: (시작년도, 종료년도)
            force_refresh: 캐시 무시

        Returns:
            Paper 리스트
        """
        cache_key = f"search:{query}:{limit}:{categories}:{year_range}"

        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached:
                return [Paper.from_dict(p) if isinstance(p, dict) else p for p in cached]

        try:
            self.rate_limiter.wait()

            # 쿼리 구성
            search_query = query
            if categories:
                cat_query = " OR ".join([f"cat:{cat}" for cat in categories])
                search_query = f"({query}) AND ({cat_query})"

            search = arxiv.Search(
                query=search_query,
                max_results=limit,
                sort_by=arxiv.SortCriterion.Relevance
            )

            papers = []
            for result in self.client.results(search):
                # 연도 필터
                if year_range and result.published:
                    year = result.published.year
                    if year < year_range[0] or year > year_range[1]:
                        continue

                paper = Paper.from_arxiv(result)
                papers.append(paper)

            # 캐시 저장 (Paper 객체를 dict로 변환)
            self.cache.set(cache_key, [p.to_dict() for p in papers])

            logger.info(f"arXiv search '{query}': {len(papers)} papers")
            return papers

        except _API_EXCEPTIONS as e:
            logger.error(f"arXiv search error: {e}")
            return []

    def get_paper(
        self,
        arxiv_id: str,
        force_refresh: bool = False
    ) -> Optional[Paper]:
        """
        arXiv ID로 논문 조회

        Args:
            arxiv_id: arXiv ID (예: "2301.00001")
            force_refresh: 캐시 무시

        Returns:
            Paper 또는 None
        """
        cache_key = f"paper:{arxiv_id}"

        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached:
                # dict에서 Paper 복원
                return Paper(**cached) if isinstance(cached, dict) else cached

        try:
            self.rate_limiter.wait()

            search = arxiv.Search(id_list=[arxiv_id])
            results = list(self.client.results(search))

            if results:
                paper = Paper.from_arxiv(results[0])
                self.cache.set(cache_key, paper.to_dict())
                return paper

            return None

        except _API_EXCEPTIONS as e:
            logger.error(f"arXiv get paper error: {e}")
            return None


class OpenCitationsClient:
    """
    OpenCitations API 클라이언트

    인용 데이터 전문 API
    """

    BASE_URL = "https://opencitations.net/index/coci/api/v1"

    def __init__(
        self,
        api_token: Optional[str] = None,
        cache_ttl_days: int = 7
    ):
        """
        Args:
            api_token: OpenCitations API 토큰
            cache_ttl_days: 캐시 유효 기간
        """
        self.api_token = api_token or os.getenv("OPENCITATIONS_API_TOKEN")
        self.rate_limiter = RateLimiter(calls_per_second=10.0)
        self.cache = CacheManager("opencitations", ttl_days=cache_ttl_days)

        self.headers = {}
        if self.api_token:
            self.headers["authorization"] = self.api_token

        logger.info(f"OpenCitationsClient initialized (Token: {'Yes' if self.api_token else 'No'})")

    def get_citations(
        self,
        doi: str,
        force_refresh: bool = False
    ) -> List[CitationEdge]:
        """
        DOI 기반 인용 관계 조회 (이 논문을 인용한 논문들)

        Args:
            doi: 논문 DOI
            force_refresh: 캐시 무시

        Returns:
            CitationEdge 리스트
        """
        cache_key = f"citations:{doi}"

        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached:
                return [CitationEdge(**c) for c in cached]

        try:
            self.rate_limiter.wait()

            url = f"{self.BASE_URL}/citations/{doi}"
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()

            data = response.json()
            edges = []
            edges_data = []

            for item in data:
                edge = CitationEdge(
                    source_id=item.get("citing", ""),
                    target_id=item.get("cited", doi),
                )
                edges.append(edge)
                edges_data.append(edge.to_dict())

            self.cache.set(cache_key, edges_data)

            logger.info(f"OpenCitations citations for {doi}: {len(edges)} edges")
            return edges

        except _API_EXCEPTIONS as e:
            logger.error(f"OpenCitations error: {e}")
            return []

    def get_references(
        self,
        doi: str,
        force_refresh: bool = False
    ) -> List[CitationEdge]:
        """
        DOI 기반 참조 관계 조회 (이 논문이 인용한 논문들)

        Args:
            doi: 논문 DOI
            force_refresh: 캐시 무시

        Returns:
            CitationEdge 리스트
        """
        cache_key = f"references:{doi}"

        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached:
                return [CitationEdge(**c) for c in cached]

        try:
            self.rate_limiter.wait()

            url = f"{self.BASE_URL}/references/{doi}"
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()

            data = response.json()
            edges = []
            edges_data = []

            for item in data:
                edge = CitationEdge(
                    source_id=doi,
                    target_id=item.get("cited", ""),
                )
                edges.append(edge)
                edges_data.append(edge.to_dict())

            self.cache.set(cache_key, edges_data)

            logger.info(f"OpenCitations references for {doi}: {len(edges)} edges")
            return edges

        except _API_EXCEPTIONS as e:
            logger.error(f"OpenCitations error: {e}")
            return []


class ERICClient:
    """
    ERIC (Education Resources Information Center) API 클라이언트

    교육학 전문 데이터베이스
    - API 키 불필요 (무료 공개 API)
    - 1.8M+ 교육 관련 논문, 보고서, 저널 아티클
    """

    BASE_URL = "https://api.ies.ed.gov/eric/"

    def __init__(self, cache_ttl_days: int = 7):
        self.rate_limiter = RateLimiter(calls_per_second=5.0)
        self.cache = CacheManager("eric", ttl_days=cache_ttl_days)

        logger.info("ERICClient initialized")

    def search_papers(
        self,
        query: str,
        limit: int = 10,
        year_range: Optional[tuple] = None,
        peer_reviewed: bool = False,
        force_refresh: bool = False
    ) -> List[Paper]:
        """
        ERIC 논문 검색

        Args:
            query: 검색 쿼리
            limit: 결과 수 제한 (최대 200)
            year_range: (시작년도, 종료년도)
            peer_reviewed: 피어리뷰 논문만 검색
            force_refresh: 캐시 무시

        Returns:
            Paper 리스트
        """
        cache_key = f"search:{query}:{limit}:{year_range}:{peer_reviewed}"

        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached:
                return [self._dict_to_paper(p) for p in cached]

        try:
            self.rate_limiter.wait()

            # API 파라미터 구성
            params = {
                "search": query,
                "rows": min(limit, 200),
                "format": "json"
            }

            # 연도 필터
            if year_range:
                params["start"] = year_range[0]
                params["end"] = year_range[1]

            # 피어리뷰 필터
            if peer_reviewed:
                params["peerreviewed"] = "true"

            response = requests.get(self.BASE_URL, params=params, timeout=30)
            response.raise_for_status()

            data = response.json()
            docs = data.get("response", {}).get("docs", [])

            papers = []
            papers_data = []

            for doc in docs:
                paper = self._parse_eric_doc(doc)
                papers.append(paper)
                papers_data.append(paper.to_dict())

            self.cache.set(cache_key, papers_data)

            logger.info(f"ERIC search '{query}': {len(papers)} papers")
            return papers

        except _API_EXCEPTIONS as e:
            logger.error(f"ERIC search error: {e}")
            return []

    def get_paper(
        self,
        eric_id: str,
        force_refresh: bool = False
    ) -> Optional[Paper]:
        """
        ERIC ID로 논문 조회

        Args:
            eric_id: ERIC ID (예: "EJ1234567", "ED567890")
            force_refresh: 캐시 무시

        Returns:
            Paper 또는 None
        """
        cache_key = f"paper:{eric_id}"

        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached:
                return self._dict_to_paper(cached)

        try:
            self.rate_limiter.wait()

            params = {
                "search": f"id:{eric_id}",
                "rows": 1,
                "format": "json"
            }

            response = requests.get(self.BASE_URL, params=params, timeout=30)
            response.raise_for_status()

            data = response.json()
            docs = data.get("response", {}).get("docs", [])

            if docs:
                paper = self._parse_eric_doc(docs[0])
                self.cache.set(cache_key, paper.to_dict())
                return paper

            return None

        except _API_EXCEPTIONS as e:
            logger.error(f"ERIC get paper error: {e}")
            return None

    def _parse_eric_doc(self, doc: Dict[str, Any]) -> Paper:
        """ERIC 문서를 Paper 객체로 변환"""
        # 저자 파싱
        authors = []
        author_names = doc.get("author", [])
        if isinstance(author_names, str):
            author_names = [author_names]
        for name in author_names:
            authors.append(Author(name=name))

        # 연도 추출
        year = None
        pub_date = doc.get("publicationdateyear")
        if pub_date:
            try:
                year = int(pub_date)
            except ValueError:
                pass

        # ERIC ID
        eric_id = doc.get("id", "")

        return Paper(
            paper_id=f"eric:{eric_id}",
            title=doc.get("title", ""),
            authors=authors,
            year=year,
            venue=doc.get("source", ""),
            abstract=doc.get("description", ""),
            url=f"https://eric.ed.gov/?id={eric_id}",
            fields_of_study=doc.get("subject", []) if isinstance(doc.get("subject"), list) else [doc.get("subject", "")]
        )

    def _dict_to_paper(self, data: Dict[str, Any]) -> Paper:
        """딕셔너리에서 Paper 복원"""
        authors = [Author(**a) if isinstance(a, dict) else Author(name=a) for a in data.get("authors", [])]
        return Paper(
            paper_id=data.get("paper_id", ""),
            title=data.get("title", ""),
            authors=authors,
            year=data.get("year"),
            venue=data.get("venue"),
            abstract=data.get("abstract"),
            url=data.get("url"),
            fields_of_study=data.get("fields_of_study", [])
        )


class KCIClient:
    """
    KCI (한국학술지인용색인) Open API 클라이언트 — litdisc 어댑터

    국내 학술지 검색. kci.kci_core.KCIClient(코어)를
    재사용해 Paper 모델로 변환한다 (XML 파서 단일화 — twin-parse drift 방지).
    KCI_API_KEY 미설정이거나 코어 임포트 실패 시 fail-soft로 빈 결과 반환.
    """

    def __init__(self, api_key: Optional[str] = None, cache_ttl_days: int = 7):
        self._core = None
        api_key = api_key or os.getenv("KCI_API_KEY")
        if not api_key:
            logger.info("KCIClient(litdisc): KCI_API_KEY 미설정 — 검색 비활성 (fail-soft)")
            return

        try:
            from kci.kci_core import KCIClient as _CoreKCIClient
            self._core = _CoreKCIClient(api_key=api_key, cache_ttl_days=cache_ttl_days)
        except ImportError as e:
            logger.warning(f"KCIClient(litdisc): 코어 임포트 실패 — 비활성 (fail-soft): {e}")

        logger.info(f"KCIClient(litdisc) initialized (active: {self._core is not None})")

    @property
    def available(self) -> bool:
        """검색 가능 여부 (키 보유 + 코어 정상)"""
        return self._core is not None

    def search_papers(
        self,
        query: str,
        limit: int = 10,
        year_range: Optional[tuple] = None,
        force_refresh: bool = False
    ) -> List[Paper]:
        """
        KCI 논문 검색 (제목 기반)

        Args:
            query: 검색 쿼리 (한국어/영어 — KCI가 제목 전 언어 변형을 검색)
            limit: 결과 수 제한
            year_range: (시작년도, 종료년도) → KCI dateFrom/dateTo(YYYYMM) 매핑
            force_refresh: 캐시 무시

        Returns:
            Paper 리스트 (비활성/오류 시 빈 리스트 — fail-soft)
        """
        if not self._core:
            return []

        try:
            articles = self._core.search_articles(
                title=query,
                date_from=f"{year_range[0]}01" if year_range else None,
                date_to=f"{year_range[1]}12" if year_range else None,
                limit=limit,
                force_refresh=force_refresh,
            )
        except _API_EXCEPTIONS as e:
            logger.error(f"KCI search error: {e}")
            return []

        papers = [self._to_paper(a) for a in articles]
        logger.info(f"KCI search '{query}': {len(papers)} papers")
        return papers

    @staticmethod
    def _to_paper(article: Dict[str, Any]) -> Paper:
        """코어 search_articles dict → Paper 변환"""
        authors = [
            Author(name=a.get("name") or a.get("name_en") or "")
            for a in article.get("authors", [])
            if a.get("name") or a.get("name_en")
        ]
        # "사회과학 > 사회과학일반" 형태의 연구분야를 계층 분해
        fields = [
            c.strip() for c in (article.get("categories") or "").split(">") if c.strip()
        ]
        return Paper(
            paper_id=f"kci:{article.get('article_id', '')}",
            title=article.get("title") or "",
            authors=authors,
            year=article.get("year"),
            venue=article.get("journal") or None,
            abstract=article.get("abstract") or None,
            doi=article.get("doi") or None,
            citation_count=article.get("citation_count_kci") or 0,
            url=article.get("url") or None,
            fields_of_study=fields,
            source_db="kci",
        )


class OpenAlexClient:
    """
    OpenAlex API 클라이언트 (https://api.openalex.org)

    2026 usage-based 정책: 무료 키($1/day 크레딧 — 검색 1,000회/day) 권장.
    무키도 $0.10/day 계량 쿼터로 동작하므로 S2와 달리 무키 시에도 호출한다.
    402/429(쿼터 소진)는 fail-soft 빈 결과 + 무료 키 안내 1회 로그.
    """

    BASE_URL = "https://api.openalex.org/works"

    def __init__(self, api_key: Optional[str] = None, cache_ttl_days: int = 7):
        self.api_key = api_key or os.getenv("OPENALEX_API_KEY")
        # 실제 제약은 초당이 아니라 일일 크레딧 — 보수적 5/s 고정
        self.rate_limiter = RateLimiter(calls_per_second=5.0)
        self.cache = CacheManager("openalex", ttl_days=cache_ttl_days)
        self._quota_hint_shown = False
        logger.info(f"OpenAlexClient initialized (API key: {'Yes' if self.api_key else 'No'})")

    def _warn_quota_once(self) -> None:
        if not self._quota_hint_shown:
            self._quota_hint_shown = True
            logger.warning(
                "OpenAlex daily quota exhausted. A free API key raises the limit "
                "(~1,000 searches/day) — 30-second signup at "
                "https://openalex.org/settings/api, then set OPENALEX_API_KEY."
            )

    def search_papers(
        self,
        query: str,
        limit: int = 10,
        year_range: Optional[tuple] = None,
        force_refresh: bool = False,
    ) -> List[Paper]:
        cache_key = f"openalex:search:{query}:{limit}:{year_range}"
        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached:
                return [Paper.from_openalex(p) for p in cached]

        params: Dict[str, Any] = {"search": query, "per-page": min(max(limit, 1), 200)}
        if year_range:
            params["filter"] = f"publication_year:{year_range[0]}-{year_range[1]}"
        if self.api_key:
            params["api_key"] = self.api_key

        try:
            self.rate_limiter.wait()
            resp = requests.get(self.BASE_URL, params=params, timeout=30)
            if resp.status_code in (402, 429):
                self._warn_quota_once()
                return []
            resp.raise_for_status()
            results = resp.json().get("results", [])[:limit]
        except _API_EXCEPTIONS as e:
            logger.warning(f"OpenAlex search failed (fail-soft): {e}")
            return []

        self.cache.set(cache_key, results)
        papers = [Paper.from_openalex(p) for p in results]
        logger.info(f"OpenAlex search '{query}': {len(papers)} papers")
        return papers
