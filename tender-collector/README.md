# 市政工程公告采集与条款检索服务

面向招投标专员的公告采集 API：提交市政工程公告站的**列表页地址**，服务自动遍历
所有分页与详情页，将**更正公告按其引用的原公告编号归并**到同一项目，并**按发布
时间**（而非页面排列顺序）确定当前生效的投标截止时间，标记是否已过期。

同时，服务从每个详情页抽取**"正文条款"段落原文**，按**入口目录**（提交采集的
列表页地址）建立可检索索引：招标资料员可以直接检索"夜间施工"等条款——中文
两字词与连续短语均按原文命中——返回原公告编号、命中段落原文与详情链接，
便于核对原文后安排标书。

## 核心规则

1. **分页遍历**：从提交的列表页出发，沿 `nav.pagination` 链接抓取全部列表页
   （去重、防环、限量），收集所有详情页链接后逐页抓取。
2. **更正归并**：更正公告通过"原公告编号"归并到对应原公告所在项目；引用悬空
   （原公告不存在/不是原公告）视为数据错误，本次采集整体失败。
3. **生效截止时间**：同一项目内按**发布时间**排序，取发布时间最新且声明了
   截止时间的公告；发布时间相同时按公告编号确定次序，结果与页面顺序完全无关。
   若最新更正公告未给出截止时间（例如只改联系人），沿用上一份带截止时间的公告。
4. **过期判定**：`is_expired = 截止时间 <= 当前时间`（截止时间按服务器本地时区
   的 naive 时间比较，与公告页 `YYYY-MM-DD HH:MM` 格式一致）。
5. **正文条款抽取**：从详情页 `section.body-clauses` 内的每个 `p.clause` 抽取
   一段条款原文，按文档顺序编号（从 1 开始）。页面缺失条款容器或没有任何条款
   段落时，本次采集整体失败。
6. **按目录建检索索引**：段落原文按"可检索字符连续段"切分为二元语法 token，
   写入 SQLite FTS5 倒排索引（`app/indexing.py`）。检索时查询做同样切分并以
   短语方式 MATCH，**经索引定位而非逐篇扫描**：
   - 中文两字词（如"夜间"）按原文精确命中；
   - 连续短语（如"夜间施工"）按原文连续命中——原文被标点/空白隔开的相同
     字符序列（如"夜间。施工"）**不会**误命中；
   - 命中返回：公告编号、项目名称、命中段落（序号 + 原文）、详情页链接、
     所属入口目录。
7. **目录级原子切换**：公告/段落/索引以入口目录（`catalog_url`）分区。复采某
   目录成功后，在**单个事务**内整体替换该目录的公告、条款段落与检索索引——
   该目录的正文检索结果与截止时间查询**同时切换**；被撤下或改写的段落不再
   命中，其他目录保持原样。
8. **失败保留**：任一列表页/详情页读取失败（网络错误、非 2xx、字段或条款
   缺失）、归并校验失败或索引写入失败时，本次采集不写入任何数据（事务整体
   回滚），**两种查询都完整保留上次成功采集结果**；失败原因记入
   `collection_runs` 表并通过 HTTP 422 返回。
9. **幂等采集**：同一目录重复采集只替换不产生重复记录。

## 目录结构

