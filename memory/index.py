from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Sequence

from livecode.memory.chunker import chunk_hash, chunk_markdown
from livecode.memory.embed import (
    EmbedFn,
    cosine_similarity,
    default_embedder,
    pack_embedding,
    unpack_embedding,
)
from livecode.memory import ranking
from livecode.memory.storage import index_db_path, list_memory_files, memory_root

TEXT_WEIGHT = 0.4
VECTOR_WEIGHT = 0.6
DEFAULT_MIN_SCORE = 0.0
DEFAULT_MAX_RESULTS = 6

@dataclass
class SearchResult:
    chunk_id: str
    path: str
    start_line: int
    end_line: int
    snippet: str
    score: float
    source: str
    created_at: int = 0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
  rowid INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT UNIQUE NOT NULL,
  path TEXT NOT NULL,
  start_line INTEGER,
  end_line INTEGER,
  text TEXT,
  hash TEXT,
  source TEXT,
  created_at INTEGER,
  updated_at INTEGER,
  access_count INTEGER DEFAULT 0,
  last_accessed INTEGER
);
CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(hash);
CREATE TABLE IF NOT EXISTS chunk_embeddings (
  chunk_id TEXT PRIMARY KEY,
  embedding BLOB NOT NULL,
  dims INTEGER NOT NULL
);
"""

def _connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
    ).fetchone()
    if not row:
        conn.execute(
            "CREATE VIRTUAL TABLE chunks_fts USING fts5(text, chunk_id UNINDEXED)"
        )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')"
    )
    conn.commit()
    return conn

def open_index(project_path: str) -> sqlite3.Connection:
    return _connect(index_db_path(project_path, create=True))

def reindex_file(
    project_path: str,
    abs_path: str,
    source: str,
    rel_path: str | None = None,
) -> dict[str, int]:
    if not abs_path or not os.path.isfile(abs_path):
        return {"added": 0, "updated": 0, "removed": 0}
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return {"added": 0, "updated": 0, "removed": 0}

    root = memory_root(project_path, create=True)
    path_key = rel_path or os.path.relpath(abs_path, root).replace("\\", "/")
    chunks = chunk_markdown(content)
    now = int(time.time())
    try:
        content_ts = int(os.path.getmtime(abs_path))
    except OSError:
        content_ts = now
    conn = open_index(project_path)
    added = updated = removed = 0
    try:
        existing = {
            row["id"]: row
            for row in conn.execute(
                "SELECT id, hash, rowid FROM chunks WHERE path = ?", (path_key,)
            )
        }
        seen: set[str] = set()
        for i, ch in enumerate(chunks):
            cid = f"{path_key}:{i}"
            seen.add(cid)
            h = chunk_hash(ch.text)
            prev = existing.get(cid)
            if prev and prev["hash"] == h:
                continue
            if prev:
                conn.execute(
                    "UPDATE chunks SET start_line=?, end_line=?, text=?, hash=?, "
                    "source=?, created_at=?, updated_at=?, access_count=0, last_accessed=NULL WHERE id=?",
                    (ch.start_line, ch.end_line, ch.text, h, source, content_ts, now, cid),
                )
                conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (cid,))
                conn.execute(
                    "INSERT INTO chunks_fts(text, chunk_id) VALUES (?, ?)",
                    (ch.text, cid),
                )
                conn.execute("DELETE FROM chunk_embeddings WHERE chunk_id = ?", (cid,))
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO chunks(id, path, start_line, end_line, text, hash, source, "
                    "created_at, updated_at, access_count) VALUES (?,?,?,?,?,?,?,?,?,0)",
                    (
                        cid,
                        path_key,
                        ch.start_line,
                        ch.end_line,
                        ch.text,
                        h,
                        source,
                        content_ts,
                        now,
                    ),
                )
                conn.execute(
                    "INSERT INTO chunks_fts(text, chunk_id) VALUES (?, ?)",
                    (ch.text, cid),
                )
                added += 1
        for cid, _row in existing.items():
            if cid in seen:
                continue
            conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (cid,))
            conn.execute("DELETE FROM chunk_embeddings WHERE chunk_id = ?", (cid,))
            conn.execute("DELETE FROM chunks WHERE id = ?", (cid,))
            removed += 1
        conn.commit()
    finally:
        conn.close()
    return {"added": added, "updated": updated, "removed": removed}

def reindex_all(project_path: str) -> dict[str, int]:
    totals = {"added": 0, "updated": 0, "removed": 0}
    for item in list_memory_files(project_path):
        r = reindex_file(project_path, item["abs_path"], item["source"], item["path"])
        for k in totals:
            totals[k] += r[k]
    return totals

def remove_path_chunks(project_path: str, path_key: str) -> int:
    if not path_key:
        return 0
    conn = open_index(project_path)
    try:
        ids = [row["id"] for row in conn.execute(
            "SELECT id FROM chunks WHERE path = ?", (path_key,)
        )]
        for cid in ids:
            conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (cid,))
            conn.execute("DELETE FROM chunk_embeddings WHERE chunk_id = ?", (cid,))
            conn.execute("DELETE FROM chunks WHERE id = ?", (cid,))
        conn.commit()
        return len(ids)
    finally:
        conn.close()

def embed_missing_chunks(
    project_path: str,
    embed_fn: EmbedFn | None = None,
    *,
    batch_size: int = 64,
) -> int:
    embed_fn = embed_fn or default_embedder()
    conn = open_index(project_path)
    try:
        rows = conn.execute(
            "SELECT c.id, c.text FROM chunks c "
            "LEFT JOIN chunk_embeddings e ON e.chunk_id = c.id "
            "WHERE e.chunk_id IS NULL"
        ).fetchall()
        written = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            texts = [r["text"] or "" for r in batch]
            vectors = embed_fn(texts)
            if len(vectors) != len(batch):
                break
            for row, vec in zip(batch, vectors):
                if not vec:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO chunk_embeddings(chunk_id, embedding, dims) "
                    "VALUES (?,?,?)",
                    (row["id"], pack_embedding(vec), len(vec)),
                )
                written += 1
            conn.commit()
        return written
    finally:
        conn.close()

def _memory_fingerprint(project_path: str) -> str:
    parts: list[str] = []
    for item in list_memory_files(project_path):
        try:
            st = os.stat(item["abs_path"])
        except OSError:
            continue
        parts.append(f"{item['path']}:{st.st_mtime_ns}:{st.st_size}")
    return "|".join(sorted(parts))


def ensure_index(project_path: str, *, embed: bool = True) -> None:
    fingerprint = _memory_fingerprint(project_path)
    conn = open_index(project_path)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'reindex_fingerprint'"
        ).fetchone()
        unchanged = bool(row) and row["value"] == fingerprint
    except sqlite3.Error:
        unchanged = False
    finally:
        conn.close()

    if not unchanged:
        reindex_all(project_path)
        try:
            conn = open_index(project_path)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('reindex_fingerprint', ?)",
                    (fingerprint,),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    if embed:
        embed_missing_chunks(project_path)

def _sanitize_fts_query(query: str) -> str:
    tokens = ranking.keywords(query, limit=24)
    if not tokens:
        return ""
    return " OR ".join(tokens)

def search_fts(
    project_path: str,
    query: str,
    *,
    max_results: int = 18,
) -> list[dict[str, Any]]:
    fts_q = _sanitize_fts_query(query)
    if not fts_q:
        return []
    conn = open_index(project_path)
    try:
        rows = conn.execute(
            """
            SELECT c.id, c.path, c.start_line, c.end_line, c.text, c.source, c.created_at,
                   c.access_count, bm25(chunks_fts) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.chunk_id
            WHERE chunks_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (fts_q, max_results),
        ).fetchall()
        out = []
        for r in rows:
            rank = float(r["rank"] or 0.0)
            out.append(
                {
                    "chunk_id": r["id"],
                    "path": r["path"],
                    "start_line": int(r["start_line"] or 0),
                    "end_line": int(r["end_line"] or 0),
                    "text": r["text"] or "",
                    "source": r["source"] or "session",
                    "created_at": int(r["created_at"] or 0),
                    "access_count": int(r["access_count"] or 0),
                    "fts_rank": rank,
                    "fts_score": max(0.0, -rank),
                }
            )
        return out
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

