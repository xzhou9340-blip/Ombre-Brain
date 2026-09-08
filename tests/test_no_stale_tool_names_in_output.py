# ============================================================
# 工具返回串里不许出现已下线的工具名
#
# iter 2.3 把 diary_read / letter_write / reading_* / bark_push 合进了
# 带 action 的入口。docstring 和文档都改了，但**工具自己返回给模型的那些
# 指路文案**漏了 9 处，例如：
#
#   reading(action="progress") 的返回末尾："用 reading_progress(book_id=...) 看…"
#   breath 搜空时："…信件用 letter_read。"
#   speak(action="push") 没配 BARK_KEY 时："❌ bark_push 未配置…"
#
# 这比 docstring 写错更隐蔽也更有害：docstring 至少还在工具列表里，
# 而这些字符串是模型**运行时**读到的下一步指令，照着调必然扑空——
# 跟「提示词里写着一个不存在的工具」是同一个失败模式，只是藏在代码里。
# 部署后靠人肉发现，等于不会发现。
#
# 这里扫的是「会返回给模型看的字符串」，刻意排除两类：
#   · rt.mark_op("diary_read") —— tool_stats 的统计口径，合并时明确承诺不变，
#     改了会把历史数据割断。它必须继续用旧名字。
#   · logger.* 的内部日志 —— 给人看的，用内部函数名反而更好定位。
# ============================================================

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 已从 MCP 注册表下线、只剩内部函数的名字。
# anchor / release / night_fall 不列入：它们同时是普通名词和字段名
# （set_anchor、anchor 字段、release the lock），逐字匹配噪声太大。
RETIRED = [
    "diary_read", "diary_write",
    "letter_read", "letter_write",
    "reading_progress", "reading_text", "reading_search",
    "reading_annotate", "reading_annotations",
    "bark_push",
]

SCAN_DIRS = ["src/tools", "src/server.py"]


def _python_files():
    for entry in SCAN_DIRS:
        p = ROOT / entry
        if p.is_file():
            yield p
        else:
            yield from sorted(p.rglob("*.py"))


def _docstring_nodes(tree):
    """模块 / 类 / 函数的 docstring 节点——docstring 由别的测试管，这里跳过。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _exempt_nodes(tree):
    """豁免三类，都不是给模型看的：

    · rt.mark_op("diary_read") 与 _with_notice(op="diary_read") 的 op 名
      —— 同一个统计口径的两个写法。合并入口调的是原薄壳，薄壳里的 op= 保持
      旧名字正是「tool_stats 口径不变」这个承诺的实现方式，改了就割断历史数据。
    · logger.* 的日志文案 —— 给人看的，用内部函数名反而更好定位。
    """
    out = set()
    # op="..." 关键字实参
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "op" \
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            out.add(id(node.value))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = ""
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        is_logger = name in {"debug", "info", "warning", "error", "exception", "critical"}
        if name == "mark_op" or is_logger:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    out.add(id(sub))
    return out


def _model_facing_strings(path):
    """返回 [(行号, 字符串)]：排除 docstring / mark_op / logger 之后剩下的字面量。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_nodes(tree) | _exempt_nodes(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            found.append((node.lineno, node.value))
    return found


@pytest.mark.parametrize("path", list(_python_files()), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_retired_tool_name_in_model_facing_strings(path):
    hits = []
    for lineno, text in _model_facing_strings(path):
        for dead in RETIRED:
            if dead in text:
                hits.append(f"{path.relative_to(ROOT)}:{lineno} 出现 {dead!r} → {text.strip()[:70]!r}")
    assert not hits, (
        "工具返回串里还写着已下线的工具名，模型照着调会扑空。改成 action 写法：\n  "
        + "\n  ".join(hits)
    )


def test_mark_op_still_uses_the_original_names():
    """反向钉住：统计口径不许跟着改名，否则 tool_stats 的历史数据被割断。

    上面那条测试很容易被「全局替换一下旧工具名」草率满足，那会顺手把这些
    也改掉。两条测试互为夹逼：指路文案必须换新写法，统计口径必须留旧名字。
    """
    diary = (ROOT / "src/tools/diary/core.py").read_text(encoding="utf-8")
    assert 'rt.mark_op("diary_read")' in diary
    assert 'rt.mark_op("diary_write")' in diary

    server = (ROOT / "src/server.py").read_text(encoding="utf-8")
    for op in ("bark_push", "letter_read", "letter_write", "diary_read",
               "diary_write", "reading_progress", "reading_text",
               "reading_search", "reading_annotate", "reading_annotations"):
        assert f'op="{op}"' in server, f"{op} 的统计口径被改掉了"
