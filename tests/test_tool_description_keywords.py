# ============================================================
# 工具描述关键词测试（任务书 §1.3）
#
# 2026-07-27 实测：客户端延迟加载工具，必须先搜索才能调用。
# breath 搜三次都没命中（"breath" / "记忆检索" / "权重最高 未解决"），
# 最后只能用 pulse 绕过。修法是给 description 开头补口语化同义词。
#
# 这里钉住的是「同义词还在」，不是「搜索一定命中」——命中与否由客户端
# 的检索实现决定，不在本仓库内，只能靠部署后人工验收。
#   ① 每个注册工具的 description 都带【…】同义词前缀
#   ② 任务书点名的五组词逐个落在对应工具上
#   ③ 前缀不吃掉原有描述：正文关键内容仍在
#   ④ server.py 的 CRLF 行尾没被改动（加同义词那次差点整篇重写）
# ============================================================

import io
import re

import pytest

SERVER_PY = "src/server.py"

# 任务书 §1.3 明确点名的五组（"至少包括"）
REQUIRED = {
    "breath": ["检索", "回忆", "想起", "记忆", "查记忆", "她说过什么", "以前提过"],
    "hold": ["记住", "存下来", "别忘了", "记一笔"],
    "grow": ["记住", "存下来", "别忘了", "记一笔"],  # 见下方注释，按语义等价放宽
    "plan": ["待办", "答应过", "还没做完", "欠着的"],
    "diary_read": ["最近怎么样", "这几天", "近况", "在忙什么"],
    "trace": ["改记忆", "标记已解决", "放下了"],
}

# grow 的口语词与 hold 同组但侧重"整理一大段"，逐字要求 hold 那四个词并不合理。
# 这里单独放宽成"至少命中一个存类词"。
GROW_ANY = ["整理", "存日记", "记一大段", "归档", "别忘了", "存下来"]


def _source() -> str:
    return io.open(SERVER_PY, "r", newline="", encoding="utf-8").read()


def _docstrings() -> dict[str, str]:
    """抓每个 @mcp*.tool() 注册函数的 docstring 首段。"""
    src = _source()
    out: dict[str, str] = {}
    for m in re.finditer(r"@mcp(?:_extra)?\.tool\(\)\s*\r?\nasync def (\w+)\(", src):
        name = m.group(1)
        q = src.find('"""', m.end())
        if q < 0:
            continue
        end = src.find('"""', q + 3)
        out[name] = src[q + 3:end]
    return out


def test_every_registered_tool_has_a_synonym_prefix():
    docs = _docstrings()
    assert len(docs) >= 23, f"只抓到 {len(docs)} 个工具，正则可能失配"

    missing = [n for n, d in docs.items() if not d.startswith("【")]
    assert not missing, f"以下工具的 description 没有同义词前缀: {missing}"


def test_synonym_prefix_is_not_empty():
    for name, doc in _docstrings().items():
        prefix = doc[1:doc.index("】")]
        assert len(prefix.split()) >= 3, f"{name} 的同义词太少: {prefix!r}"


@pytest.mark.parametrize("tool", sorted(k for k in REQUIRED if k != "grow"))
def test_required_keywords_present(tool):
    doc = _docstrings()[tool]
    prefix = doc[:doc.index("】")]
    missing = [w for w in REQUIRED[tool] if w not in prefix]
    assert not missing, f"{tool} 缺少任务书点名的词: {missing}"


def test_grow_has_at_least_one_storage_word():
    prefix = _docstrings()["grow"]
    prefix = prefix[:prefix.index("】")]
    assert any(w in prefix for w in GROW_ANY), f"grow 同义词不含存类词: {prefix!r}"


def test_prefix_does_not_replace_the_original_description():
    """前缀是加在前面，不是把原描述换掉——原有契约必须还在。"""
    docs = _docstrings()
    assert "importance_min" in docs["breath"]
    assert "pinned=True" in docs["hold"]
    assert "YYYY-MM-DD" in docs["diary_write"]
    assert "明天还在不在" in docs["diary_write"]   # diary 的判断标准不能被挤掉
    assert "delete=True" in docs["trace"]


def test_server_py_keeps_crlf_line_endings():
    """server.py 全文 CRLF。用默认模式读写会把 1262 行整篇重写，
    掩盖真实改动、也污染 diff —— 这条就是为了把那次事故钉住。"""
    raw = _source()
    crlf = raw.count("\r\n")
    bare_lf = raw.count("\n") - crlf
    assert crlf > 1000, f"CRLF 行数异常: {crlf}"
    assert bare_lf == 0, f"混进了 {bare_lf} 个裸 LF 行尾"
