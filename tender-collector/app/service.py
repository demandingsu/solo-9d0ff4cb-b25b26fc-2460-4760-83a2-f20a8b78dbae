"""采集编排与查询服务。

采集：抓取 -> 归并校验 -> 分词建索引，全部在内存完成后，于**单个事务**内
替换该入口目录的公告、段落与检索索引——正文检索与截止时间查询读取同一份
数据，因此复采成功后两种查询同时切换；页面读取、归并或索引写入任一环节
失败时整体回滚，两种查询都保留此前完整结果，其他目录不受影响。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from . import database
from .indexing import document_tokens, query_to_match
from .merge import EffectiveDeadline, MergeError, merge_projects
from .scraper import CollectionError, collect

TIME_FORMAT = "%Y-%m-%d %H:%M"


class ServiceError(Exception):
    """对外统一的采集错误（带可读原因）。"""


def _is_expired(deadline_text: str, now: datetime | None = None) -> bool:
    try:
        deadline = datetime.strptime(deadline_text, TIME_FORMAT)
    except ValueError:
        # 容错：支持带秒或 ISO 形式
        deadline = datetime.fromisoformat(deadline_text)
    return deadline <= (now or datetime.now())


def _to_project(d: EffectiveDeadline, now: datetime | None = None) -> dict:
    return {
        "project_name": d.project_name,
        "current_deadline": d.deadline,
        "deadline_source_code": d.source_code,
        "deadline_source_url": d.source_url,
        "catalog_url": d.catalog_url,
        "is_expired": _is_expired(d.deadline, now),
    }


def build_projects(rows: list[dict], now: datetime | None = None) -> list[dict]:
    return [_to_project(d, now) for d in merge_projects(rows)]


def _build_paragraph_rows(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把每条公告的正文条款段落展开为待入库/建索引的段落记录（含 token 流）。"""
    paragraph_rows: list[dict[str, Any]] = []
    for r in raw:
        for i, text in enumerate(r["paragraphs"], start=1):
            paragraph_rows.append(
                {
                    "code": r["code"],
                    "paragraph_index": i,
                    "text": text,
                    "tokens": document_tokens(text),
                }
            )
    return paragraph_rows


def _record_failure(source_url: str, error: str, started_at: str) -> None:
    """失败原因记入 collection_runs（独立事务，不影响已保留的数据）。"""
    try:
        with database.get_conn() as conn:
            database.record_run(
                conn,
                source_url=source_url,
                status="failed",
                items_collected=0,
                error=error,
                started_at=started_at,
            )
            conn.commit()
    except Exception:
        pass  # 记录失败本身出错时不掩盖原始错误


def run_collection(list_url: str) -> dict:
    started_at = database.utcnow_iso()
    try:
        raw = collect(list_url)
        # 先在内存完成归并校验与分词：数据有问题时不写库
        projects = build_projects(raw)
        paragraph_rows = _build_paragraph_rows(raw)
    except (CollectionError, MergeError) as exc:
        # 记录失败采集；announcements/paragraphs/索引完全不动，保留上次成功结果
        _record_failure(list_url, str(exc), started_at)
        raise ServiceError(str(exc)) from exc

    # 成功：单事务替换该目录的公告 + 段落 + 检索索引 + 运行记录，保证原子性
    with database.get_conn() as conn:
        try:
            database.delete_catalog(conn, list_url)
            database.insert_announcements(conn, list_url, raw)
            paragraph_ids = database.insert_paragraphs(conn, list_url, paragraph_rows)
            database.index_paragraphs(
                conn,
                [
                    (pid, p["tokens"])
                    for pid, p in zip(paragraph_ids, paragraph_rows)
                ],
            )
            database.record_run(
                conn,
                source_url=list_url,
                status="success",
                items_collected=len(raw),
                error=None,
                started_at=started_at,
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            _record_failure(list_url, f"写入数据库失败: {exc}", started_at)
            raise ServiceError(f"写入数据库失败: {exc}") from exc

    return {
        "source_url": list_url,
        "collected": len(raw),
        "paragraphs_indexed": len(paragraph_rows),
        "projects": projects,
    }


def query_projects(now: datetime | None = None) -> list[dict]:
    with database.get_conn() as conn:
        rows = [dict(r) for r in database.fetch_all_announcements(conn)]
    if not rows:
        return []
    return build_projects(rows, now)


def search_clauses(query: str, catalog_url: str | None = None) -> dict:
    """按原文检索正文条款段落：FTS5 索引定位，按公告归组返回命中段落。"""
    match = query_to_match(query)
    if match is None:
        return {"query": query, "total": 0, "hits": []}

    with database.get_conn() as conn:
        rows = [dict(r) for r in database.search_paragraphs(conn, match, catalog_url)]

    hits: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for r in rows:
        key = (r["catalog_url"], r["code"])
        if current is None or current["_key"] != key:
            current = {
                "_key": key,
                "code": r["code"],
                "title": r["title"],
                "project_name": r["project_name"],
                "published_at": r["published_at"],
                "catalog_url": r["catalog_url"],
                "detail_url": r["detail_url"],
                "matches": [],
            }
            hits.append(current)
        current["matches"].append(
            {"paragraph_index": r["paragraph_index"], "text": r["text"]}
        )

    for h in hits:
        del h["_key"]
    return {"query": query, "total": len(hits), "hits": hits}