def search_vectors(
    project_path: str,
    query_vec: Sequence[float],
    *,
    max_results: int = 18,
) -> list[dict[str, Any]]:
    if not query_vec:
        return []
    q = unpack_embedding(pack_embedding(query_vec))
    conn = open_index(project_path)
    try:
        rows = conn.execute(
            """
            SELECT c.id, c.path, c.start_line, c.end_line, c.text, c.source, c.created_at,
                   c.access_count, e.embedding
            FROM chunk_embeddings e
            JOIN chunks c ON c.id = e.chunk_id
            """
        ).fetchall()
        scored = []
        for r in rows:
            vec = unpack_embedding(r["embedding"])
            scored.append((cosine_similarity(q, vec), r))
        scored.sort(key=lambda x: x[0], reverse=True)
        out = []
        for sim, r in scored[:max_results]:
            out.append(
                {
                    "chunk_id": r["id"],
                    "path": r["path"],
                    "start_line": int(r["start_line"] or 0),
                    "end_line": int(r["end_line"] or 0),
                    "text": r["text"] or "",
                    "source": r["source"] or "session",
                    "created_at": int(r["created_at"] or 0),
                    "access_count": int(r["access_count"] or 0),
                    "vec_score": float(sim),
                }
            )
        return out
    finally:
        conn.close()

