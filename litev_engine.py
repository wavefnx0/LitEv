from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote
import os

import httpx
import networkx as nx
import pandas as pd
import certifi

import argparse
import sys


def _configure_utf8_stdio() -> None:
    """Make Windows/PyInstaller console and pipe output Unicode-safe.

    The GUI reads the engine subprocess as UTF-8. Windows otherwise commonly
    gives a frozen console process a legacy code page (for example cp1252),
    which can both corrupt status text and raise UnicodeEncodeError when an API
    error or bibliographic string contains characters such as U+2010.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)
        except (AttributeError, ValueError):
            # Some embedded/frozen stream objects do not implement reconfigure.
            # The GUI also supplies PYTHONIOENCODING/PYTHONUTF8 as a second layer.
            pass


_configure_utf8_stdio()

APP_NAME = "litev"
APP_VERSION = "1.0.1"

import json
from datetime import datetime, timezone
import math
import re
import hashlib
from html import escape
from collections import Counter, defaultdict

import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio



CROSSREF = "https://api.crossref.org"
OPENALEX = "https://api.openalex.org"

# Optional Crossref contact email. Keep it empty by default rather than sending
# a placeholder address. OpenAlex uses API keys rather than the retired polite-
# pool email convention; an environment variable keeps the key out of reports.
MAILTO = ""
OPENALEX_API_KEY = os.environ.get("OPENALEX_API_KEY", "").strip()


def crossref_params(**extra: Any) -> Dict[str, Any]:
    params: Dict[str, Any] = dict(extra)
    if MAILTO:
        params["mailto"] = MAILTO
    return params


def openalex_params(**extra: Any) -> Dict[str, Any]:
    params: Dict[str, Any] = dict(extra)
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY
    return params


async def get_with_backoff(
    client: httpx.AsyncClient, url: str, *, params: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0, retries: int = 4,
) -> httpx.Response:
    """GET with bounded exponential backoff for transient API/network failures."""
    last_error: Optional[BaseException] = None
    response: Optional[httpx.Response] = None
    for attempt in range(max(1, retries)):
        try:
            response = await client.get(url, params=params, timeout=timeout)
            if response.status_code not in {429, 500, 502, 503, 504}:
                return response
            last_error = httpx.HTTPStatusError(
                f"transient HTTP {response.status_code}", request=response.request, response=response
            )
        except httpx.RequestError as exc:
            last_error = exc
        if attempt < retries - 1:
            retry_after = 0.0
            if response is not None:
                try:
                    retry_after = float(response.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    retry_after = 0.0
            await asyncio.sleep(max(retry_after, min(8.0, 0.75 * (2 ** attempt))))
    if response is not None:
        return response
    if last_error:
        raise last_error
    raise RuntimeError(f"Request failed without a response: {url}")


def user_agent() -> str:
    contact = f"; mailto:{MAILTO}" if MAILTO else ""
    return f"{APP_NAME}/{APP_VERSION}{contact}"


def normalize_doi(doi: str) -> str:
    value = str(doi or "").strip()
    value = re.sub(r"^doi:\s*", "", value, flags=re.I)
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
    # DOI links copied from browsers sometimes carry query/fragment suffixes or
    # sentence punctuation.  Remove only characters that cannot be meaningful
    # as a terminal DOI token in normal user input; preserve internal punctuation.
    value = value.split("#", 1)[0].split("?", 1)[0].strip()
    value = value.rstrip(".,;:")
    return value.lower()


@dataclass
class WorkCore:
    doi: str
    title: str
    year: Optional[int]
    authors: List[str]
    funders: List[str]
    awards: List[str]
    abstract: str = ""
    venue: str = ""
    role: str = "unknown"
    work_type: str = ""
    identifiers: Dict[str, str] = field(default_factory=dict)


async def crossref_work(client: httpx.AsyncClient, doi: str, want_references: bool = True) -> Dict[str, Any]:
    url = f"{CROSSREF}/works/{quote(doi, safe='')}"
    params = crossref_params()

    if want_references:
        params["select"] = "DOI,title,issued,author,reference,funder,abstract,container-title,type,URL,ISBN,ISSN"
    else:
        params["select"] = "DOI,title,issued,author,funder,abstract,container-title,type,URL,ISBN,ISSN"

    r = await get_with_backoff(client, url, params=params)
    if r.status_code == 400 and "select" in params:
        params.pop("select", None)
        r = await get_with_backoff(client, url, params=params)

    r.raise_for_status()
    return r.json()["message"]


async def openalex_work_by_doi(client: httpx.AsyncClient, doi: str) -> Optional[Dict[str, Any]]:
    # Current OpenAlex external-ID singleton syntax.
    url = f"{OPENALEX}/works/doi:{quote(normalize_doi(doi), safe='')}"
    r = await get_with_backoff(client, url, params=openalex_params(), timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()



def normalize_isbn(value: str) -> str:
    """Normalize ISBN-10/ISBN-13 for identifier matching."""
    return re.sub(r"[^0-9Xx]", "", str(value or "")).upper()


def normalize_arxiv(value: str) -> str:
    """Normalize modern and legacy arXiv identifiers while preserving legacy slashes."""
    x = str(value or "").strip()
    x = re.sub(r"^arxiv:?\s*", "", x, flags=re.I)
    x = re.sub(r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/", "", x, flags=re.I)
    x = re.sub(r"\.pdf$", "", x, flags=re.I)
    return x.strip().lower()


def identifier_kind(identifier: str) -> str:
    """Classify common scholarly literature identifiers."""
    x = str(identifier or "").strip()
    xl = x.lower()
    if xl.startswith(("https://doi.org/", "http://doi.org/", "doi:")) or re.match(r"^10\.\d{4,9}/\S+$", x, re.I):
        return "doi"
    if re.match(r"^(97[89])?\d{9}[\dXx]$", normalize_isbn(x)):
        return "isbn"
    if re.match(r"^pmid:?\s*\d+$", x, re.I) or x.isdigit():
        return "pmid"
    if (
        re.match(r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/\S+$", x, re.I)
        or re.match(r"^arxiv:?\s*\S+$", x, re.I)
        or re.match(r"^\d{4}\.\d{4,5}(?:v\d+)?$", x, re.I)
        or re.match(r"^[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?$", x, re.I)
    ):
        return "arxiv"
    if re.match(r"^(?:pmcid:?\s*)?PMC\d+$", x, re.I):
        return "pmcid"
    if re.match(r"^W\d+$", x, re.I) or "openalex.org/w" in xl:
        return "openalex"
    # Anything else is treated as bibliographic text.  This is intentionally
    # separate from exact identifiers above, which fail closed if unresolved.
    return "text" if x else "unknown"


def _identifier_candidates_from_openalex(work: Dict[str, Any]) -> Dict[str, str]:
    ids = work.get("ids") or {}
    out = {}
    for key in ("doi", "pmid", "pmcid", "arxiv", "mag"):
        value = ids.get(key)
        if value:
            out[key] = str(value)
    return out


def _work_matches_arxiv(work: Dict[str, Any], arxiv_id: str) -> bool:
    """Check an OpenAlex work for an exact arXiv location/identifier match.

    Current OpenAlex work ``ids`` do not consistently expose arXiv IDs, while
    arXiv URLs are often present in work locations.  This keeps resolution
    exact instead of accepting a fuzzy title/search hit.
    """
    wanted = normalize_arxiv(arxiv_id)
    ids = work.get("ids") or {}
    if ids.get("arxiv") and normalize_arxiv(str(ids["arxiv"])) == wanted:
        return True
    locations = list(work.get("locations") or [])
    if work.get("primary_location"):
        locations.append(work.get("primary_location") or {})
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        for key in ("landing_page_url", "pdf_url"):
            url = str(loc.get(key) or "")
            if "arxiv.org" in url.lower() and normalize_arxiv(url) == wanted:
                return True
    return False


async def resolve_literature_identifier(client: httpx.AsyncClient, identifier: str) -> str:
    """Resolve a common literature identifier to a DOI, which remains the graph's canonical key.

    Supported inputs include DOI, ISBN, PMID, PMCID, arXiv identifiers and OpenAlex work IDs/URLs.
    The Crossref/OpenAlex citation pipeline continues to operate on DOI keys after resolution.
    """
    raw = str(identifier or "").strip()
    kind = identifier_kind(raw)
    if kind == "doi":
        return normalize_doi(raw)

    # OpenAlex has direct work-ID addressing.
    if kind == "openalex":
        wid = raw.rstrip("/").split("/")[-1]
        if wid.lower().startswith("w"):
            r = await get_with_backoff(client, f"{OPENALEX}/works/{wid}", params=openalex_params(), timeout=30)
            r.raise_for_status()
            doi = openalex_id_to_doi(r.json())
            if doi:
                return doi

    # ISBN is an exact identifier: use Crossref's exact ISBN filter and never
    # substitute a fuzzy bibliographic match, which could seed the wrong work.
    if kind == "isbn":
        isbn = normalize_isbn(raw)
        r = await get_with_backoff(client, f"{CROSSREF}/works", params=crossref_params(filter=f"isbn:{isbn}", rows=20), timeout=30)
        r.raise_for_status()
        for item in (r.json().get("message") or {}).get("items", []):
            item_isbns = {normalize_isbn(v) for v in (item.get("ISBN") or [])}
            if isbn in item_isbns and item.get("DOI"):
                return normalize_doi(item["DOI"])
        raise ValueError(f"Could not resolve ISBN {raw!r} to a DOI with an exact ISBN match")

    # OpenAlex supports direct singleton lookup for PMID and PMCID. Prefer that
    # over fuzzy search so a failed identifier cannot silently become another work.
    if kind in {"pmid", "pmcid"}:
        prefix = kind
        value = re.sub(rf"^{kind}:?\s*", "", raw, flags=re.I)
        try:
            r = await get_with_backoff(client, f"{OPENALEX}/works/{prefix}:{quote(value, safe='')}", params=openalex_params(), timeout=30)
            if r.status_code == 200:
                doi = openalex_id_to_doi(r.json())
                if doi:
                    return doi
            elif r.status_code != 404:
                r.raise_for_status()
        except httpx.HTTPStatusError:
            raise
        raise ValueError(f"Could not resolve {kind.upper()} {raw!r} to a DOI")

    if kind == "arxiv":
        query = normalize_arxiv(raw)
        r = await get_with_backoff(
            client, f"{OPENALEX}/works",
            params=openalex_params(search=query, per_page=25), timeout=30
        )
        r.raise_for_status()
        for work in (r.json() or {}).get("results", []):
            if _work_matches_arxiv(work, query):
                doi = openalex_id_to_doi(work)
                if doi:
                    return doi
                raise ValueError(
                    f"OpenAlex matched arXiv identifier {raw!r}, but that work has no DOI; "
                    "litev currently requires a DOI-canonical seed for citation traversal"
                )
        raise ValueError(f"Could not resolve arXiv identifier {raw!r} to a DOI from exact OpenAlex location metadata")

    # Only free bibliographic text is allowed to use fuzzy bibliographic search.
    # Exact identifiers above fail closed rather than silently selecting another work.
    if kind == "text":
        r = await get_with_backoff(client, f"{CROSSREF}/works", params=crossref_params(**{"query.bibliographic": raw, "rows": 5}), timeout=30)
        r.raise_for_status()
        items = (r.json().get("message") or {}).get("items", [])
        if items and items[0].get("DOI"):
            return normalize_doi(items[0]["DOI"])
    raise ValueError(f"Could not resolve literature identifier to a DOI: {identifier}")

def parse_year_from_crossref(msg: Dict[str, Any]) -> Optional[int]:
    # Crossref "issued" often like {"date-parts": [[YYYY, MM, DD]]}
    issued = msg.get("issued", {})
    parts = issued.get("date-parts", [])
    if parts and parts[0] and isinstance(parts[0][0], int):
        return parts[0][0]
    return None


def parse_core_from_crossref(msg: Dict[str, Any]) -> WorkCore:
    doi = normalize_doi(msg.get("DOI", ""))
    title = (msg.get("title") or [""])[0]
    year = parse_year_from_crossref(msg)

    authors = []
    for a in msg.get("author") or []:
        given = a.get("given", "") or ""
        family = a.get("family", "") or ""
        name = (given + " " + family).strip() or family or given
        if name:
            authors.append(name)

    funders, awards = extract_funding(msg)
    abstract = clean_abstract(msg.get("abstract") or "")
    venue = (msg.get("container-title") or [""])[0] if isinstance(msg.get("container-title"), list) else ""
    role = classify_paper_role(title, abstract, msg.get("type") or "")
    identifiers = {"doi": doi}
    for key in ("URL", "url"):
        if msg.get(key): identifiers["url"] = str(msg[key])
    for key in ("ISBN", "ISSN", "eISSN"):
        vals = msg.get(key) or []
        if vals: identifiers[key.lower()] = "; ".join(str(x) for x in vals if x)
    if msg.get("pmid"): identifiers["pmid"] = str(msg["pmid"])
    return WorkCore(
        doi=doi,
        title=title,
        year=year,
        authors=authors,
        funders=funders,
        awards=awards,
        abstract=abstract,
        venue=venue,
        role=role,
        work_type=str(msg.get("type") or ""),
        identifiers=identifiers,
    )



TAG_RE = re.compile(r"<[^>]+>")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{2,}")
STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "were", "are", "was", "have", "has", "had",
    "into", "using", "used", "use", "based", "between", "among", "their", "there", "these", "those",
    "paper", "study", "results", "show", "shows", "new", "novel", "research", "method", "methods"
}


def clean_abstract(text: str) -> str:
    return TAG_RE.sub(" ", text or "").replace("\n", " ").strip()


def openalex_abstract(oa: Dict[str, Any]) -> str:
    inv = oa.get("abstract_inverted_index") or {}
    if not inv:
        return ""
    positions = []
    for word, idxs in inv.items():
        for i in idxs:
            positions.append((int(i), word))
    return " ".join(w for _, w in sorted(positions))


def core_from_openalex(doi: str, work: Dict[str, Any]) -> WorkCore:
    """Create a WorkCore from OpenAlex when Crossref metadata is unavailable."""
    doi = normalize_doi(doi)
    title = str(work.get("display_name") or "")
    year = work.get("publication_year")
    authors = []
    for authorship in work.get("authorships") or []:
        name = ((authorship.get("author") or {}).get("display_name"))
        if name:
            authors.append(str(name))
    abstract = openalex_abstract(work)
    source = ((work.get("primary_location") or {}).get("source") or {})
    identifiers = {"doi": doi}
    for key, value in (work.get("ids") or {}).items():
        if value is not None:
            identifiers[str(key).lower()] = str(value)
    return WorkCore(
        doi=doi,
        title=title,
        year=int(year) if year else None,
        authors=authors,
        funders=[],
        awards=[],
        abstract=abstract,
        venue=str(source.get("display_name") or ""),
        role=classify_paper_role(title, abstract, str(work.get("type") or "")),
        work_type=str(work.get("type") or ""),
        identifiers=identifiers,
    )


def text_for_work(doi: str, cores: Dict[str, WorkCore], oa_cache: Dict[str, Any]) -> str:
    core = cores.get(doi)
    oa = oa_cache.get(doi) or {}
    title = (core.title if core else "") or oa.get("display_name") or ""
    abstract = (core.abstract if core else "") or openalex_abstract(oa)
    concepts = " ".join((c.get("display_name") or "") for c in (oa.get("concepts") or [])[:12])
    return f"{title} {abstract} {concepts}".strip()


def tokenize(text: str) -> Counter:
    toks = [t.lower() for t in WORD_RE.findall(text or "")]
    return Counter(t for t in toks if t not in STOPWORDS)


def cosine_counter(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[k] * b[k] for k in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return float(dot / (na * nb)) if na and nb else 0.0


def openalex_topic_labels(oa: Dict[str, Any], limit: int = 8) -> List[Tuple[str, float]]:
    """Return OpenAlex topic labels, falling back to legacy concepts.

    OpenAlex metadata has evolved over time, so this accepts both the current
    ``topics[].display_name`` shape and the older nested/topic/concept shapes.
    The result is deduplicated while preserving source order.
    """
    out: List[Tuple[str, float]] = []
    seen: set[str] = set()
    for item in (oa or {}).get("topics") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("display_name") or ((item.get("topic") or {}).get("display_name"))
        if not name:
            continue
        key = str(name).strip()
        if not key or key.lower() in seen:
            continue
        seen.add(key.lower())
        out.append((key, float(item.get("score") or 1.0)))
        if len(out) >= limit:
            return out
    if out:
        return out
    for item in (oa or {}).get("concepts") or []:
        if not isinstance(item, dict) or not item.get("display_name"):
            continue
        key = str(item["display_name"]).strip()
        if not key or key.lower() in seen:
            continue
        seen.add(key.lower())
        out.append((key, float(item.get("score") or 0.0)))
        if len(out) >= limit:
            break
    return out


def primary_topic(oa: Dict[str, Any]) -> str:
    labels = openalex_topic_labels(oa, limit=1)
    return labels[0][0] if labels else "Unknown"


def concept_names(oa: Dict[str, Any]) -> set[str]:
    return {name.lower() for name, _ in openalex_topic_labels(oa, limit=12)}


def concept_overlap(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    ca, cb = concept_names(a), concept_names(b)
    if not ca or not cb:
        return 0.0
    return len(ca & cb) / len(ca | cb)


def semantic_text_for_work(doi: str, cores: Dict[str, WorkCore], oa_cache: Dict[str, Any]) -> str:
    """Return title + abstract text for semantic comparison.

    Topic/concept labels are intentionally excluded here because topic overlap is
    measured separately.  This avoids counting the same OpenAlex metadata twice
    in the combined contextual-similarity score.
    """
    core = cores.get(doi)
    oa = oa_cache.get(doi) or {}
    title = (core.title if core else "") or oa.get("display_name") or ""
    abstract = (core.abstract if core else "") or openalex_abstract(oa)
    return f"{title} {abstract}".strip()


def contextual_similarity_to_seed(
    doi: str,
    seed_doi: str,
    cores: Dict[str, WorkCore],
    oa_cache: Dict[str, Any],
) -> Dict[str, Any]:
    """Compare one analyzed paper with the seed using available evidence.

    The score is descriptive, symmetric, and non-causal.  Semantic text
    similarity (title/abstract) and OpenAlex topic overlap are kept as separate
    components.  Missing components are omitted from the weighted mean rather
    than treated as evidence of dissimilarity.
    """
    doi = normalize_doi(doi)
    seed_doi = normalize_doi(seed_doi)

    # The seed is the identity anchor of this chart.
    if doi == seed_doi:
        return {
            "score": 1.0,
            "semantic": 1.0,
            "topic_overlap": 1.0,
            "basis": "seed anchor",
        }

    text_a = semantic_text_for_work(doi, cores, oa_cache)
    text_b = semantic_text_for_work(seed_doi, cores, oa_cache)
    tok_a, tok_b = tokenize(text_a), tokenize(text_b)
    semantic_available = bool(tok_a and tok_b)
    semantic = cosine_counter(tok_a, tok_b) if semantic_available else None

    oa_a, oa_b = oa_cache.get(doi, {}) or {}, oa_cache.get(seed_doi, {}) or {}
    topics_a, topics_b = concept_names(oa_a), concept_names(oa_b)
    topic_available = bool(topics_a and topics_b)
    topic = (len(topics_a & topics_b) / len(topics_a | topics_b)) if topic_available else None

    components = []
    if semantic is not None:
        components.append((0.65, float(semantic), "title/abstract"))
    if topic is not None:
        components.append((0.35, float(topic), "OpenAlex topics"))
    if not components:
        return {"score": None, "semantic": None, "topic_overlap": None, "basis": "unavailable"}

    weight_total = sum(w for w, _, _ in components)
    score = sum(w * value for w, value, _ in components) / weight_total
    return {
        "score": round(float(score), 4),
        "semantic": round(float(semantic), 4) if semantic is not None else None,
        "topic_overlap": round(float(topic), 4) if topic is not None else None,
        "basis": " + ".join(label for _, _, label in components),
    }


def classify_paper_role(title: str, abstract: str = "", work_type: str = "") -> str:
    text = f"{title} {abstract} {work_type}".lower()
    if "review" in text or "survey" in text or "meta-analysis" in text:
        return "review/synthesis"
    if any(x in text for x in ["dataset", "database", "benchmark", "corpus", "resource"]):
        return "dataset/benchmark/resource"
    if any(x in text for x in ["protocol", "tool", "software", "package", "library", "pipeline"]):
        return "tooling/method implementation"
    if any(x in text for x in ["method", "algorithm", "model", "framework", "approach", "technique"]):
        return "method/theory"
    if any(x in text for x in ["application", "clinical", "case study", "trial", "patients"]):
        return "application/translation"
    if any(x in text for x in ["theory", "mechanism", "principle"]):
        return "foundational theory"
    return "research article"


def enrich_edge_scores(G: nx.DiGraph, cores: Dict[str, WorkCore], oa_cache: Dict[str, Any]) -> None:
    """Attach separable evidence signals to observed citation edges.

    The citation edge itself is the observed fact. Semantic/concept similarity
    and citation impact are secondary descriptive signals; they are never used
    to claim intellectual causation.
    """
    token_cache = {doi: tokenize(text_for_work(doi, cores, oa_cache)) for doi in G.nodes}
    for u, v in G.edges:
        sem = cosine_counter(token_cache.get(u, Counter()), token_cache.get(v, Counter()))
        conc = concept_overlap(oa_cache.get(u, {}), oa_cache.get(v, {}))
        cited = cited_by_count_for_doi(u, oa_cache) or 0
        impact = min(1.0, math.log1p(cited) / math.log1p(10000))
        similarity = 0.65 * sem + 0.35 * conc
        G.edges[u, v]["edge_kind"] = G.edges[u, v].get("edge_kind", "citation")
        G.edges[u, v]["semantic_score"] = round(sem, 4)
        G.edges[u, v]["concept_overlap"] = round(conc, 4)
        G.edges[u, v]["citation_impact_signal"] = round(impact, 4)
        G.edges[u, v]["similarity_signal"] = round(similarity, 4)




def key_transition_papers(G: nx.DiGraph, seed: str, cores: Dict[str, WorkCore], oa_cache: Dict[str, Any], top_k: int = 20) -> pd.DataFrame:
    """Rank exploratory transition candidates using transparent, separable signals.

    This is not an importance score.  The composite combines structural bridge
    position, within-dataset connectivity, citation-impact percentile, and topic
    shift relative to directly observed references.  The seed itself is excluded
    because it is the anchor rather than a candidate transition paper.
    """
    if G.number_of_nodes() == 0:
        return pd.DataFrame()
    try:
        btw = nx.betweenness_centrality(G, k=min(200, G.number_of_nodes()), seed=42) if G.number_of_nodes() > 3 else {n: 0 for n in G.nodes}
    except Exception:
        btw = {n: 0 for n in G.nodes}
    seed = normalize_doi(seed)
    rows = []
    for doi in G.nodes:
        if doi == seed:
            continue
        core = cores.get(doi)
        if not core:
            continue
        cited = cited_by_count_for_doi(doi, oa_cache) or 0
        in_d = G.in_degree(doi)
        out_d = G.out_degree(doi)
        try:
            dist = len(nx.shortest_path(G, source=doi, target=seed)) - 1
        except Exception:
            dist = None
        try:
            dist_from = len(nx.shortest_path(G, source=seed, target=doi)) - 1
        except Exception:
            dist_from = None
        bridge = float(btw.get(doi, 0.0))
        ref_sims = [float(G.edges[u, doi].get("similarity_signal", 0.0) or 0.0) for u in G.predecessors(doi)]
        mean_ref_similarity = sum(ref_sims) / len(ref_sims) if ref_sims else 0.0
        topic_shift = max(0.0, 1.0 - mean_ref_similarity) if ref_sims else 0.0
        position = "upstream" if dist is not None else ("downstream" if dist_from is not None else "other")
        rows.append({
            "doi": doi, "title": core.title, "year": core.year, "role": core.role,
            "position": position, "distance_to_seed": dist, "distance_from_seed": dist_from,
            "betweenness": bridge, "cited_by_count": cited,
            "observed_reference_count": int(in_d), "observed_citing_count": int(out_d),
            "connectivity": int(in_d + out_d), "mean_reference_similarity": round(mean_ref_similarity, 4),
            "topic_shift_signal": round(topic_shift, 4),
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    def positive_percentile(values: pd.Series) -> pd.Series:
        vals = pd.to_numeric(values, errors="coerce").fillna(0.0)
        out = pd.Series(0.0, index=vals.index)
        mask = vals > 0
        if mask.any():
            out.loc[mask] = vals.loc[mask].rank(pct=True, method="average")
        return out

    # Zero evidence remains zero rather than receiving a non-zero percentile
    # simply because all papers tie at zero.
    df["bridge_pct"] = positive_percentile(df["betweenness"])
    df["impact_pct"] = positive_percentile(df["cited_by_count"])
    df["connectivity_pct"] = positive_percentile(df["connectivity"])
    df["transition_score"] = (
        0.45 * df["bridge_pct"]
        + 0.25 * pd.to_numeric(df["topic_shift_signal"], errors="coerce").fillna(0)
        + 0.15 * df["connectivity_pct"]
        + 0.15 * df["impact_pct"]
    ).round(4)
    return df.sort_values(["transition_score", "year"], ascending=[False, True], na_position="last").head(top_k)


def era_segments(concept_pivot: pd.DataFrame, boundaries: List[int]) -> List[Dict[str, Any]]:
    if concept_pivot is None or concept_pivot.empty:
        return []
    years = [int(y) for y in concept_pivot.index.tolist()]
    if not years:
        return []
    cuts = [min(years)] + [b for b in boundaries if min(years) < b <= max(years)] + [max(years) + 1]
    segments = []
    for start, end_excl in zip(cuts[:-1], cuts[1:]):
        block = concept_pivot[(concept_pivot.index >= start) & (concept_pivot.index < end_excl)]
        if block.empty:
            continue
        top = block.sum().sort_values(ascending=False).head(5)
        segments.append({
            "start_year": int(start),
            "end_year": int(end_excl - 1),
            "top_concepts": [{"concept": str(k), "score": float(v)} for k, v in top.items()],
        })
    return segments


def author_trajectories(oa_cache: Dict[str, Any], cores: Dict[str, WorkCore], top_k: int = 12) -> pd.DataFrame:
    rows = []
    for doi, oa in oa_cache.items():
        year = cores.get(doi).year if doi in cores else oa.get("publication_year")
        if not year:
            continue
        concepts = [c.get("display_name") for c in (oa.get("concepts") or [])[:3] if c.get("display_name")]
        for auth in oa.get("authorships") or []:
            a = auth.get("author") or {}
            name = a.get("display_name")
            aid = a.get("id")
            if name and aid:
                rows.append({"author_id": aid, "author": name, "year": int(year), "concepts": concepts, "count": 1})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    top = df.groupby(["author_id", "author"])["count"].sum().sort_values(ascending=False).head(top_k).reset_index()
    out = []
    for _, r in top.iterrows():
        sub = df[df["author_id"] == r["author_id"]]
        c = Counter(x for lst in sub["concepts"] for x in lst)
        out.append({
            "author_id": r["author_id"], "author": r["author"], "works": int(r["count"]),
            "first_year": int(sub["year"].min()), "last_year": int(sub["year"].max()),
            "span_years": int(sub["year"].max() - sub["year"].min()),
            "main_concepts": ", ".join([k for k, _ in c.most_common(4)]),
        })
    return pd.DataFrame(out).sort_values(["works", "span_years"], ascending=False)


def funding_evolution(cores: Dict[str, WorkCore]) -> pd.DataFrame:
    rows = []
    for doi, core in cores.items():
        if not core.year:
            continue
        if core.funders:
            for f in core.funders:
                rows.append({"year": int(core.year), "funder": f, "count": 1})
        else:
            rows.append({"year": int(core.year), "funder": "No funder metadata", "count": 1})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["decade"] = (df["year"] // 10 * 10).astype(int).astype(str) + "s"
    return df.groupby(["decade", "funder"])["count"].sum().reset_index().sort_values(["decade", "count"], ascending=[True, False])


def institutional_mobility(oa_cache: Dict[str, Any], cores: Dict[str, WorkCore], top_k: int = 15) -> pd.DataFrame:
    rows = []
    for doi, oa in oa_cache.items():
        year = cores.get(doi).year if doi in cores else oa.get("publication_year")
        if not year:
            continue
        for auth in oa.get("authorships") or []:
            for inst in auth.get("institutions") or []:
                name = inst.get("display_name")
                country = inst.get("country_code") or ""
                if name:
                    rows.append({"institution": name, "country": country, "year": int(year), "count": 1})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    top = df.groupby("institution")["count"].sum().sort_values(ascending=False).head(top_k).index
    out = df[df["institution"].isin(top)].groupby(["institution", "country"]).agg(
        count=("count", "sum"), first_year=("year", "min"), last_year=("year", "max")
    ).reset_index().sort_values("count", ascending=False)
    return out










def classify_relationship(u: str, v: str, data: Dict[str, Any], cores: Dict[str, WorkCore]) -> Tuple[str, str]:
    """Classify citation direction and contextual similarity without causal claims."""
    uy = cores.get(u).year if cores.get(u) else None
    vy = cores.get(v).year if cores.get(v) else None
    edge_kind = data.get("edge_kind", "citation")

    if uy is not None and vy is not None:
        if uy > vy:
            category = "date-inconsistent citation record"
        elif uy == vy:
            category = "same-year citation (ordering uncertain)"
        elif edge_kind == "forward_citation":
            category = "later citing work"
        else:
            category = "prior literature / context"
    elif edge_kind == "forward_citation":
        category = "later citing work (date uncertain)"
    else:
        category = "citation relationship (date/order uncertain)"

    similarity = float(data.get("similarity_signal", 0) or 0)
    semantic = float(data.get("semantic_score", 0) or 0)
    concept = float(data.get("concept_overlap", 0) or 0)
    if similarity >= .65 and (semantic >= .45 or concept >= .35):
        evidence = "high contextual similarity (non-causal)"
    elif similarity >= .35 and (semantic >= .20 or concept >= .15):
        evidence = "moderate contextual similarity (non-causal)"
    elif similarity > 0:
        evidence = "low contextual similarity (non-causal)"
    else:
        evidence = "contextual similarity unavailable"
    return category, evidence

def annotate_relationships(G: nx.DiGraph, cores: Dict[str, WorkCore]) -> None:
    for u,v,data in G.edges(data=True):
        data["relationship_category"], data["relationship_evidence"] = classify_relationship(u,v,data,cores)


def extract_reference_dois(crossref_msg: Dict[str, Any]) -> List[str]:
    refs = crossref_msg.get("reference") or []
    out = []
    for ref in refs:
        # not all references include DOI
        d = ref.get("DOI")
        if d:
            out.append(normalize_doi(d))
    # unique, stable order
    seen = set()
    uniq = []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq



def openalex_id_to_doi(work: Dict[str, Any]) -> Optional[str]:
    ids = work.get("ids") or {}
    doi = ids.get("doi") or work.get("doi")
    if doi:
        return normalize_doi(str(doi))
    return None


async def openalex_citing_works(client: httpx.AsyncClient, openalex_work_id: str, per_page: int = 25) -> List[Dict[str, Any]]:
    """Return works that cite an OpenAlex work id. Used for downstream citation paths."""
    url = f"{OPENALEX}/works"
    params = openalex_params(filter=f"cites:{openalex_work_id}", per_page=min(100, max(1, int(per_page))))
    r = await get_with_backoff(client, url, params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("results", [])


async def expand_forward_graph(
    G: nx.DiGraph,
    cores: Dict[str, WorkCore],
    oa_cache: Dict[str, Any],
    seed_doi: str,
    forward_hops: int = 0,
    forward_cap: int = 25,
    max_nodes: Optional[int] = None,
) -> None:
    """Add descendants: seed -> citing papers -> papers citing those, using OpenAlex."""
    if forward_hops <= 0 or forward_cap <= 0:
        return
    seed = normalize_doi(seed_doi)
    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent()},
        verify=certifi.where(), follow_redirects=True, timeout=30.0, http2=False,
    ) as client:
        frontier = [(seed, 0)]
        seen = {seed}
        while frontier:
            if max_nodes is not None and max_nodes > 0 and G.number_of_nodes() >= max_nodes:
                break
            doi, depth = frontier.pop(0)
            if depth >= forward_hops:
                continue
            oa = oa_cache.get(doi)
            if not oa:
                try:
                    oa = await openalex_work_by_doi(client, doi)
                except Exception as e:
                    print(f"[OpenAlex work FAIL] doi={doi} {type(e).__name__}: {e}")
                    oa = None
                if oa:
                    oa_cache[doi] = oa
            if not oa or not oa.get("id"):
                continue
            try:
                citing = await openalex_citing_works(client, oa["id"], per_page=forward_cap)
            except Exception as e:
                print(f"[OpenAlex citing FAIL] doi={doi} {type(e).__name__}: {e}")
                continue
            for w in citing:
                if max_nodes is not None and max_nodes > 0 and G.number_of_nodes() >= max_nodes:
                    break
                cdoi = openalex_id_to_doi(w)
                if not cdoi:
                    continue
                oa_cache[cdoi] = w
                year = w.get("publication_year")
                title = w.get("display_name") or ""
                if cdoi not in cores:
                    cores[cdoi] = core_from_openalex(cdoi, w)
                G.add_node(cdoi)
                G.add_edge(doi, cdoi, edge_kind="forward_citation")
                if cdoi not in seen:
                    seen.add(cdoi)
                    frontier.append((cdoi, depth + 1))
        enrich_edge_scores(G, cores, oa_cache)
        annotate_relationships(G, cores)

async def build_prior_graph(
    seed_doi: str,
    max_hops: int = 3,
    per_node_cap: int = 90,
    max_nodes: int = 1200
) -> Tuple[nx.DiGraph, Dict[str, WorkCore], Dict[str, Any]]:

    seed_doi = normalize_doi(seed_doi)
    G = nx.DiGraph()
    # Keep the seed in the dataset even if Crossref metadata/reference retrieval
    # fails. OpenAlex can then still supply metadata and downstream citations.
    G.add_node(seed_doi)
    cores: Dict[str, WorkCore] = {}
    oa_cache: Dict[str, Any] = {}
    retrieval_warnings: List[str] = []

    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent()},
        verify=certifi.where(),
        follow_redirects=True,
        timeout=30.0,
        http2=False,
    ) as client:

        frontier = [(seed_doi, 0)]
        visited = set()

        while frontier and G.number_of_nodes() < max_nodes:
            doi, depth = frontier.pop(0)
            if doi in visited or depth > max_hops:
                continue
            visited.add(doi)

            print(f"[Crossref] depth={depth} fetching {doi} (nodes={G.number_of_nodes()})")

            # Terminal-depth works still need metadata, but their own references
            # must not be added. This keeps the requested hop count exact:
            # 0 = seed only, 1 = direct references, 2 = references of references.
            want_references = depth < max_hops
            try:
                msg = await crossref_work(client, doi, want_references=want_references)
            except Exception as e:
                warning = f"Crossref retrieval failed for {doi} at upstream depth {depth}: {type(e).__name__}: {e}"
                print(f"[Crossref FAIL] {warning}")
                retrieval_warnings.append(warning)
                continue

            core = parse_core_from_crossref(msg)
            doi_key = core.doi or doi

            cores[doi_key] = core
            G.add_node(doi_key)

            if not want_references:
                continue
            ref_dois = extract_reference_dois(msg)[:per_node_cap]
            for ref in ref_dois:
                if ref not in G and G.number_of_nodes() >= max_nodes:
                    break
                G.add_node(ref)
                G.add_edge(ref, doi_key, edge_kind="reference")
                if ref not in visited:
                    frontier.append((ref, depth + 1))

        # OpenAlex enrichment (concurrent)
        print(f"[OpenAlex] enriching {G.number_of_nodes()} works...")
        oa_cache = await openalex_bulk_by_doi(client, list(G.nodes), concurrency=10)
        # Crossref can fail or omit metadata for a DOI that OpenAlex resolves.
        # Keep those papers in the analyzable dataset by filling their core
        # metadata from OpenAlex rather than leaving invisible graph-only nodes.
        for d in list(G.nodes):
            if d not in cores and oa_cache.get(d):
                cores[d] = core_from_openalex(d, oa_cache[d])
        enrich_edge_scores(G, cores, oa_cache)
        annotate_relationships(G, cores)

    G.graph["retrieval_warnings"] = list(dict.fromkeys(retrieval_warnings))
    return G, cores, oa_cache


def summarize_timeline(G: nx.DiGraph, cores: Dict[str, WorkCore]) -> pd.DataFrame:
    rows = []
    for doi, core in cores.items():
        rows.append({
            "doi": doi,
            "year": core.year,
            "title": core.title,
            "role": core.role,
            "venue": core.venue,
            # Edges are reference -> citing work.  Incoming edges are therefore
            # observed references, while outgoing edges are observed citing works.
            "observed_reference_count": int(G.in_degree(doi)),
            "observed_citing_count": int(G.out_degree(doi)),
        })

    if not rows:
        return pd.DataFrame(columns=[
            "doi", "year", "title",
            "observed_reference_count", "observed_citing_count"
        ])

    df = pd.DataFrame(rows)

    if "year" not in df.columns:
        return df

    return df.sort_values(["year", "observed_reference_count"], ascending=[True, False], na_position="last")

def concepts_over_time(oa_cache: Dict[str, Any], cores: Dict[str, WorkCore], top_k: int = 8) -> pd.DataFrame:
    """
    Build a year->topic signal table using OpenAlex Topics, with Concepts as a
    fallback for records that do not expose Topics.
    """
    rows = []
    for doi, oa in oa_cache.items():
        year = cores.get(doi).year if doi in cores else None
        if not year:
            continue
        for name, score in openalex_topic_labels(oa, limit=8):
            rows.append({"year": year, "concept": name, "score": score})
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # pick global top concepts by total score
    top = (df.groupby("concept")["score"].sum().sort_values(ascending=False).head(top_k).index.tolist())
    df = df[df["concept"].isin(top)]
    pivot = df.pivot_table(index="year", columns="concept", values="score", aggfunc="sum", fill_value=0.0).sort_index()
    # Compare topic composition rather than raw yearly volume. Values are shares
    # among the displayed global top topics and sum to 100 per represented year.
    row_totals = pivot.sum(axis=1).replace(0, 1)
    return pivot.div(row_totals, axis=0) * 100.0


def institutions_over_time(oa_cache: Dict[str, Any], cores: Dict[str, WorkCore], top_k: int = 10) -> pd.DataFrame:
    rows = []
    for doi, oa in oa_cache.items():
        year = cores.get(doi).year if doi in cores else None
        if not year:
            continue
        for auth in oa.get("authorships") or []:
            for inst in auth.get("institutions") or []:
                name = inst.get("display_name")
                if name:
                    rows.append({"year": year, "institution": name, "count": 1})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    top = df.groupby("institution")["count"].sum().sort_values(ascending=False).head(top_k).index.tolist()
    df = df[df["institution"].isin(top)]
    pivot = df.pivot_table(index="year", columns="institution", values="count", aggfunc="sum", fill_value=0).sort_index()
    return pivot

async def openalex_bulk_by_doi(client: httpx.AsyncClient, dois: List[str], concurrency: int = 10) -> Dict[str, Any]:
    """Batch-enrich DOI works through OpenAlex, up to 100 IDs per request.

    A singleton fallback is used for any DOI omitted from a successful batch,
    which protects against partial results while keeping the normal path fast.
    """
    requested = list(dict.fromkeys(normalize_doi(d) for d in dois if normalize_doi(d)))
    out: Dict[str, Any] = {}
    select = "id,display_name,publication_year,cited_by_count,topics,concepts,ids,authorships,abstract_inverted_index,primary_location,type"

    for start in range(0, len(requested), 100):
        chunk = requested[start:start + 100]
        doi_values = "|".join(f"https://doi.org/{d}" for d in chunk)
        params = openalex_params(filter=f"doi:{doi_values}", per_page=100, select=select)
        try:
            r = await get_with_backoff(client, f"{OPENALEX}/works", params=params, timeout=30)
            r.raise_for_status()
            for work in (r.json() or {}).get("results", []):
                d = openalex_id_to_doi(work)
                if d:
                    out[d] = work
        except Exception as exc:
            print(f"[OpenAlex batch FAIL] {len(chunk)} DOI(s): {type(exc).__name__}: {exc}")

    missing = [d for d in requested if d not in out]
    if missing:
        sem = asyncio.Semaphore(max(1, min(int(concurrency), 6)))
        async def fetch_one(d: str):
            async with sem:
                try:
                    work = await openalex_work_by_doi(client, d)
                    if work:
                        out[d] = work
                except Exception:
                    pass
        await asyncio.gather(*(fetch_one(d) for d in missing))
    return out


def explain_paths_to_seed(G: nx.DiGraph, seed: str, max_paths: int = 800):
    """
    G edges are ref -> citing. So ancestors of seed are the 'before' papers.
    Returns dict: doi -> [path nodes...] ending at seed
    """
    seed = normalize_doi(seed)
    paths = {}
    for node in list(G.nodes)[:max_paths]:
        if node == seed:
            continue
        try:
            p = nx.shortest_path(G, source=node, target=seed)
            paths[node] = p
        except nx.NetworkXNoPath:
            pass
    return paths

def era_boundaries(concept_pivot: pd.DataFrame, min_gap: int = 3) -> List[int]:
    """
    Returns a list of boundary years where concept mix shifts.
    """
    if concept_pivot.empty or len(concept_pivot) < 6:
        return []

    # Normalize each year vector
    X = concept_pivot.div(concept_pivot.sum(axis=1).replace(0, 1), axis=0)
    years = X.index.tolist()

    # L1 distance between consecutive yearly topic compositions. Do not force
    # boundaries when changes are small: require a robust outlier-like change
    # plus a modest absolute threshold (the normalized L1 range is 0–2).
    diffs = (X.diff().abs().sum(axis=1)).fillna(0)
    nonzero = diffs[diffs > 0]
    if nonzero.empty:
        return []
    med = float(nonzero.median())
    mad = float((nonzero - med).abs().median())
    threshold = max(0.20, med + 1.5 * mad)
    candidates = diffs[diffs >= threshold].sort_values(ascending=False).head(3).index.tolist()

    boundaries = []
    for y in sorted(candidates):
        if not boundaries or (int(y) - boundaries[-1]) >= min_gap:
            boundaries.append(int(y))
    return boundaries

def top_authors_openalex(oa_cache: Dict[str, Any], cores: Dict[str, WorkCore], top_k: int = 15) -> pd.DataFrame:
    rows = []
    for doi, oa in oa_cache.items():
        year = cores.get(doi).year if doi in cores else None
        if not year:
            continue
        for auth in oa.get("authorships") or []:
            a = auth.get("author") or {}
            aid = a.get("id")
            name = a.get("display_name")
            if aid and name:
                rows.append({"author_id": aid, "author": name, "year": year, "count": 1})
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    out = df.groupby(["author_id", "author"])["count"].sum().reset_index().sort_values("count", ascending=False).head(top_k)
    return out

def extract_funding(crossref_msg: Dict[str, Any]) -> tuple[list[str], list[str]]:
    funders = crossref_msg.get("funder") or []
    names: list[str] = []
    awards: list[str] = []
    for f in funders:
        n = f.get("name")
        if n:
            names.append(n)
        for a in (f.get("award") or []):
            if a:
                awards.append(str(a))
    # unique, preserve order
    def uniq(xs):
        seen=set(); out=[]
        for x in xs:
            if x not in seen:
                seen.add(x); out.append(x)
        return out
    return uniq(names), uniq(awards)


def funders_summary(cores: Dict[str, WorkCore], top_k: int = 15):
    rows = []
    total = 0
    with_funders = 0

    for core in cores.values():
        total += 1
        if core.funders:
            with_funders += 1
        for f in core.funders:
            rows.append({"funder": f, "count": 1})

    coverage = (with_funders / total * 100) if total else 0.0
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(), coverage
    top = df.groupby("funder")["count"].sum().sort_values(ascending=False).head(top_k).reset_index()
    return top, coverage


def df_to_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    return df.reset_index(drop=True).to_dict(orient="records")


def pivot_to_year_dict(pivot: pd.DataFrame) -> Dict[str, Dict[str, float]]:
    """
    Converts a pivot table indexed by year into { "YYYY": { "col": value, ...}, ... }.
    """
    if pivot is None or pivot.empty:
        return {}
    out: Dict[str, Dict[str, float]] = {}
    for year, row in pivot.iterrows():
        # convert numpy types to plain python
        out[str(int(year))] = {str(k): float(v) for k, v in row.to_dict().items() if float(v) != 0.0}
    return out


async def _ollama_chat(
    client: httpx.AsyncClient,
    model: str,
    prompt: str,
    base_url: str = "http://127.0.0.1:11434",
    timeout: float = 120.0,
    num_predict: int = 650,
) -> str:
    """Call a local Ollama model. No API key is required."""
    url = base_url.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": "10m",
        "options": {"temperature": 0.15, "num_predict": num_predict, "num_ctx": 12000},
    }
    r = await client.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return str(((data.get("message") or {}).get("content") or "")).strip()

async def _ollama_chat_streaming(
    client: httpx.AsyncClient,
    model: str,
    prompt: str,
    base_url: str = "http://127.0.0.1:11434",
    timeout: float = 300.0,
    num_predict: int = 1800,
) -> str:
    """Streaming Ollama call used only by --ai-evolution.

    Streaming prevents a long local generation from appearing as an HTTP
    Read timeout is intentionally long because local Ollama models can emit
    incremental NDJSON slowly on CPU or modest GPUs.
    """
    url = base_url.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "keep_alive": "10m",
        "options": {"temperature": 0.15, "num_predict": num_predict, "num_ctx": 8000},
    }
    parts: List[str] = []
    async with client.stream("POST", url, json=payload, timeout=httpx.Timeout(timeout, connect=30.0)) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = data.get("message") or {}
            chunk = msg.get("content") or ""
            if chunk:
                parts.append(str(chunk))
            if data.get("done"):
                break
    return "".join(parts).strip()

def _why_matters_prompt(paper: Dict[str, Any], seed: Dict[str, Any], neighbors: List[Dict[str, Any]]) -> str:
    """Prompt for a findings-only AI summary of one paper."""
    abstract = paper.get("abstract") or "No abstract available."
    return f"""You are a careful research-literature analyst. Summarize the FINDINGS of ONE PAPER using only the supplied bibliographic metadata and abstract.

