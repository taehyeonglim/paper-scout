"""
Literature Discovery Team - Data Models

논문, 저자, 인용 관계 등의 데이터 모델 정의
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from datetime import datetime
from enum import Enum


class RelevanceLevel(Enum):
    """논문 관련성 수준"""
    HIGH = "high"           # 유사도 > 0.8
    MODERATE = "moderate"   # 유사도 0.5-0.8
    LOW = "low"             # 유사도 < 0.5


@dataclass
class Author:
    """저자 정보"""
    name: str
    affiliation: Optional[str] = None
    author_id: Optional[str] = None  # Semantic Scholar Author ID
    orcid: Optional[str] = None
    h_index: Optional[int] = None
    paper_count: Optional[int] = None
    citation_count: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "affiliation": self.affiliation,
            "author_id": self.author_id,
            "orcid": self.orcid,
            "h_index": self.h_index,
        }


@dataclass
class Paper:
    """논문 정보"""
    # 기본 식별 정보
    paper_id: str                       # Semantic Scholar ID
    title: str

    # 저자 및 출판 정보
    authors: List[Author] = field(default_factory=list)
    year: Optional[int] = None
    venue: Optional[str] = None         # 저널/학회명
    abstract: Optional[str] = None

    # 외부 식별자
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    pubmed_id: Optional[str] = None

    # 인용 통계
    citation_count: int = 0
    reference_count: int = 0
    influential_citation_count: int = 0

    # URL
    url: Optional[str] = None
    pdf_url: Optional[str] = None

    # 분야 정보
    fields_of_study: List[str] = field(default_factory=list)

    # Related Paper Finder용 - 관련성 정보
    relevance_score: float = 0.0
    relevance_reason: Optional[str] = None
    relevance_level: RelevanceLevel = RelevanceLevel.LOW

    # Citation Network Explorer용 - 네트워크 분석 정보
    pagerank: float = 0.0
    betweenness: float = 0.0
    in_degree: int = 0
    out_degree: int = 0
    cluster_id: Optional[int] = None

    # Deep Researcher용 - 소스 품질 등급 (A-E)
    quality_grade: str = "C"            # A: 최상위, B: 주요, C: 일반, D: 프리프린트, E: 비학술
    source_db: Optional[str] = None     # semantic_scholar, arxiv, eric
    is_peer_reviewed: Optional[bool] = None  # ERIC에서 제공
    fetched_at: Optional[str] = None    # ISO 형식 타임스탬프

    @property
    def author_names(self) -> List[str]:
        """저자 이름 목록"""
        return [a.name for a in self.authors]

    @property
    def first_author(self) -> Optional[str]:
        """제1저자 이름"""
        return self.authors[0].name if self.authors else None

    @property
    def short_citation(self) -> str:
        """짧은 인용 형식 (Author, Year)"""
        author = self.first_author or "Unknown"
        year = self.year or "n.d."
        return f"{author}, {year}"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Paper":
        """to_dict()로 직렬화된 dict에서 Paper 복원"""
        authors = [
            Author(name=a.get("name", ""), author_id=a.get("author_id"),
                   affiliation=a.get("affiliation"))
            if isinstance(a, dict) else a
            for a in data.get("authors", [])
        ]
        relevance_level = data.get("relevance_level", "low")
        if isinstance(relevance_level, str):
            try:
                relevance_level = RelevanceLevel(relevance_level)
            except ValueError:
                relevance_level = RelevanceLevel.LOW

        return cls(
            paper_id=data.get("paper_id", ""),
            title=data.get("title", ""),
            authors=authors,
            year=data.get("year"),
            venue=data.get("venue"),
            abstract=data.get("abstract"),
            doi=data.get("doi"),
            arxiv_id=data.get("arxiv_id"),
            citation_count=data.get("citation_count", 0),
            reference_count=data.get("reference_count", 0),
            url=data.get("url"),
            fields_of_study=data.get("fields_of_study", []),
            relevance_score=data.get("relevance_score", 0.0),
            relevance_level=relevance_level,
            quality_grade=data.get("quality_grade", "C"),
            source_db=data.get("source_db"),
        )

    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리 변환"""
        return {
            "paper_id": self.paper_id,
            "title": self.title,
            "authors": [a.to_dict() for a in self.authors],
            "year": self.year,
            "venue": self.venue,
            "abstract": self.abstract,
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "citation_count": self.citation_count,
            "reference_count": self.reference_count,
            "url": self.url,
            "fields_of_study": self.fields_of_study,
            "relevance_score": self.relevance_score,
            "relevance_level": self.relevance_level.value,
            "quality_grade": self.quality_grade,
        }

    def to_source_dict(self) -> Dict[str, Any]:
        """sources.jsonl 저장용 딕셔너리 (Deep Research Kit 형식)"""
        return {
            "id": self.paper_id,
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "title": self.title,
            "authors": self.author_names,
            "year": self.year,
            "venue": self.venue,
            "citations": self.citation_count,
            "source_db": self.source_db,
            "quality_grade": self.quality_grade,
            "relevance_score": round(self.relevance_score, 3) if self.relevance_score else 0,
            "is_peer_reviewed": self.is_peer_reviewed,
            "url": self.url or (f"https://doi.org/{self.doi}" if self.doi else None),
            "fetched_at": self.fetched_at,
            "fields_of_study": self.fields_of_study[:3] if self.fields_of_study else [],
        }

    @classmethod
    def from_semantic_scholar(cls, data: Dict[str, Any]) -> "Paper":
        """Semantic Scholar API 응답에서 Paper 생성"""
        authors = []
        for author_data in data.get("authors", []):
            authors.append(Author(
                name=author_data.get("name", "Unknown"),
                author_id=author_data.get("authorId"),
            ))

        return cls(
            paper_id=data.get("paperId", ""),
            title=data.get("title", ""),
            authors=authors,
            year=data.get("year"),
            venue=data.get("venue"),
            abstract=data.get("abstract"),
            doi=data.get("externalIds", {}).get("DOI"),
            arxiv_id=data.get("externalIds", {}).get("ArXiv"),
            pubmed_id=data.get("externalIds", {}).get("PubMed"),
            citation_count=data.get("citationCount", 0),
            reference_count=data.get("referenceCount", 0),
            influential_citation_count=data.get("influentialCitationCount", 0),
            url=data.get("url"),
            pdf_url=data.get("openAccessPdf", {}).get("url") if data.get("openAccessPdf") else None,
            fields_of_study=[f.get("category", "") for f in data.get("s2FieldsOfStudy", [])],
        )

    @classmethod
    def from_arxiv(cls, result) -> "Paper":
        """arXiv API 결과에서 Paper 생성"""
        authors = [Author(name=a.name) for a in result.authors]

        # arXiv ID 추출 (URL에서)
        arxiv_id = result.entry_id.split("/")[-1]

        return cls(
            paper_id=f"arxiv:{arxiv_id}",
            title=result.title,
            authors=authors,
            year=result.published.year if result.published else None,
            abstract=result.summary,
            arxiv_id=arxiv_id,
            doi=result.doi,
            url=result.entry_id,
            pdf_url=result.pdf_url,
            fields_of_study=result.categories if hasattr(result, 'categories') else [],
        )


