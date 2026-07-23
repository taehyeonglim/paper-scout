[English](README.md) | [한국어](README.ko.md)

# paper-scout

[![test](https://github.com/taehyeonglim/paper-scout/actions/workflows/test.yml/badge.svg)](https://github.com/taehyeonglim/paper-scout/actions/workflows/test.yml)

Literature-discovery agents for [Claude Code](https://claude.com/claude-code), backed by a deterministic academic-API layer.

## What it is

paper-scout is a Claude Code plugin: 4 literature-discovery subagents paired with a Python backbone that does the actual API calls, rate limiting, caching, and scoring deterministically — the LLM only reads the resulting JSON and writes the natural-language synthesis. The backbone queries Semantic Scholar, arXiv, OpenCitations, and ERIC, plus **KCI (Korea Citation Index)** for Korean-language journal coverage that most literature tools skip entirely.

Every run also emits a `coverage_manifest` documenting which sources were queried, how many results each returned, and why any source was excluded (missing key, disabled flag, etc.) — so you can tell a thin result set from a broken one.

## Agents

| Agent | Purpose |
|---|---|
| `related-paper-finder` | Finds related papers from keywords or a seed DOI/paper ID — multi-source search, dedup, relevance scoring. |
| `deep-researcher` | Multi-round deep research on a topic: expands queries, verifies DOIs, and synthesizes a structured report. Supports resuming a prior session. |
| `citation-network-explorer` | Maps the citation network around a paper or topic (PageRank, betweenness, clustering) to surface key papers and clusters. |
| `research-trend-analyzer` | Analyzes multi-year publication trends, emerging/declining topics, and leading authors/venues. |

Full behavior and hallucination guardrails for each agent are documented in `agents/*.md`.

## How each agent works

All four agents share one division of labor: the Python backbone performs every search, computation, and metric deterministically, and the agent (LLM) only reads the resulting JSON and writes the narrative. Each agent definition carries explicit hallucination guardrails — papers, authors, DOIs, and metrics may only come from the backbone's API-verified JSON, never from model memory, and a paper without a DOI keeps `doi: null` rather than getting an invented one. In Claude Code you don't run the scripts yourself: describe what you need ("find papers related to this DOI", "map the citation network around this paper") and the matching agent invokes the pipeline below. The orchestrator subcommands support `--output-format json|markdown|both`; reports land in `./literature-discovery/`.

### related-paper-finder

Give it keywords, a seed DOI, or a Semantic Scholar paper ID. The backbone then:

1. searches Semantic Scholar, arXiv, ERIC, and KCI in parallel,
2. deduplicates by DOI (paper-ID fallback),
3. scores relevance deterministically (TF-IDF and citation signals, normalized 0.0–1.0),
4. optionally reranks by semantic fit through your `PAPER_SCOUT_LLM_CMD` (keyword-heuristic fallback otherwise — `coverage_manifest.rerank_mode` tells you which ran),
5. classifies results into `highly_relevant` / `moderately_relevant`,
6. optionally synthesizes themes / consensus / conflicts / research gaps across the found papers (only when LLM-wired; `synthesis.mode: "unavailable"` otherwise) — with a citation guard that quarantines any cited ID not present in the input set into `flagged_uncited`,
7. with `--include-packet`, emits a machine-readable `discovery_packet.yaml` (core papers plus a verified DOI registry) for downstream tooling.

The agent layer then writes a top-N commentary strictly from that JSON.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" related-papers \
    --keywords "virtual reality learning" --limit 10 --year-range 2020-2026
```

### deep-researcher

Takes a topic plus `--depth shallow|medium|deep` and runs a wider, deeper variant of the same machinery: it over-retrieves from all four sources, semantically reranks to drop off-topic hits (honestly surfacing `near_matches` — close-but-not-relevant candidates — when on-topic results are scarce), synthesizes across the retained papers, and writes a structured report set — executive summary, influential papers, recent work, trends synthesis, research gaps, and a bibliography — under `./literature-discovery/RESEARCH/{session}/outputs/`. Long runs are resumable: `--list-sessions` and `--resume [session_id]` continue where a session stopped. On top of the backbone's verification, the agent DOI-checks reported papers against Crossref (sampling at least 50%).

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/deep_researcher.py" "AI literacy in teacher education" --depth medium
```

### citation-network-explorer

Takes a seed DOI or paper ID and crawls its citation neighborhood (Semantic Scholar + OpenCitations) up to `--depth` hops, in citing / cited / both directions, capped at `--max-nodes`. networkx then computes the graph facts: PageRank, betweenness centrality, in/out-degree, and community clusters (Louvain when `python-louvain` is installed, connected-components fallback otherwise); the top-N papers by PageRank come back as `key_papers`. The agent interprets — which papers anchor the field, what each cluster is about, what to read next — without ever estimating a metric itself. For Korean seed papers it supplements with KCI reverse citations (`scripts/kci/kci_cited_by.py`), reported as a separate "domestic (KCI): N" count that is never merged into Semantic Scholar totals, because the overlap between the two sources is unknown.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" citation-network \
    --doi "10.1007/s10055-023-00926-5" --depth 2 --top-n 10
```

### research-trend-analyzer

Takes a topic and a window (`--years`, default 5) and searches Semantic Scholar year by year. Aggregation is plain deterministic counting: publications per year with growth rates, keyword-frequency evolution (words appearing ≥3 times), emerging topics (>30% growth), declining topics (>20% decline), top authors scored as papers × log(citations+1), and top venues. The agent turns those aggregates into a narrative — trend interpretation, key researchers, and a Foundational-5 + Cutting-edge-5 reading list drawn only from the returned papers. When a Korean journal appears among the top venues, it can pull that journal's KCI registration tier and citation-index history (`scripts/kci/kci_journal.py`) into the commentary.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" research-trends \
    "virtual reality learning" --years 5 --top-n 10
```

## Install

**1. Plugin (Claude Code)**

```
/plugin marketplace add taehyeonglim/paper-scout
/plugin install paper-scout@paper-scout
```

Installing from the GitHub marketplace is recommended over a local path install — a local install copies the whole plugin directory rather than referencing it.

**2. Python dependencies**

The orchestrator scripts need Python 3.10+ and their own dependency set (not bundled with the plugin install):

```bash
pip install -r requirements.txt
```

## Configuration

All environment variables are optional — every feature works without any of them, in fail-soft mode.

| Variable | Purpose | Default |
|---|---|---|
| `SEMANTIC_SCHOLAR_API_KEY` | Semantic Scholar API key ([free to request](https://www.semanticscholar.org/product/api)). Strongly recommended — see below. | unset (shared keyless pool) |
| `OPENCITATIONS_API_TOKEN` | OpenCitations access token for authenticated (higher-limit) citation queries. | unset |
| `KCI_API_KEY` | Enables Korean journal search via KCI (Korea Citation Index). | unset (KCI search skipped) |
| `PAPER_SCOUT_LLM_CMD` | Command template for an LLM CLI used for reranking and multi-paper synthesis. Reads the prompt from stdin. E.g. `PAPER_SCOUT_LLM_CMD=claude -p` or `PAPER_SCOUT_LLM_CMD=codex exec --sandbox read-only --skip-git-repo-check -` | unset (heuristic fallback, no narrative synthesis) |
| `PAPER_SCOUT_OUTPUT_DIR` | Directory for report/session output files. | `./literature-discovery` |

**Be aware:** Semantic Scholar's keyless (no-API-key) pool is a *shared* rate-limit pool across every anonymous caller worldwide, and it gets throttled hard — expect frequent `429` responses and thin or empty result sets without a key. A key is free and takes a couple of minutes to request; it moves you off the shared pool onto your own rate limit. Everything still runs without one, but don't judge result quality on a keyless run.

## Graceful degradation

| Condition | Behavior |
|---|---|
| No keys, no `PAPER_SCOUT_LLM_CMD` | arXiv + ERIC work fully (no key needed). Semantic Scholar uses the shared keyless pool (slow, frequently `429`s). KCI search is skipped. Reranking/synthesis fall back to keyword-match heuristics — no narrative output, just ranked JSON. `coverage_manifest` records all of this. |
| `SEMANTIC_SCHOLAR_API_KEY` set | Own rate limit instead of the shared pool — faster, more complete Semantic Scholar coverage. |
| `KCI_API_KEY` set | Korean journal search is added to results. |
| `OPENCITATIONS_API_TOKEN` set | Authenticated OpenCitations queries for `citation-network-explorer` (higher limits). |
| `PAPER_SCOUT_LLM_CMD` set | Reranking uses your LLM's judgment instead of keyword matching, and multi-paper synthesis (themes, consensus, conflicts, gaps) is produced. |

Nothing ever hard-fails for a missing key or missing LLM command — the pipeline degrades to the deterministic fallback and says so in its output.

## Usage

```bash
python3 scripts/orchestrator.py related-papers --keywords "agentic AI in education" --limit 3
```

Captured output below is from a real, keyless run (no API keys, no `PAPER_SCOUT_LLM_CMD` set) — Semantic Scholar hit its shared-pool `429` throttle, so this run's 4 results came from arXiv + ERIC only. This is the honest worst case, not a cherry-picked example; `coverage_manifest` documents exactly what happened. Abridged for length only, and this is the complete list of edits: every top-level key is shown; abridged nested objects carry an inline `"..."` entry naming their elided keys; each paper object shows 8 of its 19 fields (elided: `venue`, `citation_count`, `abstract`, `relevance_level`, `pagerank`, `betweenness`, `in_degree`, `out_degree`, `cluster_id`, `quality_grade`, `source_db`). No shown value is altered. One `excluded_sources` string is emitted in Korean by the tool regardless of locale.

```json
{
  "type": "related_papers",
  "search_query": "agentic AI in education",
  "search_metadata": {
    "sources": ["semantic_scholar", "arxiv", "eric"],
    "limit": 3,
    "...": "3 keys elided: search_date, year_range, min_citations"
  },
  "seed_paper": {
    "paper_id": "query",
    "title": "Search: agentic AI in education",
    "...": "17 keys elided (the remaining fields of the same 19-field paper shape used by the entries below) — stub entry generated for keyword searches"
  },
  "highly_relevant": [
    {
      "paper_id": "arxiv:2408.00025v3",
      "title": "Need of AI in Modern Education: in the Eyes of Explainable AI (xAI)",
      "doi": null,
      "authors": ["Supriya Manna", "Niladri Sett"],
      "year": 2024,
      "url": "http://arxiv.org/abs/2408.00025v3",
      "relevance_score": 1.0,
      "relevance_reason": "keyword_match"
    },
    {
      "paper_id": "arxiv:1303.0042v1",
      "title": "Twelve Years of Education and Public Outreach with the Fermi Gamma-ray Space Telescope",
      "doi": null,
      "authors": ["Lynn Cominsky", "Kevin McLin", "Aurore Simonnet", "the Fermi Education", "Public Outreach Team"],
      "year": 2013,
      "url": "http://arxiv.org/abs/1303.0042v1",
      "relevance_score": 0.95,
      "relevance_reason": "keyword_match"
    },
    {
      "paper_id": "arxiv:2504.08817v2",
      "title": "Exploring utilization of generative AI for research and education in data-driven materials science",
      "doi": "10.1080/27660400.2025.2535956",
      "authors": ["Takahiro Misawa", "Ai Koizumi", "Ryo Tamura", "Kazuyoshi Yoshimi"],
      "year": 2025,
      "url": "http://arxiv.org/abs/2504.08817v2",
      "relevance_score": 0.9,
      "relevance_reason": "keyword_match"
    }
  ],
  "moderately_relevant": [
    {
      "paper_id": "eric:ED677111",
      "title": "Reskilling the U.S. Military Workforce for the Agentic AI Era: A Framework for Educational Transformation",
      "doi": null,
      "authors": ["Satyadhar Joshi"],
      "year": 2025,
      "url": "https://eric.ed.gov/?id=ED677111",
      "relevance_score": 0.75,
      "relevance_reason": "keyword_match"
    }
  ],
  "synthesis": {
    "mode": "unavailable",
    "...": "7 keys elided (all empty in this run): themes, consensus, conflicts, gaps, limitations, cited_ids, flagged_uncited"
  },
  "coverage_manifest": {
    "sources_queried": ["semantic_scholar", "arxiv", "eric"],
    "total_retrieved": 4,
    "returned": 4,
    "rerank_mode": "heuristic_fallback",
    "excluded_sources": ["RISS", "DBpia", "KCI (키 미설정 또는 --no-kci)"],
    "confidence": "medium",
    "...": "6 keys elided: search_query, counts_per_source, year_range, rerank_dropped, influential_threshold, influential_count"
  }
}
```

With `SEMANTIC_SCHOLAR_API_KEY` set, the same command returns Semantic Scholar results too; with `PAPER_SCOUT_LLM_CMD` set, `synthesis.mode` switches from `"unavailable"` to an actual themes/consensus/conflicts/gaps writeup instead of an empty stub.

The same backbone powers `citation-network` and `research-trends` subcommands (`--doi`/`--paper-id` seed and a topic string, respectively) — run `orchestrator.py <subcommand> --help` for their options.

## Development

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

CI runs the same suite on Ubuntu and macOS against Python 3.10 and 3.12 (`.github/workflows/test.yml`).

## License

[MIT](LICENSE) — Copyright (c) 2026 Taehyeong Lim