def _bump_access(project_path: str, chunk_ids: list[str]) -> None:
    if not chunk_ids:
        return
    try:
        conn = open_index(project_path)
        try:
            placeholders = ",".join("?" for _ in chunk_ids)
            conn.execute(
                f"UPDATE chunks SET access_count = access_count + 1, last_accessed = ? "
                f"WHERE id IN ({placeholders})",
                (int(time.time()), *chunk_ids),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def search_memory(
    project_path: str,
    query: str,
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    min_score: float = DEFAULT_MIN_SCORE,
    embed_fn: EmbedFn | None = None,
) -> list[SearchResult]:
    ensure_index(project_path, embed=True)
    pool = max(max_results * 3, 12)
    fts_list = search_fts(project_path, query, max_results=pool)

    fts_norms = ranking.normalize_bm25([float(h.get("fts_rank") or 0.0) for h in fts_list])
    fts_hits: dict[str, dict[str, Any]] = {}
    for h, norm in zip(fts_list, fts_norms):
        rec = dict(h)
        rec["fts_norm"] = norm
        fts_hits[h["chunk_id"]] = rec

    embed_fn = embed_fn or default_embedder()
    vectors = embed_fn([query])
    vec_hits: dict[str, dict[str, Any]] = {}
    if vectors and vectors[0]:
        for h in search_vectors(project_path, vectors[0], max_results=pool):
            vec_hits[h["chunk_id"]] = h

    merged: dict[str, dict[str, Any]] = {}
    for cid, h in fts_hits.items():
        merged[cid] = dict(h)
        merged[cid]["fts_norm"] = float(h.get("fts_norm") or 0.0)
        merged[cid]["vec_score"] = 0.0
    for cid, h in vec_hits.items():
        if cid in merged:
            merged[cid]["vec_score"] = float(h.get("vec_score") or 0.0)
        else:
            rec = dict(h)
            rec["fts_norm"] = 0.0
            merged[cid] = rec

    now_secs = int(time.time())
    scored: list[dict[str, Any]] = []
    for h in merged.values():
        text = h.get("text") or ""
        source = h.get("source") or "session"
        if ranking.is_scaffold(text):
            continue
        fts_n = float(h.get("fts_norm") or 0.0)
        vec_s = float(h.get("vec_score") or 0.0)
        if fts_n > 0.0 and vec_s > 0.0:
            base = max(TEXT_WEIGHT * fts_n + VECTOR_WEIGHT * vec_s, fts_n)
        elif fts_n > 0.0:
            base = fts_n
        else:
            base = VECTOR_WEIGHT * vec_s

        created_at = int(h.get("created_at") or 0)
        raw = (
            base
            * ranking.temporal_decay(source, created_at, now=now_secs)
            * ranking.source_weight(source)
            * ranking.access_boost(int(h.get("access_count") or 0))
        )
        display = min(1.0, raw)
        if display < min_score:
            continue
        h["_raw"] = raw
        h["_display"] = display
        scored.append(h)

    scored.sort(key=lambda x: (x["_raw"], x.get("vec_score", 0.0)), reverse=True)
    scored = scored[:pool]

    order = ranking.mmr_rerank(
        [h.get("text") or "" for h in scored],
        [float(h["_raw"]) for h in scored],
    )
    scored = [scored[i] for i in order][:max_results]

    results = [
        SearchResult(
            chunk_id=h["chunk_id"],
            path=h["path"],
            start_line=int(h.get("start_line") or 0),
            end_line=int(h.get("end_line") or 0),
            snippet=h.get("text") or "",
            score=float(h["_display"]),
            source=h.get("source") or "session",
            created_at=int(h.get("created_at") or 0),
        )
        for h in scored
    ]
    _bump_access(project_path, [r.chunk_id for r in results])
    return results

hybrid_search = search_memory
