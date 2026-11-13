"""生成一个本地分页 HTML 示例公告站（市政工程招投标公告）。

生成结果写入 demo_site/，纯静态文件，可用任意 HTTP 服务器托管：
    python3 -m http.server 8001 --directory demo_site

数据特意构造为：
- 列表页按发布时间倒序排列（更正公告排在其引用的原公告之前），
  服务端不能依赖页面顺序来判断截止时间；
- 每个项目的"生效截止时间"由发布时间最新的那份公告（原公告或更正公告）决定；
- 每条公告详情页含"正文条款"段落（section.body-clauses > p.clause），
  其中散布"夜间施工""夜间作业"等条款，用于验证两字词/连续短语按原文命中；
- 含已过期与未过期两种项目。

变体（--variant）：
- v1（默认）：完整示例站；
- v2：段落变化样例——部分条款被改写或撤下，且项目 A 的更正公告再次延期，
  用于验证"复采后正文检索与截止时间查询同时切换、被撤下/改写的段落不再命中"；
- broken-clauses：故障样例——某详情页缺失正文条款容器（页面解析失败）；
- broken-ref：故障样例——某更正公告引用不存在的原公告编号（归并失败）。
"""
from __future__ import annotations

import argparse
import copy
import html
from dataclasses import dataclass, field
from pathlib import Path

PAGE_SIZE = 4
OUT_DIR = Path(__file__).resolve().parent.parent / "demo_site"


@dataclass
class Announcement:
    code: str                      # 公告编号
    title: str                     # 公告标题
    kind: str                      # original | correction
    project_name: str              # 项目名称
    published_at: str              # 发布时间 YYYY-MM-DD HH:MM
    deadline: str | None = None    # 投标截止时间（更正公告可为空）
    corrects_code: str | None = None  # 更正公告引用的原公告编号
    reason: str = ""               # 更正原因（仅更正公告）
    clauses: list[str] = field(default_factory=list)  # 正文条款段落
    omit_clauses: bool = False     # 故障样例：不输出正文条款容器

    @property
    def filename(self) -> str:
        return f"{self.code}.html"


