"""
KCI (Korea Citation Index) Open API 클라이언트 — self-contained 발췌

academic_api.py의 KCIClient만 발췌한 패키지. CrossRef/DataCite 클라이언트는
제외 — paper-scout는 KCI 조회만 필요.
"""

import os
import re
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
import requests

from .rate_limiter import RateLimiter, AdaptiveRateLimiter
from .cache_manager import UnifiedCacheManager

logger = logging.getLogger(__name__)


@dataclass
class DOIMetadata:
    """DOI 메타데이터"""
    doi: str
    title: str = ""
    title_en: str = ""
    authors: List[Dict[str, str]] = field(default_factory=list)
    year: Optional[int] = None
    journal: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    publisher: str = ""
    issn: str = ""
    url: str = ""
    source: str = ""  # crossref, kci, datacite
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doi": self.doi,
            "title": self.title,
            "title_en": self.title_en,
            "authors": self.authors,
            "year": self.year,
            "journal": self.journal,
            "volume": self.volume,
            "issue": self.issue,
            "pages": self.pages,
            "publisher": self.publisher,
            "issn": self.issn,
            "url": self.url,
            "source": self.source,
            "confidence": self.confidence,
        }


def _xml_text(parent: Optional[ET.Element], tag: str) -> str:
    """XML 자식 요소 텍스트 안전 추출 (부모/요소 부재·공백은 "")"""
    if parent is None:
        return ""
    value = parent.findtext(tag)
    return value.strip() if value else ""


