"""正文条款检索与目录级原子切换的端到端测试。

mutable_site 在同一台静态服务器下提供 a/ 与 b/ 两个入口目录（两个目录互
为独立目录），测试可原地重写某个目录的页面来模拟"段落被改写/撤下"与
各类故障，验证：
- 中文两字词与连续短语按原文命中（跨标点的相同字符不误命中）；
- 检索经 FTS5 索引定位（查询计划可见 VIRTUAL TABLE，而非逐篇扫描）；
- 复采某目录成功后，该目录的正文检索与截止时间查询同时切换，其他目录不变；
- 页面读取、归并或索引写入失败时，两种查询都保留此前完整结果。
"""
from __future__ import annotations

import sqlite3

import pytest


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def collect(client, list_url):
    resp = client.post("/api/collect", json={"list_url": list_url})
    assert resp.status_code == 200, resp.text
    return resp.json()


def search(client, q, catalog_url=None):
    params = {"q": q}
    if catalog_url:
        params["catalog_url"] = catalog_url
    resp = client.get("/api/search", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def hit_codes(payload):
    return {h["code"] for h in payload["hits"]}


def projects_of(client, catalog_url):
    return {
        p["project_name"]: p
        for p in client.get("/api/projects").json()
        if p["catalog_url"] == catalog_url
    }


# ---------------------------------------------------------------------------
# 检索语义
# ---------------------------------------------------------------------------
def test_two_char_word_and_continuous_phrase_match(client, mutable_site):
    c, _, _ = client
    collect(c, mutable_site.list_url("a"))

    # 连续短语"夜间施工"：4 条公告命中；SZ-2026-003 原文为"夜间。施工"，
    # 被标点隔开，不得命中
    payload = search(c, "夜间施工")
    assert payload["total"] == 4
    assert hit_codes(payload) == {
        "SZ-2026-001",
        "SZ-2026-001-G1",
        "SZ-2026-002",
        "SZ-2026-002-G1",
    }

    # 两字词"夜间"：上述 4 条 + 含"夜间作业"的 SZ-2026-001 第三段
    # + 含"每日夜间。"的 SZ-2026-003
    assert hit_codes(search(c, "夜间")) == {
        "SZ-2026-001",
        "SZ-2026-001-G1",
        "SZ-2026-002",
        "SZ-2026-002-G1",
        "SZ-2026-003",
    }

    # "夜间作业"只命中真正连续出现的段落（"夜间施工作业"不算）
    assert hit_codes(search(c, "夜间作业")) == {"SZ-2026-001"}

    # 数字与中文混排的连续短语同样按原文命中
    assert hit_codes(search(c, "55分贝")) == {"SZ-2026-001"}
    # 跨段落/跨公告的字符组合不得命中
    assert search(c, "夜间施工便道")["total"] == 0


def test_search_hit_contains_code_paragraph_and_link(client, mutable_site):
    c, _, _ = client
    collect(c, mutable_site.list_url("a"))

    payload = search(c, "夜间施工")
    hit = next(h for h in payload["hits"] if h["code"] == "SZ-2026-001")
    assert hit["project_name"] == "滨河路市政改造工程"
    assert hit["catalog_url"] == mutable_site.list_url("a")
    assert hit["detail_url"] == f"{mutable_site.base}/a/SZ-2026-001.html"
    assert hit["title"] == "滨河路市政改造工程施工招标公告"
    assert hit["published_at"] == "2026-10-20 09:00"

    # 命中段落：原文 + 段落序号，可回原文核对
    assert len(hit["matches"]) == 1
    match = hit["matches"][0]
    assert match["paragraph_index"] == 2
    assert "夜间施工" in match["text"]
    assert match["text"].startswith("因滨河路沿线居民密集")

    # 一条公告多段命中时全部返回（"夜间"命中 SZ-2026-001 的第 2、3 段）
    payload = search(c, "夜间")
    hit = next(h for h in payload["hits"] if h["code"] == "SZ-2026-001")
    assert [m["paragraph_index"] for m in hit["matches"]] == [2, 3]


def test_search_uses_fts_index_not_scan(client, mutable_site, monkeypatch):
    c, _, database = client
    collect(c, mutable_site.list_url("a"))

    # 禁掉逐篇扫描路径：检索不得依赖 announcements 全表读取
    def forbidden(*args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("检索走了全表扫描")

    monkeypatch.setattr(database, "fetch_all_announcements", forbidden)
    assert search(c, "夜间施工")["total"] == 4

    # 查询计划：MATCH 经 FTS5 虚表索引定位
    with database.get_conn() as conn:
        plan = "\n".join(
            str(tuple(row))
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT rowid FROM paragraph_fts"
                " WHERE paragraph_fts MATCH ?",
                ('"夜间 间施 施工"',),
            )
        )
    assert "VIRTUAL TABLE" in plan


def test_query_without_searchable_chars_returns_empty(client, mutable_site):
    c, _, _ = client
    collect(c, mutable_site.list_url("a"))
    payload = search(c, "，。、")
    assert payload["total"] == 0
    assert payload["hits"] == []


def test_catalog_filter_limits_scope(client, mutable_site):
    c, _, _ = client
    collect(c, mutable_site.list_url("a"))
    collect(c, mutable_site.list_url("b"))

    # 不指定目录：两个目录的公告都命中
    assert search(c, "夜间施工")["total"] == 8
    # 指定目录：只命中该目录
    payload = search(c, "夜间施工", catalog_url=mutable_site.list_url("a"))
    assert payload["total"] == 4
    assert {h["catalog_url"] for h in payload["hits"]} == {mutable_site.list_url("a")}


# ---------------------------------------------------------------------------
# 复采切换与故障保留
# ---------------------------------------------------------------------------
def test_recollect_switches_both_queries_and_keeps_other_catalog(
    client, mutable_site
):
    c, _, _ = client
    collect(c, mutable_site.list_url("a"))
    collect(c, mutable_site.list_url("b"))

    # 切换前：目录 a 的项目 A 截止时间与检索结果
    before_a = projects_of(c, mutable_site.list_url("a"))
    assert before_a["滨河路市政改造工程"]["current_deadline"] == "2026-11-25 14:30"
    assert hit_codes(search(c, "夜间施工", mutable_site.list_url("a"))) == {
        "SZ-2026-001",
        "SZ-2026-001-G1",
        "SZ-2026-002",
        "SZ-2026-002-G1",
    }
    before_b_projects = projects_of(c, mutable_site.list_url("b"))
    before_b_hits = search(c, "夜间施工", mutable_site.list_url("b"))

    # 目录 a 的站点内容变化：段落被改写/撤下，项目 A 再次延期
    mutable_site.regenerate("v2", "a")
    payload = collect(c, mutable_site.list_url("a"))
    assert payload["collected"] == 10

    # 截止时间查询与正文检索同时切换到新内容
    after_a = projects_of(c, mutable_site.list_url("a"))
    assert after_a["滨河路市政改造工程"]["current_deadline"] == "2026-12-02 14:30"
    assert after_a["滨河路市政改造工程"]["deadline_source_code"] == "SZ-2026-001-G1"

    # 被改写/撤下的段落不再命中
    assert hit_codes(search(c, "夜间施工", mutable_site.list_url("a"))) == {
        "SZ-2026-002",
        "SZ-2026-002-G1",
    }
    assert search(c, "半幅通行", mutable_site.list_url("a"))["total"] == 0
    # 新表述可以命中
    assert hit_codes(search(c, "夜间作业", mutable_site.list_url("a"))) == {
        "SZ-2026-001"
    }

    # 其他目录（b）保持原样
    assert projects_of(c, mutable_site.list_url("b")) == before_b_projects
    assert search(c, "夜间施工", mutable_site.list_url("b")) == before_b_hits


def test_broken_clauses_failure_keeps_previous_results(client, mutable_site):
    c, _, _ = client
    collect(c, mutable_site.list_url("a"))
    collect(c, mutable_site.list_url("b"))
    before_projects = projects_of(c, mutable_site.list_url("a"))
    before_hits = search(c, "夜间施工", mutable_site.list_url("a"))

    # 故障样例：详情页缺失正文条款容器
    mutable_site.regenerate("broken-clauses", "a")
    resp = c.post("/api/collect", json={"list_url": mutable_site.list_url("a")})
    assert resp.status_code == 422
    assert "正文条款" in resp.json()["detail"]

    # 两种查询都保留此前完整结果
    assert projects_of(c, mutable_site.list_url("a")) == before_projects
    assert search(c, "夜间施工", mutable_site.list_url("a")) == before_hits

    # 失败被记录；站点恢复后可再次采集
    runs = c.get("/api/collections").json()
    assert runs[0]["status"] == "failed"
    mutable_site.regenerate("v1", "a")
    assert collect(c, mutable_site.list_url("a"))["collected"] == 10


def test_broken_reference_failure_keeps_previous_results(client, mutable_site):
    c, _, _ = client
    collect(c, mutable_site.list_url("a"))
    before_projects = projects_of(c, mutable_site.list_url("a"))
    before_hits = search(c, "夜间施工", mutable_site.list_url("a"))

    # 故障样例：更正公告引用不存在的原公告编号（归并失败）
    mutable_site.regenerate("broken-ref", "a")
    resp = c.post("/api/collect", json={"list_url": mutable_site.list_url("a")})
    assert resp.status_code == 422
    assert "SZ-2026-099" in resp.json()["detail"]

    assert projects_of(c, mutable_site.list_url("a")) == before_projects
    assert search(c, "夜间施工", mutable_site.list_url("a")) == before_hits


def test_index_write_failure_rolls_back(client, mutable_site, monkeypatch):
    c, _, database = client
    collect(c, mutable_site.list_url("a"))
    before_projects = projects_of(c, mutable_site.list_url("a"))
    before_hits = search(c, "夜间施工", mutable_site.list_url("a"))

    # 站点内容已变化，但索引写入失败 -> 整个目录替换必须回滚
    mutable_site.regenerate("v2", "a")

    def boom(conn, entries):
        raise sqlite3.OperationalError("injected index write failure")

    monkeypatch.setattr(database, "index_paragraphs", boom)
    resp = c.post("/api/collect", json={"list_url": mutable_site.list_url("a")})
    assert resp.status_code == 422
    assert "索引" in resp.json()["detail"] or "写入数据库失败" in resp.json()["detail"]
    monkeypatch.undo()

    # 两种查询都保留此前完整结果（不是新公告 + 旧索引之类的中间态）
    assert projects_of(c, mutable_site.list_url("a")) == before_projects
    assert search(c, "夜间施工", mutable_site.list_url("a")) == before_hits

    # 故障消除后复采成功，两种查询同时切换到新内容
    assert collect(c, mutable_site.list_url("a"))["collected"] == 10
    after_a = projects_of(c, mutable_site.list_url("a"))
    assert after_a["滨河路市政改造工程"]["current_deadline"] == "2026-12-02 14:30"
    assert hit_codes(search(c, "夜间施工", mutable_site.list_url("a"))) == {
        "SZ-2026-002",
        "SZ-2026-002-G1",
    }


# ---------------------------------------------------------------------------
# 分词单元测试与旧库迁移
# ---------------------------------------------------------------------------
def test_tokenizer_units():
    from app.indexing import SEPARATOR, document_tokens, query_to_match, tokenize

    assert tokenize("夜间施工") == ["夜间", "间施", "施工"]
    assert tokenize("夜间") == ["夜间"]
    assert tokenize("夜间。施工") == ["夜间", SEPARATOR, "施工"]
    assert tokenize("桥") == ["桥"]
    assert query_to_match("夜间施工") == '"夜间 间施 施工"'
    assert query_to_match("，。") is None
    # 文档与查询使用同一套切分，保证命中语义一致
    assert "间施" not in tokenize("每日夜间。施工便道")
    assert document_tokens("夜间施工") == "夜间 间施 施工"


def test_legacy_database_migrated(tmp_path):
    """旧版（无 catalog_url）数据库升级后公告记录保留，可继续查询。"""
    from app import database

    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            kind TEXT NOT NULL,
            project_name TEXT NOT NULL,
            published_at TEXT NOT NULL,
            deadline TEXT,
            corrects_code TEXT,
            detail_url TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        CREATE TABLE collection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_url TEXT NOT NULL,
            status TEXT NOT NULL,
            items_collected INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO announcements VALUES (NULL,?,?,?,?,?,?,?,?,?)",
        (
            "SZ-2026-003",
            "东风桥加固维修工程招标公告",
            "original",
            "东风桥加固维修工程",
            "2026-09-15 08:45",
            "2026-10-30 17:00",
            None,
            "http://legacy/SZ-2026-003.html",
            "2026-11-01T00:00:00+00:00",
        ),
    )
    conn.execute(
        "INSERT INTO collection_runs VALUES (NULL,?,?,?,?,?,?)",
        (
            "http://legacy/list_1.html",
            "success",
            1,
            None,
            "2026-11-01T00:00:00+00:00",
            "2026-11-01T00:00:01+00:00",
        ),
    )
    conn.commit()
    conn.close()

    database.init_db(db_path)
    with database.get_conn(db_path) as conn:
        rows = [dict(r) for r in database.fetch_all_announcements(conn)]
        # 新表结构已建齐
        conn.execute("SELECT COUNT(*) FROM paragraphs").fetchone()
        conn.execute("SELECT COUNT(*) FROM paragraph_fts").fetchone()
    assert len(rows) == 1
    assert rows[0]["code"] == "SZ-2026-003"
    assert rows[0]["catalog_url"] == "http://legacy/list_1.html"
