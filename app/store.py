"""SQLite 存储层：文档 / 段落 / 译文缓存 / 术语表。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS docs (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    title       TEXT NOT NULL,
    num_pages   INTEGER NOT NULL DEFAULT 0,
    n_blocks    INTEGER NOT NULL DEFAULT 0,
    meta        TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS blocks (
    doc_id  TEXT NOT NULL,
    idx     INTEGER NOT NULL,
    page    INTEGER NOT NULL DEFAULT 0,
    kind    TEXT NOT NULL DEFAULT 'para',
    level   INTEGER NOT NULL DEFAULT 0,
    cont    INTEGER NOT NULL DEFAULT 0,
    text    TEXT NOT NULL,
    zh      TEXT,
    status  TEXT NOT NULL DEFAULT 'pending',
    hash    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (doc_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_blocks_doc ON blocks(doc_id, idx);

CREATE TABLE IF NOT EXISTS cache (
    hash       TEXT PRIMARY KEY,
    zh         TEXT NOT NULL,
    model      TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS glossary (
    en   TEXT PRIMARY KEY,
    zh   TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    builtin INTEGER NOT NULL DEFAULT 0
);
"""


def _now() -> float:
    return time.time()


class Store:
    def __init__(self, db_path: Path, default_glossary: Iterable[tuple[str, str]] = ()) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._seed_glossary(default_glossary)

    # ------------------------------------------------------------------ 基础
    def _execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def _query_one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ 文档
    def create_doc(self, doc_id: str, filename: str, title: str, num_pages: int,
                   blocks: list[dict[str, Any]], meta: dict[str, Any] | None = None,
                   variant: str = "") -> dict[str, Any]:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO docs (id, filename, title, num_pages, n_blocks, meta, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (doc_id, filename, title, num_pages, len(blocks),
                 json.dumps(meta or {}, ensure_ascii=False), _now()),
            )
            self._conn.execute("DELETE FROM blocks WHERE doc_id=?", (doc_id,))
            rows = []
            for b in blocks:
                h = cache_key(b["text"], variant)
                cached = self._conn.execute("SELECT zh FROM cache WHERE hash=?", (h,)).fetchone()
                zh = cached["zh"] if cached else None
                rows.append((
                    doc_id, b["idx"], b.get("page", 0), b.get("kind", "para"),
                    b.get("level", 0), 1 if b.get("cont") else 0, b["text"],
                    zh, "done" if zh else "pending", h,
                ))
            self._conn.executemany(
                "INSERT INTO blocks (doc_id, idx, page, kind, level, cont, text, zh, status, hash)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)", rows,
            )
            self._conn.commit()
        return self.get_doc(doc_id)

    def get_doc(self, doc_id: str) -> dict[str, Any] | None:
        row = self._query_one("SELECT * FROM docs WHERE id=?", (doc_id,))
        if not row:
            return None
        doc = dict(row)
        doc["meta"] = json.loads(doc.get("meta") or "{}")
        doc.update(self.progress(doc_id))
        return doc

    def list_docs(self) -> list[dict[str, Any]]:
        out = []
        for row in self._query("SELECT * FROM docs ORDER BY created_at DESC"):
            doc = dict(row)
            doc["meta"] = json.loads(doc.get("meta") or "{}")
            doc.update(self.progress(doc["id"]))
            out.append(doc)
        return out

    def delete_doc(self, doc_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM blocks WHERE doc_id=?", (doc_id,))
            self._conn.execute("DELETE FROM docs WHERE id=?", (doc_id,))
            self._conn.commit()

    def progress(self, doc_id: str) -> dict[str, int]:
        """进度只统计需要翻译的块：meta/ref/refhead 不计入分母。"""
        row = self._query_one(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,"
            " SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS failed"
            " FROM blocks WHERE doc_id=? AND kind NOT IN ('meta','ref','refhead')", (doc_id,))
        all_row = self._query_one("SELECT COUNT(*) AS n FROM blocks WHERE doc_id=?", (doc_id,))
        total = int(row["total"] or 0) if row else 0
        done = int(row["done"] or 0) if row else 0
        failed = int(row["failed"] or 0) if row else 0
        return {"total": total, "total_all": int(all_row["n"] or 0) if all_row else 0,
                "done": done, "failed": failed,
                "percent": round(done * 100 / total) if total else 0}

    # ------------------------------------------------------------------ 段落
    def get_blocks(self, doc_id: str, only_pending: bool = False,
                   indices: list[int] | None = None, include_refs: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM blocks WHERE doc_id=?"
        params: list[Any] = [doc_id]
        if only_pending:
            sql += " AND status!='done'"
        if indices:
            sql += f" AND idx IN ({','.join('?' * len(indices))})"
            params.extend(indices)
        if not include_refs:
            sql += " AND kind!='ref'"
        sql += " ORDER BY idx"
        return [dict(r) for r in self._query(sql, params)]

    def set_block_translation(self, doc_id: str, idx: int, zh: str | None, status: str) -> None:
        self._execute("UPDATE blocks SET zh=?, status=? WHERE doc_id=? AND idx=?", (zh, status, doc_id, idx))

    def reset_doc(self, doc_id: str, include_done: bool = True) -> None:
        if include_done:
            self._execute("UPDATE blocks SET zh=NULL, status='pending' WHERE doc_id=?", (doc_id,))
        else:
            self._execute("UPDATE blocks SET zh=NULL, status='pending'"
                          " WHERE doc_id=? AND status!='done'", (doc_id,))

    # ------------------------------------------------------------------ 缓存
    def cache_get_many(self, hashes: list[str]) -> dict[str, str]:
        if not hashes:
            return {}
        out: dict[str, str] = {}
        for chunk_start in range(0, len(hashes), 400):
            chunk = hashes[chunk_start:chunk_start + 400]
            sql = f"SELECT hash, zh FROM cache WHERE hash IN ({','.join('?' * len(chunk))})"
            for row in self._query(sql, chunk):
                out[row["hash"]] = row["zh"]
        return out

    def cache_put(self, key: str, zh: str, model: str = "") -> None:
        self._execute("INSERT OR REPLACE INTO cache (hash, zh, model, created_at) VALUES (?,?,?,?)",
                      (key, zh, model, _now()))

    def cache_delete(self, keys: list[str]) -> int:
        if not keys:
            return 0
        n = 0
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            sql = f"DELETE FROM cache WHERE hash IN ({','.join('?' * len(chunk))})"
            cur = self._execute(sql, chunk)
            n += cur.rowcount or 0
        return n

    def cache_stats(self) -> dict[str, Any]:
        rows = self._query("SELECT model, COUNT(*) AS n, COALESCE(SUM(LENGTH(zh)),0) AS chars"
                           " FROM cache GROUP BY model ORDER BY n DESC")
        return {"entries": sum(int(r["n"]) for r in rows),
                "chars": sum(int(r["chars"]) for r in rows),
                "by_model": [dict(r) for r in rows]}

    def cache_clear(self) -> int:
        cur = self._execute("DELETE FROM cache")
        return cur.rowcount or 0

    def cache_size(self) -> int:
        row = self._query_one("SELECT COUNT(*) AS n FROM cache")
        return int(row["n"] or 0) if row else 0

    # ------------------------------------------------------------------ 术语表
    def _seed_glossary(self, defaults: Iterable[tuple[str, str]]) -> None:
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) AS n FROM glossary").fetchone()["n"]
            if n:
                return
            self._conn.executemany(
                "INSERT OR IGNORE INTO glossary (en, zh, note, builtin) VALUES (?,?,?,1)",
                [(en, zh, "") for en, zh in defaults],
            )
            self._conn.commit()

    def glossary_all(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM glossary ORDER BY builtin DESC, en COLLATE NOCASE")]

    def glossary_map(self) -> dict[str, str]:
        return {r["en"]: r["zh"] for r in self._query("SELECT en, zh FROM glossary WHERE zh!=''")}

    def glossary_upsert(self, en: str, zh: str, note: str = "") -> None:
        self._execute("INSERT INTO glossary (en, zh, note, builtin) VALUES (?,?,?,0)"
                      " ON CONFLICT(en) DO UPDATE SET zh=excluded.zh, note=excluded.note",
                      (en.strip(), zh.strip(), note.strip()))

    def glossary_delete(self, en: str) -> None:
        self._execute("DELETE FROM glossary WHERE en=?", (en,))


def cache_key(text: str, variant: str = "") -> str:
    """段落级译文缓存键：段落内容 + 变体指纹（提示词版本 / 术语表 / 模型）。"""
    import hashlib
    payload = f"{variant}|{text.strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
