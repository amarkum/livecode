"""Retrieval-quality helpers for memory search.

On top of the plain ``0.4*fts + 0.6*vec`` blend this adds: stop-word-stripped
FTS queries, min-max normalised BM25 so the blend weights actually mean
something, exponential temporal decay for session chunks (curated ``MEMORY.md``
is evergreen), per-source weighting, a mild access-frequency boost, scaffold
filtering, and MMR diversity re-ranking.
"""
from __future__ import annotations

import math
import re
import time

# --- FTS query cleanup ------------------------------------------------------

# Conversational filler dropped from FTS queries so a phrasing like
# "that thing we discussed about the api" reduces to just "discussed api".
_STOP_WORDS = frozenset(
    {
        "a", "an", "the", "this", "that", "these", "those",
        "i", "me", "my", "we", "our", "you", "your", "he", "she", "it", "they",
        "him", "her", "its", "them", "us",
        "is", "are", "was", "were", "be", "been", "being", "have", "has", "had",
        "do", "does", "did", "will", "would", "could", "should", "can", "may", "might",
        "in", "on", "at", "to", "for", "of", "with", "by", "from", "about", "into",
        "through", "during", "before", "after", "above", "below", "again", "further",
        "and", "or", "but", "if", "then", "because", "as", "while", "when", "where",
        "what", "which", "who", "how", "why",
        "thing", "things", "stuff", "something", "anything", "everything", "one",
        "some", "any", "all", "each", "every", "both", "few", "more", "most", "other",
        "such", "no", "nor", "not", "only", "own", "same", "so", "than", "too", "very",
        "yesterday", "today", "tomorrow", "earlier", "later", "recently", "now", "just", "ago",
    }
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")


def keywords(query: str, *, limit: int = 24) -> list[str]:
    """Lowercased, de-duplicated, stop-word-free tokens from a query.

    Falls back to the raw (still de-duplicated) tokens when stripping stop words
    would leave nothing to search for.
    """
    raw = [t.lower() for t in _TOKEN_RE.findall(query or "")]
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for t in raw:
        if t in _STOP_WORDS or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= limit:
            break
    if out:
        return out
    dedup: list[str] = []
    for t in raw:
        if t not in dedup:
            dedup.append(t)
    return dedup[:limit]


# --- scoring modifiers ----------------------------------------------------

# Curated long-term memory — never penalised by age.
_EVERGREEN_SOURCES = frozenset({"workspace", "global"})
# Session-chunk relevance halves every N days.
HALF_LIFE_DAYS = 30.0
# Multiplier applied after decay, by chunk source.
SOURCE_WEIGHTS = {"workspace": 1.3, "global": 1.15, "session": 1.0}


def is_evergreen(source: str) -> bool:
    return (source or "session") in _EVERGREEN_SOURCES


def temporal_decay(
    source: str,
    created_at: int,
    *,
    now: int | None = None,
    half_life_days: float | None = HALF_LIFE_DAYS,
) -> float:
    """``e^(-ln2 * age_days / half_life)`` for session chunks; ``1.0`` otherwise."""
    if not half_life_days or half_life_days <= 0 or is_evergreen(source):
        return 1.0
    now = int(time.time()) if now is None else int(now)
    age_days = max(0.0, (now - max(0, int(created_at or 0))) / 86400.0)
    return math.exp(-math.log(2.0) * age_days / half_life_days)


def source_weight(source: str) -> float:
    return SOURCE_WEIGHTS.get(source or "session", 1.0)


def access_boost(access_count: int) -> float:
    """Chunks that have been retrieved before rank slightly higher.

    ``1.0`` at zero accesses, ~``1.12`` at 10, ~``1.23`` at 100 — small enough
    that similarity stays the dominant signal.
    """
    return 1.0 + math.log1p(max(0, int(access_count or 0))) * 0.05


def normalize_bm25(ranks: list[float]) -> list[float]:
    """Map FTS5 BM25 ranks (more negative = better) onto ``[0, 1]`` (1 = best).

    A lone hit normalises to ``1.0``.
    """
    if not ranks:
        return []
    lo = min(ranks)
    hi = max(ranks)
    span = (hi - lo) or 1e-9
    return [1.0 - (r - lo) / span for r in ranks]


# --- scaffold detection --------------------------------------------------

_SCAFFOLD_HEADINGS = frozenset({"session summary", "topics discussed"})
_SCAFFOLD_META_RE = re.compile(r"^[-*]\s*\*\*(messages|date|model|session)\b", re.IGNORECASE)
_NUMBERED_RE = re.compile(r"^\d+[.)]\s")


def is_scaffold(text: str) -> bool:
    """True for an auto-generated session-metadata stub with no real prose.

    Matches the ``maybe_autosave_session`` shape — a "Session Summary" /
    "Topics Discussed" heading plus ``**Messages:** / **Date:**`` lines and a
    numbered topic list. Any other non-empty line means it is real content.
    """
    stripped = (text or "").strip()
    if not stripped:
        return True
    saw_scaffold_heading = False
    for raw in stripped.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if line.lstrip("#").strip().lower() in _SCAFFOLD_HEADINGS:
                saw_scaffold_heading = True
            continue
        if _SCAFFOLD_META_RE.match(line) or _NUMBERED_RE.match(line):
            continue
        return False
    return saw_scaffold_heading


# --- MMR diversity re-ranking ------------------------------------------

# 1.0 = pure relevance, 0.0 = pure diversity.
MMR_LAMBDA = 0.7
_MMR_SPLIT_RE = re.compile(r"[^A-Za-z0-9_]+")


def _mmr_tokens(text: str) -> set[str]:
    return {t for t in _MMR_SPLIT_RE.split((text or "").lower()) if t}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a) + len(b) - inter
    return inter / union if union else 0.0


def mmr_rerank(snippets: list[str], relevance: list[float], *, lam: float = MMR_LAMBDA) -> list[int]:
    """Greedy Maximal Marginal Relevance ordering.

    Returns the indices of ``snippets`` in the order MMR would surface them,
    balancing each item's ``relevance`` against Jaccard token overlap with the
    already-selected items. No-op (identity order) for <=2 items or ``lam >= 1``.
    """
    n = len(snippets)
    order = list(range(n))
    if n <= 2 or lam >= 1.0:
        return order
    toks = [_mmr_tokens(s) for s in snippets]
    lo = min(relevance)
    hi = max(relevance)
    span = (hi - lo) or 1e-9
    remaining = list(range(n))
    chosen: list[int] = []
    while remaining:
        best_idx = remaining[0]
        best_score = float("-inf")
        for idx in remaining:
            norm = (relevance[idx] - lo) / span
            penalty = max((_jaccard(toks[idx], toks[c]) for c in chosen), default=0.0)
            score = lam * norm - (1.0 - lam) * penalty
            if score > best_score:
                best_score = score
                best_idx = idx
        chosen.append(best_idx)
        remaining.remove(best_idx)
    return chosen
