"""更正公告归并与生效截止时间判定。

规则（不依赖任何页面/输入顺序）：
1. 公告按"入口目录"（catalog_url）分组，各目录独立归并、互不影响；
2. 更正公告按其引用的原公告编号 (corrects_code) 归并到对应项目；
3. 同一项目内，取"发布时间最新"的公告所声明的投标截止时间作为生效截止时间；
   发布时间相同时以公告编号作为确定性次序；
4. 最新公告未声明截止时间（例如只更正联系方式）时，沿用上一份带截止时间的公告；
5. 若整组都没有任何截止时间，抛出数据错误（本次采集整体失败）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


class MergeError(ValueError):
    """公告数据无法归并（缺字段、引用悬空等）。"""


@dataclass(frozen=True)
class EffectiveDeadline:
    project_name: str
    deadline: str
    source_code: str
    source_url: str
    catalog_url: str = ""


def _sort_key(row: dict[str, Any]) -> tuple[str, str]:
    return (row["published_at"] or "", row["code"] or "")


def _merge_catalog(rows: list[dict[str, Any]], catalog_url: str) -> list[EffectiveDeadline]:
    codes = {r["code"] for r in rows}
    # 以原公告编号确定项目；更正公告必须引用一个真实存在的原公告
    originals: dict[str, dict[str, Any]] = {}
    groups: dict[str, list[dict[str, Any]]] = {}

    for r in rows:
        if r["kind"] == "original":
            if r["code"] in originals:
                raise MergeError(f"原公告编号重复: {r['code']}")
            originals[r["code"]] = r
            groups.setdefault(r["code"], []).append(r)

    for r in rows:
        if r["kind"] == "correction":
            ref = r.get("corrects_code")
            if not ref:
                raise MergeError(f"更正公告 {r['code']} 缺少引用的原公告编号")
            if ref not in codes:
                raise MergeError(
                    f"更正公告 {r['code']} 引用的原公告 {ref} 不存在"
                )
            if ref not in originals:
                raise MergeError(
                    f"更正公告 {r['code']} 引用的 {ref} 不是原公告"
                )
            groups.setdefault(ref, []).append(r)

    results: list[EffectiveDeadline] = []
    for code, group in groups.items():
        project_name = originals[code]["project_name"]
        # 按发布时间升序；最新发布的公告优先决定截止时间
        ordered = sorted(group, key=_sort_key)
        source = next((r for r in reversed(ordered) if r.get("deadline")), None)
        if source is None:
            raise MergeError(f"项目 {project_name} 的所有公告均未给出投标截止时间")
        results.append(
            EffectiveDeadline(
                project_name=project_name,
                deadline=source["deadline"],
                source_code=source["code"],
                source_url=source["detail_url"],
                catalog_url=catalog_url,
            )
        )

    return results


def merge_projects(rows: Iterable[dict[str, Any]]) -> list[EffectiveDeadline]:
    """按入口目录分组归并；同一编号在不同目录中互不干扰。"""
    by_catalog: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_catalog.setdefault(r.get("catalog_url") or "", []).append(r)

    results: list[EffectiveDeadline] = []
    for catalog_url, catalog_rows in by_catalog.items():
        results.extend(_merge_catalog(catalog_rows, catalog_url))

    return sorted(results, key=lambda d: (d.project_name, d.catalog_url))