class KCIClient:
    """
    KCI (Korea Citation Index) Open API 클라이언트

    국내 학술지 논문·저널 조회 (KCI Open API Service 활용가이드 기준, 인증키 필수)
    - 제목/저자 기반 검색 (apiCode=articleSearch)
    - DOI 역검색 (apiCode=articleSearch + doi)
    - KCI ID 상세 조회 (apiCode=articleDetail)
    - 저널 인용지수 검색 (apiCode=citation — IF·즉시성·자기인용률)
    - 저널 프로필 상세 (apiCode=citationDetail — 등재구분·IF 추이·SJR·변경이력)
    - 역인용 탐색 (apiCode=referenceSearch — 해당 문헌을 참고문헌에 실은 KCI 논문)

    응답은 XML(MetaData/outputData). 빈 결과·에러 모두 HTTP 200으로 오고
    outputData/result/resultMsg("No Data" / "등록되지 않은 key 입니다." 등)로 판별.
    """

    BASE_URL = "https://open.kci.go.kr/po/openapi/openApiSearch.kci"
    KCI_ARTICLE_URL = (
        "https://www.kci.go.kr/kciportal/ci/sereArticleSearch/ciSereArtiView.kci"
        "?sereArticleSearchBean.artiId={article_id}"
    )

    def __init__(
        self,
        api_key: Optional[str] = None,
        cache_ttl_days: int = 7
    ):
        """
        Args:
            api_key: KCI API 키 (필수)
            cache_ttl_days: 캐시 유효 기간
        """
        self.api_key = api_key or os.getenv("KCI_API_KEY")
        if not self.api_key:
            logger.warning("KCI API key not set. KCI lookups will fail.")

        # Rate Limiter: 일 10,000회 제한
        self.rate_limiter = RateLimiter(calls_per_second=1.0)

        # Cache
        self.cache = UnifiedCacheManager("kci", ttl_days=cache_ttl_days)

        logger.info(f"KCIClient initialized (API key: {'Yes' if self.api_key else 'No'})")

    def search_by_title(
        self,
        title: str,
        authors: Optional[List[str]] = None,
        year: Optional[int] = None,
        limit: int = 5,
        force_refresh: bool = False
    ) -> List[DOIMetadata]:
        """
        제목 기반 KCI 논문 검색

        Args:
            title: 논문 제목
            authors: 저자 목록 (선택)
            year: 발행 연도 (선택)
            limit: 결과 수 제한
            force_refresh: 캐시 무시

        Returns:
            DOIMetadata 리스트
        """
        if not self.api_key:
            logger.error("KCI API key required")
            return []

        # 신뢰도 재정렬 폭 확보를 위해 서버 최소 반환폭(10)만큼 받은 뒤 limit 절단
        articles = self.search_articles(
            title=title,
            author=authors[0] if authors else None,
            limit=max(limit, 10),
            force_refresh=force_refresh,
        )

        results = []
        for parsed in articles:
            metadata = self._to_doi_metadata(parsed)
            metadata.confidence = self._calculate_confidence(
                query_title=title,
                result_title=metadata.title,
                query_authors=authors,
                result_authors=[a.get("name", "") for a in metadata.authors],
                query_year=year,
                result_year=metadata.year
            )
            results.append(metadata)

        results.sort(key=lambda x: x.confidence, reverse=True)
        results = results[:limit]

        logger.info(f"KCI search '{title[:30]}...': {len(results)} results")
        return results

    def search_by_doi(
        self,
        doi: str,
        force_refresh: bool = False
    ) -> Optional[DOIMetadata]:
        """
        DOI 역검색 (apiCode=articleSearch + doi 파라미터)

        KCI 인덱스에 해당 DOI가 등록된 경우에만 반환 (미등록이면 None —
        doi_audit의 ALIVE_REGISTRY_GAP 실검증 용도).
        """
        if not self.api_key or not doi:
            return None

        articles = self.search_articles(doi=doi, limit=1, force_refresh=force_refresh)
        if not articles:
            return None

        metadata = self._to_doi_metadata(articles[0])
        metadata.confidence = 1.0

        return metadata

    def search_articles(
        self,
        title: Optional[str] = None,
        author: Optional[str] = None,
        doi: Optional[str] = None,
        keyword: Optional[str] = None,
        journal: Optional[str] = None,
        institution: Optional[str] = None,
        affiliation: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 10,
        force_refresh: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        articleSearch 범용 검색 — 전체 필드 dict 리스트 반환

        abstract/citation_count/article_id까지 필요한 소비자(litdisc 등)용
        저수준 검색. 검색 조건 최소 1개 필수 (없으면 KCI가 에러 응답).

        Args:
            date_from / date_to: 발행년월 YYYYMM (KCI dateFrom/dateTo 필터)
            limit: 결과 수 (서버 displayCount 10~100 클램프 후 클라이언트 절단)

        Returns:
            _parse_kci_record dict 리스트 (article_id/title*/authors/year/journal/
            doi/url/abstract*/keywords/citation_count_* 등)
        """
        if not self.api_key:
            logger.error("KCI API key required")
            return []

        criteria = {
            "title": title, "author": author, "doi": doi, "keyword": keyword,
            "journal": journal, "institution": institution, "affiliation": affiliation,
            "dateFrom": date_from, "dateTo": date_to,
        }
        params = {k: v for k, v in criteria.items() if v}
        if not params:
            logger.error("KCI search_articles: 검색 조건이 최소 1개 필요")
            return []

        cache_key = "articles:" + ":".join(
            f"{k}={v}" for k, v in sorted(params.items())
        ) + f":{limit}"

        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        params["apiCode"] = "articleSearch"
        articles = [
            self._parse_kci_record(r)
            for r in self._request(params, display_count=limit)
        ][:limit]

        self.cache.set(cache_key, articles)

        return articles

    def get_by_kci_id(
        self,
        kci_id: str,
        force_refresh: bool = False
    ) -> Optional[DOIMetadata]:
        """
        KCI ID로 논문 조회

        Args:
            kci_id: KCI ID (예: "ART002xxxxxx")
            force_refresh: 캐시 무시

        Returns:
            DOIMetadata 또는 None
        """
        if not self.api_key:
            return None

        cache_key = f"kci_id:{kci_id}"

        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached:
                return DOIMetadata(**cached)

        records = self._request({"apiCode": "articleDetail", "id": kci_id})
        if not records:
            return None

        metadata = self._to_doi_metadata(self._parse_kci_record(records[0]))
        metadata.confidence = 1.0

        self.cache.set(cache_key, metadata.to_dict())

        return metadata

    def search_journal_citations(
        self,
        journal: Optional[str] = None,
        doi: Optional[str] = None,
        institution: Optional[str] = None,
        year: Optional[int] = None,
        years: int = 2,
        limit: int = 10,
        force_refresh: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        저널 인용지수 검색 (apiCode=citation)

        저널명/발행기관/DOI로 필터, 무필터 시 전체 저널(~2,900) 페이지네이션.

        Args:
            year: 기준연도 (KCI 필수 파라미터 — 미지정 시 직전 연도.
                  ⚠ 실측: year 부재 시 에러가 아니라 "No Data"로 조용히 빈 결과)
            years: 포함 년수 (KCI 허용 범위 2~5로 클램프)

        Returns:
            dict 리스트 (journal_id/journal_name/publisher_name/major/url/
            impact_factor/wos_impact_factor/ex_impact_factor/immediacy_index/
            self_cited_rate)
        """
        if not self.api_key:
            logger.error("KCI API key required")
            return []

        if year is None:
            year = datetime.now().year - 1
        years = max(2, min(int(years), 5))

        criteria = {"journal": journal, "doi": doi, "institution": institution}
        params: Dict[str, Any] = {k: v for k, v in criteria.items() if v}
        params["year"] = year
        params["years"] = years

        cache_key = "citation:" + ":".join(
            f"{k}={v}" for k, v in sorted(params.items())
        ) + f":{limit}"

        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        params["apiCode"] = "citation"
        results = [
            self._parse_citation_record(r)
            for r in self._request(params, display_count=limit)
        ][:limit]

        self.cache.set(cache_key, results)

        logger.info(f"KCI citation search (journal={journal!r}, year={year}): {len(results)} journals")
        return results

    def get_journal_detail(
        self,
        journal_id: str,
        force_refresh: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        저널 프로필 상세 조회 (apiCode=citationDetail)

        Args:
            journal_id: 저널 제어번호 — 숫자형("000750")과 SER 접두형
                        ("SER000002778") 모두 실측 동작

        Returns:
            dict (kci_registration 등재구분/저널명 4종/major/issn/발행기관/
            change_history/citation_index_history 연도별 IF·SJR 등) 또는 None
        """
        if not self.api_key or not journal_id:
            return None

        cache_key = f"journal:{journal_id}"

        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        records = self._request({"apiCode": "citationDetail", "id": journal_id})
        if not records:
            return None

        detail = self._parse_journal_detail_record(records[0])
        self.cache.set(cache_key, detail)

        return detail

    def search_references(
        self,
        title: str,
        author: Optional[str] = None,
        institution: Optional[str] = None,
        ref_year: Optional[int] = None,
        limit: int = 10,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """
        참고문헌 DB 검색 = KCI 역인용(cited-by) 탐색 (apiCode=referenceSearch)

        title(피인용 문헌 제목)로 검색하면 그 문헌을 참고문헌 목록에 실은
        KCI 논문들이 나온다 — record의 article-id는 **인용한 논문**의 KCI ID
        (2026-07-22 실측 확정: articleDetail 교차검증). total은 국내 피인용
        수 proxy.

        실측 quirk:
        - page 파라미터는 에코만 되고 무시됨 → 커버리지 상한 = displayCount
          100. total > 100이면 ref_year 분할 등으로 보완
        - ref_year(pubiYr)는 참고문헌 문자열에 적힌 **피인용 문헌의 발행연도**
          필터 (인용한 논문의 발행연도가 아님 — 동명 문헌 연도 구분용)

        Returns:
            {"total": int, "references": [{"article_id", "reference"}...]}
        """
        if not self.api_key:
            logger.error("KCI API key required")
            return {"total": 0, "references": []}

        criteria = {
            "title": title, "author": author,
            "institution": institution, "pubiYr": ref_year,
        }
        params: Dict[str, Any] = {k: v for k, v in criteria.items() if v}

        cache_key = "references:" + ":".join(
            f"{k}={v}" for k, v in sorted(params.items())
        ) + f":{limit}"

        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        params["apiCode"] = "referenceSearch"
        root = self._request_root(params, display_count=limit)
        if root is None:
            return {"total": 0, "references": []}

        total_text = (root.findtext("outputData/result/total") or "").strip()
        references = [
            {
                "article_id": r.get("article-id", ""),
                "reference": (r.text or "").strip(),
            }
            for r in root.findall("outputData/record")
        ][:limit]

        result = {
            "total": int(total_text) if total_text.isdigit() else 0,
            "references": references,
        }

        self.cache.set(cache_key, result)

        logger.info(f"KCI reference search '{title[:30]}...': total={result['total']}")
        return result

    @staticmethod
    def _to_metric(value: Optional[str]) -> Optional[float]:
        """지표 문자열 → float ("-"·빈값 → None, "16.14%" → 16.14)"""
        if not value:
            return None
        value = value.strip().rstrip("%").strip()
        if not value or value == "-":
            return None
        try:
            return float(value)
        except ValueError:
            return None

    def _parse_citation_record(self, record: ET.Element) -> Dict[str, Any]:
        """citation record(journalInfo + citationInfo) → 지표 dict"""
        journal_info = record.find("journalInfo")
        citation_info = record.find("citationInfo")

        return {
            "journal_id": journal_info.get("journal-id", "") if journal_info is not None else "",
            "journal_name": _xml_text(journal_info, "journal-name"),
            "publisher_name": _xml_text(journal_info, "publisher-name"),
            "major": _xml_text(journal_info, "major"),
            "url": _xml_text(journal_info, "url"),
            "impact_factor": self._to_metric(_xml_text(citation_info, "impactFactor")),
            "wos_impact_factor": self._to_metric(_xml_text(citation_info, "wosImpactFactor")),
            "ex_impact_factor": self._to_metric(_xml_text(citation_info, "exImpactFactor")),
            "immediacy_index": self._to_metric(_xml_text(citation_info, "immediacyIndex")),
            "self_cited_rate": self._to_metric(_xml_text(citation_info, "selfCitedRate")),
        }

    def _parse_journal_detail_record(self, record: ET.Element) -> Dict[str, Any]:
        """citationDetail record → 저널 프로필 dict"""
        journal_info = record.find("journalInfo")
        registration = journal_info.find("registration") if journal_info is not None else None
        publisher = journal_info.find("publisher") if journal_info is not None else None

        changes = []
        for el in record.findall("journal-change-history/journal-change"):
            changes.append({
                "date": el.get("date", ""),
                "div_cd": el.get("div-cd", ""),
                "registration": el.get("registration", ""),
                "note": (el.text or "").strip(),
            })

        _METRIC_TAGS = [
            ("impactFactor", "impact_factor"),
            ("impactFactor3", "impact_factor_3y"),
            ("impactFactor4", "impact_factor_4y"),
            ("impactFactor5", "impact_factor_5y"),
            ("wosImpactFactor", "wos_impact_factor"),
            ("sjr", "sjr"),
            ("immediacyIndex", "immediacy_index"),
            ("selfCitedRate", "self_cited_rate"),
            ("exImpactFactor", "ex_impact_factor"),
        ]
        history = []
        for el in record.findall("journal-citation-index-history/journal-citation-index"):
            year_attr = el.get("year", "")
            entry: Dict[str, Any] = {"year": int(year_attr) if year_attr.isdigit() else None}
            for tag, key in _METRIC_TAGS:
                entry[key] = self._to_metric(el.findtext(tag))
            for tag, key in [("yearsArticles2", "articles_2y"), ("yearsCited2", "cited_2y")]:
                value = (el.findtext(tag) or "").strip()
                entry[key] = int(value) if value.isdigit() else None
            history.append(entry)

        return {
            "journal_id": journal_info.get("journal-id", "") if journal_info is not None else "",
            "kci_registration": _xml_text(registration, "kci-registration"),
            "foreign_registration": _xml_text(registration, "foreign-registration"),
            "name_kor": _xml_text(journal_info, "journal-kor-name"),
            "name_kor_abbr": _xml_text(journal_info, "journal-kor-abbr-name"),
            "name_foreign": _xml_text(journal_info, "journal-fola-name"),
            "name_foreign_abbr": _xml_text(journal_info, "journal-fola-abbr-name"),
            "major": _xml_text(journal_info, "major"),
            "issn": _xml_text(journal_info, "issn"),
            "eissn": _xml_text(journal_info, "eissn"),
            "founded": _xml_text(journal_info, "fsed-yr"),
            "frequency": _xml_text(journal_info, "impr"),
            "current_issue": _xml_text(journal_info, "current-issue"),
            "language": _xml_text(journal_info, "use-lang"),
            "publisher": {
                "name_kor": _xml_text(publisher, "publisher-kor-name"),
                "name_eng": _xml_text(publisher, "publisher-eng-name"),
                "tel": _xml_text(publisher, "publisher-tel"),
                "homepage": _xml_text(publisher, "publisher-homp"),
                "address": _xml_text(publisher, "publisher-addr"),
            },
            "change_history": changes,
            "citation_index_history": history,
        }

    def _request_root(
        self,
        params: Dict[str, Any],
        display_count: Optional[int] = None
    ) -> Optional[ET.Element]:
        """
        KCI Open API GET 호출 → 응답 XML root (total 등 result 메타 접근용)

        HTTP/파싱 오류·에러 resultMsg(미등록 key·사용기간 종료·등록되지 않은
        서비스 등)는 에러 로그 후 None. "No Data"는 정상 root 반환 (record 없음).
        display_count는 서버가 10 미만을 무시하므로 10~100으로 클램프해 전송.
        """
        query = {"key": self.api_key, **params}
        if display_count is not None:
            query["displayCount"] = max(10, min(int(display_count), 100))

        try:
            self.rate_limiter.wait()

            response = requests.get(self.BASE_URL, params=query, timeout=30)
            response.raise_for_status()
            root = ET.fromstring(response.content)

        except (requests.RequestException, ET.ParseError) as e:
            logger.error(f"KCI API request error: {e}")
            return None

        result_msg = (root.findtext("outputData/result/resultMsg") or "").strip()
        if result_msg and result_msg != "No Data":
            logger.error(f"KCI API error: {result_msg}")
            return None

        return root

    def _request(
        self,
        params: Dict[str, Any],
        display_count: Optional[int] = None
    ) -> List[ET.Element]:
        """KCI Open API GET 호출 → outputData/record 요소 리스트 (오류·빈 결과는 [])"""
        root = self._request_root(params, display_count)
        if root is None:
            return []
        return root.findall("outputData/record")

    def _parse_kci_record(self, record: ET.Element) -> Dict[str, Any]:
        """KCI XML record(journalInfo + articleInfo)를 전체 필드 dict로 변환"""
        journal_info = record.find("journalInfo")
        article_info = record.find("articleInfo")

        _text = _xml_text

        # 제목/초록 — lang 변형 수집
        # (초록은 articleSearch가 abstract-group/abstract, articleDetail이 abstract 직속)
        titles: Dict[str, str] = {}
        abstracts: Dict[str, str] = {}
        if article_info is not None:
            for el in article_info.findall("title-group/article-title"):
                titles[el.get("lang", "")] = (el.text or "").strip()
            for el in (article_info.findall("abstract-group/abstract")
                       + article_info.findall("abstract")):
                abstracts[el.get("lang", "")] = (el.text or "").strip()

        # 저자 — articleSearch: <author english="영문명">이름(소속)</author>
        #        articleDetail: <author><name>이름</name><name-eng>영문명</name-eng></author>
        authors = []
        if article_info is not None:
            for el in article_info.findall("author-group/author"):
                name_child = el.findtext("name")
                if name_child is not None:
                    name = name_child.strip()
                    name_en = (el.findtext("name-eng") or "").strip()
                else:
                    raw = (el.text or "").strip()
                    name = re.sub(r"\([^)]*\)$", "", raw).strip()
                    name_en = (el.get("english") or "").strip()
                if name or name_en:
                    authors.append({"name": name, "name_en": name_en})

        # 연도
        year = None
        pub_year = _text(journal_info, "pub-year")
        if pub_year:
            try:
                year = int(pub_year)
            except ValueError:
                pass

        # 페이지
        fpage = _text(article_info, "fpage")
        lpage = _text(article_info, "lpage")

        # DOI — CDATA로 "http://dx.doi.org/10.x/..." 형태가 오므로 bare DOI로 정규화
        doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", _text(article_info, "doi"))

        # URL — resolve_doi Stage 4가 artiId= 패턴으로 kci_id를 추출하므로
        # DOI 유무와 무관하게 항상 KCI 포털 URL을 유지
        article_id = article_info.get("article-id", "") if article_info is not None else ""
        kci_url = _text(article_info, "url")
        if not kci_url and article_id:
            kci_url = self.KCI_ARTICLE_URL.format(article_id=article_id)

        # 인용 횟수 — <citation-count kci="4" wos="0">
        citation_kci = 0
        citation_wos = 0
        if article_info is not None:
            cc = article_info.find("citation-count")
            if cc is not None:
                try:
                    citation_kci = int(cc.get("kci") or 0)
                except ValueError:
                    pass
                try:
                    citation_wos = int(cc.get("wos") or 0)
                except ValueError:
                    pass

        keywords = []
        if article_info is not None:
            keywords = [
                (el.text or "").strip()
                for el in article_info.findall("keyword-group/keyword")
                if (el.text or "").strip()
            ]

        return {
            "article_id": article_id,
            "title": titles.get("original") or titles.get("english") or titles.get("foreign") or "",
            "title_en": titles.get("english", ""),
            "title_foreign": titles.get("foreign", ""),
            "authors": authors,
            "year": year,
            "journal": _text(journal_info, "journal-name"),
            "publisher": _text(journal_info, "publisher-name"),
            "volume": _text(journal_info, "volume"),
            "issue": _text(journal_info, "issue"),
            "issn": _text(journal_info, "issn"),
            "pages": f"{fpage}-{lpage}" if fpage and lpage else "",
            "doi": doi,
            "url": kci_url,
            "abstract": abstracts.get("original") or abstracts.get("english") or "",
            "abstract_en": abstracts.get("english", ""),
            "categories": _text(article_info, "article-categories"),
            "language": _text(article_info, "article-language"),
            "keywords": keywords,
            "citation_count_kci": citation_kci,
            "citation_count_wos": citation_wos,
            "verified": _text(article_info, "verified"),
        }

    def _to_doi_metadata(self, parsed: Dict[str, Any]) -> DOIMetadata:
        """전체 필드 dict → DOIMetadata (doi-resolver 소비 형태)"""
        return DOIMetadata(
            doi=parsed["doi"],
            title=parsed["title"],
            title_en=parsed["title_en"],
            authors=parsed["authors"],
            year=parsed["year"],
            journal=parsed["journal"],
            volume=parsed["volume"],
            issue=parsed["issue"],
            pages=parsed["pages"],
            publisher=parsed["publisher"],
            issn=parsed["issn"],
            url=parsed["url"],
            source="kci"
        )

    def _calculate_confidence(
        self,
        query_title: str,
        result_title: str,
        query_authors: Optional[List[str]],
        result_authors: List[str],
        query_year: Optional[int],
        result_year: Optional[int]
    ) -> float:
        """신뢰도 계산 (한글 특화)"""
        score = 0.0

        # 제목 유사도 (45% - 한글 논문은 제목 가중치 높임)
        if query_title and result_title:
            q = query_title.strip()
            r = result_title.strip()
            if q == r:
                score += 0.45
            elif q in r or r in q:
                score += 0.35
            else:
                # 단어 기반 유사도
                q_words = set(q.replace(",", " ").replace(":", " ").split())
                r_words = set(r.replace(",", " ").replace(":", " ").split())
                if q_words and r_words:
                    overlap = len(q_words & r_words) / max(len(q_words), len(r_words))
                    score += 0.45 * overlap

        # 저자 일치 (25%)
        if query_authors and result_authors:
            q_first = query_authors[0] if query_authors else ""
            r_first = result_authors[0] if result_authors else ""
            if q_first and r_first:
                if q_first == r_first:
                    score += 0.25
                elif q_first in r_first or r_first in q_first:
                    score += 0.15
        else:
            score += 0.125

        # 연도 일치 (15%)
        if query_year and result_year:
            if query_year == result_year:
                score += 0.15
            elif abs(query_year - result_year) == 1:
                score += 0.075
        else:
            score += 0.075

        # 기본 점수 (15%)
        score += 0.15

        return round(score, 2)