@dataclass
class CitationEdge:
    """인용 관계"""
    source_id: str      # 인용하는 논문 ID
    target_id: str      # 인용되는 논문 ID
    context: Optional[str] = None  # 인용 맥락 텍스트
    is_influential: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source_id,
            "target": self.target_id,
            "context": self.context,
            "is_influential": self.is_influential,
        }


@dataclass
class TrendData:
    """트렌드 분석 데이터"""
    keyword: str
    year: int
    count: int
    growth_rate: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "keyword": self.keyword,
            "year": self.year,
            "count": self.count,
            "growth_rate": self.growth_rate,
        }


@dataclass
class SearchResult:
    """검색 결과 컨테이너"""
    query: str
    source: str                         # semantic_scholar, arxiv, etc.
    papers: List[Paper] = field(default_factory=list)
    total_results: int = 0
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "source": self.source,
            "papers": [p.to_dict() for p in self.papers],
            "total_results": self.total_results,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class NetworkAnalysis:
    """인용 네트워크 분석 결과"""
    total_nodes: int = 0
    total_edges: int = 0
    density: float = 0.0
    avg_in_degree: float = 0.0
    avg_out_degree: float = 0.0
    num_clusters: int = 0

    # 핵심 논문들
    key_papers_by_pagerank: List[Paper] = field(default_factory=list)
    key_papers_by_citations: List[Paper] = field(default_factory=list)
    key_papers_by_betweenness: List[Paper] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_nodes": self.total_nodes,
            "total_edges": self.total_edges,
            "density": self.density,
            "avg_in_degree": self.avg_in_degree,
            "avg_out_degree": self.avg_out_degree,
            "num_clusters": self.num_clusters,
        }
