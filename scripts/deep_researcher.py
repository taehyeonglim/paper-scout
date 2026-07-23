"""
Literature Discovery Team - Deep Researcher

심층적이고 광범위한 자료 탐색을 수행하는 에이전트.
ChatGPT/Gemini Deep Research와 유사한 반복적 탐색 방식.

Usage:
    python deep_researcher.py "Agentic AI in education"
    python deep_researcher.py "LLM applications" --depth deep
    python deep_researcher.py "transformer" --sources academic --year-range 2022-2026

Credits:
    세션 관리, 품질 등급 시스템, sources.jsonl 형식 등의 구조는
    fivetaku의 Deep Research Kit에서 영감을 받아 학술 검색용으로 재구현함.
    https://github.com/fivetaku/deep-research-kit
"""

import argparse
import logging
import re
import sys
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Set, Optional, Any, Tuple, Union

import requests

# 프로젝트 경로 설정
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from utils.api_clients import SemanticScholarClient, ArxivClient, ERICClient, KCIClient, OpenAlexClient
from utils.paper_models import Paper, Author, RelevanceLevel
from utils.markdown_writer import MarkdownWriter
from utils.quality_grader import grade_paper, get_grade_description, get_grade_color
from utils.session_manager import SessionManager, SessionConfig, SessionProgress, SessionStatus
from intelligence.semantic_rerank import rerank
from intelligence.synthesize import synthesize

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# =============================================================================
# 데이터 클래스 정의
# =============================================================================

@dataclass
class ResearchConfig:
    """Deep Research 탐색 설정"""
    depth: str = "medium"           # shallow/medium/deep
    sources: str = "all"            # all/academic/web
    year_start: Optional[int] = None
    year_end: Optional[int] = None

    # 깊이별 설정 (자동 계산)
    max_papers: int = 50
    max_iterations: int = 5
    papers_per_source: int = 20

    def __post_init__(self):
        """깊이에 따른 설정 자동 조정"""
        depth_configs = {
            "shallow": {"max_papers": 20, "max_iterations": 2, "papers_per_source": 10},
            "medium": {"max_papers": 50, "max_iterations": 5, "papers_per_source": 20},
            "deep": {"max_papers": 100, "max_iterations": 10, "papers_per_source": 30},
        }
        config = depth_configs.get(self.depth, depth_configs["medium"])
        self.max_papers = config["max_papers"]
        self.max_iterations = config["max_iterations"]
        self.papers_per_source = config["papers_per_source"]

    @property
    def year_range(self) -> Optional[Tuple[int, int]]:
        """연도 범위 튜플"""
        if self.year_start and self.year_end:
            return (self.year_start, self.year_end)
        return None


@dataclass
class ResearchPlan:
    """검색 계획 - Query Decomposition 결과"""
    original_topic: str
    primary_queries: List[str] = field(default_factory=list)
    secondary_queries: List[str] = field(default_factory=list)
    expansion_queries: List[str] = field(default_factory=list)
    keywords: Set[str] = field(default_factory=set)

    def get_queries_for_iteration(self, iteration: int) -> List[str]:
        """반복 횟수에 따른 쿼리 반환"""
        if iteration == 1:
            return self.primary_queries[:3]
        elif iteration == 2:
            return self.primary_queries[3:] + self.secondary_queries[:2]
        else:
            # 이후 반복에서는 expansion 쿼리 사용
            start = (iteration - 3) * 2
            return self.expansion_queries[start:start + 2] + self.secondary_queries[2:4]


@dataclass
class ResearchState:
    """탐색 상태 관리"""
    iteration: int = 0
    papers_found: Dict[str, Paper] = field(default_factory=dict)  # paper_id -> Paper
    queries_executed: Set[str] = field(default_factory=set)
    keywords_discovered: Set[str] = field(default_factory=set)

    # 품질 메트릭
    high_quality_count: int = 0     # 인용수 > 50
    recent_paper_count: int = 0     # 최근 2년

    # 종료 조건 추적
    no_new_papers_count: int = 0    # 연속 신규 논문 없음 횟수
    consecutive_api_failures: int = 0  # 연속 API 전체 실패 횟수

    def should_continue(self, config: ResearchConfig) -> bool:
        """탐색 계속 여부 판단"""
        if self.iteration >= config.max_iterations:
            logger.info(f"Stop: Max iterations ({config.max_iterations}) reached")
            return False
        if len(self.papers_found) >= config.max_papers:
            logger.info(f"Stop: Target papers ({config.max_papers}) reached")
            return False
        if self.no_new_papers_count >= 3:
            logger.info("Stop: Diminishing returns (3 iterations with no new papers)")
            return False
        if self.consecutive_api_failures >= 5:
            logger.error("Stop: Too many consecutive API failures (5). Check network/API keys.")
            return False
        return True


@dataclass
class DeepResearchResult:
    """Deep Research 최종 결과"""
    topic: str
    config: ResearchConfig

    # 분류된 논문
    influential_papers: List[Paper] = field(default_factory=list)  # 인용 50+
    recent_papers: List[Paper] = field(default_factory=list)       # 최근 2년
    other_papers: List[Paper] = field(default_factory=list)

    # 트렌드 분석
    emerging_keywords: List[Dict[str, Any]] = field(default_factory=list)
    year_distribution: Dict[int, int] = field(default_factory=dict)
    venue_distribution: Dict[str, int] = field(default_factory=dict)
    author_stats: List[Dict[str, Any]] = field(default_factory=list)

    # 연계 정보
    key_paper_dois: List[str] = field(default_factory=list)
    discovered_keywords: List[str] = field(default_factory=list)

    # 지능 레이어 (semantic rerank + synthesis)
    synthesis: Dict[str, Any] = field(default_factory=dict)        # themes/consensus/conflicts/gaps/limitations/mode
    near_matches: List[Paper] = field(default_factory=list)        # 희소 시 근접 후보 (off-topic, LLM 이유 동반)
    rerank_mode: str = "heuristic_fallback"                         # "llm" | "heuristic_fallback"
    rerank_dropped: int = 0

    # 통계
    total_papers: int = 0
    iterations_completed: int = 0
    queries_executed: int = 0


# =============================================================================
# 헬퍼 함수
# =============================================================================

# 학술 분야 영어 불용어
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "been",
    "be", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "must", "shall", "can", "need",
    "this", "that", "these", "those", "it", "its", "they", "them",
    "we", "us", "our", "you", "your", "he", "she", "his", "her",
    "what", "which", "who", "whom", "how", "when", "where", "why",
    "all", "each", "every", "both", "few", "more", "most", "other",
    "some", "such", "no", "nor", "not", "only", "own", "same", "so",
    "than", "too", "very", "just", "also", "now", "here", "there",
    "about", "above", "after", "again", "against", "before", "below",
    "between", "during", "into", "through", "under", "until", "up",
    "using", "based", "study", "research", "paper", "approach", "method",
    "analysis", "results", "data", "model", "system", "new", "proposed",
}

# 간단한 동의어 사전
SYNONYM_MAP = {
    "ai": ["artificial intelligence", "machine intelligence"],
    "ml": ["machine learning"],
    "dl": ["deep learning"],
    "nlp": ["natural language processing"],
    "llm": ["large language model", "large language models"],
    "gpt": ["generative pre-trained transformer"],
    "bert": ["bidirectional encoder representations"],
    "transformer": ["attention mechanism", "self-attention"],
    "education": ["learning", "teaching", "pedagogy", "instruction"],
    "student": ["learner", "trainee"],
    "teacher": ["instructor", "educator"],
}


def tokenize(text: str) -> List[str]:
    """텍스트 토큰화"""
    # 소문자 변환 및 특수문자 제거
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s-]', ' ', text)
    tokens = text.split()
    return [t for t in tokens if len(t) > 2 and t not in STOPWORDS]


def normalize_title(title: str) -> str:
    """제목 정규화 (중복 체크용)"""
    title = title.lower()
    title = re.sub(r'[^a-z0-9\s]', '', title)
    title = re.sub(r'\s+', ' ', title).strip()
    return title[:100]  # 처음 100자만


# =============================================================================
# 메인 클래스: DeepResearcher
# =============================================================================

