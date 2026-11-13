from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from . import database
from .models import (
    CollectRequest,
    CollectResponse,
    ProjectOut,
    RunOut,
    SearchResponse,
)
from .service import ServiceError, query_projects, run_collection, search_clauses


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="市政工程公告采集与条款检索 API",
    description=(
        "提交公告列表页地址，服务抓取全部详情页：将更正公告按引用的原公告编号归并，"
        "按发布时间确定生效的投标截止时间；同时抽取详情页“正文条款”段落，"
        "按入口目录建立可检索索引（中文两字词与连续短语按原文命中）。"
        "复采某目录成功后，该目录的正文检索与截止时间查询同时切换；"
        "任一页面读取、归并或索引写入失败时整体报错，两种查询都保留上次成功结果。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"service": "tender-collector", "docs": "/docs"}


@app.post("/api/collect", response_model=CollectResponse, status_code=200)
def collect_endpoint(req: CollectRequest) -> CollectResponse:
    try:
        result = run_collection(req.list_url)
    except ServiceError as exc:
        # 422 语义：采集到的源站内容/可用性有问题，本次未入库
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CollectResponse(status="success", **result)


@app.get("/api/projects", response_model=list[ProjectOut])
def list_projects() -> list[ProjectOut]:
    return [ProjectOut(**p) for p in query_projects()]


@app.get("/api/search", response_model=SearchResponse)
def search(
    q: str = Query(..., min_length=1, description="检索词：中文两字词或连续短语"),
    catalog_url: str | None = Query(
        None, description="可选：只检索该入口目录（提交采集时的列表页地址）"
    ),
) -> SearchResponse:
    return SearchResponse(**search_clauses(q, catalog_url))


@app.get("/api/collections", response_model=list[RunOut])
def list_collections(limit: int = Query(20, ge=1, le=200)) -> list[RunOut]:
    with database.get_conn() as conn:
        return [RunOut(**dict(r)) for r in database.fetch_runs(conn, limit)]
