"""正文条款原文的二元语法（bigram）分词与 FTS5 检索串构造。

设计目标：中文没有天然词边界，直接按"词"索引容易漏命中。这里把段落原文
切成"可检索字符连续段"（CJK 表意文字 + ASCII 字母数字），段内生成重叠的
二元语法 token，段与段之间插入边界 token，再交给 SQLite FTS5 建倒排索引。
查询做完全相同的切分，并以 FTS5 短语（phrase）方式匹配，从而保证：

- 中文两字词（如"夜间"）按原文精确命中；
- 连续短语（如"夜间施工"）按原文连续命中——原文被标点/空白隔开的相同
  字符序列（如"夜间。施工"）不会误命中；
- 检索通过 FTS5 索引定位，不逐篇扫描段落。

token 流只含 CJK 与 ASCII 字母数字，因此构造 MATCH 串时无需额外转义。
"""
from __future__ import annotations

# 段间边界 token：纯 ASCII 字母，长度大于 2，正文 token（单字/双字）不可能与之冲突
SEPARATOR = "zzrunboundaryzz"


def _is_cjk(ch: str) -> bool:
    return (
        "㐀" <= ch <= "䶿"  # CJK 扩展 A (U+3400–U+4DBF)
        or "一" <= ch <= "鿿"  # CJK 基本区 (U+4E00–U+9FFF)
        or "豈" <= ch <= "﫿"  # CJK 兼容表意文字 (U+F900–U+FAFF)
    )


def _is_searchable(ch: str) -> bool:
    return _is_cjk(ch) or (ch.isascii() and ch.isalnum())


def _runs(text: str) -> list[str]:
    """把文本切成可检索字符的极大连续段（忽略标点、空白等）。"""
    runs: list[str] = []
    current: list[str] = []
    for ch in text:
        if _is_searchable(ch):
            current.append(ch)
        elif current:
            runs.append("".join(current))
            current = []
    if current:
        runs.append("".join(current))
    return runs


def tokenize(text: str) -> list[str]:
    """把一段原文/查询切成 FTS5 token 序列。

    每个连续段：长度为 1 时产出该单字 token，否则产出全部重叠二元语法；
    段间插入边界 token，保证跨段的"伪连续"不会被短语查询误命中。
    """
    tokens: list[str] = []
    for run in _runs(text):
        if tokens:
            tokens.append(SEPARATOR)
        run = run.lower()
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def document_tokens(text: str) -> str:
    """段落原文 -> 写入 FTS5 索引的 token 流。"""
    return " ".join(tokenize(text))


def query_to_match(query: str) -> str | None:
    """用户查询 -> FTS5 MATCH 短语串；查询不含可检索字符时返回 None。"""
    tokens = tokenize(query)
    if not tokens:
        return None
    return '"' + " ".join(tokens) + '"'
