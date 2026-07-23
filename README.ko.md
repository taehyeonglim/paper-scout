[English](README.md) | [한국어](README.ko.md)

# paper-scout

[![test](https://github.com/taehyeonglim/paper-scout/actions/workflows/test.yml/badge.svg)](https://github.com/taehyeonglim/paper-scout/actions/workflows/test.yml)

[Claude Code](https://claude.com/claude-code)용 문헌탐색 에이전트 모음 — 결정론적 학술 API 백본을 기반으로 동작합니다.

## 소개

paper-scout는 Claude Code 플러그인입니다: 4종의 문헌탐색 서브에이전트가 Python 백본과 짝을 이루며, API 호출·rate limiting·캐싱·스코어링 같은 결정론적 작업은 백본이 전담하고 LLM은 그 결과 JSON을 읽어 자연어로 종합하는 역할만 합니다. 백본은 Semantic Scholar·arXiv·OpenCitations·ERIC·OpenAlex에 더해 **KCI(한국학술지인용색인)** 까지 조회하여, 대부분의 문헌탐색 도구가 놓치는 한국어 학술지 커버리지를 제공합니다.

모든 실행은 `coverage_manifest`를 함께 반환합니다 — 어떤 소스를 조회했는지, 각 소스가 몇 건을 반환했는지, 소스가 제외됐다면 왜인지(키 없음, 플래그 비활성 등)를 기록합니다. 결과가 빈약한 것과 파이프라인이 고장난 것을 구분할 수 있게 해줍니다.

## 에이전트

| 에이전트 | 용도 |
|---|---|
| `related-paper-finder` | 키워드 또는 시드 DOI/paper ID로 관련 논문을 탐색합니다 — 다중 소스 검색, 중복 제거, 관련성 스코어링. |
| `deep-researcher` | 특정 주제에 대해 다라운드 심층 탐색을 수행합니다: 질의 확장, DOI 검증, 구조화된 리포트 종합. 이전 세션 재개도 지원합니다. |
| `citation-network-explorer` | 논문/주제 주변의 인용 네트워크를 매핑합니다(PageRank·betweenness·클러스터링)로 핵심 논문과 연구 클러스터를 드러냅니다. |
| `research-trend-analyzer` | 다년도 출판 트렌드, 신흥/쇠퇴 주제, 주요 저자·저널을 분석합니다. |

각 에이전트의 상세 동작과 할루시네이션 방지 규칙은 `agents/*.md`에 문서화되어 있습니다.

## 각 에이전트의 작동 방식

네 에이전트는 하나의 역할 분담을 공유합니다: 모든 검색·계산·수치는 Python 백본이 결정론적으로 처리하고, 에이전트(LLM)는 그 결과 JSON을 읽어 서술만 담당합니다. 각 에이전트 정의에는 명시적 할루시네이션 방지 규칙이 박혀 있습니다 — 논문·저자·DOI·지표는 백본의 API 검증 JSON에서만 가져올 수 있고 모델 기억에서 생성할 수 없으며, DOI가 없는 논문은 지어내는 대신 `doi: null`로 유지됩니다. Claude Code에서는 스크립트를 직접 실행할 필요 없이 원하는 것을 설명하면("이 DOI와 관련된 논문 찾아줘", "이 논문 주변 인용 네트워크 그려줘") 해당 에이전트가 아래 파이프라인을 호출합니다. orchestrator 서브커맨드는 `--output-format json|markdown|both`를 지원하며, 보고서는 `./literature-discovery/`에 저장됩니다.

### related-paper-finder

키워드, 시드 DOI, 또는 Semantic Scholar paper ID를 주면 백본이 다음을 수행합니다:

1. 키워드 검색 시 Semantic Scholar · arXiv · ERIC · KCI · OpenAlex 병렬 검색 — 시드 DOI/paper ID는 대신 Semantic Scholar만으로 확장(추천 논문·인용 논문·참조 논문·제목 기반 키워드 검색; 나머지 4개 소스는 참여하지 않음),
2. DOI 우선 중복 제거 (paper ID 폴백),
3. 결정론적 관련성 스코어링 (TF-IDF + 인용 신호, 0.0–1.0 정규화),
4. `PAPER_SCOUT_LLM_CMD` 설정 시 의미 부합도 기반 재랭킹 (미설정 시 키워드 휴리스틱 폴백 — 어느 쪽이 돌았는지는 `coverage_manifest.rerank_mode`에 기록),
5. `highly_relevant` / `moderately_relevant` 분류,
6. LLM 연결 시 발견 논문 전체에 걸친 주제·합의·불일치·연구 갭 종합 (미설정 시 `synthesis.mode: "unavailable"`) — 입력 목록에 없는 인용 ID는 `flagged_uncited`로 격리하는 인용 가드 포함,
7. `--include-packet` 지정 시 후속 도구가 소비할 수 있는 기계 판독 `discovery_packet.yaml` (핵심 논문 + 검증된 DOI 레지스트리) 생성.

에이전트 층은 그 JSON에서만 근거를 가져와 Top-N 해설을 작성합니다.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" related-papers \
    --keywords "virtual reality learning" --limit 10 --year-range 2020-2026
```

### deep-researcher

주제와 `--depth shallow|medium|deep`를 받아 같은 기계장치의 더 넓고 깊은 변형을 실행합니다: 5개 소스에서 과잉 수집(over-retrieve)한 뒤 의미 재랭킹으로 주제 이탈 결과를 제거하고(적합 결과가 희소하면 `near_matches` — 근접하지만 관련 판정은 아닌 후보 — 를 정직하게 노출), 잔류 논문을 종합해 구조화된 보고서 세트를 작성합니다 — 요약(executive summary)·영향력 있는 논문·최신 연구·트렌드 종합·연구 갭·참고문헌이 `./literature-discovery/RESEARCH/{세션}/outputs/`에 저장됩니다. 긴 실행은 재개 가능합니다: `--list-sessions`와 `--resume [session_id]`. 백본 검증에 더해, 에이전트가 보고 전 Crossref로 논문 DOI를 추가 검증합니다(전체의 50% 이상 샘플링).

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/deep_researcher.py" "AI literacy in teacher education" --depth medium
```

### citation-network-explorer

시드 DOI 또는 paper ID를 받아 인용 이웃(Semantic Scholar + OpenCitations)을 `--depth` 홉까지, citing / cited / both 방향으로, `--max-nodes` 상한 내에서 탐색합니다. 이후 networkx가 그래프 사실을 계산합니다: PageRank·betweenness 중심성·in/out-degree·커뮤니티 클러스터(`python-louvain` 설치 시 Louvain, 미설치 시 connected-components 폴백). PageRank 상위 N편이 `key_papers`로 반환됩니다. 에이전트는 해석만 담당합니다 — 어떤 논문이 분야의 축인지, 각 클러스터가 무엇인지, 다음에 무엇을 읽을지 — 지표를 스스로 "추정"하는 일은 없습니다. 시드가 국내 문헌이면 KCI 역인용(`scripts/kci/kci_cited_by.py`)으로 보강하되, 두 소스의 중복 범위를 알 수 없으므로 Semantic Scholar 인용수와 합산하지 않고 "국내(KCI) N건"으로 별도 표기합니다.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" citation-network \
    --doi "10.1007/s10055-023-00926-5" --depth 2 --top-n 10
```

### research-trend-analyzer

주제와 분석 기간(`--years`, 기본 5년)을 받아 Semantic Scholar와 OpenAlex를 연도별로 검색하고 DOI/제목 기준으로 병합합니다. 집계는 순수 결정론 카운팅입니다: 연도별 출판 수와 성장률, 키워드 빈도 진화(3회 이상 등장), 신흥 주제(성장률 >30%), 쇠퇴 주제(감소율 >20%), 핵심 저자(papers × log(citations+1) 점수), 주요 저널. 에이전트는 이 집계를 서사로 바꿉니다 — 트렌드 해석, 핵심 연구자, 그리고 반환된 논문 안에서만 고른 Foundational 5 + Cutting-edge 5 추천 목록. 상위 저널에 국내 학술지가 나타나면 해당 저널의 KCI 등재구분·인용지수 이력(`scripts/kci/kci_journal.py`)을 해설에 반영할 수 있습니다.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" research-trends \
    "virtual reality learning" --years 5 --top-n 10
```

## 설치

**1. 플러그인 (Claude Code)**

```
/plugin marketplace add taehyeonglim/paper-scout
/plugin install paper-scout@paper-scout
```

로컬 경로 설치보다 GitHub 마켓플레이스 경유 설치를 권장합니다 — 로컬 설치는 플러그인 디렉토리를 참조하는 대신 통째로 복사합니다.

**2. Python 의존성**

오케스트레이터 스크립트는 Python 3.10+와 별도 의존성 세트가 필요합니다(플러그인 설치에 번들되어 있지 않음):

```bash
pip install -r requirements.txt
```

## 설정

모든 환경변수는 선택 사항입니다 — 아무것도 설정하지 않아도 모든 기능이 fail-soft로 동작합니다.

| 변수 | 용도 | 기본값 |
|---|---|---|
| `SEMANTIC_SCHOLAR_API_KEY` | Semantic Scholar API 키([무료 신청](https://www.semanticscholar.org/product/api)). 강력 권장 — 아래 참조. | 미설정 (공유 무키 풀) |
| `OPENCITATIONS_API_TOKEN` | OpenCitations 인증 토큰 — 인증 시 인용 조회 한도가 상향됩니다. | 미설정 |
| `KCI_API_KEY` | KCI(한국학술지인용색인) 경유 한국 저널 검색을 활성화합니다. | 미설정 (KCI 검색 skip) |
| `OPENALEX_API_KEY` | OpenAlex API 키([무료 가입, 약 30초](https://openalex.org/settings/api)) — $1/day 크레딧으로 일일 한도를 약 1,000회 검색까지 올립니다. Semantic Scholar·KCI와 달리 OpenAlex는 키가 없어도 자체 계량 무키 쿼터($0.10/day)로 조회됩니다. | 미설정 (더 작은 무키 쿼터로 계속 조회됨) |
| `PAPER_SCOUT_LLM_CMD` | 재랭킹/다중 논문 종합에 사용할 LLM CLI 커맨드 템플릿. 프롬프트를 stdin으로 받습니다. 예: `PAPER_SCOUT_LLM_CMD=claude -p` 또는 `PAPER_SCOUT_LLM_CMD=codex exec --sandbox read-only --skip-git-repo-check -` | 미설정 (휴리스틱 폴백, 자연어 종합 없음) |
| `PAPER_SCOUT_OUTPUT_DIR` | 리포트/세션 출력 파일 디렉토리. | `./literature-discovery` |

**유의:** Semantic Scholar의 무키(API 키 미설정) 풀은 전 세계 익명 호출자가 공유하는 rate-limit 풀이며 상당히 throttle되어 있습니다 — 키 없이는 잦은 `429` 응답과 빈약한(또는 빈) 결과를 예상해야 합니다. 키는 무료이며 신청에 몇 분이면 충분합니다 — 공유 풀에서 벗어나 자체 rate limit을 갖게 됩니다. 키 없이도 전부 동작하긴 하지만, 무키 실행 결과로 품질을 판단하지 마십시오.

## 단계적 성능 저하 (graceful degradation)

| 조건 | 동작 |
|---|---|
| 키 없음, `PAPER_SCOUT_LLM_CMD` 없음 | arXiv + ERIC은 키 없이 완전히 동작. OpenAlex도 자체 계량 무키 쿼터($0.10/day)로 계속 동작. Semantic Scholar는 공유 무키 풀 사용(느리고 잦은 `429`). KCI 검색은 skip. 재랭킹은 결정론적 소스별 순위 휴리스틱으로 폴백(각 소스 1위는 1.0, 순위당 0.1씩 감쇠, 0.5에서 floor) — 자연어 종합 없이 랭킹된 JSON만 반환하며 `coverage_manifest.rerank_mode: "heuristic_fallback"`으로 표시됨. |
| `SEMANTIC_SCHOLAR_API_KEY` 설정 | 공유 풀 대신 자체 rate limit — 더 빠르고 완전한 Semantic Scholar 커버리지. |
| `OPENALEX_API_KEY` 설정 | $0.10/day 무키 쿼터 대신 자체 $1/day 크레딧(약 1,000회 검색). |
| `KCI_API_KEY` 설정 | 한국 저널 검색 결과가 추가됨. |
| `OPENCITATIONS_API_TOKEN` 설정 | `citation-network-explorer`의 OpenCitations 조회가 인증되어 한도 상향. |
| `PAPER_SCOUT_LLM_CMD` 설정 | 재랭킹이 키워드 매칭 대신 LLM 판단을 사용하고, 다중 논문 종합(테마·합의·충돌·갭)이 생성됨. |

키나 LLM 커맨드가 없다고 해서 하드 실패하는 경우는 없습니다 — 파이프라인은 결정론적 폴백으로 저하되고, 그 사실을 출력에 명시합니다.

## 사용 예시

```bash
python3 scripts/orchestrator.py related-papers --keywords "agentic AI in education" --limit 3
```

아래 출력은 실제 무키 실행(API 키 없음, `PAPER_SCOUT_LLM_CMD` 미설정)에서 캡처한 결과입니다 — 이번 실행에서는 Semantic Scholar와 arXiv가 동시에 `429`에 걸려, 3건 결과 전부가 OpenAlex + ERIC에서 나왔습니다. 임의로 고른 예시가 아니라 정직한 최악의 케이스이며, `coverage_manifest`가 정확히 무슨 일이 있었는지 기록합니다 — `counts_per_source`도 실명 소스별로 표시됩니다. 축약은 길이 때문이며 다음이 편집의 전부입니다: top-level 키는 모두 표시; 축약된 중첩 객체는 인라인 `"..."` 항목으로 생략된 키를 명시; 각 논문 객체는 19개 필드 중 8개만 표시(생략: `venue`, `citation_count`, `abstract`, `relevance_level`, `pagerank`, `betweenness`, `in_degree`, `out_degree`, `cluster_id`, `quality_grade`, `source_db`). 표시된 값의 변조는 없습니다.

```json
{
  "type": "related_papers",
  "search_query": "agentic AI in education",
  "search_metadata": {
    "sources": ["semantic_scholar", "arxiv", "eric", "openalex"],
    "limit": 3,
    "...": "3개 키 생략: search_date, year_range, min_citations"
  },
  "seed_paper": {
    "paper_id": "query",
    "title": "Search: agentic AI in education",
    "...": "17개 키 생략(아래 논문 항목과 동일한 19필드 구조의 나머지 필드) — 키워드 검색 시 생성되는 스텁 항목"
  },
  "highly_relevant": [
    {
      "paper_id": "openalex:W4319662928",
      "title": "Performance of ChatGPT on USMLE: Potential for AI-assisted medical education using large language models",
      "doi": "10.1371/journal.pdig.0000198",
      "authors": ["Tiffany H. Kung", "Morgan Cheatham", "Arielle Medenilla", "Czarina Sillos", "Lorie De Leon", "Camille Elepaño", "Maria Madriaga", "Rimel Aggabao", "Giezel Diaz-Candido", "James Maningo", "Victor Tseng"],
      "year": 2023,
      "url": "https://openalex.org/W4319662928",
      "relevance_score": 1.0,
      "relevance_reason": "keyword_match"
    },
    {
      "paper_id": "eric:EJ1494645",
      "title": "Comparing Traditional AI, Agentic AI and Agentic Rag for Dialogic Online Education",
      "doi": null,
      "authors": ["Vincent English"],
      "year": 2025,
      "url": "https://eric.ed.gov/?id=EJ1494645",
      "relevance_score": 1.0,
      "relevance_reason": "keyword_match"
    },
    {
      "paper_id": "openalex:W2981731882",
      "title": "Explainable Artificial Intelligence (XAI): Concepts, taxonomies, opportunities and challenges toward responsible AI",
      "doi": "10.1016/j.inffus.2019.12.012",
      "authors": ["Alejandro Barredo Arrieta", "Natalia Díaz-Rodríguez", "Javier Del Ser", "Adrien Bennetot", "Siham Tabik", "Alberto Barbado", "Salvador García", "Sergio Gil-López", "Daniel Molina", "Richard Benjamins", "Raja Chatila", "Francisco Herrera"],
      "year": 2019,
      "url": "https://openalex.org/W2981731882",
      "relevance_score": 0.9,
      "relevance_reason": "keyword_match"
    }
  ],
  "moderately_relevant": [],
  "synthesis": {
    "mode": "unavailable",
    "...": "7개 키 생략(이번 실행에서는 모두 빈 값): themes, consensus, conflicts, gaps, limitations, cited_ids, flagged_uncited"
  },
  "coverage_manifest": {
    "sources_queried": ["semantic_scholar", "arxiv", "eric", "openalex"],
    "counts_per_source": {"openalex": 2, "eric": 1},
    "total_retrieved": 3,
    "returned": 3,
    "rerank_mode": "heuristic_fallback",
    "excluded_sources": ["RISS", "DBpia", "KCI (키 미설정 또는 --no-kci)"],
    "confidence": "medium",
    "...": "5개 키 생략: search_query, year_range, rerank_dropped, influential_threshold, influential_count"
  }
}
```

`SEMANTIC_SCHOLAR_API_KEY`를 설정하면 동일 커맨드가 Semantic Scholar 결과도 반환합니다. `PAPER_SCOUT_LLM_CMD`를 설정하면 `synthesis.mode`가 `"unavailable"`에서 실제 테마/합의/충돌/갭 작성문으로 바뀝니다(빈 스텁이 아니라).

동일한 백본이 `citation-network`·`research-trends` 서브커맨드도 구동합니다(각각 `--doi`/`--paper-id` 시드, 주제 문자열 입력) — 옵션은 `orchestrator.py <subcommand> --help`로 확인하십시오.

## 개발

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

CI는 동일한 테스트 스위트를 Ubuntu·macOS × Python 3.10·3.12 매트릭스로 실행합니다(`.github/workflows/test.yml`).

## 라이선스

[MIT](LICENSE) — Copyright (c) 2026 Taehyeong Lim