# ---------------------------------------------------------------------------
# 示例数据（v1）：5 个项目、10 条公告、3 个列表页
# ---------------------------------------------------------------------------
ANNOUNCEMENTS: list[Announcement] = [
    # 项目 A：原公告已过期，更正延期后仍在有效期内
    Announcement(
        code="SZ-2026-001",
        title="滨河路市政改造工程施工招标公告",
        kind="original",
        project_name="滨河路市政改造工程",
        published_at="2026-10-20 09:00",
        deadline="2026-11-05 14:30",
        clauses=[
            "本工程采用资格后审方式确定中标候选人，评标办法详见招标文件第五章。",
            "因滨河路沿线居民密集，如需夜间施工，施工单位须提前三日向监理单位报备并公告周边居民。",
            "夜间作业期间噪声不得超过55分贝，违者按合同约定承担违约责任。",
        ],
    ),
    Announcement(
        code="SZ-2026-001-G1",
        title="滨河路市政改造工程施工招标更正公告（第一次）",
        kind="correction",
        project_name="滨河路市政改造工程",
        published_at="2026-10-28 16:00",
        deadline="2026-11-25 14:30",
        corrects_code="SZ-2026-001",
        reason="因招标文件图纸调整，投标截止时间顺延。",
        clauses=[
            "投标截止时间顺延至2026-11-25 14:30，其余安排不变。",
            "原公告中夜间施工报备条款继续有效。",
        ],
    ),
    # 项目 B：两次更正，最终截止时间以发布时间最新的第二次更正为准
    Announcement(
        code="SZ-2026-002",
        title="城北污水处理厂扩建工程招标公告",
        kind="original",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-10-12 10:30",
        deadline="2026-11-10 09:00",
        clauses=[
            "投标人须具备市政公用工程施工总承包一级及以上资质。",
            "原则上不安排夜间施工作业；确需夜间施工的，须办理夜间施工许可证。",
        ],
    ),
    Announcement(
        code="SZ-2026-002-G1",
        title="城北污水处理厂扩建工程招标更正公告",
        kind="correction",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-10-22 15:00",
        deadline="2026-11-18 09:00",
        corrects_code="SZ-2026-002",
        reason="答疑澄清，截止时间调整。",
        clauses=[
            "答疑澄清：夜间施工许可证办理流程详见附件三。",
        ],
    ),
    Announcement(
        code="SZ-2026-002-G2",
        title="城北污水处理厂扩建工程招标更正公告（第二次）",
        kind="correction",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-11-03 11:20",
        deadline="2026-11-30 10:00",
        corrects_code="SZ-2026-002",
        reason="再次延期，以本公告时间为准。",
        clauses=[
            "投标截止时间再次顺延，以本公告载明时间为准。",
        ],
    ),
    # 项目 C：无更正，且已过期
    Announcement(
        code="SZ-2026-003",
        title="东风桥加固维修工程招标公告",
        kind="original",
        project_name="东风桥加固维修工程",
        published_at="2026-09-15 08:45",
        deadline="2026-10-30 17:00",
        clauses=[
            "桥梁加固施工期间实行半幅通行，过往车辆须按导行标志减速慢行。",
            "警示灯设置时段为每日夜间。施工便道两端须安排专人值守。",
        ],
    ),
    # 项目 D：更正公告不修改截止时间（deadline 为空），生效时间仍取原公告
    Announcement(
        code="SZ-2026-004",
        title="高新区雨污分流管网工程招标公告",
        kind="original",
        project_name="高新区雨污分流管网工程",
        published_at="2026-10-25 14:00",
        deadline="2026-11-28 09:30",
        clauses=[
            "管网开挖须避开雨季，雨天停止开挖作业并做好基坑排水。",
        ],
    ),
    Announcement(
        code="SZ-2026-004-G1",
        title="高新区雨污分流管网工程招标更正公告",
        kind="correction",
        project_name="高新区雨污分流管网工程",
        published_at="2026-11-01 09:10",
        deadline=None,
        corrects_code="SZ-2026-004",
        reason="仅更正联系人电话，投标截止时间不变。",
        clauses=[
            "仅更正联系人电话，投标截止时间及其余条款不变。",
        ],
    ),
    # 项目 E：原公告在最后一个列表页、更正公告在第一页（跨页乱序）
    Announcement(
        code="SZ-2026-005",
        title="湖滨公园配套市政道路工程招标公告",
        kind="original",
        project_name="湖滨公园配套市政道路工程",
        published_at="2026-10-08 13:00",
        deadline="2026-11-08 15:00",
        clauses=[
            "道路照明工程须与主体工程同步设计、同步施工、同步验收。",
        ],
    ),
    Announcement(
        code="SZ-2026-005-G1",
        title="湖滨公园配套市政道路工程招标更正公告",
        kind="correction",
        project_name="湖滨公园配套市政道路工程",
        published_at="2026-11-05 17:30",
        deadline="2026-11-20 15:00",
        corrects_code="SZ-2026-005",
        reason="工程量清单调整，截止时间顺延。",
        clauses=[
            "工程量清单调整，投标截止时间顺延至2026-11-20 15:00。",
        ],
    ),
]


# ---------------------------------------------------------------------------
# 变体：段落变化样例与故障样例
# ---------------------------------------------------------------------------
def _by_code(announcements: list[Announcement], code: str) -> Announcement:
    return next(a for a in announcements if a.code == code)


def _v2() -> list[Announcement]:
    """段落变化样例：改写/撤下部分条款，同时项目 A 的更正公告再次延期。"""
    data = copy.deepcopy(ANNOUNCEMENTS)
    # 项目 A 再次延期（验证截止时间查询与正文检索同时切换）
    a_g1 = _by_code(data, "SZ-2026-001-G1")
    a_g1.deadline = "2026-12-02 14:30"
    a_g1.clauses[0] = "投标截止时间顺延至2026-12-02 14:30，其余安排不变。"
    # 撤下：更正公告中"夜间施工报备条款继续有效"一段被删除
    del a_g1.clauses[1]
    # 改写：原公告的"夜间施工"条款改为"夜间作业"（原表述不再命中）
    a = _by_code(data, "SZ-2026-001")
    a.clauses[1] = (
        "因滨河路沿线居民密集，如需夜间作业，施工单位须提前五日向监理单位报备。"
    )
    # 撤下：东风桥项目"半幅通行"条款整段删除
    c = _by_code(data, "SZ-2026-003")
    del c.clauses[0]
    return data


def _broken_clauses() -> list[Announcement]:
    """故障样例：某详情页缺失正文条款容器（页面解析失败，采集整体报错）。"""
    data = copy.deepcopy(ANNOUNCEMENTS)
    _by_code(data, "SZ-2026-002-G1").omit_clauses = True
    return data


def _broken_ref() -> list[Announcement]:
    """故障样例：更正公告引用不存在的原公告编号（归并失败，采集整体报错）。"""
    data = copy.deepcopy(ANNOUNCEMENTS)
    _by_code(data, "SZ-2026-002-G2").corrects_code = "SZ-2026-099"
    return data


