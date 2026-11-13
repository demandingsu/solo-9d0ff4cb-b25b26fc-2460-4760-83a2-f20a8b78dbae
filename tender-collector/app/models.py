from __future__ import annotations

from pydantic import BaseModel, Field


class CollectRequest(BaseModel):
    list_url: str = Field(
        ..., min_length=1, description="公告列表页地址（服务会自动跟随分页链接）"
    )


class ProjectOut(BaseModel):
    project_name: str
    current_deadline: str
    deadline_source_code: str
    deadline_source_url: str
    catalog_url: str = ""
    is_expired: bool


class CollectResponse(BaseModel):
    status: str
    source_url: str
    collected: int
    paragraphs_indexed: int
    projects: list[ProjectOut]


class RunOut(BaseModel):
    id: int
    source_url: str
    status: str
    items_collected: int
    error: str | None
    started_at: str
    finished_at: str


class ParagraphHit(BaseModel):
    paragraph_index: int = Field(..., description="正文条款段落序号（从 1 开始）")
    text: str = Field(..., description="命中段落原文")


class SearchHit(BaseModel):
    code: str = Field(..., description="公告编号")
    title: str
    project_name: str
    published_at: str
    catalog_url: str = Field(..., description="入口目录（提交采集的列表页地址）")
    detail_url: str = Field(..., description="详情页链接")
    matches: list[ParagraphHit]


class SearchResponse(BaseModel):
    query: str
    total: int = Field(..., description="命中公告数")
    hits: list[SearchHit]