STRICT RULES
1. Summarize what the paper reports finding, observing, demonstrating, measuring, or concluding.
2. Do NOT explain why the paper matters, its role in the seed's citation context, or whether it influenced another paper.
3. Do NOT infer importance, novelty, causality, author motivation, or impact unless explicitly stated in the supplied abstract.
4. Do not invent results, methods, datasets, numbers, or conclusions.
5. If the abstract does not contain a clear finding, say that the available metadata does not establish a specific finding rather than guessing.
6. Keep the summary concise: 2-4 sentences, about 50-100 words.
7. Return ONLY the summary, with no heading, bullets, caveats about being an AI, or metadata.

PAPER
Title: {paper.get('title') or 'Untitled'}
Year: {paper.get('year') or 'unknown'}
DOI: {paper.get('doi') or 'Unknown'}
Abstract: {abstract}

Write a findings-only summary."""

def _load_why_matters_cache(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_why_matters_cache(cache: Dict[str, Any], path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

def _why_matters_candidates(transitions: pd.DataFrame, timeline: pd.DataFrame, seed_doi: str, max_papers: int) -> List[str]:
    seed_doi = normalize_doi(seed_doi)
    selected: List[str] = []
    if transitions is not None and not transitions.empty:
        for doi in transitions.get("doi", pd.Series(dtype=str)).tolist():
            d = normalize_doi(str(doi))
            if d and d != seed_doi and d not in selected:
                selected.append(d)
            if len(selected) >= max_papers:
                return selected[:max_papers]
    if timeline is not None and not timeline.empty:
        candidates = timeline.copy()
        for col in ("mean_reference_similarity", "cited_by_count"):
            candidates[col] = pd.to_numeric(candidates.get(col, 0), errors="coerce").fillna(0)
        candidates = candidates.sort_values(["cited_by_count", "mean_reference_similarity"], ascending=False)
        for doi in candidates.get("doi", pd.Series(dtype=str)).tolist():
            d = normalize_doi(str(doi))
            if d and d != seed_doi and d not in selected:
                selected.append(d)
            if len(selected) >= max_papers:
                break
    return selected[:max_papers]

async def generate_why_matters(report: Dict[str, Any], cores: Dict[str, WorkCore], oa_cache: Dict[str, Any], G: nx.DiGraph, model: str = "gemma3", base_url: str = "http://127.0.0.1:11434", max_papers: int = 8, cache_path: str = "litev_why_matters_cache.json") -> Dict[str, Dict[str, Any]]:
    """Generate short local-LLM findings summaries for papers selected by the report."""
    transitions = safe_df(report.get("key_transition_papers", []))
    timeline = safe_df(report.get("timeline", []))
    seed = report.get("seed", {}) or {}
    candidates = _why_matters_candidates(transitions, timeline, seed.get("doi", ""), max_papers)
    cache = _load_why_matters_cache(cache_path)
    results: Dict[str, Dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=180.0) as client:
        for doi in candidates:
            core = cores.get(doi)
            oa = oa_cache.get(doi) or {}
            if not core:
                continue
            concepts = [name for name, _ in openalex_topic_labels(oa, limit=8)]
            paper = {
                "doi": doi, "title": core.title, "year": core.year, "role": core.role,
                "abstract": core.abstract or openalex_abstract(oa),
                "cited_by_count": cited_by_count_for_doi(doi, oa_cache) or 0,
                "distance_to_seed": None, "concepts": concepts,
            }
            if not paper["abstract"].strip():
                results[doi] = {
                    "text": "No abstract was available in the retrieved metadata, so litev did not ask the local model to infer this paper's findings.",
                    "model": model,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "source": "metadata_only",
                }
                continue
            seed_doi = normalize_doi(seed.get("doi", ""))
            if not transitions.empty and "doi" in transitions.columns:
                hit = transitions[transitions["doi"].astype(str).map(normalize_doi) == doi]
                if not hit.empty:
                    paper["distance_to_seed"] = hit.iloc[0].get("distance_to_seed")
            if paper["distance_to_seed"] is None:
                try:
                    paper["distance_to_seed"] = len(nx.shortest_path(G, source=doi, target=seed_doi)) - 1
                except Exception:
                    pass
            payload = json.dumps({
                "version": "findings-v2", "model": model, "doi": doi,
                "title": paper["title"], "year": paper["year"], "abstract": paper["abstract"],
                "topics": paper["concepts"],
            }, ensure_ascii=False, sort_keys=True)
            cache_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
            cache_key = f"findings-v2:{model}:{doi}:{cache_hash}"
            if cache_key in cache and cache[cache_key].get("text"):
                results[doi] = cache[cache_key]
                continue
            prompt = _why_matters_prompt(paper, seed, [])
            try:
                text = await _ollama_chat(client, model, prompt, base_url=base_url)
            except Exception as e:
                print(f"[Findings AI] skipped {doi}: {type(e).__name__}: {e}")
                continue
            if not text:
                continue
            record = {
                "text": text, "model": model,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source": "local_ollama",
            }
            cache[cache_key] = record
            results[doi] = record
            _save_why_matters_cache(cache, cache_path)
            await asyncio.sleep(0.05)
    return results

def _ai_evolution_evidence(report: Dict[str, Any], cores: Dict[str, WorkCore], G: nx.DiGraph) -> str:
    """Create an upstream-only evidence packet for higher-level local-LLM analysis.

    Future directions/open questions are intentionally conditioned on the seed
    and its upstream/reference-side literature.  Downstream citing papers are
    excluded even when the same run also requested downstream hops.
    """
    seed = report.get("seed", {}) or {}
    timeline = safe_df(report.get("timeline", []))
    transitions = safe_df(report.get("key_transition_papers", []))

    seed_doi = normalize_doi(seed.get("doi", ""))
    seed_year = int(seed.get("year")) if str(seed.get("year") or "").isdigit() else None
    try:
        upstream_nodes = set(nx.ancestors(G, seed_doi)) | {seed_doi}
    except Exception:
        upstream_nodes = {seed_doi}

    rows = []
    if not timeline.empty:
        t = timeline[timeline.get("doi", pd.Series(dtype=str)).astype(str).map(normalize_doi).isin(upstream_nodes)].copy()
        t["year_num"] = pd.to_numeric(t.get("year"), errors="coerce")
        t["similarity_num"] = pd.to_numeric(t.get("mean_reference_similarity", 0), errors="coerce").fillna(0)
        t = t.sort_values(["year_num", "similarity_num"], ascending=[True, False], na_position="last")
        for _, r in t.head(32).iterrows():
            rows.append(
                f"PAPER | {r.get('year','?')} | {r.get('title','Untitled')} | doi={r.get('doi','')} | role={r.get('role','unknown')} | mean_reference_similarity={float(r.get('similarity_num',0)):.3f} | citations={r.get('cited_by_count',0)}"
            )

    transition_rows = []
    if not transitions.empty:
        tt = transitions[transitions.get("doi", pd.Series(dtype=str)).astype(str).map(normalize_doi).isin(upstream_nodes)]
        for _, r in tt.head(8).iterrows():
            transition_rows.append(
                f"TRANSITION | {r.get('year','?')} | {r.get('title','Untitled')} | doi={r.get('doi','')} | score={float(r.get('transition_score', r.get('score',0)) or 0):.3f}"
            )

    # Build topic evidence only from upstream papers. Using the report-wide
    # concepts_by_year table here could leak downstream papers from the same year
    # into the future-analysis prompt when both directions are requested.
    concept_rows = []
    if not timeline.empty:
        t_topics = timeline[timeline.get("doi", pd.Series(dtype=str)).astype(str).map(normalize_doi).isin(upstream_nodes)].copy()
        if "primary_topic" in t_topics.columns and "year" in t_topics.columns:
            t_topics = t_topics.dropna(subset=["year", "primary_topic"])
            for year, group in t_topics.groupby("year"):
                counts = Counter(str(x) for x in group["primary_topic"].tolist() if str(x).strip())
                if counts:
                    concept_rows.append(
                        f"TOPICS | {int(year) if str(year).replace('.0','').isdigit() else year} | "
                        + "; ".join(f"{name}={count}" for name, count in counts.most_common(8))
                    )

    branch_rows = []
    upstream_graph = G.subgraph(upstream_nodes).copy()
    for node in upstream_graph.nodes:
        if node == seed_doi:
            continue
        succ = list(upstream_graph.successors(node))
        if len(succ) < 2:
            continue
        c = cores.get(node)
        if not c:
            continue
        branch_rows.append((len(succ), c.year or 9999, f"BRANCH | {c.year or '?'} | {c.title} | doi={node} | outgoing={len(succ)} | children=" + " || ".join((cores.get(x).title if cores.get(x) else x) for x in succ[:6])))
    branch_rows = [x[2] for x in sorted(branch_rows, key=lambda x: (-x[0], x[1]))[:15]]

    return "\n".join([
        f"SEED | {seed.get('year','?')} | {seed.get('title','Untitled')} | doi={seed.get('doi','')}",
        (f"SEED ABSTRACT | {str(seed.get('abstract') or '')[:3500]}" if seed.get('abstract') else "SEED ABSTRACT | unavailable"),
        f"UPSTREAM DATASET | nodes={upstream_graph.number_of_nodes()} | observed_citation_links={upstream_graph.number_of_edges()}",
        *concept_rows[:24], *transition_rows, *branch_rows[:8], *rows
    ])

def _ai_evolution_prompt(report: Dict[str, Any], evidence: str, analysis: str) -> str:
    seed = report.get("seed", {}) or {}
    task_prompts = {
        "future_directions": "Produce 4-6 evidence-grounded possible future directions.",
        "research_questions": "Produce 8-12 concrete open research questions grounded in observed gaps, tensions, transitions, or unresolved developments.",
    }
    return f"""You are a careful research-evolution analyst running as a local language model. Analyze ONLY the supplied evidence packet. Do not invent papers, findings, methods, motivations, causal links, or future events. Citation links and publication dates alone do not prove intellectual influence. Separate observation from inference and state uncertainty explicitly.

