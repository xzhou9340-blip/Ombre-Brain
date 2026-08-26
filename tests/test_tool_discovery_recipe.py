"""
========================================
test_tool_discovery_recipe.py — 「找不到 breath」的取工具配方
========================================

背景：客户端延迟加载工具，关键词搜索默认只回 5 个，而这里有 24 个。
breath 反复搜不到，根因是**排名截断**，不是描述写得不好——之前一路在加同义词
（见 test_tool_description_keywords.py），治标不治本，因为排名不在本仓库手里。

2026-08-26 实测结论：
  · tool_search(query="select:breath,hold,peek")            → 空手
  · tool_search(query="select:mcp__ombre__breath,...")      → 点名几个回几个
`select:` 不走排名，是唯一确定性的取法；早先「别用 select:」的结论是拿**短名**
测出来的，错的是名字写短了，不是 select: 本身。

本文件钉住这条配方在两处提示词里都在、且不自相矛盾：
  ① server.py 的 OMBRE_CONNECTOR_INSTRUCTIONS（握手时随 initialize 下发）
  ② docs/CLAUDE_PROMPT.md（用户手动粘进项目说明的完整版）
  ③ 配方里点名的工具**真的注册过**——名字打错会静默少回一个，没人看得出来
  ④ 提示词里写的工具总数与实际注册数一致
========================================
"""

import io
import re

import pytest

SERVER_PY = "src/server.py"
PROMPT_MD = "docs/CLAUDE_PROMPT.md"

# 连接器前缀。FastMCP 侧固定不了它（取决于客户端怎么命名这个连接器），
# 但绝大多数安装都是 ombre，提示词按这个给，并附带前缀不对时的兜底路径。
TOOL_PREFIX = "mcp__ombre__"

# night_fall 由 register_night_fall() 在外部包里注册，不带 @mcp.tool() 装饰器，
# 抓不到，单独计入。
_EXTERNALLY_REGISTERED = {"night_fall"}


def _read(path: str) -> str:
    return io.open(path, "r", newline="", encoding="utf-8").read()


def _registered_tools() -> set[str]:
    src = _read(SERVER_PY)
    names = set(
        re.findall(r"@mcp(?:_extra)?\.tool\(\)\s*\r?\nasync def (\w+)\(", src)
    )
    assert len(names) >= 23, f"只抓到 {len(names)} 个工具，正则可能失配"
    return names | _EXTERNALLY_REGISTERED


def _instructions() -> str:
    src = _read(SERVER_PY)
    m = re.search(
        r'OMBRE_CONNECTOR_INSTRUCTIONS = """(.*?)"""', src, re.S
    )
    assert m, "没抓到 OMBRE_CONNECTOR_INSTRUCTIONS"
    return m.group(1)


def _select_recipe_names(text: str) -> list[str]:
    """把文本里所有 select: 配方点名的工具短名抠出来。"""
    out: list[str] = []
    for chunk in re.findall(r"select:([A-Za-z0-9_,]+)", text):
        for full in chunk.split(","):
            full = full.strip()
            if full.startswith(TOOL_PREFIX):
                out.append(full[len(TOOL_PREFIX):])
    return out


@pytest.mark.parametrize("path", [SERVER_PY, PROMPT_MD])
def test_select_recipe_uses_fully_qualified_names(path):
    """两处提示词都必须给出带前缀的 select: 配方。"""
    text = _instructions() if path == SERVER_PY else _read(path)
    assert f"select:{TOOL_PREFIX}breath" in text, (
        f"{path} 里没有带前缀的 select: 配方——只有它是不走排名的确定性取法"
    )


@pytest.mark.parametrize("path", [SERVER_PY, PROMPT_MD])
def test_select_recipe_names_are_really_registered(path):
    """配方点名的每个工具都得真的注册过：打错一个字就静默少回一个。"""
    text = _instructions() if path == SERVER_PY else _read(path)
    registered = _registered_tools()
    named = _select_recipe_names(text)
    assert named, f"{path} 的 select: 配方一个工具都没点到"
    unknown = sorted({n for n in named if n not in registered})
    assert not unknown, f"{path} 的 select: 配方点了不存在的工具: {unknown}"


@pytest.mark.parametrize("path", [SERVER_PY, PROMPT_MD])
def test_select_recipe_covers_the_window_open_essentials(path):
    """开窗必需的几个不能漏——漏了就等于把「先查再说」这条规矩废掉。"""
    text = _instructions() if path == SERVER_PY else _read(path)
    named = set(_select_recipe_names(text))
    essentials = {"breath", "hold", "diary_read", "peek", "phone_activity_query"}
    missing = sorted(essentials - named)
    assert not missing, f"{path} 的 select: 配方漏了开窗必需工具: {missing}"


@pytest.mark.parametrize("path", [SERVER_PY, PROMPT_MD])
def test_no_blanket_ban_on_select(path):
    """不能再出现「别用 select:」这类一刀切结论——它把唯一可靠的取法堵死了。"""
    text = _instructions() if path == SERVER_PY else _read(path)
    for banned in ("别用 select:", "不要用 `select:`", "不要用 select:"):
        assert banned not in text, (
            f"{path} 里还留着「{banned}」：select: 本身是对的，"
            f"失败的是写短名，别再一刀切禁掉"
        )


@pytest.mark.parametrize("path", [SERVER_PY, PROMPT_MD])
def test_short_name_pitfall_is_still_documented(path):
    """但短名会空手这个坑必须留着，否则下一个人还会踩。"""
    text = _instructions() if path == SERVER_PY else _read(path)
    assert "select:breath" in text, f"{path} 没写「select:breath 会空手」这个坑"


@pytest.mark.parametrize("path", [SERVER_PY, PROMPT_MD])
def test_stated_tool_count_matches_reality(path):
    """提示词里写的工具总数要和实际注册数对上：数字飘了，整段话就不可信了。"""
    text = _instructions() if path == SERVER_PY else _read(path)
    actual = len(_registered_tools())
    stated = re.findall(r"(\d+)\s*个工具", text)
    assert stated, f"{path} 里没写工具总数"
    for s in stated:
        assert int(s) == actual, (
            f"{path} 写着 {s} 个工具，实际注册 {actual} 个——"
            f"改完工具记得同步这个数"
        )


def test_keyword_fallback_still_present():
    """前缀不是 ombre 的客户端只能靠关键词搜索兜底，这条退路不能删。"""
    for text in (_instructions(), _read(PROMPT_MD)):
        assert "max_results=30" in text, "关键词兜底那行没了"
