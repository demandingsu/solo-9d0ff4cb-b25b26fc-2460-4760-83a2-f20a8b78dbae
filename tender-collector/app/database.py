"""SQLite 持久化层。

数据按"入口目录"（提交采集的列表页地址，catalog_url）分区：
- announcements 以 (catalog_url, code) 为唯一键；
- paragraphs 保存每条公告抽取到的"正文条款"段落原文；
- paragraph_fts 是段落原文二元语法 token 的 FTS5 倒排索引（rowid 与
  paragraphs.id 一一对应），检索时通过 MATCH 走索引定位，不逐篇扫描。

复采某目录时，在**单个事务**内删除该目录的旧公告/段落/索引并写入新数据：
正文检索与截止时间查询读到的是同一份数据，因此同时切换；任一环节失败
整体回滚，该目录（以及其他目录）保留上次成功采集的完整结果。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_url     TEXT NOT NULL,
    code            TEXT NOT NULL,
    title           TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('original', 'correction')),
    project_name    TEXT NOT NULL,
    published_at    TEXT NOT NULL,
    deadline        TEXT,
    corrects_code   TEXT,
    detail_url      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    UNIQUE (catalog_url, code)
);
CREATE INDEX IF NOT EXISTS idx_announcements_project
    ON announcements(project_name);
CREATE INDEX IF NOT EXISTS idx_announcements_catalog
    ON announcements(catalog_url);

CREATE TABLE IF NOT EXISTS paragraphs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_url     TEXT NOT NULL,
    code            TEXT NOT NULL,
    paragraph_index INTEGER NOT NULL,
    text            TEXT NOT NULL,
    UNIQUE (catalog_url, code, paragraph_index)
);
CREATE INDEX IF NOT EXISTS idx_paragraphs_catalog
    ON paragraphs(catalog_url);

CREATE VIRTUAL TABLE IF NOT EXISTS paragraph_fts USING fts5(
    tokens,
    tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS collection_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url      TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    items_collected INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL
);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path or settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    """把没有 catalog_url 的旧版 announcements 表迁移到当前结构。

    旧数据的 catalog_url 取最近一次成功采集的列表页地址（没有则占位），
    公告记录完整保留；旧数据没有段落索引，重新采集后即可检索。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(announcements)")]
    if not cols or "catalog_url" in cols:
        return
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    row = (
        conn.execute(
            "SELECT source_url FROM collection_runs"
            " WHERE status = 'success' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if "collection_runs" in tables
        else None
    )
    legacy_catalog = row[0] if row else "legacy://unknown"
    conn.execute("ALTER TABLE announcements RENAME TO announcements_legacy")
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO announcements
            (catalog_url, code, title, kind, project_name, published_at,
             deadline, corrects_code, detail_url, last_seen_at)
        SELECT ?, code, title, kind, project_name, published_at,
               deadline, corrects_code, detail_url, last_seen_at
        FROM announcements_legacy
        """,
        (legacy_catalog,),
    )
    conn.execute("DROP TABLE announcements_legacy")


def init_db(db_path: str | None = None) -> None:
    with get_conn(db_path) as conn:
        _migrate_legacy(conn)
        # 再执行一遍完整 SCHEMA：建齐迁移后缺失的表与索引（IF NOT EXISTS 幂等）
        conn.executescript(SCHEMA)
        conn.commit()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 目录级原子替换（须在调用方的事务内依次执行）
# ---------------------------------------------------------------------------
def delete_catalog(conn: sqlite3.Connection, catalog_url: str) -> None:
    """删除某目录的公告、段落及其检索索引（同事务内才对外生效）。"""
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM paragraphs WHERE catalog_url = ?", (catalog_url,)
        )
    ]
    if ids:
        conn.executemany(
            "DELETE FROM paragraph_fts WHERE rowid = ?", [(i,) for i in ids]
        )
    conn.execute("DELETE FROM paragraphs WHERE catalog_url = ?", (catalog_url,))
    conn.execute("DELETE FROM announcements WHERE catalog_url = ?", (catalog_url,))


def insert_announcements(
    conn: sqlite3.Connection, catalog_url: str, rows: list[dict[str, Any]]
) -> None:
    now = utcnow_iso()
    # 原公告先写，保证更正公告引用的数据已存在（顺序不影响业务判定，仅约束友好）
    ordered = sorted(rows, key=lambda r: 0 if r["kind"] == "original" else 1)
    conn.executemany(
        """
        INSERT INTO announcements
            (catalog_url, code, title, kind, project_name, published_at,
             deadline, corrects_code, detail_url, last_seen_at)
        VALUES
            (:catalog_url, :code, :title, :kind, :project_name, :published_at,
             :deadline, :corrects_code, :detail_url, :last_seen_at)
        """,
        [
            {
                "catalog_url": catalog_url,
                "code": r["code"],
                "title": r["title"],
                "kind": r["kind"],
                "project_name": r["project_name"],
                "published_at": r["published_at"],
                "deadline": r["deadline"],
                "corrects_code": r["corrects_code"],
                "detail_url": r["detail_url"],
                "last_seen_at": now,
            }
            for r in ordered
        ],
    )


def insert_paragraphs(
    conn: sqlite3.Connection, catalog_url: str, paragraph_rows: list[dict[str, Any]]
) -> list[int]:
    """写入段落原文，返回与输入顺序一致的段落 id 列表。"""
    ids: list[int] = []
    for p in paragraph_rows:
        cur = conn.execute(
            """
            INSERT INTO paragraphs (catalog_url, code, paragraph_index, text)
            VALUES (?, ?, ?, ?)
            """,
            (catalog_url, p["code"], p["paragraph_index"], p["text"]),
        )
        ids.append(cur.lastrowid)
    return ids


def index_paragraphs(
    conn: sqlite3.Connection, entries: list[tuple[int, str]]
) -> None:
    """把段落 token 流写入 FTS5 索引（rowid 与 paragraphs.id 对应）。"""
    conn.executemany(
        "INSERT INTO paragraph_fts (rowid, tokens) VALUES (?, ?)", entries
    )


# ---------------------------------------------------------------------------
# 运行记录与查询
# ---------------------------------------------------------------------------
def record_run(
    conn: sqlite3.Connection,
    *,
    source_url: str,
    status: str,
    items_collected: int,
    error: str | None,
    started_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO collection_runs
            (source_url, status, items_collected, error, started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (source_url, status, items_collected, error, started_at, utcnow_iso()),
    )


def fetch_all_announcements(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM announcements"
            " ORDER BY catalog_url, project_name, published_at, code"
        )
    )


def search_paragraphs(
    conn: sqlite3.Connection, match_query: str, catalog_url: str | None = None
) -> list[sqlite3.Row]:
    """通过 FTS5 索引定位命中段落，并关联出公告信息。"""
    sql = """
        SELECT p.catalog_url, p.code, p.paragraph_index, p.text,
               a.title, a.project_name, a.published_at, a.detail_url
        FROM paragraph_fts
        JOIN paragraphs p ON p.id = paragraph_fts.rowid
        JOIN announcements a
          ON a.catalog_url = p.catalog_url AND a.code = p.code
        WHERE paragraph_fts MATCH ?
    """
    params: list[Any] = [match_query]
    if catalog_url:
        sql += " AND p.catalog_url = ?"
        params.append(catalog_url)
    sql += " ORDER BY p.catalog_url, p.code, p.paragraph_index"
    return list(conn.execute(sql, params))


def fetch_runs(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM collection_runs ORDER BY id DESC LIMIT ?", (limit,)
        )
    )