SEED
Title: {seed.get('title','Untitled')}
Year: {seed.get('year','unknown')}
DOI: {seed.get('doi','unknown')}

TASK
{task_prompts[analysis]}

RETURN VALID JSON ONLY, matching this schema exactly:
{{
  "analysis_type": "{analysis}",
  "items": [
    {{
      "title": "short label",
      "observation": "what is directly observed in the evidence packet",
      "inference": "what this may suggest, clearly marked as inference",
      "evidence": ["specific year/paper/concept/transition from the packet"],
      "uncertainty": "what is not established or could weaken the interpretation",
      "research_value": "why this matters for understanding the trajectory"
    }}
  ],
  "overall_uncertainty": "brief statement of major limitations"
}}

Do not include markdown fences, greetings, commentary, or any text outside the JSON object.

EVIDENCE PACKET
{evidence}"""

def _parse_structured_ai(text: str, analysis: str) -> Optional[Dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
    try:
        obj = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, flags=re.S)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except Exception:
            return None
    if not isinstance(obj, dict) or not isinstance(obj.get("items"), list):
        return None
    cleaned=[]
    for item in obj["items"]:
        if not isinstance(item, dict):
            continue
        cleaned.append({
            "title": str(item.get("title") or "Untitled"),
            "observation": str(item.get("observation") or ""),
            "inference": str(item.get("inference") or ""),
            "evidence": [str(x) for x in (item.get("evidence") or [])][:8],
            "uncertainty": str(item.get("uncertainty") or ""),
            "research_value": str(item.get("research_value") or ""),
        })
    if not cleaned:
        return None
    return {
        "analysis_type": analysis,
        "items": cleaned,
        "overall_uncertainty": str(obj.get("overall_uncertainty") or ""),
    }

async def generate_evolution_ai(
    report: Dict[str, Any],
    cores: Dict[str, WorkCore],
    G: nx.DiGraph,
    model: str = "gemma3",
    base_url: str = "http://127.0.0.1:11434",
    cache_path: str = "litev_evolution_ai_cache.json",
    parallel: int = 2,
) -> Dict[str, Any]:
    """Generate structured, evidence-grounded AI analyses."""
    # This AI analysis is only meaningful when upstream literature was requested.
    # The downstream branch is handled deterministically from the following papers.
    analyses = ["future_directions", "research_questions"]
    evidence = _ai_evolution_evidence(report, cores, G)
    cache = _load_why_matters_cache(cache_path)
    out: Dict[str, Any] = {}
    evidence_hash = hashlib.sha256(evidence.encode("utf-8")).hexdigest()[:16]
    pending=[]
    for analysis in analyses:
        key=f"evolution-v6:{model}:{analysis}:{evidence_hash}"
        rec=cache.get(key)
        if rec and rec.get("structured"):
            out[analysis]=rec
        else:
            pending.append((analysis,key))
    if not pending:
        print("[AI evolution] structured analyses loaded from cache")
        return out

    seed=report.get("seed",{}) or {}
    combined_prompt=f"""You are a careful research-evolution analyst running as a local language model. Analyze ONLY the supplied evidence packet. Do not invent papers, findings, methods, motivations, causal links, or future events. Citation links and publication dates alone do not prove intellectual influence. Separate observation from inference and state uncertainty explicitly.