```
tender-collector/
├── app/                    # FastAPI 应用
│   ├── main.py             # 路由：/api/collect、/api/projects、/api/search、/api/collections
│   ├── service.py          # 采集编排 + 目录级事务边界 + 检索归组
│   ├── scraper.py          # httpx + BeautifulSoup 抓取/解析（含正文条款抽取）
│   ├── indexing.py         # 中文二元语法分词 + FTS5 检索串构造
│   ├── merge.py            # 更正归并与生效截止时间判定（纯函数，按目录分组）
│   ├── database.py         # SQLite 连接、建表/迁移、目录级替换、FTS5 检索
│   ├── models.py           # Pydantic 模型
│   └── config.py           # 环境变量配置
├── demo_site/              # 生成的本地分页 HTML 示例公告站（静态文件，v1）
├── scripts/generate_demo_site.py  # 示例站生成器（含段落变化/故障变体，数据可复现）
├── tests/                  # 端到端 + 归并/分词单元测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
# 1. 生成示例公告站静态文件（已随仓库提供，可重复生成）
python3 scripts/generate_demo_site.py

# 2. 启动：API 在 :8000，示例公告站在 :8001
docker compose up --build -d

# 3. 提交列表页地址触发采集
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://demo-site/list_1.html"}'

# 4. 查询项目当前截止时间
curl http://localhost:8000/api/projects

# 5. 检索正文条款（如"夜间施工"）
curl 'http://localhost:8000/api/search?q=夜间施工'
```

容器内 API 通过 compose 网络访问示例站（主机名 `demo-site`）；从宿主机浏览
示例站则打开 <http://localhost:8001/list_1.html>。SQLite 数据存于命名卷
`tender-data`（容器内 `/data/tenders.db`）。全部接口离线可用，不依赖外网。

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除采集数据
```

## 本地开发运行（不使用 Docker）

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/generate_demo_site.py

# 终端 A：示例公告站
python3 -m http.server 8001 --directory demo_site

# 终端 B：API（数据库默认在 ./data/tenders.db）
uvicorn app.main:app --reload --port 8000

# 终端 C：测试
pytest -q

# 采集
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url': "http://127.0.0.1:8001/list_1.html"}'
```

交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>

## API 说明

### `POST /api/collect` — 提交列表页，触发采集

请求：

```json
{ "list_url": "http://demo-site/list_1.html" }
```

成功响应（200）：

```json
{
  "status": "success",
  "source_url": "http://demo-site/list_1.html",
  "collected": 10,
  "paragraphs_indexed": 15,
  "projects": [
    {
      "project_name": "城北污水处理厂扩建工程",
      "current_deadline": "2026-11-30 10:00",
      "deadline_source_code": "SZ-2026-002-G2",
      "deadline_source_url": "http://demo-site/SZ-2026-002-G2.html",
      "catalog_url": "http://demo-site/list_1.html",
      "is_expired": false
    }
  ]
}
```

失败响应（422，任一页面不可读/条款缺失/数据无法归并/索引写入失败）：

```json
{ "detail": "读取页面失败 http://demo-site/SZ-2026-002-G2.html: HTTP 404" }
```

### `GET /api/projects` — 查询全部项目的当前截止时间

返回字段：项目名称、当前生效截止时间、截止时间来源公告编号、来源链接、
所属入口目录（`catalog_url`）、是否过期。多个目录的项目合并返回，可按
`catalog_url` 区分来源。

### `GET /api/search?q=...&catalog_url=...` — 检索正文条款

- `q`（必填）：检索词。中文两字词（如 `夜间`）与连续短语（如 `夜间施工`）
  均按原文命中；不含可检索字符的查询返回空结果。
- `catalog_url`（可选）：只检索该入口目录（提交采集时的列表页地址）。

响应（命中按公告归组，含命中段落原文与详情链接）：

```json
{
  "query": "夜间施工",
  "total": 1,
  "hits": [
    {
      "code": "SZ-2026-001",
      "title": "滨河路市政改造工程施工招标公告",
      "project_name": "滨河路市政改造工程",
      "published_at": "2026-10-20 09:00",
      "catalog_url": "http://demo-site/list_1.html",
      "detail_url": "http://demo-site/SZ-2026-001.html",
      "matches": [
        {
          "paragraph_index": 2,
          "text": "因滨河路沿线居民密集，如需夜间施工，施工单位须提前三日向监理单位报备并公告周边居民。"
        }
      ]
    }
  ]
}
```

### `GET /api/collections?limit=20` — 采集运行历史

包含每次采集的成功/失败状态、采集条数与错误信息。

## 示例站数据设计

