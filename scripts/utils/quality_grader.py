"""
Literature Discovery Team - Quality Grader

학술 논문/저널 품질 등급 시스템 (A-E)

등급 기준:
- A: 최상위 저널 (Nature, Science, Lancet, Cell)
- B: 주요 학회/저널 (NeurIPS, ACL, CHI, IEEE)
- C: 워크숍, 중소 학회, 일반 저널
- D: arXiv 프리프린트, 기술 보고서
- E: 비학술 소스 (블로그, 뉴스)

Credits:
    fivetaku의 Deep Research Kit (quality_rubric.md)에서 영감을 받아
    학술 검색용 A-E 등급 시스템으로 재구현함.
    https://github.com/fivetaku/deep-research-kit
"""

from typing import Optional
import re


class QualityGrader:
    """
    학술 논문 품질 등급 평가기

    저널/학회명 기반으로 A-E 등급 부여
    """

    # A-tier: 최상위 저널
    A_TIER_KEYWORDS = {
        # 종합 과학
        "nature", "science", "cell", "lancet", "nejm",
        "new england journal of medicine", "proceedings of the national academy",
        "pnas",

        # 컴퓨터 과학 최상위
        "ieee transactions on pattern analysis",  # TPAMI
        "acm computing surveys",
        "journal of machine learning research", "jmlr",

        # 교육학 최상위
        "educational researcher", "review of educational research",
        "american educational research journal",
    }

    # B-tier: 주요 학회 및 저널
    B_TIER_KEYWORDS = {
        # AI/ML 주요 학회
        "neurips", "nips", "icml", "iclr",
        "aaai", "ijcai",

        # CV/NLP 주요 학회
        "cvpr", "iccv", "eccv",
        "acl", "emnlp", "naacl", "coling", "eacl",

        # HCI 주요 학회
        "chi", "uist", "cscw", "ubicomp",

        # 교육공학 주요 학회/저널
        "computers & education", "computers and education",
        "educational technology research", "british journal of educational technology",
        "international journal of artificial intelligence in education",
        "learning and instruction", "journal of the learning sciences",
        "lai", "lak", "edm", "aied",  # Learning Analytics, Educational Data Mining

        # 기타 주요 저널
        "ieee access", "plos one", "scientific reports",
        "frontiers in", "ieee/acm transactions",

        # WWW, KDD 등
        "www", "kdd", "sigir", "wsdm", "recsys",
    }

    # C-tier: 워크숍, 중소 학회
    C_TIER_KEYWORDS = {
        "workshop", "symposium", "companion",
        "extended abstract", "demo", "poster",
        "late breaking", "doctoral consortium",
        "short paper", "position paper",

        # 중소 학회/저널
        "springer", "elsevier", "mdpi", "sage",
        "taylor & francis", "wiley",
    }

    # D-tier: 프리프린트
    D_TIER_KEYWORDS = {
        "arxiv", "biorxiv", "medrxiv", "ssrn",
        "preprint", "working paper", "technical report",
        "white paper",
    }

    # E-tier: 비학술 (검색에 잘 안 걸리지만 혹시 모를 경우)
    E_TIER_KEYWORDS = {
        "blog", "medium", "substack",
        "twitter", "reddit", "hackernews",
        "news", "press release",
    }

    def __init__(self):
        """초기화"""
        # 정규화된 키워드 세트 (소문자)
        self.a_tier = {kw.lower() for kw in self.A_TIER_KEYWORDS}
        self.b_tier = {kw.lower() for kw in self.B_TIER_KEYWORDS}
        self.c_tier = {kw.lower() for kw in self.C_TIER_KEYWORDS}
        self.d_tier = {kw.lower() for kw in self.D_TIER_KEYWORDS}
        self.e_tier = {kw.lower() for kw in self.E_TIER_KEYWORDS}

    def grade(
        self,
        venue: Optional[str] = None,
        arxiv_id: Optional[str] = None,
        doi: Optional[str] = None,
        source_db: Optional[str] = None,
        citation_count: int = 0,
        is_peer_reviewed: Optional[bool] = None
    ) -> str:
        """
        논문 품질 등급 평가

        Args:
            venue: 저널/학회명
            arxiv_id: arXiv ID (있으면 D 등급 후보)
            doi: DOI (있으면 정식 출판)
            source_db: 검색 소스 (semantic_scholar, arxiv, eric)
            citation_count: 인용수
            is_peer_reviewed: 피어리뷰 여부 (ERIC에서 제공)

        Returns:
            등급 (A, B, C, D, E 중 하나)
        """
        # 1. arXiv 프리프린트 체크 (DOI 없는 arXiv = D)
        if arxiv_id and not doi:
            return "D"

        # 2. 소스가 arXiv인 경우
        if source_db == "arxiv":
            # DOI가 있으면 정식 출판된 것
            if doi:
                # venue로 추가 판정
                pass
            else:
                return "D"

        # 3. venue 기반 등급 판정
        if venue:
            venue_lower = venue.lower()

            # A-tier 체크
            for keyword in self.a_tier:
                if keyword in venue_lower:
                    return "A"

            # B-tier 체크
            for keyword in self.b_tier:
                if keyword in venue_lower:
                    return "B"

            # D-tier 체크 (프리프린트 키워드)
            for keyword in self.d_tier:
                if keyword in venue_lower:
                    return "D"

            # C-tier 체크 (워크숍 등)
            for keyword in self.c_tier:
                if keyword in venue_lower:
                    return "C"

            # E-tier 체크
            for keyword in self.e_tier:
                if keyword in venue_lower:
                    return "E"

        # 4. 피어리뷰 여부 (ERIC 소스)
        if is_peer_reviewed is True:
            return "B"  # 피어리뷰면 최소 B

        # 5. 인용수 기반 폴백
        if citation_count >= 100:
            return "B"  # 고인용 = 최소 B
        elif citation_count >= 20:
            return "C"  # 중인용 = C
        elif citation_count >= 5:
            return "C"

        # 6. 기본값
        # DOI가 있으면 정식 출판이므로 C
        if doi:
            return "C"

        # DOI도 없으면 D
        return "D"

    def get_grade_description(self, grade: str) -> str:
        """등급 설명 반환"""
        descriptions = {
            "A": "Top-tier journal (Nature, Science, Lancet, IEEE TPAMI)",
            "B": "Major conference/journal (NeurIPS, ACL, CHI, peer-reviewed)",
            "C": "Workshop, symposium, standard journal",
            "D": "Preprint, technical report (arXiv, working paper)",
            "E": "Non-academic source (blog, news)",
        }
        return descriptions.get(grade, "Unknown")

    def get_grade_color(self, grade: str) -> str:
        """등급별 색상 (Markdown/HTML용)"""
        colors = {
            "A": "🟢",  # 초록
            "B": "🔵",  # 파랑
            "C": "🟡",  # 노랑
            "D": "🟠",  # 주황
            "E": "🔴",  # 빨강
        }
        return colors.get(grade, "⚪")


# 모듈 레벨 인스턴스 (편의용)
_default_grader = QualityGrader()


def grade_paper(
    venue: Optional[str] = None,
    arxiv_id: Optional[str] = None,
    doi: Optional[str] = None,
    source_db: Optional[str] = None,
    citation_count: int = 0,
    is_peer_reviewed: Optional[bool] = None
) -> str:
    """
    논문 품질 등급 평가 (편의 함수)

    사용 예:
        from utils.quality_grader import grade_paper
        grade = grade_paper(venue="NeurIPS 2024", doi="10.xxx")
    """
    return _default_grader.grade(
        venue=venue,
        arxiv_id=arxiv_id,
        doi=doi,
        source_db=source_db,
        citation_count=citation_count,
        is_peer_reviewed=is_peer_reviewed
    )


def get_grade_description(grade: str) -> str:
    """등급 설명 반환 (편의 함수)"""
    return _default_grader.get_grade_description(grade)


def get_grade_color(grade: str) -> str:
    """등급 색상 반환 (편의 함수)"""
    return _default_grader.get_grade_color(grade)