SEED
Title: {seed.get('title','Untitled')}
Year: {seed.get('year','unknown')}
DOI: {seed.get('doi','unknown')}

Return VALID JSON ONLY with exactly these top-level keys: future_directions and research_questions. Each value must be an object with keys analysis_type, items, overall_uncertainty. Each item must have exactly: title, observation, inference, evidence (array), uncertainty, research_value.

For future_directions produce 4-6 plausible directions. For research_questions produce 8-12 concrete questions. Use specific years/papers/concepts/transitions from the packet. Explain what is observed versus inferred. Do not predict with certainty. No markdown fences or text outside JSON.

EVIDENCE PACKET
{evidence}"""
    if len(pending)==2:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
                text=await _ollama_chat_streaming(client,model,combined_prompt,base_url=base_url,timeout=600.0,num_predict=4200)
            raw=text.strip(); raw=re.sub(r"^```(?:json)?\s*|\s*```$","",raw,flags=re.I|re.S).strip()
            obj=json.loads(raw)
            if isinstance(obj,dict):
                now=datetime.now(timezone.utc).isoformat()
                ok=True
                for analysis,key in pending:
                    structured=obj.get(analysis)
                    if not isinstance(structured,dict): ok=False; break
                    parsed=_parse_structured_ai(json.dumps(structured),analysis)
                    if not parsed: ok=False; break
                    rec={"structured":parsed,"model":model,"generated_at":now,"source":"local_ollama","analysis_version":"v6"}
                    cache[key]=rec; out[analysis]=rec
                if ok:
                    _save_why_matters_cache(cache,cache_path)
                    print("[AI evolution] completed structured 2/2 analyses in one local-model request")
                    return out
            print("[AI evolution] structured combined response incomplete; using individual fallback")
        except Exception as e:
            print(f"[AI evolution] structured combined request failed; using fallback: {type(e).__name__}: {e}")

    sem=asyncio.Semaphore(max(1,min(int(parallel),len(pending))))
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0,connect=30.0)) as client:
        async def run_one(analysis,key):
            prompt=_ai_evolution_prompt(report,evidence,analysis)
            async with sem:
                for attempt in range(2):
                    try:
                        text=await _ollama_chat_streaming(client,model,prompt,base_url=base_url,timeout=600.0,num_predict=2200)
                        parsed=_parse_structured_ai(text,analysis)
                        if parsed:
                            return analysis,key,{"structured":parsed,"model":model,"generated_at":datetime.now(timezone.utc).isoformat(),"source":"local_ollama","analysis_version":"v6"},None
                    except Exception as e:
                        err=f"{type(e).__name__}: {e}"
                        if attempt==0: await asyncio.sleep(.5)
                return analysis,key,None,"invalid or empty structured response"
        results=await asyncio.gather(*(run_one(a,k) for a,k in pending))
    for analysis,key,rec,error in results:
        if rec:
            cache[key]=rec; out[analysis]=rec; print(f"[AI evolution] completed {analysis}")
        else: print(f"[AI evolution] failed {analysis}: {error}")
    _save_why_matters_cache(cache,cache_path)
    return out


def seed_cited_topic_summary(G: nx.DiGraph, seed_doi: str, oa_cache: Dict[str, Any], top_k: int = 10) -> Dict[str, Any]:
    """Summarize topic signals among works directly cited by the seed paper.

    This is a descriptive summary of the metadata available for the seed's
    references. It does not infer that a cited topic caused or influenced the
    seed's contribution. OpenAlex topic/concept labels are used when available.
    """
    seed = normalize_doi(seed_doi)
    cited = [d for d in G.predecessors(seed) if d != seed]
    rows = []
    topic_counts = Counter()
    topic_examples = {}
    covered = 0

    for doi in cited:
        oa = oa_cache.get(doi) or {}
        labels = [name for name, _ in openalex_topic_labels(oa, limit=6)]
        # Keep the strongest unique labels per cited work to avoid one record
        # dominating the summary simply because it has many metadata labels.
        labels = list(dict.fromkeys(labels))[:6]
        if labels:
            covered += 1
            for label in labels:
                topic_counts[label] += 1
                topic_examples.setdefault(label, []).append(doi)
        core_title = None
        # Core metadata is not passed here; OpenAlex display_name is sufficient
        # for examples and the dashboard can resolve DOI links.
        rows.append({"doi": doi, "title": str(oa.get("display_name") or "Untitled"), "topics": labels})

    total = len(cited)
    ranked = []
    for topic, count in topic_counts.most_common(top_k):
        ranked.append({
            "topic": topic,
            "cited_papers": int(count),
            "share_of_cited_papers_pct": round(100.0 * count / total, 1) if total else 0.0,
            "example_dois": topic_examples.get(topic, [])[:5],
        })
    return {
        "seed_doi": seed,
        "cited_paper_count": total,
        "topic_coverage_papers": covered,
        "topic_coverage_pct": round(100.0 * covered / total, 1) if total else 0.0,
        "topics": ranked,
        "method": "Top OpenAlex Topics where available, otherwise OpenAlex Concepts; counts are the number of directly cited seed references carrying each label. Topic labels are non-exclusive, so percentages need not sum to 100%. A label is descriptive metadata, not evidence of causal influence.",
    }

def downstream_topics_and_questions(G: nx.DiGraph, seed_doi: str, cores: Dict[str, WorkCore], oa_cache: Dict[str, Any], max_papers: int = 40) -> Dict[str, Any]:
    """Build a non-AI summary of later papers that cite the seed/downstream branch.

    Topics come from OpenAlex metadata. Questions and reported answers are extracted
    conservatively from titles/abstracts; no causal or intellectual-influence claim is made.
    """
    seed = normalize_doi(seed_doi)
    # Only traverse edges explicitly added from OpenAlex citing-work expansion.
    # This prevents unusual citation cycles/date anomalies in the reference-side
    # graph from being mislabeled as downstream development.
    forward = nx.DiGraph()
    forward.add_node(seed)
    for u, v, data in G.edges(data=True):
        if data.get("edge_kind") == "forward_citation":
            forward.add_edge(u, v)

    descendants=[]
    try:
        lengths = nx.single_source_shortest_path_length(forward, seed)
    except Exception:
        lengths = {}
    for doi, depth in lengths.items():
        if doi == seed or depth <= 0:
            continue
        c=cores.get(doi)
        if not c:
            continue
        oa=oa_cache.get(doi) or {}
        descendants.append((int(depth), c.year or 9999, doi, c, oa))
    descendants=sorted(descendants, key=lambda x:(x[0],x[1],x[2]))[:max_papers]

    topic_counts=Counter(); topic_examples={}
    paper_rows=[]
    for depth, year, doi, c, oa in descendants:
        topics=[name for name, _ in openalex_topic_labels(oa, limit=6)]
        for topic in topics:
            topic_counts[topic]+=1
            topic_examples.setdefault(topic,[]).append(doi)
        abstract=c.abstract or openalex_abstract(oa) or ''
        sentences=[x.strip() for x in re.split(r'(?<=[.!?])\s+', re.sub(r'\s+',' ',abstract)) if x.strip()]
        q_sent=next((x for x in sentences if re.search(r'\b(aim|aimed|objective|investigate|investigat|examine|assess|determine|whether|question|we ask|we tested|we sought)\b',x,re.I)), '')
        ans_sent=next((x for x in sentences if re.search(r'\b(find|found|show|shows|showed|demonstrat|reveal|revealed|observe|observed|conclude|concluded|result|results|indicate|indicated|suggest|suggested|associated|increased|decreased|improved|reduced)\b',x,re.I)), '')
        paper_rows.append({
            'doi':doi,'title':c.title,'year':c.year,'distance':depth,'topics':topics,
            'question':q_sent[:500], 'answer':ans_sent[:600],
            'evidence_status': 'question_and_finding' if q_sent and ans_sent else ('finding_only' if ans_sent else ('question_only' if q_sent else 'abstract_not_explicit')),
        })
    total=len(descendants)
    topics_ranked=[{'topic':t,'papers':int(n),'share_pct':round(100*n/total,1) if total else 0.0,'example_dois':topic_examples.get(t,[])[:5]} for t,n in topic_counts.most_common(12)]
    return {'paper_count':total,'topics':topics_ranked,'papers':paper_rows,
            'method':'Later citing papers are identified by directed downstream citation paths from the seed. Topics use OpenAlex Topics/Concepts and are non-exclusive, so topic percentages need not sum to 100%. Question and reported-finding text is conservatively extracted from the available title/abstract; it is not AI-generated and does not establish causality or intellectual influence.'}


def build_evolution_report(
    seed: str,
    G: nx.DiGraph,
    cores: Dict[str, WorkCore],
    oa_cache: Dict[str, Any],
    influence_top_k: int = 25,
    influence_min_score: float = 0.0,
    analysis_scope: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    seed_n = normalize_doi(seed)
    seed_core = cores.get(seed_n)

    # The scientific analysis uses the complete observed citation graph.
    # Signal-based reduction is reserved for optional display, never for the
    # underlying timeline, transition metrics, or relationship evidence.
    G_signal = G.copy()
    annotate_relationships(G_signal, cores)

    # timeline dataframe
    tl = summarize_timeline(G_signal, cores)

    tl = add_distance_to_seed(G_signal, seed_n, tl)

    # explainability paths
    paths = explain_paths_to_seed(G_signal, seed_n)
    why_path_col = []
    for doi in tl["doi"].tolist() if not tl.empty else []:
        p = paths.get(doi)
        why_path_col.append(p if p else [])
    if not tl.empty:
        tl = tl.copy()
        tl["why_path"] = why_path_col

    if not tl.empty:
        tl = tl.copy()
        tl["cited_by_count"] = tl["doi"].apply(lambda d: cited_by_count_for_doi(d, oa_cache))
        tl["primary_topic"] = tl["doi"].apply(lambda d: primary_topic(oa_cache.get(d, {}) or {}))
        def mean_reference_similarity(d: str) -> Optional[float]:
            vals = [float(G_signal.edges[u, d].get("similarity_signal", 0.0) or 0.0) for u in G_signal.predecessors(d)]
            return float(sum(vals) / len(vals)) if vals else None
        tl["mean_reference_similarity"] = tl["doi"].apply(mean_reference_similarity)

        # A paper-level series for the dashboard: compare every analyzed paper
        # directly with the seed.  The older mean_reference_similarity metric is
        # still retained for transition analysis, but it is unsuitable for a
        # one-hop upstream timeline because terminal reference papers have no
        # loaded predecessors of their own.
        seed_similarity = {
            d: contextual_similarity_to_seed(d, seed_n, cores, oa_cache)
            for d in tl["doi"].tolist()
        }
        tl["seed_context_similarity"] = tl["doi"].map(lambda d: seed_similarity.get(d, {}).get("score"))
        tl["seed_semantic_similarity"] = tl["doi"].map(lambda d: seed_similarity.get(d, {}).get("semantic"))
        tl["seed_topic_overlap"] = tl["doi"].map(lambda d: seed_similarity.get(d, {}).get("topic_overlap"))
        tl["seed_similarity_basis"] = tl["doi"].map(lambda d: seed_similarity.get(d, {}).get("basis"))

    # Topics represented in the seed paper's directly cited references.
    seed_cited_topics = seed_cited_topic_summary(G_signal, seed_n, oa_cache)
    downstream_analysis = downstream_topics_and_questions(G_signal, seed_n, cores, oa_cache)

    # concept/institution pivots
    concept_pivot = concepts_over_time(oa_cache, cores)
    inst_pivot = institutions_over_time(oa_cache, cores)

    # authors + funders + advanced intelligence
    top_auth = top_authors_openalex(oa_cache, cores)
    author_traj = author_trajectories(oa_cache, cores)
    funders_df, coverage = funders_summary(cores)
    funding_evo = funding_evolution(cores)
    transitions = key_transition_papers(G_signal, seed_n, cores, oa_cache)
    boundaries = era_boundaries(concept_pivot)
    eras = era_segments(concept_pivot, boundaries)
    inst_mobility = institutional_mobility(oa_cache, cores)
    role_counts = pd.DataFrame([{"role": c.role, "count": 1} for c in cores.values()]).groupby("role")["count"].sum().reset_index() if cores else pd.DataFrame()
    years = {d: cores[d].year for d in G_signal.nodes if d in cores and cores[d].year}
    impossible_edges = []
    same_year_edges = 0
    for u, v, ed in G_signal.edges(data=True):
        uy, vy = years.get(u), years.get(v)
        if uy is not None and vy is not None:
            if uy > vy:
                impossible_edges.append({"source": u, "target": v, "source_year": uy, "target_year": vy})
            elif uy == vy:
                same_year_edges += 1
    edge_categories = Counter(ed.get("relationship_category", "citation relationship (date/order uncertain)") for _, _, ed in G_signal.edges(data=True))
    graph_nodes = list(G_signal.nodes)
    topic_covered = sum(1 for d in graph_nodes if openalex_topic_labels(oa_cache.get(d, {}) or {}, limit=1))
    abstract_covered = sum(1 for d in graph_nodes if d in cores and bool((cores[d].abstract or openalex_abstract(oa_cache.get(d, {}) or {})).strip()))
    citation_qc = {
        "canonical_edge_definition": "A directed edge source -> target means the target record cites the source record; this is an observed citation relationship, not proof of intellectual influence.",
        "citation_edges": int(G_signal.number_of_edges()),
        "nodes_with_year": int(len(years)),
        "same_year_edges": int(same_year_edges),
        "date_inconsistent_edges": int(len(impossible_edges)),
        "date_inconsistent_examples": impossible_edges[:20],
        "relationship_categories": dict(edge_categories),
        "metadata_coverage": {
            "papers_total": int(len(graph_nodes)),
            "core_metadata": int(sum(1 for d in graph_nodes if d in cores)),
            "openalex": int(sum(1 for d in graph_nodes if d in oa_cache)),
            "year": int(len(years)),
            "topic": int(topic_covered),
            "abstract": int(abstract_covered),
        },
        "semantic_similarity_is_secondary": True,
        "citation_impact_is_secondary": True,
    }
    # Merge external identifiers from OpenAlex into the canonical DOI record.
    for doi, core in cores.items():
        ids = dict(core.identifiers or {"doi": doi}); ids.setdefault("doi", doi)
        oa_ids = (oa_cache.get(doi) or {}).get("ids") or {}
        for key in ("pmid", "pmcid", "arxiv", "mag"):
            if oa_ids.get(key): ids[key] = str(oa_ids[key])
        core.identifiers = ids

    report = {
        "software": {"name": APP_NAME, "version": APP_VERSION},
        "seed_input": seed,
        "seed": {
            "doi": seed_n,
            "title": seed_core.title if seed_core else None,
            "year": seed_core.year if seed_core else None,
            "authors": seed_core.authors if seed_core else [],
            "role": seed_core.role if seed_core else None,
            "venue": seed_core.venue if seed_core else None,
            "abstract": seed_core.abstract if seed_core else "",
            "identifiers": seed_core.identifiers if seed_core else {"doi": seed_n},
        },
        "identifiers": {d: (cores[d].identifiers or {"doi": d}) for d in cores},
        "relationship_legend": {
            "prior literature / context": "Referenced work published before the citing work; temporal precedence only.",
            "same-year citation (ordering uncertain)": "Same publication year; year-level chronology cannot establish ordering.",
            "later citing work": "Later work returned by the downstream citing-work expansion.",
            "later citing work (date uncertain)": "Downstream citing relationship with incomplete year metadata.",
            "date-inconsistent citation record": "Recorded direction conflicts with available publication years and should be treated as a metadata-quality warning.",
            "citation relationship (date/order uncertain)": "Citation edge with incomplete publication ordering."
        },
        "citation_quality": citation_qc,
        "analysis_scope": dict(analysis_scope or {}),
        "retrieval_warnings": list(G_signal.graph.get("retrieval_warnings", []) or []),
        "summary": {
            "nodes_raw": int(G.number_of_nodes()),
            "edges_raw": int(G.number_of_edges()),
            "nodes": int(G_signal.number_of_nodes()),
            "edges": int(G_signal.number_of_edges()),
            "works_with_openalex": int(len(oa_cache)),
            "papers_with_metadata": int(len(tl)),
            "citation_basis": "complete observed citation relationships within the requested traversal scope; display filtering is separate",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "timeline": df_to_records(tl),
        "concepts_by_year": pivot_to_year_dict(concept_pivot),
        "seed_cited_topics": seed_cited_topics,
        "downstream_topics_questions": downstream_analysis,
        "institutions_by_year": pivot_to_year_dict(inst_pivot),
        "top_authors": df_to_records(top_auth),
        "author_trajectories": df_to_records(author_traj),
        "transition_method": {
            "status": "exploratory",
            "weights": {"structural_bridge_percentile": 0.45, "topic_shift_signal": 0.25, "dataset_connectivity_percentile": 0.15, "openalex_citation_percentile": 0.15},
            "note": "Composite transition signals help prioritize inspection; they are not measures of scientific importance, quality, or causal influence."
        },
        "key_transition_papers": df_to_records(transitions),
        "era_boundaries": boundaries,
        "eras": eras,
        "paper_roles": df_to_records(role_counts),
        "institutional_mobility": df_to_records(inst_mobility),
        "top_funders": {
            "coverage_pct": float(coverage),
            "funders": df_to_records(funders_df),
            "by_decade": df_to_records(funding_evo),
        },
    }
    return report


def write_report_json(report: Dict[str, Any], path: str = "litev_report.json") -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

def add_distance_to_seed(G: nx.DiGraph, seed: str, tl: pd.DataFrame) -> pd.DataFrame:
    seed = normalize_doi(seed)
    if tl.empty:
        return tl
    dists_to = {}
    dists_from = {}
    for doi in tl["doi"].tolist():
        try:
            dists_to[doi] = len(nx.shortest_path(G, source=doi, target=seed)) - 1
        except nx.NetworkXNoPath:
            dists_to[doi] = None
        try:
            dists_from[doi] = len(nx.shortest_path(G, source=seed, target=doi)) - 1
        except nx.NetworkXNoPath:
            dists_from[doi] = None
    tl = tl.copy()
    tl["distance_to_seed"] = tl["doi"].map(dists_to)
    tl["distance_from_seed"] = tl["doi"].map(dists_from)
    tl["is_seed"] = tl["doi"].eq(seed)
    tl["position"] = tl.apply(
        lambda r: "seed" if r["is_seed"] else (
            "upstream" if pd.notna(r.get("distance_to_seed")) else (
                "downstream" if pd.notna(r.get("distance_from_seed")) else "other"
            )
        ), axis=1
    )
    return tl

def cited_by_count_for_doi(doi: str, oa_cache: dict) -> int | None:
    oa = oa_cache.get(doi)
    if not oa:
        return None
    c = oa.get("cited_by_count")
    return int(c) if c is not None else None


def _bibtex_key(r):
    a = str(r.get("authors") or "").split(";")[0].strip().split()
    family = a[-1] if a else "paper"
    doi = str(r.get("doi") or "")
    suffix = hashlib.sha1(doi.encode("utf-8")).hexdigest()[:6] if doi else "000000"
    return re.sub(r"[^A-Za-z0-9]", "", family) + str(r.get("year") or "") + suffix


def _bibtex_type(work_type: str) -> str:
    wt = str(work_type or "").lower()
    if wt in {"book", "monograph", "reference-book", "edited-book"}:
        return "book"
    if wt in {"book-chapter", "reference-entry"}:
        return "incollection"
    if wt in {"proceedings-article", "proceedings"}:
        return "inproceedings"
    if wt in {"journal-article", "article"}:
        return "article"
    return "misc"


def _ris_type(work_type: str) -> str:
    wt = str(work_type or "").lower()
    if wt in {"book", "monograph", "reference-book", "edited-book"}:
        return "BOOK"
    if wt in {"book-chapter", "reference-entry"}:
        return "CHAP"
    if wt in {"proceedings-article", "proceedings"}:
        return "CPAPER"
    if wt in {"dataset"}:
        return "DATA"
    return "JOUR"


def export_litev(report, cores, export_dir="litev_exports") -> Dict[str, Path]:
    """Write researcher-friendly exports and return named output paths."""
    d = Path(export_dir)
    d.mkdir(parents=True, exist_ok=True)
    timeline = safe_df(report.get("timeline", []))
    timeline_by_doi = {}
    if not timeline.empty and "doi" in timeline.columns:
        timeline_by_doi = {normalize_doi(str(r.get("doi") or "")): r for r in timeline.to_dict(orient="records")}

    rows = []
    for doi, c in cores.items():
        ids = c.identifiers or {"doi": doi}
        t = timeline_by_doi.get(normalize_doi(doi), {})
        rows.append({
            "doi": doi,
            "title": c.title,
            "year": c.year,
            "authors": "; ".join(c.authors),
            "venue": c.venue,
            "work_type": c.work_type,
            "role": c.role,
            "position": t.get("position", ""),
            "distance_to_seed": t.get("distance_to_seed", ""),
            "distance_from_seed": t.get("distance_from_seed", ""),
            "primary_topic": t.get("primary_topic", ""),
            "cited_by_count": t.get("cited_by_count", ""),
            "observed_reference_count": t.get("observed_reference_count", ""),
            "observed_citing_count": t.get("observed_citing_count", ""),
            **{f"id_{k}": v for k, v in ids.items() if k != "doi"},
        })
    df = pd.DataFrame(rows)
    paths: Dict[str, Path] = {}

    cp = d / "litev_papers.csv"
    df.to_csv(cp, index=False, encoding="utf-8-sig")
    paths["csv"] = cp.resolve()

    try:
        xp = d / "litev_papers.xlsx"
        with pd.ExcelWriter(xp, engine="openpyxl") as w:
            df.to_excel(w, index=False, sheet_name="Papers")
            timeline.to_excel(w, index=False, sheet_name="Timeline")
            safe_df(report.get("key_transition_papers", [])).to_excel(w, index=False, sheet_name="Transitions")
            safe_df((report.get("seed_cited_topics", {}) or {}).get("topics", [])).to_excel(w, index=False, sheet_name="Seed cited topics")
            safe_df((report.get("downstream_topics_questions", {}) or {}).get("papers", [])).to_excel(w, index=False, sheet_name="Downstream papers")
        paths["excel"] = xp.resolve()
    except Exception as e:
        print(f"[Export] Excel skipped: {e}")

    bib = []
    ris = []
    used_keys: Counter = Counter()
    for r in rows:
        key = _bibtex_key(r)
        used_keys[key] += 1
        if used_keys[key] > 1:
            key = f"{key}{used_keys[key]}"
        bt = _bibtex_type(r.get("work_type"))
        clean_title = str(r.get("title") or "").replace("{", "").replace("}", "")
        fields = [
            f"  title = {{{clean_title}}}",
            f"  author = {{{' and '.join(r['authors'].split('; '))}}}",
            f"  year = {{{r.get('year') or ''}}}",
        ]
        if r.get("venue"):
            fields.append(f"  journal = {{{r.get('venue')}}}")
        if r.get("doi"):
            fields.append(f"  doi = {{{r['doi']}}}")
        bib.append(f"@{bt}{{{key},\n" + ",\n".join(fields) + "\n}")

        ris_lines = [f"TY  - {_ris_type(r.get('work_type'))}", f"TI  - {r.get('title') or ''}"]
        ris_lines.extend(f"AU  - {a}" for a in r["authors"].split("; ") if a)
        ris_lines.extend([
            f"PY  - {r.get('year') or ''}",
            f"JO  - {r.get('venue') or ''}",
            f"DO  - {r['doi']}",
            "ER  -",
        ])
        ris.append("\n".join(ris_lines))

    bp = d / "litev_references.bib"
    bp.write_text("\n\n".join(bib), encoding="utf-8")
    paths["bibtex"] = bp.resolve()
    rp = d / "litev_references.ris"
    rp.write_text("\n\n".join(ris), encoding="utf-8")
    paths["ris"] = rp.resolve()

    o = _build_user_summary(report)
    lines = [
        f"# {APP_NAME} {APP_VERSION} — Literature Evolution Analysis",
        "",
        f"Seed: {report.get('seed_input','')} → {report.get('seed',{}).get('doi','')}",
        "",
        f"Papers analyzed: {o['paper_count']}",
        f"Observed citation links: {o['edge_count']}",
        f"Time span: {o['first_year'] or '—'}–{o['last_year'] or '—'}",
        f"Transition candidates: {o['transition_count']}",
        "",
        "## Research story",
        *[f"- {x}" for x in o["story"]],
        "",
        "## Potential transition papers",
        *[f"- {r.get('year','—')} — {r.get('title','')} ({r.get('doi','')})" for r in (report.get('key_transition_papers',[]) or [])[:15]],
    ]
    seed_topics = (report.get("seed_cited_topics", {}) or {}).get("topics", [])
    if seed_topics:
        lines += ["", "## Topics represented in the seed paper's references"]
        lines += [f"- {r.get('topic')}: {r.get('cited_papers')} cited paper(s)" for r in seed_topics[:12]]
    downstream = report.get("downstream_topics_questions", {}) or {}
    if downstream.get("topics"):
        lines += ["", "## Topics in downstream citing literature"]
        lines += [f"- {r.get('topic')}: {r.get('papers')} paper(s)" for r in downstream.get("topics", [])[:12]]

    mp = d / "litev_research_brief.md"
    mp.write_text("\n".join(lines), encoding="utf-8")
    paths["markdown"] = mp.resolve()
    return paths


# -----------------------------
# Dashboard rendering (formerly viz_evolution.py)
# -----------------------------

pio.templates.default = "plotly_white"
DEFAULT_FONT = dict(family="Inter, Arial, sans-serif", size=14, color="#222")


def load_report(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dict_of_dicts_to_long(
    d: dict,
    index_name: str = "year",
    col_name: str = "item",
    val_name: str = "value",
) -> pd.DataFrame:
    """{"2010":{"A":1,"B":2}, "2011":{"A":3}} -> long DataFrame."""
    rows = []
    for year, inner in (d or {}).items():
        for k, v in (inner or {}).items():
            rows.append({index_name: int(year), col_name: k, val_name: float(v)})
    return pd.DataFrame(rows)


def safe_df(records) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records)


def _distance_as_category(series: pd.Series) -> pd.Series:
    """Convert distance values to readable categorical strings without choking on nulls."""
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric.astype("Int64").astype(str).replace("<NA>", "unknown")



def _log_size(values: pd.Series, base: float = 8.0, scale: float = 6.0, max_size: float = 44.0) -> List[float]:
    nums = pd.to_numeric(values, errors="coerce").fillna(0).clip(lower=0)
    return [float(min(max_size, base + scale * math.log1p(v))) for v in nums]







def _short_title(title: str, n: int = 78) -> str:
    title = str(title or 'Untitled').strip()
    return title if len(title) <= n else title[: n - 1].rstrip() + '…'


def _build_user_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    seed = report.get('seed', {}) or {}
    summary = report.get('summary', {}) or {}
    timeline = safe_df(report.get('timeline', []))
    transitions = safe_df(report.get('key_transition_papers', []))
    eras = report.get('eras', []) or []
    years = pd.to_numeric(timeline.get('year', pd.Series(dtype=float)), errors='coerce').dropna()
    first_year = int(years.min()) if not years.empty else seed.get('year')
    last_year = int(years.max()) if not years.empty else seed.get('year')
    key = []
    for _, r in transitions.head(6).iterrows():
        key.append({
            'doi': str(r.get('doi', '')), 'title': str(r.get('title', 'Untitled')),
            'year': int(r['year']) if pd.notna(r.get('year')) else None,
            'role': str(r.get('role', 'unknown')), 'score': float(r.get('transition_score', 0) or 0),
            'cited_by_count': int(r.get('cited_by_count', 0) or 0)
        })
    story = []
    if first_year and last_year:
        story.append(f'The analyzed literature set spans {first_year}–{last_year}.')
    for e in eras[:3]:
        cs = [c.get('concept', '') for c in e.get('top_concepts', [])[:2] if c.get('concept')]
        if cs:
            story.append(f"{e.get('start_year')}–{e.get('end_year')}: {', '.join(cs)} dominated the concept signal.")
    if key:
        story.append(f"The highest exploratory transition signal is {key[0]['year'] or 'undated'}: {_short_title(key[0]['title'], 105)}.")
    if not story:
        story.append('The literature set was retrieved, but there is not enough dated/topic signal to produce a reliable narrative.')
    return {
        'seed_title': seed.get('title') or seed.get('doi') or 'Seed paper', 'seed_doi': seed.get('doi'),
        'first_year': first_year, 'last_year': last_year,
        'paper_count': int(summary.get('papers_with_metadata', summary.get('nodes', 0)) or 0), 'edge_count': int(summary.get('edges', 0) or 0),
        'transition_count': len(transitions), 'eras': len(eras), 'key_papers': key, 'story': story
    }


def _fig_base(fig: go.Figure, height: int = 430) -> go.Figure:
    fig.update_layout(template='plotly_white', height=height, margin=dict(l=55,r=30,t=65,b=55),
                      font=dict(family='Inter, Arial, sans-serif', size=13, color='#1f2937'),
                      title=dict(x=0, xanchor='left', font=dict(size=18, color='#111827')),
                      paper_bgcolor='white', plot_bgcolor='white', hoverlabel=dict(font_size=12))
    return fig


def _empty_fig(message: str) -> go.Figure:
    f = go.Figure(); f.add_annotation(text=message, x=.5, y=.5, showarrow=False); return _fig_base(f)


def _make_story_timeline(timeline: pd.DataFrame) -> go.Figure:
    if timeline.empty:
        return _empty_fig('No dated papers available.')
    df = timeline.copy()
    df['year'] = pd.to_numeric(df['year'], errors='coerce')
    df = df.dropna(subset=['year']).copy()
    if df.empty:
        return _empty_fig('No dated papers available.')
    df['year'] = df['year'].astype(int)
    df['similarity'] = pd.to_numeric(df.get('seed_context_similarity'), errors='coerce')
    df = df.dropna(subset=['similarity']).copy()
    if df.empty:
        return _empty_fig('Contextual similarity to the seed is unavailable for the analyzed papers.')

    impact = pd.to_numeric(df.get('cited_by_count', 0), errors='coerce').fillna(0)
    df['size'] = _log_size(impact + 1, base=7, scale=4, max_size=30)

    # Keep the two component signals available in hover text.  NaN values are
    # left blank by Plotly and mean that component metadata was unavailable.
    if 'seed_semantic_similarity' not in df.columns:
        df['seed_semantic_similarity'] = None
    if 'seed_topic_overlap' not in df.columns:
        df['seed_topic_overlap'] = None
    if 'seed_similarity_basis' not in df.columns:
        df['seed_similarity_basis'] = ''

    fig = px.scatter(
        df.sort_values(['year', 'similarity']),
        x='year',
        y='similarity',
        size='size',
        color='position' if 'position' in df.columns else ('role' if 'role' in df.columns else None),
        hover_name='title',
        hover_data={
            'seed_semantic_similarity': ':.3f',
            'seed_topic_overlap': ':.3f',
            'seed_similarity_basis': True,
            'size': False,
        },
        custom_data=['doi'],
        labels={
            'year': 'Year',
            'similarity': 'Contextual similarity to seed (0–1)',
            'position': 'Position',
            'seed_semantic_similarity': 'Text similarity to seed',
            'seed_topic_overlap': 'Topic overlap with seed',
            'seed_similarity_basis': 'Evidence used',
        },
        title='Contextual similarity to the seed over time',
        size_max=30,
    )
    fig.update_traces(marker=dict(opacity=.82, line=dict(width=1)))
    fig.update_yaxes(range=[0, 1.02])
    return _fig_base(fig, 600)


def _make_concept_fig(report: Dict[str, Any]) -> go.Figure:
    long = dict_of_dicts_to_long(report.get('concepts_by_year', {}), col_name='concept', val_name='score')
    if long.empty: return _empty_fig('No concept metadata available.')
    top = long.groupby('concept')['score'].sum().nlargest(6).index.tolist()
    pivot = long[long['concept'].isin(top)].pivot_table(index='year', columns='concept', values='score', aggfunc='sum', fill_value=0).sort_index()
    fig = go.Figure()
    for c in pivot.columns: fig.add_trace(go.Scatter(x=pivot.index, y=pivot[c], mode='lines', stackgroup='one', name=c))
    fig.update_layout(title='How research topics changed', xaxis_title='Year', yaxis_title='Share of displayed topic signal (%)')
    return _fig_base(fig, 430)


def _make_impact_fig(timeline: pd.DataFrame) -> go.Figure:
    if timeline.empty or 'cited_by_count' not in timeline.columns: return _empty_fig('Citation-impact data unavailable.')
    df=timeline.copy(); df['year']=pd.to_numeric(df['year'],errors='coerce'); df['cited_by_count']=pd.to_numeric(df['cited_by_count'],errors='coerce'); df=df.dropna(subset=['year','cited_by_count']).copy()
    if df.empty: return _empty_fig('Citation-impact data unavailable.')
    df['size']=_log_size(df['cited_by_count']+1,base=7,scale=4,max_size=28)
    fig=px.scatter(df,x='year',y='cited_by_count',size='size',color='position' if 'position' in df.columns else ('role' if 'role' in df.columns else None),hover_name='title',custom_data=['doi'],log_y=True,labels={'year':'Year','cited_by_count':'OpenAlex cited-by count','position':'Position'},title='Citation impact across analyzed papers')
    fig.update_traces(marker=dict(opacity=.8,line=dict(width=1))); return _fig_base(fig,560)


def _make_key_papers_fig(transitions: pd.DataFrame) -> go.Figure:
    if transitions.empty: return _empty_fig('No transition papers detected.')
    df=transitions.head(10).copy().sort_values('transition_score'); df['label']=df['title'].map(lambda x:_short_title(x,68))
    fig=px.bar(df,x='transition_score',y='label',orientation='h',custom_data=['doi'],labels={'transition_score':'Exploratory transition signal','label':''},title='Potential transition markers')
    fig.update_traces(hovertemplate='%{y}<br>signal=%{x:.3f}<extra>Click to open DOI</extra>'); return _fig_base(fig,500)


def write_dashboard_html(report: Dict[str, Any], out_html: str | Path = 'litev_dashboard.html', cores: Optional[Dict[str, WorkCore]] = None) -> Path:
    """Render a self-contained, research-oriented dashboard.

    Citation relationships are used internally for traversal and metrics, but no
    citation-network/lineage visualization is rendered.
    """
    cores = cores or {}
    out_html = Path(out_html)
    seed = report.get('seed', {}) or {}
    summary = report.get('summary', {}) or {}
    scope = report.get('analysis_scope', {}) or {}
    timeline = safe_df(report.get('timeline', []))
    transitions = safe_df(report.get('key_transition_papers', []))
    overview = _build_user_summary(report)
    cited_topics = report.get('seed_cited_topics', {}) or {}
    upstream_hops = int(scope.get('upstream_hops') or 0)
    downstream_hops = int(scope.get('downstream_hops') or 0)
    retrieval_warnings = [str(x) for x in (report.get('retrieval_warnings') or []) if str(x).strip()]
    warning_html = ''
    if retrieval_warnings:
        warning_items = ''.join(f'<li>{escape(x)}</li>' for x in retrieval_warnings[:8])
        extra = len(retrieval_warnings) - min(len(retrieval_warnings), 8)
        more = f'<div class="hint" style="margin-top:6px">Plus {extra} additional retrieval warning(s) recorded in the JSON report.</div>' if extra > 0 else ''
        warning_html = (
            '<div class="retrieval-warning"><b>Partial metadata retrieval</b>'
            '<div>Some Crossref records could not be retrieved. The report continues with available OpenAlex/metadata evidence; missing references can reduce upstream coverage.</div>'
            f'<ul>{warning_items}</ul>{more}</div>'
        )

    figs = [
        ('story_timeline', _make_story_timeline(timeline)),
        ('impact_plot', _make_impact_fig(timeline)),
        ('concept_plot', _make_concept_fig(report)),
        ('transition_plot', _make_key_papers_fig(transitions)),
    ]
    for _, fig in figs:
        _fig_base(fig)

    plotly_embedded = False
    def fig_html(fig: go.Figure, div_id: str) -> str:
        nonlocal plotly_embedded
        include = True if not plotly_embedded else False
        plotly_embedded = True
        return fig.to_html(
            full_html=False,
            include_plotlyjs=include,
            config={'responsive': True, 'displaylogo': False, 'scrollZoom': False},
            div_id=div_id,
        )

    key_cards = []
    for i, r in enumerate(overview['key_papers'][:6], 1):
        key_cards.append(
            f'<div class="paper-card clickable" data-doi="{escape(str(r.get("doi") or ""))}">'
            f'<div class="paper-rank">{i}</div><div><div class="paper-year">{escape(str(r.get("year") or "—"))} · {escape(str(r.get("role") or "unknown"))}</div>'
            f'<div class="paper-title">{escape(str(r.get("title") or "Untitled"))}</div>'
            f'<div class="paper-meta">Exploratory transition signal {float(r.get("score") or 0):.3f} · {int(r.get("cited_by_count") or 0):,} citations</div></div></div>'
        )
    key_cards_html = ''.join(key_cards) or '<div class="empty">No transition candidates were detected.</div>'

    findings = report.get('paper_findings_ai') or report.get('why_they_matter') or {}
    findings_rows = []
    for doi, info in findings.items():
        core = cores.get(normalize_doi(doi))
        if not core:
            continue
        findings_rows.append({
            'doi': normalize_doi(doi), 'title': core.title, 'year': core.year,
            'role': core.role, 'text': str(info.get('text') or ''),
            'model': info.get('model', 'local model'), 'source': info.get('source', 'local_ollama'),
        })
    findings_cards = ''.join(
        f'<article class="why-card clickable" data-doi="{escape(r["doi"])}"><div>'
        f'<div class="paper-year">{escape(str(r.get("year") or "—"))} · {escape(str(r.get("role") or "unknown"))}</div>'
        f'<div class="paper-title">{escape(str(r.get("title") or "Untitled"))}</div>'
        f'<div class="why-text">{escape(r["text"])}</div>'
        f'<div class="paper-meta">{("Generated locally with " + escape(str(r.get("model") or "local model"))) if r.get("source") == "local_ollama" else "Abstract unavailable — no model inference performed"}</div>'
        f'</div></article>'
        for r in findings_rows
    )

    cited_topic_rows = cited_topics.get('topics') or []
    cited_topic_html = ''.join(
        f'<div class="topic-row"><div><b>{escape(str(r.get("topic") or "Untitled"))}</b>'
        f'<span class="hint">{int(r.get("cited_papers") or 0)} cited paper(s) · {float(r.get("share_of_cited_papers_pct") or 0):.1f}%</span></div>'
        f'<div class="topic-bar"><span style="width:{min(100.0, float(r.get("share_of_cited_papers_pct") or 0)):.1f}%"></span></div></div>'
        for r in cited_topic_rows
    ) or '<div class="empty">No OpenAlex topic metadata was available for the seed paper’s directly cited references.</div>'

    downstream = report.get('downstream_topics_questions', {}) or {}
    downstream_topics = downstream.get('topics') or []
    downstream_papers = downstream.get('papers') or []
    downstream_topic_html = ''.join(
        f'<div class="topic-row"><div><b>{escape(str(r.get("topic") or "Untitled"))}</b>'
        f'<span class="hint">{int(r.get("papers") or 0)} paper(s) · {float(r.get("share_pct") or 0):.1f}%</span></div>'
        f'<div class="topic-bar"><span style="width:{min(100.0, float(r.get("share_pct") or 0)):.1f}%"></span></div></div>'
        for r in downstream_topics
    ) or '<div class="empty">No downstream topic metadata was available.</div>'
    downstream_paper_html = ''.join(
        f'<article class="downstream-item clickable" data-doi="{escape(str(r.get("doi") or ""))}">'
        f'<div class="paper-year">Generation {int(r.get("distance") or 0)} · {escape(str(r.get("year") or "—"))}</div>'
        f'<div class="paper-title">{escape(str(r.get("title") or "Untitled"))}</div>'
        f'<div class="paper-meta">{escape(" · ".join(r.get("topics") or []) or "No topic metadata")}</div>'
        + (f'<div class="down-label">Question / objective identifiable in abstract</div><div class="down-text">{escape(str(r.get("question")))}</div>' if r.get('question') else '<div class="down-label">Question / objective</div><div class="down-text muted">Not explicit in the retrieved abstract.</div>')
        + (f'<div class="down-label">Reported answer / finding</div><div class="down-text">{escape(str(r.get("answer")))}</div>' if r.get('answer') else '<div class="down-label">Reported answer / finding</div><div class="down-text muted">Not explicit in the retrieved abstract.</div>')
        + '</article>'
        for r in downstream_papers
    ) or '<div class="empty">No downstream citing papers were retrieved within the requested scope.</div>'

    ai = report.get('ai_evolution', {}) or {}
    def ai_html(key: str, empty: str) -> str:
        rec = ai.get(key) or {}
        structured = rec.get('structured') or {}
        items = structured.get('items') if isinstance(structured, dict) else None
        if items:
            blocks = []
            for i, item in enumerate(items, 1):
                evidence = ''.join(f'<li>{escape(str(x))}</li>' for x in (item.get('evidence') or []))
                blocks.append(
                    f'<article class="ai-item"><div class="ai-item-title"><span class="ai-num">{i}</span><h4>{escape(str(item.get("title") or "Untitled"))}</h4></div>'
                    f'<div class="ai-field"><b>Observation</b><p>{escape(str(item.get("observation") or ""))}</p></div>'
                    f'<div class="ai-field"><b>Inference</b><p>{escape(str(item.get("inference") or ""))}</p></div>'
                    f'<div class="ai-field"><b>Evidence</b><ul>{evidence or "<li>No specific evidence listed.</li>"}</ul></div>'
                    f'<div class="ai-field"><b>Uncertainty</b><p>{escape(str(item.get("uncertainty") or ""))}</p></div>'
                    f'<div class="ai-field"><b>Research value</b><p>{escape(str(item.get("research_value") or ""))}</p></div></article>'
                )
            overall = escape(str(structured.get('overall_uncertainty') or ''))
            return ''.join(blocks) + (f'<div class="ai-uncertainty"><b>Overall uncertainty:</b> {overall}</div>' if overall else '')
        return f'<div class="empty">{escape(empty)}</div>'

    ai_model = next((str((ai.get(k) or {}).get('model') or '') for k in ('future_directions', 'research_questions') if (ai.get(k) or {}).get('model')), 'Local model')
    ai_cards = [
        ('future_directions', 'Possible future directions', 'Evidence-grounded possibilities rather than predictions'),
        ('research_questions', 'Open research questions', 'Questions inferred from unresolved upstream patterns and the seed'),
    ]
    ai_card_html = ''.join(
        f'<div class="ai-card"><div class="ai-card-head"><div><h3>{escape(title)}</h3><div class="hint">{escape(sub)}</div></div><span class="ai-badge">{escape(ai_model)}</span></div>'
        f'<div class="ai-body">{ai_html(key, "The requested AI analysis was not generated.")}</div></div>'
        for key, title, sub in ai_cards
    )

    explorer_rows = []
    if not timeline.empty:
        t = timeline.copy()
        t['year_sort'] = pd.to_numeric(t.get('year'), errors='coerce')
        t = t.sort_values(['year_sort', 'title'], na_position='last')
        for r in t.to_dict(orient='records'):
            doi = str(r.get('doi') or '')
            title = str(r.get('title') or 'Untitled')
            year = '' if pd.isna(r.get('year')) else str(int(r.get('year')))
            role = str(r.get('role') or 'unknown')
            topic = str(r.get('primary_topic') or 'Unknown')
            position = str(r.get('position') or 'other')
            cites_known = pd.notna(r.get('cited_by_count'))
            cites = int(r.get('cited_by_count')) if cites_known else 0
            cites_label = f"{cites:,} citations" if cites_known else "citation count unavailable"
            explorer_rows.append(
                f'<div class="explorer-paper clickable" data-doi="{escape(doi)}" data-year="{escape(year)}" data-role="{escape(role)}" data-topic="{escape(topic)}" data-position="{escape(position)}" data-cites="{cites}" data-cites-known="{1 if cites_known else 0}" data-search="{escape((title + " " + doi).lower())}">'
                f'<div class="paper-main"><div class="paper-year">{escape(year or "Undated")} · {escape(position)} · {escape(role)}</div><div class="paper-title">{escape(title)}</div><div class="paper-meta">{escape(topic)} · {escape(cites_label)}</div></div><span class="open">↗</span></div>'
            )
    explorer_html = ''.join(explorer_rows) or '<div class="empty">No paper records are available for the explorer.</div>'

    roles = sorted(set(str(x) for x in timeline.get('role', pd.Series(dtype=str)).dropna().tolist())) if not timeline.empty else []
    topics = sorted(set(str(x) for x in timeline.get('primary_topic', pd.Series(dtype=str)).dropna().tolist() if str(x) and str(x) != 'Unknown')) if not timeline.empty else []
    positions = [p for p in ('upstream', 'seed', 'downstream', 'other') if not timeline.empty and p in set(timeline.get('position', pd.Series(dtype=str)).astype(str))]
    role_options = ''.join(f'<option value="{escape(x)}">{escape(x)}</option>' for x in roles)
    topic_options = ''.join(f'<option value="{escape(x)}">{escape(x)}</option>' for x in topics)
    position_options = ''.join(f'<option value="{escape(x)}">{escape(x.capitalize())}</option>' for x in positions)
    years = [int(x) for x in pd.to_numeric(timeline.get('year', pd.Series(dtype=float)), errors='coerce').dropna().tolist()] if not timeline.empty else []
    min_year = min(years) if years else ''
    max_year = max(years) if years else ''

    reading = [{'label': 'Seed', 'doi': seed.get('doi'), 'title': seed.get('title') or seed.get('doi'), 'year': seed.get('year')}]
    for r in overview['key_papers'][:5]:
        if normalize_doi(r.get('doi', '')) != normalize_doi(seed.get('doi', '')):
            reading.append({'label': 'Transition candidate', 'doi': r['doi'], 'title': r['title'], 'year': r['year']})
    reading_html = ''.join(
        f'<div class="reading-row clickable" data-doi="{escape(str(r.get("doi") or ""))}"><span class="reading-num">{i}</span><span><b>{escape(str(r["label"]))}</b> · {escape(str(r.get("year") or "—"))}<br>{escape(_short_title(str(r.get("title") or "Untitled"), 105))}</span><span class="open">↗</span></div>'
        for i, r in enumerate(reading, 1) if r.get('doi')
    )
    story_html = ''.join(f'<li>{escape(str(x))}</li>' for x in overview['story'])
    eras = report.get('eras', []) or []
    era_html = ''.join(
        f'<div class="era"><b>{escape(str(e.get("start_year") or "—"))}–{escape(str(e.get("end_year") or "—"))}</b><span>{escape(", ".join(str(c.get("concept") or "") for c in e.get("top_concepts", [])[:4]))}</span></div>'
        for e in eras
    ) or '<div class="empty">No distinct topic eras detected.</div>'

    authors = safe_df(report.get('top_authors', []))
    funders = safe_df((report.get('top_funders', {}) or {}).get('funders', []))
    roles_df = safe_df(report.get('paper_roles', []))
    inst = safe_df(report.get('institutional_mobility', []))
    details = (
        '<div class="detail-grid">'
        '<div><h3>Leading authors</h3>' + (''.join(f'<div class="mini-row"><span>{escape(str(r.get("author") or ""))}</span><b>{int(r.get("count", 0))}</b></div>' for _, r in authors.head(8).iterrows()) or '<div class="empty">No author metadata.</div>') + '</div>'
        '<div><h3>Leading institutions</h3>' + (''.join(f'<div class="mini-row"><span>{escape(str(r.get("institution") or ""))}</span><b>{int(r.get("count", 0))}</b></div>' for _, r in inst.head(8).iterrows()) or '<div class="empty">No institution metadata.</div>') + '</div>'
        f'<div><h3>Funding</h3><div class="detail-note">Coverage: {float((report.get("top_funders", {}) or {}).get("coverage_pct", 0)):.1f}%</div>' + (''.join(f'<div class="mini-row"><span>{escape(str(r.get("funder") or ""))}</span><b>{int(r.get("count", 0))}</b></div>' for _, r in funders.head(8).iterrows()) or '<div class="empty">No funder metadata.</div>') + '</div>'
        '<div><h3>Paper roles</h3>' + (''.join(f'<div class="mini-row"><span>{escape(str(r.get("role") or ""))}</span><b>{int(r.get("count", 0))}</b></div>' for _, r in roles_df.sort_values("count", ascending=False).iterrows()) if not roles_df.empty else '<div class="empty">No role data.</div>') + '</div>'
        '</div>'
    )

    qc = report.get('citation_quality') or report.get('graph_quality') or {}
    category_html = ''.join(
        f'<div class="mini-row"><span>{escape(str(k))}</span><b>{int(v)}</b></div>'
        for k, v in sorted((qc.get('relationship_categories') or {}).items(), key=lambda x: (-x[1], x[0]))
    ) or '<div class="empty">No relationship counts available.</div>'

    coverage = qc.get('metadata_coverage') or {}
    coverage_total = int(coverage.get('papers_total') or 0)
    def coverage_label(key: str) -> str:
        value = int(coverage.get(key) or 0)
        pct = (100.0 * value / coverage_total) if coverage_total else 0.0
        return f"{value:,} ({pct:.1f}%)"

    export_labels = [('csv', 'CSV'), ('excel', 'Excel'), ('bibtex', 'BibTeX'), ('ris', 'RIS'), ('markdown', 'Research brief')]
    export_paths = report.get('exports', {}) or {}
    export_links = []
    for key, label in export_labels:
        raw = export_paths.get(key)
        if raw:
            try:
                href = os.path.relpath(str(raw), str(out_html.resolve().parent)).replace(os.sep, '/')
            except Exception:
                href = str(raw).replace('\\', '/')
        else:
            defaults = {'csv':'litev_papers.csv','excel':'litev_papers.xlsx','bibtex':'litev_references.bib','ris':'litev_references.ris','markdown':'litev_research_brief.md'}
            href = f"litev_exports/{defaults[key]}"
        export_links.append(f'<a class="export" href="{escape(href)}">{label}</a>')
    export_html = ''.join(export_links)

    requested = report.get('requested_features', {}) or {}
    position_counts = Counter(str(x) for x in timeline.get('position', pd.Series(dtype=str)).fillna('other').tolist()) if not timeline.empty else Counter()

    sections = [('story', 'Story')]
    if upstream_hops > 0:
        sections.append(('seed-cited-topics', 'Seed references'))
    sections.extend([('papers', 'Papers'), ('evidence', 'Evidence'), ('topics', 'Topics')])
    if downstream_hops > 0:
        sections.append(('downstream', 'Downstream'))
    if findings or requested.get('paper_findings_ai'):
        sections.append(('findings', 'Findings'))
    if ai or requested.get('future_ai'):
        sections.append(('ai', 'Future'))
    sections.extend([('methods', 'Methods'), ('exports', 'Exports')])
    nav = ''.join(f'<a href="#{sid}">{escape(label)}</a>' for sid, label in sections)

    seed_topics_section = ''
    if upstream_hops > 0:
        seed_topics_section = (
            '<section class="section" id="seed-cited-topics"><div class="section-head"><div><div class="kicker">SEED CONTEXT</div><h2>What topics did the seed paper cite?</h2></div><span class="hint">Direct references · OpenAlex metadata</span></div>'
            f'<div class="card"><div class="detail-note">{escape(str(cited_topics.get("method") or ""))}</div>{cited_topic_html}</div></section>'
        )

    downstream_section = ''
    if downstream_hops > 0:
        downstream_section = (
            '<section class="section" id="downstream"><div class="section-head"><div><div class="kicker">DOWNSTREAM DEVELOPMENT</div><h2>What did later citing papers tackle?</h2></div>'
            '<span class="hint">Topics and explicit question/finding sentences from retrieved abstracts</span></div><div class="grid2">'
            f'<div class="card"><h3>Topics tackled</h3><div class="detail-note">{escape(str(downstream.get("method") or ""))}</div>{downstream_topic_html}</div>'
            f'<div class="card"><h3>Questions and reported findings</h3>{downstream_paper_html}</div></div></section>'
        )

    findings_section = ''
    if findings or requested.get('paper_findings_ai'):
        findings_section = (
            '<section class="section" id="findings"><div class="section-head"><div><div class="kicker">LOCAL AI · PAPER LEVEL</div><h2>What did selected papers find?</h2></div>'
            '<span class="hint">Findings-only summaries grounded in retrieved abstracts</span></div><div class="ai-grid">'
            + (findings_cards or '<div class="card empty">No findings summaries were generated. Check Ollama/model availability and abstract coverage.</div>')
            + '</div></section>'
        )

    ai_section = ''
    if ai or requested.get('future_ai'):
        ai_section = (
            '<section class="section" id="ai"><div class="section-head"><div><div class="kicker">LOCAL AI · UPSTREAM CONTEXT ONLY</div><h2>How could the research develop?</h2></div>'
            '<span class="hint">Future directions and open questions are hypotheses, not predictions</span></div>'
            f'<div class="ai-grid">{ai_card_html}</div></section>'
        )

    html = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{APP_NAME} {APP_VERSION} — {escape(str(overview['seed_title']))}</title><style>
:root{{--ink:#142033;--muted:#65748b;--line:#e2e8f0;--soft:#f7f9fc;--accent:#3858d6;--accent2:#6d4aff;--shadow:0 9px 30px rgba(15,23,42,.06)}}*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:#f3f6fa;color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}}.container{{max-width:1240px;margin:auto;padding:0 22px 60px}}.navlite{{position:sticky;top:0;z-index:20;margin:0 -22px 20px;padding:11px 22px;background:rgba(243,246,250,.94);backdrop-filter:blur(10px);border-bottom:1px solid rgba(226,232,240,.9);white-space:nowrap;overflow-x:auto}}.navlite a{{display:inline-block;margin-right:15px;color:#3f51b5;text-decoration:none;font-size:12px;font-weight:750}}.hero{{background:radial-gradient(circle at 90% 10%,rgba(109,74,255,.45),transparent 35%),linear-gradient(135deg,#111c33,#263a78);color:white;border-radius:0 0 26px 26px;padding:38px 38px 32px;box-shadow:0 18px 48px rgba(17,28,51,.16)}}.eyebrow,.kicker{{font-size:11px;text-transform:uppercase;letter-spacing:.13em;font-weight:800}}.eyebrow{{opacity:.72}}.kicker{{color:#5969c9;margin-bottom:4px}}h1{{font-size:35px;line-height:1.12;margin:9px 0 10px;max-width:950px}}.seed{{opacity:.82;font-size:13px;word-break:break-all}}.scope-pills{{display:flex;gap:8px;flex-wrap:wrap;margin-top:17px}}.pill{{padding:7px 10px;border:1px solid rgba(255,255,255,.16);border-radius:999px;background:rgba(255,255,255,.08);font-size:11px}}.stats{{display:grid;grid-template-columns:repeat(5,1fr);gap:11px;margin-top:25px}}.stat{{background:rgba(255,255,255,.09);border:1px solid rgba(255,255,255,.13);border-radius:14px;padding:14px}}.stat b{{display:block;font-size:23px}}.stat span{{font-size:10px;opacity:.72;text-transform:uppercase;letter-spacing:.07em}}.section{{margin-top:28px;scroll-margin-top:58px}}.section-head{{display:flex;justify-content:space-between;align-items:end;gap:20px;margin:0 2px 12px}}h2{{font-size:23px;line-height:1.2;margin:0}}h3{{font-size:14px;margin:0 0 10px}}.hint,.muted{{font-size:12px;color:var(--muted)}}.card,.ai-card{{background:white;border:1px solid var(--line);border-radius:18px;padding:20px;box-shadow:var(--shadow)}}.retrieval-warning{{margin-top:16px;padding:14px 16px;border:1px solid #f3d5a4;border-radius:14px;background:#fff8eb;color:#6f4a12;font-size:12px;line-height:1.5}}.retrieval-warning b{{display:block;font-size:13px;margin-bottom:3px}}.retrieval-warning ul{{margin:7px 0 0;padding-left:18px}}.story-grid,.key-grid,.grid2{{display:grid;grid-template-columns:1.2fr .8fr;gap:16px}}.grid2{{grid-template-columns:1fr 1fr}}.story{{font-size:14px;line-height:1.72}}.story ul{{margin:9px 0;padding-left:20px}}.era-list{{display:flex;flex-direction:column;gap:8px;margin-top:8px}}.era{{display:flex;justify-content:space-between;gap:14px;padding:11px 12px;background:var(--soft);border-radius:10px;font-size:12px}}.era span{{color:var(--muted);text-align:right}}.chart{{min-width:0;overflow:hidden}}.paper-card,.reading-row,.explorer-paper{{display:flex;gap:13px;align-items:flex-start;padding:13px;border-bottom:1px solid var(--line)}}.clickable{{cursor:pointer}}.clickable:hover{{background:#f8faff}}.paper-card:last-child,.reading-row:last-child,.explorer-paper:last-child{{border-bottom:0}}.paper-rank,.reading-num{{width:28px;height:28px;border-radius:9px;background:#eef2ff;color:#4338ca;display:grid;place-items:center;font-weight:800;font-size:12px;flex:0 0 auto}}.paper-year{{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.055em}}.paper-title{{font-weight:720;line-height:1.38;margin:3px 0}}.paper-meta{{font-size:11px;color:var(--muted)}}.reading-row{{align-items:center}}.reading-row span:nth-child(2){{font-size:13px;line-height:1.45;flex:1}}.open{{color:var(--accent);font-size:18px;margin-left:auto}}.topic-row{{padding:11px 0;border-bottom:1px solid var(--line)}}.topic-row>div:first-child{{display:flex;justify-content:space-between;gap:14px;align-items:center}}.topic-bar{{height:6px;background:#edf1f7;border-radius:99px;overflow:hidden;margin-top:7px}}.topic-bar span{{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:99px}}.detail-note,.empty{{font-size:12px;color:var(--muted);padding:7px 0;line-height:1.55}}.filters{{display:grid;grid-template-columns:1.7fr repeat(6,1fr);gap:9px;align-items:end;padding:16px;border-bottom:1px solid var(--line);background:#fbfcfe}}.filters label{{font-size:10px;font-weight:800;color:var(--muted);display:flex;flex-direction:column;gap:5px;text-transform:uppercase;letter-spacing:.04em}}.filters input,.filters select{{width:100%;min-width:0;padding:9px 10px;border:1px solid #d9e0ea;border-radius:9px;background:white;font:inherit;font-size:12px;color:var(--ink)}}.filter-actions{{display:flex;gap:8px;align-items:center;padding:0 16px 12px;background:#fbfcfe}}.filter-btn{{padding:8px 12px;border:1px solid #d7deea;border-radius:9px;background:white;color:var(--ink);font-weight:700;cursor:pointer}}.filter-count{{font-size:12px;color:var(--muted)}}.paper-explorer{{background:white;border:1px solid var(--line);border-radius:18px;overflow:hidden;box-shadow:var(--shadow)}}.explorer-scroll{{max-height:520px;overflow:auto}}.explorer-paper{{align-items:center}}.paper-main{{min-width:0;flex:1}}.hidden-filter{{display:none!important}}.ai-grid{{display:grid;grid-template-columns:1fr;gap:14px}}.ai-card-head{{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}}.ai-badge{{font-size:10px;padding:5px 8px;border-radius:999px;background:#eef2ff;color:#4338ca;white-space:nowrap}}.ai-body{{margin-top:13px;font-size:13px;line-height:1.65}}.why-card{{background:white;border:1px solid var(--line);border-radius:16px;padding:18px;box-shadow:var(--shadow)}}.why-text{{margin-top:10px;font-size:13px;line-height:1.62;white-space:pre-wrap}}.ai-item{{border:1px solid var(--line);border-radius:14px;padding:15px;margin:10px 0;background:var(--soft)}}.ai-item-title{{display:flex;gap:10px;align-items:center}}.ai-item-title h4{{margin:0;font-size:15px}}.ai-num{{width:25px;height:25px;border-radius:8px;background:#eef2ff;color:#4338ca;display:grid;place-items:center;font-weight:800;font-size:11px}}.ai-field{{margin-top:10px}}.ai-field b,.down-label{{font-size:10px;text-transform:uppercase;letter-spacing:.055em;color:var(--muted)}}.ai-field p,.ai-field ul{{margin:4px 0;font-size:12.5px;line-height:1.55}}.ai-field ul{{padding-left:18px}}.ai-uncertainty{{margin-top:12px;padding:10px 12px;background:#fff8ed;border:1px solid #fde8c7;border-radius:9px;font-size:12px}}.downstream-item{{padding:13px 0;border-bottom:1px solid var(--line)}}.downstream-item:last-child{{border-bottom:0}}.down-text{{font-size:12.5px;line-height:1.55;margin:3px 0 8px}}.method-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}.method-box{{padding:14px;background:var(--soft);border-radius:12px;font-size:12px;line-height:1.55}}.quality-stat{{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--line);font-size:12px}}.details{{border:1px solid var(--line);border-radius:18px;background:white;overflow:hidden;box-shadow:var(--shadow)}}summary{{cursor:pointer;padding:18px 20px;font-weight:750;list-style:none}}summary::-webkit-details-marker{{display:none}}.details-body{{padding:0 20px 20px;border-top:1px solid var(--line)}}.detail-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:22px;padding-top:18px}}.mini-row{{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-bottom:1px solid #edf0f4;font-size:12px}}.export{{display:inline-block;margin:4px 7px 4px 0;padding:9px 12px;border:1px solid var(--line);border-radius:10px;color:var(--accent);text-decoration:none;background:white;font-size:12px;font-weight:750}}.export:hover{{background:var(--soft)}}.footer{{margin-top:28px;color:var(--muted);font-size:11px;text-align:center}}@media(max-width:980px){{.stats{{grid-template-columns:repeat(2,1fr)}}.story-grid,.grid2,.key-grid,.method-grid,.detail-grid{{grid-template-columns:1fr}}.filters{{grid-template-columns:repeat(2,1fr)}}h1{{font-size:28px}}.hero{{padding:30px 23px}}}}@media(max-width:580px){{.filters{{grid-template-columns:1fr}}.container{{padding-left:12px;padding-right:12px}}.navlite{{margin-left:-12px;margin-right:-12px;padding-left:12px}}}}
</style></head><body><div class="container"><nav class="navlite">{nav}</nav><header class="hero"><div class="eyebrow">{APP_NAME} {APP_VERSION} · literature evolution analysis</div><h1>{escape(str(overview['seed_title']))}</h1><div class="seed">{escape(str(overview['seed_doi'] or ''))}</div><div class="scope-pills"><span class="pill">Upstream hops: {upstream_hops}</span><span class="pill">Downstream hops: {downstream_hops}</span><span class="pill">Local AI: {'enabled' if requested.get('paper_findings_ai') or requested.get('future_ai') else 'off'}</span></div><div class="stats"><div class="stat"><b>{overview['paper_count']:,}</b><span>papers analyzed</span></div><div class="stat"><b>{position_counts.get('upstream',0):,}</b><span>upstream papers</span></div><div class="stat"><b>{position_counts.get('downstream',0):,}</b><span>downstream papers</span></div><div class="stat"><b>{overview['first_year'] or '—'}–{overview['last_year'] or '—'}</b><span>time span</span></div><div class="stat"><b>{overview['transition_count']}</b><span>transition candidates</span></div></div></header>{warning_html}
<section class="section" id="story"><div class="section-head"><div><div class="kicker">OVERVIEW</div><h2>What happened?</h2></div><span class="hint">A compact, descriptive synthesis of the retrieved evidence</span></div><div class="story-grid"><div class="card story"><b>Research story</b><ul>{story_html}</ul></div><div class="card"><b>Detected topic eras</b><div class="era-list">{era_html}</div></div></div></section>
{seed_topics_section}
<section class="section" id="papers"><div class="section-head"><div><div class="kicker">PAPER-LEVEL VIEW</div><h2>Potential transition papers</h2></div><span class="hint">Exploratory ranking — not a measure of scientific importance</span></div><div class="key-grid"><div class="card">{key_cards_html}</div><div class="card"><h3>Contextual reading list</h3><div class="hint">Seed followed by selected transition candidates.</div>{reading_html}</div></div><div style="height:16px"></div><div class="paper-explorer"><div class="filters"><label>Search<input id="fsearch" type="search" placeholder="Title or DOI"></label><label>From year<input id="fmin" type="number" placeholder="{min_year}"></label><label>To year<input id="fmax" type="number" placeholder="{max_year}"></label><label>Position<select id="fposition"><option value="">All</option>{position_options}</select></label><label>Role<select id="frole"><option value="">All</option>{role_options}</select></label><label>Topic<select id="ftopic"><option value="">All</option>{topic_options}</select></label><label>Min citations<input id="fcites" type="number" min="0" value="0"></label></div><div class="filter-actions"><button class="filter-btn" type="button" onclick="resetFilters()">Reset filters</button><span class="filter-count" id="filterCount"></span></div><div class="explorer-scroll">{explorer_html}</div></div></section>
<section class="section" id="evidence"><div class="section-head"><div><div class="kicker">QUANTITATIVE SIGNALS</div><h2>Evidence over time</h2></div><span class="hint">Similarity is measured directly against the seed; point size reflects cited-by count; click a point to open its DOI</span></div><div class="card chart">{fig_html(figs[0][1],'story_timeline')}</div><div style="height:16px"></div><div class="card chart">{fig_html(figs[1][1],'impact_plot')}</div></section>
<section class="section" id="topics"><div class="section-head"><div><div class="kicker">TOPIC DEVELOPMENT</div><h2>How did the topics change?</h2></div><span class="hint">OpenAlex Topics, with Concepts as fallback</span></div><div class="card chart">{fig_html(figs[2][1],'concept_plot')}</div><div style="height:16px"></div><div class="card chart">{fig_html(figs[3][1],'transition_plot')}</div></section>
{downstream_section}{findings_section}{ai_section}
<section class="section" id="methods"><div class="section-head"><div><div class="kicker">METHODS & QUALITY CONTROL</div><h2>How should these results be interpreted?</h2></div><span class="hint">Citation connection is evidence of citation, not proof of intellectual influence</span></div><div class="method-grid"><div class="card"><h3>Analysis semantics</h3><div class="method-box"><b>Upstream</b> follows references from the seed into earlier/reference-side literature. <b>Downstream</b> follows later works returned by OpenAlex as citing the seed or its downstream branch.</div><div class="method-box" style="margin-top:10px">Semantic similarity, topic overlap, structural bridge position and cited-by counts are analytical signals. They are kept separate from the observed citation fact and should not be read as causal evidence.</div><div class="method-box" style="margin-top:10px">No citation-network visualization is displayed. Citation relationships are used internally to define scope, distances, and exploratory metrics.</div></div><div class="card"><h3>Citation-data quality</h3><div class="quality-stat"><span>Observed citation links</span><b>{int(qc.get('citation_edges') or 0):,}</b></div><div class="quality-stat"><span>Same-year links</span><b>{int(qc.get('same_year_edges') or 0):,}</b></div><div class="quality-stat"><span>Date-inconsistent links</span><b>{int(qc.get('date_inconsistent_edges') or 0):,}</b></div><div class="quality-stat"><span>OpenAlex metadata coverage</span><b>{coverage_label('openalex')}</b></div><div class="quality-stat"><span>Topic metadata coverage</span><b>{coverage_label('topic')}</b></div><div class="quality-stat"><span>Abstract coverage</span><b>{coverage_label('abstract')}</b></div><div style="margin-top:10px">{category_html}</div></div></div></section>
<section class="section" id="exports"><div class="section-head"><div><div class="kicker">REPRODUCIBILITY</div><h2>Researcher exports</h2></div><span class="hint">Reusable data and references from this run</span></div><div class="card">{export_html}</div></section>
<section class="section"><details class="details"><summary>Advanced metadata — authors, institutions, funding, roles and identifiers</summary><div class="details-body"><h3 style="margin-top:18px">Unified literature identifiers</h3><div class="detail-note">Each work retains its DOI plus available external identifiers. The analysis itself remains DOI-canonical.</div>{details}</div></details></section><div class="footer">{APP_NAME} {APP_VERSION} · Generated {escape(str(summary.get('generated_at','')))} · Crossref + OpenAlex · Analytical signals are descriptive/exploratory, not measures of scientific truth.</div></div><script>
function applyFilters(){{
  const search=(document.getElementById('fsearch').value||'').trim().toLowerCase();
  const minRaw=document.getElementById('fmin').value, maxRaw=document.getElementById('fmax').value;
  const min=minRaw===''?null:parseInt(minRaw), max=maxRaw===''?null:parseInt(maxRaw);
  const position=document.getElementById('fposition').value, role=document.getElementById('frole').value, topic=document.getElementById('ftopic').value;
  const cites=parseInt(document.getElementById('fcites').value||'0'); let visible=0;
  document.querySelectorAll('.explorer-paper').forEach(el=>{{
    const y=el.dataset.year===''?null:parseInt(el.dataset.year), c=parseInt(el.dataset.cites||'0');
    const yearOk=(y===null)?(min===null&&max===null):((min===null||y>=min)&&(max===null||y<=max));
    const ok=yearOk&&(!position||el.dataset.position===position)&&(!role||el.dataset.role===role)&&(!topic||el.dataset.topic===topic)&&c>=cites&&(!search||(el.dataset.search||'').includes(search));
    el.classList.toggle('hidden-filter',!ok); if(ok) visible++;
  }});
  const count=document.getElementById('filterCount'); if(count) count.textContent=visible+' of '+document.querySelectorAll('.explorer-paper').length+' paper records shown.';
}}
function resetFilters(){{document.getElementById('fsearch').value='';document.getElementById('fmin').value='';document.getElementById('fmax').value='';document.getElementById('fposition').value='';document.getElementById('frole').value='';document.getElementById('ftopic').value='';document.getElementById('fcites').value='0';applyFilters();}}
function openDoi(doi){{if(doi)window.open('https://doi.org/'+doi,'_blank','noopener')}}
document.querySelectorAll('.clickable[data-doi]').forEach(el=>el.addEventListener('click',()=>openDoi(el.dataset.doi)));
['fsearch','fmin','fmax','fposition','frole','ftopic','fcites'].forEach(id=>{{const el=document.getElementById(id);if(el)el.addEventListener(id==='fsearch'?'input':'change',applyFilters);}});
['story_timeline','impact_plot','transition_plot'].forEach(id=>{{const p=document.getElementById(id);if(!p||!p.on)return;p.on('plotly_click',d=>{{const x=d.points&&d.points[0];if(!x)return;const cd=x.customdata;openDoi(Array.isArray(cd)?cd[0]:cd)}});}});
applyFilters();
</script></body></html>'''
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(html, encoding='utf-8')
    return out_html.resolve()


async def main() -> None:
    global MAILTO, OPENALEX_API_KEY
    parser = argparse.ArgumentParser(description='Build a literature-evolution report and render a focused research dashboard.')
    parser.add_argument('--version', action='version', version=f'{APP_NAME} {APP_VERSION}')
    parser.add_argument('seed', nargs='?', default='10.1038/NCHEM.1111', help='Seed literature identifier: DOI, ISBN, PMID, PMCID, arXiv ID, OpenAlex work ID, or bibliographic text')
    parser.add_argument('--max-hops', type=int, default=2, help='Upstream/reference-side hops from the seed (0-6 recommended).')
    parser.add_argument('--per-node-cap', type=int, default=70, help='Maximum DOI references followed per upstream paper.')
    parser.add_argument('--max-nodes', type=int, default=1000, help='Maximum total papers retained across both directions.')
    parser.add_argument('--forward-hops', type=int, default=1, help='Downstream citing-paper hops from the seed.')
    parser.add_argument('--forward-cap', type=int, default=20, help='Maximum citing papers requested per downstream paper and hop.')
    parser.add_argument('--report', default='litev_report.json')
    parser.add_argument('--html', default='litev_dashboard.html')
    parser.add_argument('--export-dir', default='litev_exports')
    parser.add_argument('--mailto', default=MAILTO, help='Optional contact email sent to Crossref requests.')
    parser.add_argument('--openalex-api-key', default=OPENALEX_API_KEY, help='Optional OpenAlex API key. You can also set OPENALEX_API_KEY in the environment.')
    parser.add_argument('--no-html', action='store_true')
    # Retained only so older command lines do not fail; scientific analysis no
    # longer filters the observed citation relationships using these values.
    parser.add_argument('--influence-top-k', type=int, default=25, help=argparse.SUPPRESS)
    parser.add_argument('--influence-min-score', type=float, default=0.0, help=argparse.SUPPRESS)
    parser.add_argument('--paper-findings-ai', '--why-matters', dest='paper_findings_ai', action='store_true', help='Generate findings-only summaries for selected papers using a local Ollama model.')
    parser.add_argument('--ai-model', '--why-matters-model', dest='ai_model', default='gemma3', help='Local Ollama model name (default: gemma3).')
    parser.add_argument('--ollama-url', '--why-matters-url', dest='ollama_url', default='http://127.0.0.1:11434', help='Ollama base URL.')
    parser.add_argument('--paper-findings-max', '--why-matters-max', dest='paper_findings_max', type=int, default=8, help='Maximum number of papers to summarize.')
    parser.add_argument('--paper-findings-cache', '--why-matters-cache', dest='paper_findings_cache', default='litev_paper_findings_cache.json', help='Cache file for paper findings summaries.')
    parser.add_argument('--future-ai', '--ai-evolution', dest='future_ai', action='store_true', help='Generate future directions and open research questions from upstream literature; requires upstream hops > 0.')
    parser.add_argument('--future-ai-cache', '--ai-evolution-cache', dest='future_ai_cache', default='litev_future_ai_cache.json', help='Cache file for structured future analysis.')
    parser.add_argument('--future-ai-parallel', '--ai-evolution-parallel', dest='future_ai_parallel', type=int, default=2, help='Fallback parallelism for structured local AI requests.')
    args = parser.parse_args()

    MAILTO = str(args.mailto or '').strip()
    OPENALEX_API_KEY = str(args.openalex_api_key or '').strip()
    if args.max_hops < 0 or args.forward_hops < 0:
        parser.error('Hop counts must be zero or greater.')
    if args.per_node_cap < 1 or args.forward_cap < 1:
        parser.error('Per-paper caps must be at least 1.')
    if args.max_nodes < 10:
        parser.error('--max-nodes must be at least 10.')

    print(f'\n{APP_NAME} {APP_VERSION}')
    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent()},
        verify=certifi.where(), follow_redirects=True, timeout=30.0, http2=False,
    ) as resolver_client:
        try:
            resolved_seed = await resolve_literature_identifier(resolver_client, args.seed)
        except httpx.RequestError as e:
            print(f"[Identifier resolution FAIL] {args.seed!r}: {type(e).__name__}: {e}")
            raise RuntimeError(
                "Could not reach the literature metadata services while resolving the seed. "
                "Check your internet connection, VPN/proxy/firewall, then retry."
            ) from e
        except Exception as e:
            print(f"[Identifier resolution FAIL] {args.seed!r}: {type(e).__name__}: {e}")
            raise

    print(f'Input identifier: {args.seed}')
    print(f'Resolved DOI: {resolved_seed}')
    print('Building literature-evolution dataset and enriching with OpenAlex…')
    G, cores, oa_cache = await build_prior_graph(
        resolved_seed,
        max_hops=args.max_hops,
        per_node_cap=args.per_node_cap,
        max_nodes=args.max_nodes,
    )
    if resolved_seed not in cores:
        raise RuntimeError(
            f"No metadata could be retrieved for the resolved DOI {resolved_seed!r}. "
            "Crossref and OpenAlex may be unreachable, the DOI may not be indexed, or a proxy/firewall may be blocking HTTPS requests. "
            "Check the run log for the preceding Crossref/OpenAlex error."
        )

    if args.forward_hops > 0 and G.number_of_nodes() < args.max_nodes:
        print(f'Expanding downstream citations: hops={args.forward_hops}, cap={args.forward_cap}…')
        await expand_forward_graph(
            G, cores, oa_cache, resolved_seed,
            args.forward_hops, args.forward_cap,
            max_nodes=args.max_nodes,
        )

    scope = {
        'upstream_hops': int(args.max_hops),
        'downstream_hops': int(args.forward_hops),
        'upstream_reference_cap': int(args.per_node_cap),
        'downstream_citing_cap': int(args.forward_cap),
        'max_papers': int(args.max_nodes),
    }
    report = build_evolution_report(
        resolved_seed, G, cores, oa_cache,
        influence_top_k=args.influence_top_k,
        influence_min_score=args.influence_min_score,
        analysis_scope=scope,
    )
    report['seed_input'] = args.seed
    report['requested_features'] = {
        'paper_findings_ai': bool(args.paper_findings_ai),
        'future_ai': bool(args.future_ai and args.max_hops > 0),
    }

    if args.paper_findings_ai:
        print(f'[Findings AI] generating up to {args.paper_findings_max} findings summaries with local Ollama model {args.ai_model}…')
        report['paper_findings_ai'] = await generate_why_matters(
            report, cores, oa_cache, G,
            model=args.ai_model,
            base_url=args.ollama_url,
            max_papers=args.paper_findings_max,
            cache_path=args.paper_findings_cache,
        )

    if args.future_ai:
        if args.max_hops > 0:
            try:
                upstream_count = len(nx.ancestors(G, normalize_doi(resolved_seed)))
            except Exception:
                upstream_count = 0
            if upstream_count > 0:
                print(f'[Future AI] generating future directions and open research questions from {upstream_count} retrieved upstream paper(s) with local Ollama model {args.ai_model}…')
                report['ai_evolution'] = await generate_evolution_ai(
                    report, cores, G,
                    model=args.ai_model,
                    base_url=args.ollama_url,
                    cache_path=args.future_ai_cache,
                    parallel=args.future_ai_parallel,
                )
            else:
                print('[Future AI] skipped because no upstream reference papers were retrieved.')
        else:
            print('[Future AI] skipped because upstream hops are 0.')

    export_paths = export_litev(report, cores, args.export_dir)
    report['exports'] = {k: str(v) for k, v in export_paths.items()}
    write_report_json(report, args.report)
    print(f"[Export] wrote {len(export_paths)} files to {Path(args.export_dir).resolve()}")

    o = _build_user_summary(report)
    print(f"Papers analyzed: {o['paper_count']}")
    print(f"Observed citation links: {o['edge_count']}")
    print(f"Detected transition candidates: {o['transition_count']}")
    print(f"Detected topic eras: {o['eras']}")
    print(f'Wrote {Path(args.report).resolve()}')
    if not args.no_html:
        hp = write_dashboard_html(report, args.html, cores)
        print(f'Wrote {hp}')
        print('Open the HTML file in a browser to explore the research development report.')


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nAnalysis cancelled.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nERROR: {type(exc).__name__}: {exc}")
        raise SystemExit(1)