class DeepResearcher:
    """심층 자료 탐색 에이전트"""

    def __init__(self, config: Config, session_base_dir: Optional[str] = None):
        self.config = config

        # API 클라이언트 (기존 활용)
        self.ss_client = SemanticScholarClient(
            api_key=config.semantic_scholar_api_key,
            cache_ttl_days=config.cache_ttl_days
        )
        self.arxiv_client = ArxivClient(cache_ttl_days=config.cache_ttl_days)
        self.eric_client = ERICClient(cache_ttl_days=config.cache_ttl_days)
        self.kci_client = KCIClient(
            api_key=config.kci_api_key, cache_ttl_days=config.cache_ttl_days
        )
        self.openalex_client = OpenAlexClient(
            api_key=config.openalex_api_key, cache_ttl_days=config.cache_ttl_days
        )

        # 출력
        self.writer = MarkdownWriter(str(config.output_dir))

        # 세션 관리자
        base_dir = session_base_dir or str(config.output_dir / "RESEARCH")
        self.session_manager = SessionManager(base_dir=base_dir)

        logger.info("DeepResearcher initialized")

    def research(self, topic: str, research_config: ResearchConfig) -> DeepResearchResult:
        """
        메인 탐색 로직

        Args:
            topic: 탐색 주제
            research_config: 탐색 설정

        Returns:
            DeepResearchResult
        """
        logger.info(f"Starting deep research: '{topic}' (depth={research_config.depth})")

        # 1. 세션 생성
        session_config = SessionConfig(
            depth=research_config.depth,
            sources=research_config.sources,
            year_start=research_config.year_start,
            year_end=research_config.year_end
        )
        session_id = self.session_manager.create_session(topic, session_config)
        logger.info(f"Session created: {session_id}")

        try:
            # 2. 검색 계획 수립 (Query Decomposition)
            plan = self._create_research_plan(topic)
            logger.info(f"Research plan created: {len(plan.primary_queries)} primary, {len(plan.secondary_queries)} secondary queries")

            # 3. 탐색 상태 초기화
            state = ResearchState()

            # 4. 반복 탐색 루프
            while state.should_continue(research_config):
                state.iteration += 1
                logger.info(f"=== Iteration {state.iteration} ===")

                initial_count = len(state.papers_found)
                state = self._iterate_search(plan, state, research_config)

                new_count = len(state.papers_found) - initial_count
                logger.info(f"Iteration {state.iteration}: +{new_count} papers (total: {len(state.papers_found)})")

                # 세션 진행 상태 업데이트
                session_progress = SessionProgress(
                    iteration=state.iteration,
                    papers_found=len(state.papers_found),
                    queries_executed=list(state.queries_executed),
                    keywords_discovered=list(state.keywords_discovered)
                )
                self.session_manager.update_progress(session_progress)

            # 5. 결과 통합 및 분석
            result = self._aggregate_results(topic, plan, state, research_config)

            # 6. 세션 완료 처리
            self.session_manager.mark_completed()
            logger.info(f"Deep research completed: {result.total_papers} papers found")

            return result

        except KeyboardInterrupt:
            self.session_manager.mark_paused()
            logger.info(f"Session paused: {session_id}")
            raise
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError, requests.RequestException) as e:
            self.session_manager.mark_failed(str(e))
            raise

    def resume_research(self, session_id: str, research_config: Optional[ResearchConfig] = None) -> Optional[DeepResearchResult]:
        """
        중단된 세션 재개

        Args:
            session_id: 세션 ID
            research_config: 탐색 설정 (None이면 기존 설정 사용)

        Returns:
            DeepResearchResult 또는 None (재개 불가 시)
        """
        if not self.session_manager.load_session(session_id):
            logger.error(f"Session not found: {session_id}")
            return None

        if not self.session_manager.can_resume():
            logger.error(f"Session cannot be resumed: {session_id}")
            return None

        # 기존 상태 복원
        topic = self.session_manager.state.get("topic", "")
        prev_progress = self.session_manager.get_progress()

        logger.info(f"Resuming session: {session_id}")
        logger.info(f"Previous progress: iteration={prev_progress.iteration}, papers={prev_progress.papers_found}")

        # 기존 설정 복원 또는 새 설정 사용
        if research_config is None:
            config_data = self.session_manager.state.get("config", {})
            research_config = ResearchConfig(
                depth=config_data.get("depth", "medium"),
                sources=config_data.get("sources", "all"),
                year_start=config_data.get("year_range", [None, None])[0] if config_data.get("year_range") else None,
                year_end=config_data.get("year_range", [None, None])[1] if config_data.get("year_range") else None
            )

        # 기존 소스 로드
        existing_sources = self.session_manager.get_sources()
        logger.info(f"Loaded {len(existing_sources)} existing sources")

        # 검색 계획 수립
        plan = self._create_research_plan(topic)

        # 이전 쿼리 복원
        for query in prev_progress.queries_executed:
            plan.expansion_queries.append(query)

        # 상태 초기화 (기존 진행 상태 반영)
        state = ResearchState(
            iteration=prev_progress.iteration,
            queries_executed=set(prev_progress.queries_executed),
            keywords_discovered=set(prev_progress.keywords_discovered)
        )

        # 기존 소스를 상태에 추가 (중복 체크용)
        for source in existing_sources:
            paper_id = source.get("id", "")
            if paper_id:
                # 간단한 Paper 객체 생성 (중복 체크용)
                paper = Paper(
                    paper_id=paper_id,
                    title=source.get("title", ""),
                    doi=source.get("doi"),
                    year=source.get("year"),
                    citation_count=source.get("citations", 0)
                )
                state.papers_found[paper_id] = paper

        try:
            # 탐색 계속
            while state.should_continue(research_config):
                state.iteration += 1
                logger.info(f"=== Iteration {state.iteration} ===")

                initial_count = len(state.papers_found)
                state = self._iterate_search(plan, state, research_config)

                new_count = len(state.papers_found) - initial_count
                logger.info(f"Iteration {state.iteration}: +{new_count} papers (total: {len(state.papers_found)})")

                # 세션 진행 상태 업데이트
                session_progress = SessionProgress(
                    iteration=state.iteration,
                    papers_found=len(state.papers_found),
                    queries_executed=list(state.queries_executed),
                    keywords_discovered=list(state.keywords_discovered)
                )
                self.session_manager.update_progress(session_progress)

            # 결과 통합
            result = self._aggregate_results(topic, plan, state, research_config)
            self.session_manager.mark_completed()

            return result

        except KeyboardInterrupt:
            self.session_manager.mark_paused()
            raise
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError, requests.RequestException) as e:
            self.session_manager.mark_failed(str(e))
            raise

    def list_sessions(self) -> List[Dict[str, Any]]:
        """모든 세션 목록 반환"""
        return self.session_manager.list_sessions()

    def _create_research_plan(self, topic: str) -> ResearchPlan:
        """검색 계획 수립 (템플릿 기반 Query Decomposition)"""
        keywords = tokenize(topic)

        # 1. Primary 쿼리: 원본 주제 + 변형
        primary_queries = [
            topic,
            f"{topic} survey",
            f"{topic} review",
        ]

        # 키워드 조합 추가
        if len(keywords) >= 2:
            primary_queries.append(f"{keywords[0]} {keywords[1]}")
            if len(keywords) >= 3:
                primary_queries.append(f"{keywords[0]} {keywords[2]}")

        # 2. Secondary 쿼리: 템플릿 기반 확장
        secondary_templates = [
            "{topic} applications",
            "{topic} challenges",
            "{topic} recent advances",
            "{topic} framework",
            "{topic} evaluation",
        ]
        secondary_queries = [t.format(topic=topic) for t in secondary_templates]

        # 3. 동의어 확장
        for kw in keywords[:2]:
            if kw in SYNONYM_MAP:
                for syn in SYNONYM_MAP[kw][:2]:
                    secondary_queries.append(topic.replace(kw, syn))

        return ResearchPlan(
            original_topic=topic,
            primary_queries=primary_queries[:6],
            secondary_queries=secondary_queries[:6],
            expansion_queries=[],  # 탐색 중 동적 생성
            keywords=set(keywords)
        )

    def _iterate_search(
        self,
        plan: ResearchPlan,
        state: ResearchState,
        config: ResearchConfig
    ) -> ResearchState:
        """단일 탐색 반복 수행"""
        initial_count = len(state.papers_found)

        # 1. 이번 반복의 쿼리 결정
        queries = plan.get_queries_for_iteration(state.iteration)

        # 첫 반복 이후에는 expansion 쿼리도 포함
        if state.iteration > 2 and plan.expansion_queries:
            queries.extend(plan.expansion_queries[-2:])

        # 2. 다중 소스 검색
        added_count = 0
        query_api_failures = 0  # 이번 반복에서의 API 호출 실패 수
        query_api_attempts = 0  # 이번 반복에서의 API 호출 시도 수
        for query in queries:
            if query in state.queries_executed:
                continue
            if not query.strip():
                continue

            state.queries_executed.add(query)
            logger.debug(f"Executing query: {query}")

            # Semantic Scholar
            if config.sources in ["all", "academic"]:
                query_api_attempts += 1
                try:
                    papers = self.ss_client.search_papers(
                        query,
                        limit=config.papers_per_source,
                        year_range=config.year_range
                    )
                    added_count += self._add_papers_to_state(papers, state, config, source_db="semantic_scholar")
                    logger.debug(f"Semantic Scholar: {len(papers)} papers")
                except (OSError, ValueError, TypeError, AttributeError, RuntimeError, requests.RequestException) as e:
                    query_api_failures += 1
                    logger.warning(f"Semantic Scholar error: {e}")

            # arXiv (프리프린트)
            if config.sources in ["all", "academic"]:
                query_api_attempts += 1
                try:
                    papers = self.arxiv_client.search_papers(
                        query,
                        limit=config.papers_per_source,
                        year_range=config.year_range
                    )
                    added_count += self._add_papers_to_state(papers, state, config, source_db="arxiv")
                    logger.debug(f"arXiv: {len(papers)} papers")
                except (OSError, ValueError, TypeError, AttributeError, RuntimeError, requests.RequestException) as e:
                    query_api_failures += 1
                    logger.warning(f"arXiv error: {e}")

            # ERIC (교육학)
            if config.sources in ["all", "academic"]:
                query_api_attempts += 1
                try:
                    papers = self.eric_client.search_papers(
                        query,
                        limit=config.papers_per_source // 2,
                        year_range=config.year_range
                    )
                    added_count += self._add_papers_to_state(papers, state, config, source_db="eric")
                    logger.debug(f"ERIC: {len(papers)} papers")
                except (OSError, ValueError, TypeError, AttributeError, RuntimeError, requests.RequestException) as e:
                    query_api_failures += 1
                    logger.warning(f"ERIC error: {e}")

            # OpenAlex (usage-based free tier)
            if config.sources in ["all", "academic"]:
                query_api_attempts += 1
                try:
                    papers = self.openalex_client.search_papers(
                        query,
                        limit=config.papers_per_source,
                        year_range=config.year_range
                    )
                    added_count += self._add_papers_to_state(papers, state, config, source_db="openalex")
                    logger.debug(f"OpenAlex: {len(papers)} papers")
                except (OSError, ValueError, TypeError, AttributeError, RuntimeError, requests.RequestException) as e:
                    query_api_failures += 1
                    logger.warning(f"OpenAlex error: {e}")

            # KCI (국내 학술지 — 인증키 있을 때만)
            if config.sources in ["all", "academic"] and self.kci_client.available:
                query_api_attempts += 1
                try:
                    papers = self.kci_client.search_papers(
                        query,
                        limit=config.papers_per_source // 2,
                        year_range=config.year_range
                    )
                    added_count += self._add_papers_to_state(papers, state, config, source_db="kci")
                    logger.debug(f"KCI: {len(papers)} papers")
                except (OSError, ValueError, TypeError, AttributeError, RuntimeError, requests.RequestException) as e:
                    query_api_failures += 1
                    logger.warning(f"KCI error: {e}")

        # API 전체 실패 추적: 모든 API 호출이 실패했으면 카운터 증가
        if query_api_attempts > 0 and query_api_failures == query_api_attempts:
            state.consecutive_api_failures += 1
            logger.warning(f"All API calls failed this iteration ({state.consecutive_api_failures}/5)")
        elif query_api_attempts > 0:
            state.consecutive_api_failures = 0  # 하나라도 성공하면 리셋

        # 3. 탐색 확장 키워드 추출 (발견된 논문에서)
        if added_count > 0:
            # 최근 추가된 논문들에서 키워드 추출
            recent_papers = list(state.papers_found.values())[-15:]
            new_keywords = self._extract_keywords_from_papers(recent_papers)
            new_expansion = self._generate_expansion_queries(new_keywords, plan)
            plan.expansion_queries.extend(new_expansion)
            state.keywords_discovered.update(new_keywords)
            state.no_new_papers_count = 0
        else:
            state.no_new_papers_count += 1

        return state

    def _add_papers_to_state(
        self,
        papers: List[Paper],
        state: ResearchState,
        config: ResearchConfig,
        source_db: str = "unknown"
    ) -> int:
        """논문을 상태에 추가 (중복 제거, 품질 등급 부여, sources.jsonl 저장)"""
        added = 0
        seen_titles = {normalize_title(p.title) for p in state.papers_found.values()}
        seen_dois = {p.doi for p in state.papers_found.values() if p.doi}

        current_year = datetime.now().year
        fetched_at = datetime.now().isoformat()

        for paper in papers:
            # DOI 기반 중복 체크
            if paper.doi and paper.doi in seen_dois:
                continue

            # 제목 기반 중복 체크
            norm_title = normalize_title(paper.title)
            if norm_title in seen_titles:
                continue

            # 소스 정보 및 품질 등급 부여
            paper.source_db = source_db
            paper.fetched_at = fetched_at
            paper.quality_grade = grade_paper(
                venue=paper.venue,
                arxiv_id=paper.arxiv_id,
                doi=paper.doi,
                source_db=source_db,
                citation_count=paper.citation_count,
                is_peer_reviewed=paper.is_peer_reviewed
            )

            # 추가
            state.papers_found[paper.paper_id] = paper
            seen_titles.add(norm_title)
            if paper.doi:
                seen_dois.add(paper.doi)
            added += 1

            # sources.jsonl에 저장
            if self.session_manager._initialized:
                self.session_manager.add_source(paper.to_source_dict())

            # 품질 메트릭 업데이트
            if paper.citation_count >= 50:
                state.high_quality_count += 1
            if paper.year and paper.year >= current_year - 2:
                state.recent_paper_count += 1

            # 목표 달성 체크
            if len(state.papers_found) >= config.max_papers:
                break

        return added

    def _extract_keywords_from_papers(self, papers: List[Paper]) -> Set[str]:
        """논문 제목/초록에서 키워드 추출"""
        all_tokens = []

        for paper in papers:
            if paper.title:
                all_tokens.extend(tokenize(paper.title))
            if paper.abstract:
                # 초록에서는 상위 빈도 토큰만
                abstract_tokens = tokenize(paper.abstract)
                all_tokens.extend(abstract_tokens[:20])

        # 빈도 기반 상위 키워드
        counter = Counter(all_tokens)
        top_keywords = {kw for kw, count in counter.most_common(10) if count >= 2}

        return top_keywords

    def _generate_expansion_queries(
        self,
        new_keywords: Set[str],
        plan: ResearchPlan
    ) -> List[str]:
        """새 키워드로 확장 쿼리 생성"""
        expansion = []

        # 새로 발견된 키워드 중 기존에 없던 것
        novel_keywords = new_keywords - plan.keywords

        for kw in list(novel_keywords)[:3]:
            # 원본 주제와 결합
            expansion.append(f"{plan.original_topic} {kw}")

        return expansion

    def _aggregate_results(
        self,
        topic: str,
        plan: ResearchPlan,
        state: ResearchState,
        config: ResearchConfig
    ) -> DeepResearchResult:
        """결과 통합 및 분석"""
        papers = list(state.papers_found.values())
        current_year = datetime.now().year

        # 1. 의미 관련성 재채점 (LLM; 실패 시 _calculate_relevance fallback)
        for paper in papers:
            if paper.relevance_score == 0.0:
                paper.relevance_score = self._calculate_relevance(paper, topic)
        rr = rerank(topic, papers, top_k=len(papers))
        on_topic = rr.papers
        for paper in on_topic:
            paper.relevance_level = (
                RelevanceLevel.HIGH if paper.relevance_score > 0.7
                else RelevanceLevel.MODERATE if paper.relevance_score > 0.4
                else RelevanceLevel.LOW
            )
        # 종합 (on-topic 통독)
        syn = synthesize(topic, on_topic)

        # 2. 분류
        influential = [p for p in on_topic if p.citation_count >= 50]
        recent = [p for p in on_topic if p.year and p.year >= current_year - 2]
        other = [p for p in on_topic if p not in influential and p not in recent]

        # 정렬 (관련성 → 인용수)
        influential.sort(key=lambda p: (-p.relevance_score, -p.citation_count))
        recent.sort(key=lambda p: (-p.relevance_score, -(p.year or 0)))
        other.sort(key=lambda p: -p.relevance_score)

        # 3. 트렌드 분석
        year_dist = Counter(p.year for p in papers if p.year)
        venue_dist = Counter(p.venue for p in papers if p.venue)

        # 저자 통계
        author_counts: Dict[str, Dict[str, Any]] = {}
        for paper in papers:
            for author in paper.authors[:3]:  # 상위 3저자만
                if author.name not in author_counts:
                    author_counts[author.name] = {"papers": 0, "citations": 0}
                author_counts[author.name]["papers"] += 1
                author_counts[author.name]["citations"] += paper.citation_count

        top_authors = sorted(
            [{"name": name, **stats} for name, stats in author_counts.items()],
            key=lambda x: (-x["papers"], -x["citations"])
        )[:10]

        # 4. 새로 발견된 키워드 (트렌드)
        keyword_counts = Counter()
        for paper in papers:
            if paper.year and paper.year >= current_year - 2:
                if paper.title:
                    keyword_counts.update(tokenize(paper.title))

        emerging_keywords = [
            {"keyword": kw, "count": count}
            for kw, count in keyword_counts.most_common(10)
            if count >= 3
        ]

        # 5. 연계 정보
        key_dois = [p.doi for p in influential[:10] if p.doi]
        discovered_kws = list(state.keywords_discovered)[:20]

        return DeepResearchResult(
            topic=topic,
            config=config,
            influential_papers=influential[:20],
            recent_papers=recent[:20],
            other_papers=other[:30],
            emerging_keywords=emerging_keywords,
            year_distribution=dict(year_dist),
            venue_distribution=dict(venue_dist.most_common(10)),
            author_stats=top_authors,
            key_paper_dois=key_dois,
            discovered_keywords=discovered_kws,
            synthesis={
                "themes": syn.themes, "consensus": syn.consensus, "conflicts": syn.conflicts,
                "gaps": syn.gaps, "limitations": syn.limitations,
                "cited_ids": syn.cited_ids, "flagged_uncited": syn.flagged_uncited, "mode": syn.mode,
            },
            near_matches=rr.near_matches,
            rerank_mode=rr.mode,
            rerank_dropped=rr.dropped,
            total_papers=len(papers),
            iterations_completed=state.iteration,
            queries_executed=len(state.queries_executed)
        )

    def _calculate_relevance(self, paper: Paper, topic: str) -> float:
        """관련성 점수 계산 (0.0 ~ 1.0)"""
        score = 0.0
        topic_terms = set(tokenize(topic))

        if not topic_terms:
            return 0.5

        # 제목 매칭 (40%)
        if paper.title:
            title_terms = set(tokenize(paper.title))
            if title_terms:
                title_overlap = len(topic_terms & title_terms) / len(topic_terms)
                score += min(title_overlap, 1.0) * 0.4

        # 초록 매칭 (30%)
        if paper.abstract:
            abstract_terms = set(tokenize(paper.abstract))
            if abstract_terms:
                abstract_overlap = len(topic_terms & abstract_terms) / len(topic_terms)
                score += min(abstract_overlap * 2, 1.0) * 0.3  # 초록은 더 관대하게

        # 인용수 보너스 (20%)
        if paper.citation_count >= 100:
            score += 0.2
        elif paper.citation_count >= 50:
            score += 0.15
        elif paper.citation_count >= 10:
            score += 0.1
        elif paper.citation_count >= 5:
            score += 0.05

        # 최신성 보너스 (10%)
        current_year = datetime.now().year
        if paper.year:
            if paper.year >= current_year - 1:
                score += 0.1
            elif paper.year >= current_year - 2:
                score += 0.07
            elif paper.year >= current_year - 3:
                score += 0.04

        return min(score, 1.0)

    def write_multi_file_report(self, result: DeepResearchResult) -> Path:
        """
        다중 파일 리포트 작성 (세션 폴더에 저장)

        outputs/
        ├── 00_executive_summary.md
        ├── 01_influential_papers.md
        ├── 02_recent_papers.md
        ├── 03_trends.md
        └── sources/
            └── bibliography.md
        """
        if not self.session_manager._initialized:
            logger.warning("Session not initialized, falling back to single file report")
            return self.write_report(result)

        output_dir = self.session_manager.session_path / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. Executive Summary
        self._write_executive_summary(result, output_dir, timestamp)

        # 2. Influential Papers
        self._write_influential_papers(result, output_dir)

        # 3. Recent Papers
        self._write_recent_papers(result, output_dir)

        # 4. Trends
        self._write_trends(result, output_dir)

        # 5. Bibliography (품질 요약 포함)
        self._write_bibliography(result)

        # 6. Research Gaps (synthesis 기반)
        self._write_research_gaps(result, output_dir)

        logger.info(f"Multi-file report saved to: {output_dir}")
        return output_dir

    def _write_executive_summary(self, result: DeepResearchResult, output_dir: Path, timestamp: str):
        """Executive Summary 작성"""
        lines = []
        lines.append(f"# Deep Research: \"{result.topic}\"")
        lines.append("")
        lines.append(f"*Generated: {timestamp}*")
        lines.append(f"*Depth: {result.config.depth} | Sources: {result.config.sources} | Papers: {result.total_papers}*")
        lines.append("")

        lines.append("## Executive Summary")
        lines.append("")

        if result.influential_papers:
            top = result.influential_papers[0]
            lines.append(f"- **Most Influential**: {top.short_citation} ({top.citation_count} citations)")

        syn = result.synthesis or {}
        if syn.get("mode") == "llm" and syn.get("themes"):
            lines.append(f"- **핵심 주제**: {', '.join(syn['themes'][:3])}")
        elif result.emerging_keywords:
            kws = ", ".join(k["keyword"] for k in result.emerging_keywords[:3])
            lines.append(f"- **Emerging Keywords**: {kws}")

        recent_count = len(result.recent_papers)
        lines.append(f"- **Recent Papers (2 years)**: {recent_count} papers")

        # influential(인용≥50)과 recent(최근2년)는 상호배타가 아니므로 중복 제거 후 카운트
        on_topic_ids = {
            (p.doi or p.paper_id)
            for p in (result.influential_papers + result.recent_papers + result.other_papers)
        }
        on_topic_count = len(on_topic_ids)
        lines.append(f"- **커버리지**: {result.total_papers}편 검색 → {on_topic_count}편 on-topic 채택 "
                     f"(rerank: {result.rerank_mode}, off-topic {result.rerank_dropped}편 제외)")
        if on_topic_count < 3 and result.near_matches:
            lines.append("")
            lines.append("### 근접 후보 (on-topic 희소 — relevant 아님, 참고용)")
            for p in result.near_matches[:5]:
                reason = (p.relevance_reason or "")[:60]
                lines.append(f"- ({p.relevance_score:.2f}) {(p.title or '')[:70]} — {reason}")

        lines.append("")
        lines.append("## Navigation")
        lines.append("")
        lines.append("- [[01_influential_papers|Influential Papers]] - High-citation papers")
        lines.append("- [[02_recent_papers|Recent Papers]] - Last 2 years")
        lines.append("- [[03_trends|Trends & Analysis]] - Keywords, authors, venues")
        lines.append("- [[05_research_gaps|Research Gaps & Limitations]] - 연구 갭 + 한계")
        lines.append("")

        # 품질 분포 요약
        quality_dist = self.session_manager.state.get("quality_distribution", {})
        if quality_dist:
            lines.append("## Source Quality Distribution")
            lines.append("")
            lines.append("| Grade | Count | Description |")
            lines.append("|-------|-------|-------------|")
            lines.append(f"| {get_grade_color('A')} A | {quality_dist.get('A', 0)} | {get_grade_description('A')} |")
            lines.append(f"| {get_grade_color('B')} B | {quality_dist.get('B', 0)} | {get_grade_description('B')} |")
            lines.append(f"| {get_grade_color('C')} C | {quality_dist.get('C', 0)} | {get_grade_description('C')} |")
            lines.append(f"| {get_grade_color('D')} D | {quality_dist.get('D', 0)} | {get_grade_description('D')} |")
            lines.append(f"| {get_grade_color('E')} E | {quality_dist.get('E', 0)} | {get_grade_description('E')} |")
            lines.append("")

        with open(output_dir / "00_executive_summary.md", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def _write_influential_papers(self, result: DeepResearchResult, output_dir: Path):
        """Influential Papers 작성"""
        lines = []
        lines.append(f"# Influential Papers (Citations 50+)")
        lines.append("")
        lines.append(f"*{len(result.influential_papers)} papers found*")
        lines.append("")

        if result.influential_papers:
            lines.append("| # | Title | Authors | Year | Citations | Grade |")
            lines.append("|---|-------|---------|------|-----------|-------|")
            for i, paper in enumerate(result.influential_papers[:20], 1):
                title = paper.title[:45] + "..." if len(paper.title) > 45 else paper.title
                authors = ", ".join(paper.author_names[:2])
                if len(paper.author_names) > 2:
                    authors += " et al."
                grade_icon = get_grade_color(paper.quality_grade)
                lines.append(f"| {i} | {title} | {authors} | {paper.year or 'N/A'} | {paper.citation_count} | {grade_icon} {paper.quality_grade} |")
            lines.append("")

            # 상세 정보
            lines.append("---")
            lines.append("")
            lines.append("## Details")
            lines.append("")
            for i, paper in enumerate(result.influential_papers[:10], 1):
                if paper.url:
                    lines.append(f"### {i}. [{paper.title}]({paper.url})")
                elif paper.doi:
                    lines.append(f"### {i}. [{paper.title}](https://doi.org/{paper.doi})")
                else:
                    lines.append(f"### {i}. {paper.title}")

                lines.append("")
                lines.append(f"**{', '.join(paper.author_names[:3])}** ({paper.year})")
                lines.append(f"Citations: {paper.citation_count} | Grade: {get_grade_color(paper.quality_grade)} {paper.quality_grade}")
                if paper.venue:
                    lines.append(f"Venue: *{paper.venue}*")
                lines.append("")
                if paper.abstract:
                    abstract = paper.abstract[:300] + "..." if len(paper.abstract) > 300 else paper.abstract
                    lines.append(f"> {abstract}")
                    lines.append("")
        else:
            lines.append("*인용 50회 이상 논문 없음 (niche/최신 주제일 수 있음). 관련성 상위 논문은 02_recent_papers / 종합(03_trends)을 참조.*")

        with open(output_dir / "01_influential_papers.md", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def _write_recent_papers(self, result: DeepResearchResult, output_dir: Path):
        """Recent Papers 작성"""
        lines = []
        current_year = datetime.now().year
        lines.append(f"# Recent Papers ({current_year - 1}-{current_year})")
        lines.append("")
        lines.append(f"*{len(result.recent_papers)} papers found*")
        lines.append("")

        if result.recent_papers:
            for i, paper in enumerate(result.recent_papers[:15], 1):
                if paper.url:
                    lines.append(f"### {i}. [{paper.title}]({paper.url})")
                elif paper.doi:
                    lines.append(f"### {i}. [{paper.title}](https://doi.org/{paper.doi})")
                else:
                    lines.append(f"### {i}. {paper.title}")

                lines.append("")
                authors = ", ".join(paper.author_names[:3])
                if len(paper.author_names) > 3:
                    authors += " et al."
                lines.append(f"**{authors}** ({paper.year})")
                lines.append(f"Citations: {paper.citation_count} | Grade: {get_grade_color(paper.quality_grade)} {paper.quality_grade} | Relevance: {paper.relevance_score:.2f}")
                lines.append("")
                if paper.abstract:
                    abstract = paper.abstract[:250] + "..." if len(paper.abstract) > 250 else paper.abstract
                    lines.append(f"> {abstract}")
                    lines.append("")
        else:
            lines.append("*No recent papers found.*")

        with open(output_dir / "02_recent_papers.md", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def _write_trends(self, result: DeepResearchResult, output_dir: Path):
        """Trends 작성"""
        lines = []
        lines.append(f"# Research Trends: \"{result.topic}\"")
        lines.append("")

        # 0. Synthesis (LLM 가용 시 선두 배치)
        syn = result.synthesis or {}
        if syn.get("mode") == "llm" and (syn.get("themes") or syn.get("consensus") or syn.get("conflicts")):
            lines.append("## 종합 (Synthesis)")
            lines.append("")
            if syn.get("themes"):
                lines.append("**주제 묶음**")
                for t in syn["themes"]:
                    lines.append(f"- {t}")
                lines.append("")
            if syn.get("consensus"):
                lines.append("**합의점**")
                for c in syn["consensus"]:
                    lines.append(f"- {c}")
                lines.append("")
            if syn.get("conflicts"):
                lines.append("**불일치/긴장**")
                for c in syn["conflicts"]:
                    lines.append(f"- {c}")
                lines.append("")
            lines.append("> 아래 키워드 빈도/통계는 보조 지표입니다.")
            lines.append("")

        # 1. Emerging Keywords
        lines.append("## 1. Emerging Keywords")
        lines.append("")
        if result.emerging_keywords:
            for i, kw in enumerate(result.emerging_keywords[:10], 1):
                lines.append(f"{i}. **{kw['keyword']}** ({kw['count']} occurrences)")
            lines.append("")
        else:
            lines.append("*No emerging keywords identified.*")
            lines.append("")

        # 2. Key Researchers
        lines.append("## 2. Key Researchers")
        lines.append("")
        if result.author_stats:
            lines.append("| Author | Papers | Total Citations |")
            lines.append("|--------|--------|-----------------|")
            for author in result.author_stats[:10]:
                lines.append(f"| {author['name']} | {author['papers']} | {author['citations']} |")
            lines.append("")
        else:
            lines.append("*No author statistics available.*")
            lines.append("")

        # 3. Top Venues
        lines.append("## 3. Top Venues")
        lines.append("")
        if result.venue_distribution:
            lines.append("| Venue | Papers |")
            lines.append("|-------|--------|")
            for venue, count in list(result.venue_distribution.items())[:10]:
                if venue:
                    venue_name = venue[:50] + "..." if len(venue) > 50 else venue
                    lines.append(f"| {venue_name} | {count} |")
            lines.append("")
        else:
            lines.append("*No venue statistics available.*")
            lines.append("")

        # 4. Year Distribution
        lines.append("## 4. Publication Timeline")
        lines.append("")
        if result.year_distribution:
            sorted_years = sorted(result.year_distribution.items())
            lines.append("| Year | Papers |")
            lines.append("|------|--------|")
            for year, count in sorted_years[-7:]:
                if year:
                    lines.append(f"| {year} | {'█' * min(count, 20)} {count} |")
            lines.append("")
        else:
            lines.append("*No year statistics available.*")
            lines.append("")

        # 5. Discovered Keywords
        lines.append("## 5. Discovered Keywords")
        lines.append("")
        if result.discovered_keywords:
            lines.append("Keywords discovered during iterative search:")
            lines.append("")
            for kw in result.discovered_keywords[:15]:
                lines.append(f"- {kw}")
            lines.append("")
        else:
            lines.append("*No new keywords discovered.*")
            lines.append("")

        with open(output_dir / "03_trends.md", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def _write_research_gaps(self, result: DeepResearchResult, output_dir: Path):
        """05_research_gaps.md — synthesis 기반 연구 갭 + 한계."""
        syn = result.synthesis or {}
        lines = [f"# Research Gaps & Limitations: \"{result.topic}\"", ""]
        gaps = syn.get("gaps") or []
        lims = syn.get("limitations") or []
        if syn.get("mode") == "llm" and (gaps or lims):
            if gaps:
                lines.append("## 미탐구 연구 갭")
                lines.append("")
                for g in gaps:
                    lines.append(f"- {g}")
                lines.append("")
            if lims:
                lines.append("## 이 문헌집합의 한계")
                lines.append("")
                for lim in lims:
                    lines.append(f"- {lim}")
                lines.append("")
        else:
            lines.append("*종합(synthesis)을 사용할 수 없어 연구 갭을 생성하지 못했습니다 (LLM 비가용 또는 on-topic 논문 부족).*")
            lines.append("")
        with open(output_dir / "05_research_gaps.md", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def _write_bibliography(self, result: DeepResearchResult):
        """Bibliography 작성 (품질 요약 포함)"""
        lines = []
        lines.append(f"# Bibliography")
        lines.append("")

        # 품질 요약
        quality_dist = self.session_manager.state.get("quality_distribution", {})
        total = sum(quality_dist.values())

        lines.append("## Source Quality Summary")
        lines.append("")
        lines.append("| Grade | Count | Description |")
        lines.append("|-------|-------|-------------|")
        lines.append(f"| A | {quality_dist.get('A', 0)} | {get_grade_description('A')} |")
        lines.append(f"| B | {quality_dist.get('B', 0)} | {get_grade_description('B')} |")
        lines.append(f"| C | {quality_dist.get('C', 0)} | {get_grade_description('C')} |")
        lines.append(f"| D | {quality_dist.get('D', 0)} | {get_grade_description('D')} |")
        lines.append(f"| E | {quality_dist.get('E', 0)} | {get_grade_description('E')} |")
        lines.append("")
        lines.append(f"**Total: {total} sources**")
        lines.append("")

        # Visual distribution
        lines.append("### Quality Distribution")
        lines.append("```")
        for grade in ["A", "B", "C", "D", "E"]:
            count = quality_dist.get(grade, 0)
            bar = "█" * min(count, 30)
            lines.append(f"{grade} {bar} {count}")
        lines.append("```")
        lines.append("")

        # 등급별 참고문헌
        all_papers = result.influential_papers + result.recent_papers + result.other_papers
        seen_ids = set()
        unique_papers = []
        for p in all_papers:
            if p.paper_id not in seen_ids:
                seen_ids.add(p.paper_id)
                unique_papers.append(p)

        # 등급별 그룹화
        by_grade = {"A": [], "B": [], "C": [], "D": [], "E": []}
        for p in unique_papers:
            grade = p.quality_grade if p.quality_grade in by_grade else "C"
            by_grade[grade].append(p)

        for grade in ["A", "B", "C", "D", "E"]:
            papers = by_grade[grade]
            if papers:
                lines.append(f"---")
                lines.append("")
                lines.append(f"## {get_grade_color(grade)} {grade}-Tier Sources ({len(papers)})")
                lines.append("")
                papers.sort(key=lambda p: (-(p.year or 0), p.first_author or ""))
                for i, paper in enumerate(papers[:30], 1):
                    authors = ", ".join(paper.author_names[:3])
                    if len(paper.author_names) > 3:
                        authors += ", et al."
                    year = f"({paper.year})" if paper.year else "(n.d.)"
                    venue = f"*{paper.venue}*" if paper.venue else ""

                    if paper.doi:
                        lines.append(f"{i}. {authors} {year}. {paper.title}. {venue} https://doi.org/{paper.doi}")
                    elif paper.url:
                        lines.append(f"{i}. {authors} {year}. {paper.title}. {venue} {paper.url}")
                    else:
                        lines.append(f"{i}. {authors} {year}. {paper.title}. {venue}")
                lines.append("")

        lines.append("---")
        lines.append("*Generated by Deep Researcher - Literature Discovery Team*")
        lines.append("")
        lines.append("*Quality grading system inspired by [fivetaku/deep-research-kit](https://github.com/fivetaku/deep-research-kit)*")

        self.session_manager.save_bibliography("\n".join(lines))

    def write_report(self, result: DeepResearchResult, output_path: Optional[str] = None) -> Path:
        """Deep Research 리포트 작성 (단일 파일, 기존 호환)"""
        lines = []
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # === 헤더 ===
        lines.append(f"# Deep Research Report: \"{result.topic}\"")
        lines.append("")
        lines.append(f"*Generated: {timestamp}*")
        lines.append(f"*Depth: {result.config.depth} | Sources: {result.config.sources} | Papers: {result.total_papers}*")
        lines.append(f"*Iterations: {result.iterations_completed} | Queries: {result.queries_executed}*")
        lines.append("")

        # === Executive Summary ===
        lines.append("## Executive Summary")
        lines.append("")

        if result.influential_papers:
            top_paper = result.influential_papers[0]
            lines.append(f"- **Most Influential Work**: {top_paper.short_citation} - \"{top_paper.title[:60]}...\" ({top_paper.citation_count} citations)")

        syn = result.synthesis or {}
        if syn.get("mode") == "llm" and syn.get("themes"):
            lines.append(f"- **핵심 주제**: {', '.join(syn['themes'][:3])}")
        elif result.emerging_keywords:
            top_keywords = [k["keyword"] for k in result.emerging_keywords[:3]]
            lines.append(f"- **Emerging Keywords**: {', '.join(top_keywords)}")

        if result.year_distribution:
            recent_years = [y for y in result.year_distribution.keys() if y and y >= datetime.now().year - 2]
            recent_count = sum(result.year_distribution.get(y, 0) for y in recent_years)
            lines.append(f"- **Recent Papers (2 years)**: {recent_count} papers ({recent_count * 100 // max(result.total_papers, 1)}% of total)")

        on_topic_ids = {(p.doi or p.paper_id) for p in (result.influential_papers + result.recent_papers + result.other_papers)}
        on_topic_count = len(on_topic_ids)
        lines.append(f"- **커버리지**: {result.total_papers}편 검색 → {on_topic_count}편 on-topic 채택 "
                     f"(rerank: {result.rerank_mode}, off-topic {result.rerank_dropped}편 제외)")
        if on_topic_count < 3 and result.near_matches:
            lines.append("")
            lines.append("### 근접 후보 (on-topic 희소 — relevant 아님, 참고용)")
            for p in result.near_matches[:5]:
                reason = (p.relevance_reason or "")[:60]
                lines.append(f"- ({p.relevance_score:.2f}) {(p.title or '')[:70]} — {reason}")

        lines.append("")

        # === Synthesis (종합) ===
        if syn.get("mode") == "llm" and (syn.get("themes") or syn.get("consensus") or syn.get("conflicts")):
            lines.append("## Synthesis (종합)")
            lines.append("")
            if syn.get("themes"):
                lines.append("**주제 묶음**")
                for t in syn["themes"]:
                    lines.append(f"- {t}")
                lines.append("")
            if syn.get("consensus"):
                lines.append("**합의점**")
                for c in syn["consensus"]:
                    lines.append(f"- {c}")
                lines.append("")
            if syn.get("conflicts"):
                lines.append("**불일치/긴장**")
                for c in syn["conflicts"]:
                    lines.append(f"- {c}")
                lines.append("")
            lines.append("> 아래 키워드 빈도/통계는 보조 지표입니다.")
            lines.append("")

        # === 1. 개념 정의 및 배경 ===
        lines.append("## 1. Background")
        lines.append("")
        lines.append(f"This report synthesizes {result.total_papers} papers related to **\"{result.topic}\"**, ")
        lines.append(f"collected through {result.iterations_completed} iterative search cycles across multiple academic databases.")
        lines.append("")

        if result.year_distribution:
            sorted_years = sorted(result.year_distribution.items())
            if sorted_years:
                lines.append("### Publication Timeline")
                lines.append("")
                lines.append("| Year | Papers |")
                lines.append("|------|--------|")
                for year, count in sorted_years[-7:]:  # 최근 7년
                    if year:
                        lines.append(f"| {year} | {count} |")
                lines.append("")

        # === 2. 핵심 문헌 분석 ===
        lines.append("## 2. Key Literature Analysis")
        lines.append("")

        # 2.1 Highly Influential Papers
        lines.append(f"### 2.1 Highly Influential Papers (Citations 50+) - {len(result.influential_papers)} papers")
        lines.append("")
        if result.influential_papers:
            lines.append("| # | Title | Authors | Year | Citations | Relevance |")
            lines.append("|---|-------|---------|------|-----------|-----------|")
            for i, paper in enumerate(result.influential_papers[:15], 1):
                title = paper.title[:50] + "..." if len(paper.title) > 50 else paper.title
                authors = ", ".join(paper.author_names[:2])
                if len(paper.author_names) > 2:
                    authors += " et al."
                lines.append(f"| {i} | {title} | {authors} | {paper.year or 'N/A'} | {paper.citation_count} | {paper.relevance_score:.2f} |")
            lines.append("")
        else:
            lines.append("*No papers with 50+ citations found.*")
            lines.append("")

        # 2.2 Recent Key Papers
        lines.append(f"### 2.2 Recent Key Papers (Last 2 Years) - {len(result.recent_papers)} papers")
        lines.append("")
        if result.recent_papers:
            for i, paper in enumerate(result.recent_papers[:10], 1):
                if paper.url:
                    lines.append(f"**{i}. [{paper.title}]({paper.url})**")
                elif paper.doi:
                    lines.append(f"**{i}. [{paper.title}](https://doi.org/{paper.doi})**")
                else:
                    lines.append(f"**{i}. {paper.title}**")

                authors = ", ".join(paper.author_names[:3])
                if len(paper.author_names) > 3:
                    authors += " et al."
                lines.append(f"*{authors} ({paper.year})* | Citations: {paper.citation_count}")

                if paper.abstract:
                    abstract = paper.abstract[:200] + "..." if len(paper.abstract) > 200 else paper.abstract
                    lines.append(f"> {abstract}")
                lines.append("")
        else:
            lines.append("*No recent papers found.*")
            lines.append("")

        # === 3. 최신 동향 ===
        lines.append("## 3. Current Trends")
        lines.append("")

        # 3.1 Emerging Keywords
        lines.append("### 3.1 Emerging Keywords")
        lines.append("")
        if result.emerging_keywords:
            for i, kw_data in enumerate(result.emerging_keywords[:10], 1):
                lines.append(f"{i}. **{kw_data['keyword']}** - {kw_data['count']} occurrences in recent papers")
        else:
            lines.append("*No emerging keywords identified.*")
        lines.append("")

        # 3.2 Top Researchers
        lines.append("### 3.2 Key Researchers")
        lines.append("")
        if result.author_stats:
            lines.append("| Author | Papers | Total Citations |")
            lines.append("|--------|--------|-----------------|")
            for author in result.author_stats[:10]:
                lines.append(f"| {author['name']} | {author['papers']} | {author['citations']} |")
            lines.append("")
        else:
            lines.append("*No author statistics available.*")
            lines.append("")

        # 3.3 Top Venues
        lines.append("### 3.3 Top Venues")
        lines.append("")
        if result.venue_distribution:
            lines.append("| Venue | Papers |")
            lines.append("|-------|--------|")
            for venue, count in list(result.venue_distribution.items())[:10]:
                if venue:
                    venue_name = venue[:50] + "..." if len(venue) > 50 else venue
                    lines.append(f"| {venue_name} | {count} |")
            lines.append("")
        else:
            lines.append("*No venue statistics available.*")
            lines.append("")

        # === 4. 연구 갭 및 기회 ===
        lines.append("## 4. Research Gaps & Opportunities")
        lines.append("")

        if syn.get("mode") == "llm" and (syn.get("gaps") or syn.get("limitations")):
            lines.append("### 4.1 미탐구 연구 갭")
            lines.append("")
            for g in syn.get("gaps", []):
                lines.append(f"- {g}")
            lines.append("")
            if syn.get("limitations"):
                lines.append("### 4.2 이 문헌집합의 한계")
                lines.append("")
                for lim in syn["limitations"]:
                    lines.append(f"- {lim}")
                lines.append("")
        else:
            lines.append("### 4.1 Potential Research Directions")
            lines.append("")
            # 템플릿 기반 연구 갭 제안 (synthesis 미가용 시 폴백)
            if result.discovered_keywords:
                lines.append("Based on the discovered keywords, potential unexplored combinations include:")
                lines.append("")
                for kw in result.discovered_keywords[:5]:
                    lines.append(f"- {result.topic} + **{kw}**")
                lines.append("")
            else:
                lines.append("*종합(synthesis)을 사용할 수 없어 연구 갭을 생성하지 못했습니다 (LLM 비가용 또는 on-topic 논문 부족).*")
                lines.append("")

        lines.append("### 4.3 Suggested Follow-up Research")
        lines.append("")
        lines.append(f"- Deep dive into specific subtopics using `related-paper-finder`")
        lines.append(f"- Analyze citation networks of influential papers using `citation-network-explorer`")
        lines.append(f"- Track emerging trends with `research-trend-analyzer`")
        lines.append("")

        # === 5. 추가 분석 제안 ===
        lines.append("## 5. Follow-up Analysis Suggestions")
        lines.append("")

        if result.key_paper_dois:
            lines.append("### Citation Network Analysis")
            lines.append("Analyze citation networks for these influential papers:")
            lines.append("")
            for doi in result.key_paper_dois[:5]:
                lines.append(f"```bash")
                lines.append(f"python citation_network_explorer.py --doi \"{doi}\"")
                lines.append(f"```")
            lines.append("")

        if result.discovered_keywords:
            lines.append("### Additional Paper Search")
            lines.append("Explore these discovered keywords:")
            lines.append("")
            for kw in result.discovered_keywords[:5]:
                lines.append(f"```bash")
                lines.append(f"python related_paper_finder.py --keywords \"{result.topic} {kw}\"")
                lines.append(f"```")
            lines.append("")

        # === 6. 참고문헌 ===
        lines.append("## 6. References (APA Style)")
        lines.append("")

        # 모든 논문 합치기 (중복 제거 후)
        all_papers = result.influential_papers + result.recent_papers
        seen_ids = set()
        unique_papers = []
        for p in all_papers:
            if p.paper_id not in seen_ids:
                seen_ids.add(p.paper_id)
                unique_papers.append(p)

        # 정렬 (연도 → 저자)
        unique_papers.sort(key=lambda p: (-(p.year or 0), p.first_author or ""))

        for i, paper in enumerate(unique_papers[:50], 1):
            authors = ", ".join(paper.author_names[:3])
            if len(paper.author_names) > 3:
                authors += ", et al."

            year = f"({paper.year})" if paper.year else "(n.d.)"
            title = paper.title
            venue = f"*{paper.venue}*" if paper.venue else ""

            if paper.doi:
                lines.append(f"{i}. {authors} {year}. {title}. {venue} https://doi.org/{paper.doi}")
            elif paper.url:
                lines.append(f"{i}. {authors} {year}. {title}. {venue} {paper.url}")
            else:
                lines.append(f"{i}. {authors} {year}. {title}. {venue}")

        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(f"*Report generated by Deep Researcher - Literature Discovery Team*")
        lines.append("")
        lines.append("*Architecture inspired by [fivetaku/deep-research-kit](https://github.com/fivetaku/deep-research-kit)*")

        # 파일 저장
        if output_path:
            filepath = Path(output_path)
        else:
            filename = self.writer._generate_filename("deep_research", result.topic)
            filepath = self.writer.output_dir / filename

        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        logger.info(f"Deep research report saved: {filepath}")
        return filepath


# =============================================================================
# CLI
# =============================================================================

def parse_args():
    """CLI 인자 파싱"""
    parser = argparse.ArgumentParser(
        description="Deep Researcher - 심층 자료 탐색",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  # 기본 탐색 (medium depth)
  python deep_researcher.py "Agentic AI in education"

  # 심층 탐색
  python deep_researcher.py "LLM applications" --depth deep

  # 학술 소스만
  python deep_researcher.py "transformer architecture" --sources academic

  # 연도 범위 지정
  python deep_researcher.py "prompt engineering" --year-range 2022-2026

  # 세션 목록 확인
  python deep_researcher.py --list-sessions

  # 세션 재개
  python deep_researcher.py --resume session_id_here

  # 출력 파일 지정
  python deep_researcher.py "AI ethics" -o my_report.md

  # 검증 없이 탐색만
  python deep_researcher.py "AI ethics" --no-verify

  # 검증 샘플 수 지정
  python deep_researcher.py "AI ethics" --verify-sample 20
        """
    )

    # 위치 인자 (세션 관련 옵션 사용 시 선택적)
    parser.add_argument(
        "topic",
        nargs="?",
        default=None,
        help="탐색 주제"
    )

    # 세션 관리 옵션
    parser.add_argument(
        "--resume", "-r",
        metavar="SESSION_ID",
        help="이전 세션 재개"
    )
    parser.add_argument(
        "--list-sessions", "-l",
        action="store_true",
        help="모든 세션 목록 출력"
    )

    # 탐색 설정
    parser.add_argument(
        "--depth", "-d",
        choices=["shallow", "medium", "deep"],
        default="medium",
        help="탐색 깊이 (기본: medium)"
    )
    parser.add_argument(
        "--sources", "-s",
        choices=["all", "academic", "web"],
        default="all",
        help="소스 범위 (기본: all)"
    )
    parser.add_argument(
        "--year-range", "-y",
        help="연도 범위 (예: 2020-2026)"
    )

    # 출력 옵션
    parser.add_argument(
        "--output", "-o",
        help="출력 파일 경로"
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="리포트 파일 생성 안 함"
    )
    parser.add_argument(
        "--single-file",
        action="store_true",
        help="단일 파일 리포트 생성 (기본: 다중 파일)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="상세 로그 출력"
    )

    # 검증 옵션
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="리포트 검증 건너뛰기 (기본: 검증 자동 실행)"
    )
    parser.add_argument(
        "--verify-sample",
        type=int,
        default=10,
        help="검증 시 CrossRef에서 확인할 논문 수 (기본: 10)"
    )

    return parser.parse_args()


def _manifest(action: str, error: str = ""):
    """Agent Monitor 매니페스트 등록/해제 헬퍼.

    Monitoring 탭에 deep-researcher 실행 상태를 반영합니다.
    manifest_helper.py 호출 실패 시에도 탐색 자체는 중단하지 않습니다.
    """
    import subprocess
    helper = Path(__file__).resolve().parent.parent.parent / "Agents/Lab Director/agent-monitor/manifest_helper.py"
    if not helper.exists():
        return
    try:
        cmd = [sys.executable, str(helper), action, "deep-researcher"]
        if action == "start":
            cmd += ["--workflow", "literature-discovery"]
        elif action == "fail" and error:
            cmd += ["--error", error[:200]]
        subprocess.run(cmd, capture_output=True, timeout=10)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass


def main():
    """메인 함수"""
    args = parse_args()

    # 로그 레벨 설정
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # 설정 로드
    config = Config.load()

    # Deep Researcher 인스턴스
    researcher = DeepResearcher(config)

    # === 세션 목록 출력 ===
    if args.list_sessions:
        sessions = researcher.list_sessions()
        if not sessions:
            print("No sessions found.")
            return

        print(f"\n{'='*80}")
        print("Deep Research Sessions")
        print(f"{'='*80}")
        print(f"{'ID':<40} {'Status':<12} {'Papers':<8} {'Updated':<20}")
        print("-" * 80)
        for session in sessions:
            session_id = session.get("session_id", "")[:38]
            status = session.get("status", "")
            papers = session.get("sources_count", 0)
            updated = session.get("updated_at", "")[:19]
            resume_mark = " [resumable]" if session.get("can_resume") else ""
            print(f"{session_id:<40} {status:<12} {papers:<8} {updated:<20}{resume_mark}")

        print("-" * 80)
        print(f"Total: {len(sessions)} sessions")
        print("\nTo resume a session:")
        print("  python deep_researcher.py --resume <SESSION_ID>")
        return

    # === 세션 재개 ===
    if args.resume:
        session_id = args.resume
        print(f"\nResuming session: {session_id}")
        _manifest("start")

        try:
            result = researcher.resume_research(session_id)
            if result:
                _print_result_summary(result, session_id)

                # 리포트 생성
                if not args.no_report:
                    if args.single_file or args.output:
                        report_path = researcher.write_report(result, args.output)
                        print(f"\nReport saved: {report_path}")
                    else:
                        report_path = researcher.write_multi_file_report(result)
                        print(f"\nMulti-file report saved to: {report_path}")

                # 검증 (기본 실행, --no-verify 시 건너뜀)
                if not args.no_verify:
                    _run_verification(session_id, args.verify_sample, config)
                _manifest("complete")
            else:
                _manifest("fail", "Failed to resume session")
                print(f"Failed to resume session: {session_id}")
                sys.exit(1)

        except KeyboardInterrupt:
            _manifest("complete")
            print("\n\nSearch paused. You can resume later with:")
            print(f"  python deep_researcher.py --resume {session_id}")
            sys.exit(0)
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError, requests.RequestException) as e:
            _manifest("fail", str(e))
            logger.error(f"Error during research: {e}")
            if args.verbose:
                import traceback
                traceback.print_exc()
            sys.exit(1)
        return

    # === 새 탐색 ===
    if not args.topic:
        print("Error: topic is required for new research.")
        print("Usage: python deep_researcher.py \"your topic here\"")
        print("       python deep_researcher.py --list-sessions")
        print("       python deep_researcher.py --resume SESSION_ID")
        sys.exit(1)

    # 연도 범위 파싱
    year_start, year_end = None, None
    if args.year_range:
        try:
            parts = args.year_range.split("-")
            year_start = int(parts[0])
            year_end = int(parts[1])
        except (ValueError, IndexError):
            logger.error(f"Invalid year range format: {args.year_range}")
            sys.exit(1)

    # 탐색 설정
    research_config = ResearchConfig(
        depth=args.depth,
        sources=args.sources,
        year_start=year_start,
        year_end=year_end
    )

    _manifest("start")

    try:
        result = researcher.research(args.topic, research_config)

        # 콘솔 출력
        _print_result_summary(result, researcher.session_manager.session_id)

        # 리포트 생성
        if not args.no_report:
            if args.single_file or args.output:
                report_path = researcher.write_report(result, args.output)
                print(f"\nReport saved: {report_path}")
            else:
                report_path = researcher.write_multi_file_report(result)
                print(f"\nMulti-file report saved to: {report_path}")

        # 검증 (기본 실행, --no-verify 시 건너뜀)
        if not args.no_verify:
            _run_verification(researcher.session_manager.session_id, args.verify_sample, config)

        _manifest("complete")

    except KeyboardInterrupt:
        _manifest("complete")
        session_id = researcher.session_manager.session_id
        print("\n\nSearch paused. You can resume later with:")
        print(f"  python deep_researcher.py --resume {session_id}")
        sys.exit(0)
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError, requests.RequestException) as e:
        _manifest("fail", str(e))
        logger.error(f"Error during research: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


def _run_verification(session_id: str, sample_size: int, config: Config):
    """검증 실행"""
    from report_verifier import ReportVerifier

    print(f"\n{'=' * 60}")
    print("Running verification...")
    print(f"{'=' * 60}")

    try:
        verifier = ReportVerifier(config)
        v_report = verifier.verify_session(
            session_id=session_id,
            sample_size=sample_size,
        )

        # 리포트 저장
        md_path = verifier.write_report(v_report)
        verifier.write_json_report(v_report)

        # 요약 출력
        print(f"\nVerification Score: {v_report.overall_score:.2f}")
        print(f"Verdict: {v_report.verdict}")
        for phase in v_report.phases:
            print(f"  Phase {phase.phase} [{phase.name}]: {phase.score:.2f} (pass={phase.pass_count}, warn={phase.warn_count}, fail={phase.fail_count})")

        print(f"\nVerification report: {md_path}")

    except (OSError, ValueError, TypeError, RuntimeError, requests.RequestException) as e:
        logger.warning(f"Verification failed (non-blocking): {e}")
        print(f"\nVerification skipped due to error: {e}")


def _print_result_summary(result: DeepResearchResult, session_id: str):
    """결과 요약 출력"""
    print(f"\n{'='*60}")
    print(f"Deep Research Complete: \"{result.topic}\"")
    print(f"{'='*60}")
    print(f"Session ID: {session_id}")
    print(f"Total papers found: {result.total_papers}")
    print(f"  - Influential (50+ citations): {len(result.influential_papers)}")
    print(f"  - Recent (last 2 years): {len(result.recent_papers)}")
    print(f"Iterations completed: {result.iterations_completed}")
    print(f"Queries executed: {result.queries_executed}")

    if result.emerging_keywords:
        print(f"\nEmerging keywords:")
        for kw in result.emerging_keywords[:5]:
            print(f"  - {kw['keyword']} ({kw['count']})")


if __name__ == "__main__":
    main()