`demo_site/` 含 5 个项目、10 条公告、3 个列表页，列表按发布时间**倒序**排列
（更正公告排在原公告之前），专门用于验证归并不受页面顺序影响：

| 项目 | 场景 | 生效截止时间来源 |
| --- | --- | --- |
| 滨河路市政改造工程 | 一次更正延期 | 更正公告 SZ-2026-001-G1 |
| 城北污水处理厂扩建工程 | 两次更正，取最新一次 | 第二次更正 SZ-2026-002-G2 |
| 东风桥加固维修工程 | 无更正，已过期 | 原公告 SZ-2026-003 |
| 高新区雨污分流管网工程 | 更正不改截止时间 | 原公告 SZ-2026-004 |
| 湖滨公园配套市政道路工程 | 原公告在末页、更正在首页（跨页乱序） | 更正公告 SZ-2026-005-G1 |

条款检索样例（v1 数据）：

- `夜间施工` 命中 SZ-2026-001、SZ-2026-001-G1、SZ-2026-002、SZ-2026-002-G1；
  SZ-2026-003 原文为"夜间。施工"（被标点隔开），**不**命中；
- `夜间`（两字词）额外命中 SZ-2026-001 第三段（"夜间作业"）与 SZ-2026-003；
- `夜间作业` 只命中真正连续出现的 SZ-2026-001 第三段（"夜间施工作业"不算）。

### 段落变化与故障样例（站点变体）

生成器支持 `--variant`，用于本地复现"复采切换"与"失败保留"：

```bash
# v2 段落变化样例：SZ-2026-001 的"夜间施工"条款被改写为"夜间作业"，
# SZ-2026-001-G1 的报备条款段被撤下（且项目 A 再次延期至 2026-12-02），
# SZ-2026-003 的"半幅通行"段被撤下
python3 scripts/generate_demo_site.py --variant v2
# 重新采集同一列表页后：该目录的正文检索与截止时间查询同时切换，
# 被撤下/改写的段落不再命中

# broken-clauses 故障样例：SZ-2026-002-G1 详情页缺失正文条款容器
python3 scripts/generate_demo_site.py --variant broken-clauses
# 采集返回 422，此前成功采集的检索与截止时间结果完整保留

# broken-ref 故障样例：SZ-2026-002-G2 引用不存在的原公告编号 SZ-2026-099
python3 scripts/generate_demo_site.py --variant broken-ref

# 恢复默认完整数据
python3 scripts/generate_demo_site.py            # 等价于 --variant v1
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TENDER_DB_PATH` | 本地 `./data/tenders.db`；容器内 `/data/tenders.db` | SQLite 文件路径 |
| `TENDER_HTTP_TIMEOUT` | `15` | 单个页面请求超时（秒） |
| `TENDER_MAX_LIST_PAGES` | `200` | 单次采集最多遍历的列表页数（防环） |
| `TENDER_USER_AGENT` | `TenderCollector/1.0 ...` | 请求 UA |

## 数据迁移说明

首次启动时自动建表；若检测到旧版（无 `catalog_url` 列）的 `announcements`
表，会自动迁移：公告记录完整保留，`catalog_url` 取最近一次成功采集的列表页
地址。旧数据没有条款段落索引，重新采集对应目录后即可检索。

## 解析约定（接入真实站点时）

解析器依赖以下稳定选择器，适配其他站点时调整 `app/scraper.py` 即可：

- 列表页：`ul.announcement-list a.detail-link` 为详情链接，
  `nav.pagination a` 为分页链接；
- 详情页：`article.announcement-detail` 容器，支持
  `data-code` / `data-kind` / `data-published-at` / `data-corrects` 属性，
  以及 `table.detail-table` 内 `th[data-field="字段名"] + td` 表格字段
  （公告编号、公告类型、项目名称、发布时间、投标截止时间、原公告编号）；
- 正文条款：`section.body-clauses` 容器内的每个 `p.clause` 为一段条款原文，
  按文档顺序编号。