VARIANTS = {
    "v1": lambda: copy.deepcopy(ANNOUNCEMENTS),
    "v2": _v2,
    "broken-clauses": _broken_clauses,
    "broken-ref": _broken_ref,
}


def _detail_page(a: Announcement) -> str:
    rows = [
        ("公告编号", a.code),
        ("公告类型", "更正公告" if a.kind == "correction" else "招标公告"),
        ("项目名称", a.project_name),
        ("发布时间", a.published_at),
    ]
    if a.deadline is not None:
        rows.append(("投标截止时间", a.deadline))
    if a.kind == "correction":
        rows.append(("原公告编号", a.corrects_code or ""))
        rows.append(("更正事项", a.reason))

    trs = "\n".join(
        f'      <tr><th data-field="{html.escape(label)}">{html.escape(label)}</th>'
        f'<td data-value="{html.escape(label)}">{html.escape(value)}</td></tr>'
        for label, value in rows
    )
    clauses_html = ""
    if not a.omit_clauses:
        ps = "\n".join(
            f'      <p class="clause">{html.escape(c)}</p>' for c in a.clauses
        )
        clauses_html = f"""    <section class="body-clauses">
      <h2>正文条款</h2>
{ps}
    </section>
"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{html.escape(a.title)}</title>
</head>
<body>
  <article class="announcement-detail" data-code="{html.escape(a.code)}"
           data-kind="{a.kind}"
           data-published-at="{html.escape(a.published_at)}"
           {f'data-corrects="{html.escape(a.corrects_code)}"' if a.corrects_code else ""}>
    <h1 class="detail-title">{html.escape(a.title)}</h1>
    <table class="detail-table">
{trs}
    </table>
{clauses_html}    <p><a href="list_1.html">返回列表</a></p>
  </article>
</body>
</html>
"""


def _list_page(items: list[Announcement], page: int, total_pages: int) -> str:
    lis = []
    for a in items:
        lis.append(
            f'      <li class="announcement-item" data-code="{html.escape(a.code)}">\n'
            f'        <a class="detail-link" href="{html.escape(a.filename)}">{html.escape(a.title)}</a>\n'
            f'        <span class="published-at">{html.escape(a.published_at)}</span>\n'
            f'      </li>'
        )
    nav_parts = []
    for p in range(1, total_pages + 1):
        if p == page:
            nav_parts.append(f'<span class="page-current">{p}</span>')
        else:
            nav_parts.append(f'<a class="page-link" href="list_{p}.html">{p}</a>')
    next_link = (
        f'<a class="page-next" href="list_{page + 1}.html">下一页</a>'
        if page < total_pages
        else '<span class="page-next-disabled">下一页</span>'
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>市政工程公告站 - 第 {page} 页</title>
</head>
<body>
  <h1>市政工程招投标公告列表（第 {page}/{total_pages} 页）</h1>
  <ul class="announcement-list" data-page="{page}">
{chr(10).join(lis)}
  </ul>
  <nav class="pagination">
    {''.join(nav_parts)}
    {next_link}
  </nav>
</body>
</html>
"""


def write_site(out_dir: Path | str, variant: str = "v1") -> list[Announcement]:
    """把指定变体的示例站写入 out_dir，返回写入的公告数据。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    announcements = VARIANTS[variant]()
    # 列表按发布时间倒序 —— 更正公告天然排在原公告前面，制造页面顺序干扰
    ordered = sorted(announcements, key=lambda a: a.published_at, reverse=True)
    total_pages = (len(ordered) + PAGE_SIZE - 1) // PAGE_SIZE

    for a in ordered:
        (out / a.filename).write_text(_detail_page(a), encoding="utf-8")

    for page in range(1, total_pages + 1):
        chunk = ordered[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        (out / f"list_{page}.html").write_text(
            _list_page(chunk, page, total_pages), encoding="utf-8"
        )

    (out / "index.html").write_text(
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="0; url=list_1.html">'
        "<title>公告站</title></head><body>跳转中…</body></html>\n",
        encoding="utf-8",
    )
    return announcements


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--variant",
        choices=sorted(VARIANTS),
        default="v1",
        help="站点变体：v1 完整数据；v2 段落变化样例；broken-* 故障样例",
    )
    parser.add_argument(
        "--out",
        default=str(OUT_DIR),
        help="输出目录（默认 demo_site/）",
    )
    args = parser.parse_args(argv)
    announcements = write_site(args.out, args.variant)
    total_pages = (len(announcements) + PAGE_SIZE - 1) // PAGE_SIZE
    print(
        f"variant={args.variant}: generated {len(announcements)} detail pages, "
        f"{total_pages} list pages in {args.out}"
    )


if __name__ == "__main__":
    main()
