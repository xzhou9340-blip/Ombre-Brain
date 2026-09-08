# ============================================================
# 合并入口路由测试（iter 2.3 瘦身）
#
# 24 个工具让客户端的 tool_search 挑不准：搜索默认只回 5 个，一多半被截断，
# 「找不到 breath/hold」就是这么来的。修法是把同域工具收成一个带 action 的
# 入口：diary / letter / reading / speak，对外从 24 降到 14。
#
# 这里钉住的是**合并没有丢功能**：
#   ① 对外暴露的正好是那 14 个（多一个少一个都算回退）
#   ② 每条 action 落到原来那个薄壳函数上，参数一字不改地透传
#   ③ 被摘掉的 anchor / release 只是不注册，函数还在，随时能回退
#   ④ 未知 action 有明确回话，不是静默失败
#
# 上一份测试（test_tool_description_keywords.py）读的是源码文本，钉的是
# 「描述里写了什么」；这一份是真 import server 跑一遍，钉的是「调下去到底
# 走到谁身上」——描述对了但路由接错，只有这一份能拦住。
# ============================================================

import pytest

import server as s


# ------------------------------------------------------------
# 对外暴露的工具集
# ------------------------------------------------------------

EXPOSED = {
    # 原样不动的 10 个
    "breath", "hold", "grow", "trace", "dream", "plan", "pulse", "I",
    "peek", "phone_activity_query",
    # 合并出来的 4 个
    "diary", "letter", "reading", "speak",
}


def _registry() -> dict:
    """复刻启动入口的装配：副集回灌 + 摘除 _DISABLED_TOOLS。

    真正的装配写在 `if __name__ == "__main__"` 里，测试进不去，
    所以这里在一份拷贝上重演同样两步，不动 server 模块的全局状态。
    """
    tools = dict(s.mcp._tool_manager._tools)
    tools.update(s.mcp_extra._tool_manager._tools)
    for dead in s._DISABLED_TOOLS:
        tools.pop(dead, None)
    return tools


def test_exactly_fourteen_tools_are_exposed():
    assert set(_registry()) == EXPOSED


def test_disabled_tools_keep_their_code():
    """摘除只动注册表，不动代码——这是「回退一行注释的事」的前提。"""
    assert s._DISABLED_TOOLS == ("night_fall", "anchor", "release")
    # night_fall 是外部包注册的，本仓库没有它的函数；anchor/release 有
    for name in ("anchor", "release"):
        assert callable(getattr(s, name)), f"{name} 的函数被删了，回退就回不去了"


@pytest.mark.parametrize("shell", [
    "diary_read", "diary_write",
    "letter_read", "letter_write",
    "reading_progress", "reading_text", "reading_search",
    "reading_annotate", "reading_annotations",
    "bark_push", "_speak_voice",
])
def test_merged_shells_still_exist(shell):
    """合并入口调的就是这些薄壳，_with_notice 的通知/日志全靠它们带着。"""
    assert callable(getattr(s, shell))


# ------------------------------------------------------------
# 路由 + 参数透传
#
# 把薄壳换成探针，看每条 action 落到谁身上、收到什么参数。
# 断言写成完整的 kwargs 字典而不是"包含某个键"：合并入口的参数是所有
# action 的并集，最容易犯的错就是把 B 的参数漏传给 A，只查一个键查不出来。
# ------------------------------------------------------------

@pytest.fixture
def probes(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def _probe(tag):
        async def _fn(**kwargs):
            calls.append((tag, kwargs))
            return f"<{tag}>"
        return _fn

    for name in ("diary_read", "diary_write", "letter_read", "letter_write",
                 "reading_progress", "reading_text", "reading_search",
                 "reading_annotate", "reading_annotations",
                 "bark_push", "_speak_voice"):
        monkeypatch.setattr(s, name, _probe(name))
    return calls


@pytest.mark.asyncio
async def test_diary_routes_both_actions(probes):
    assert await s.diary(action="read", days=5) == "<diary_read>"
    assert probes[-1] == ("diary_read", {"days": 5})

    assert await s.diary(action="write", content="出差到周五", date="2026-09-08") == "<diary_write>"
    assert probes[-1] == ("diary_write", {"content": "出差到周五", "date": "2026-09-08"})


@pytest.mark.asyncio
async def test_diary_read_defaults_to_three_days(probes):
    """默认值必须留在合并入口上，不能指望调用方每次都传。"""
    await s.diary(action="read")
    assert probes[-1] == ("diary_read", {"days": 3})


@pytest.mark.asyncio
async def test_letter_routes_both_actions(probes):
    await s.letter(action="read", query="想你", author="ai", limit=3)
    assert probes[-1] == ("letter_read", {
        "query": "想你", "limit": 3, "author": "ai",
        "date_from": "", "date_to": "",
    })

    await s.letter(action="write", author="ai", content="见字如面",
                   title="九月", user_name="小雪", ai_name="克", date="2026-09-08")
    assert probes[-1] == ("letter_write", {
        "author": "ai", "content": "见字如面", "user_name": "小雪",
        "title": "九月", "date": "2026-09-08", "ai_name": "克",
    })


@pytest.mark.asyncio
async def test_letter_read_and_write_do_not_share_date_fields(probes):
    """letter 的 date（write 归属日期）和 date_from/date_to（read 范围）
    同名不同义，串了就会写错日期或搜错范围。"""
    await s.letter(action="read", date_from="2026-01-01", date_to="2026-02-01")
    assert "date" not in probes[-1][1]
    assert probes[-1][1]["date_from"] == "2026-01-01"

    await s.letter(action="write", author="user", content="x", date="2026-03-03")
    assert probes[-1][1]["date"] == "2026-03-03"
    assert "date_from" not in probes[-1][1]


@pytest.mark.asyncio
async def test_reading_routes_all_five_actions(probes):
    await s.reading(action="progress", book_id="b1")
    assert probes[-1] == ("reading_progress", {"book_id": "b1"})

    await s.reading(action="text", book_id="b1", from_seq=10, to_seq=20)
    assert probes[-1] == ("reading_text", {"book_id": "b1", "from_seq": 10, "to_seq": 20})

    await s.reading(action="search", book_id="b1", q="那句话")
    assert probes[-1] == ("reading_search", {"book_id": "b1", "q": "那句话"})

    await s.reading(action="annotate", book_id="b1", quote="原文一句", comment="想说的话")
    assert probes[-1] == ("reading_annotate",
                          {"book_id": "b1", "quote": "原文一句", "comment": "想说的话"})

    await s.reading(action="annotations", book_id="b1", reply_to="a1", reply_text="回你一句")
    assert probes[-1] == ("reading_annotations",
                          {"book_id": "b1", "reply_to": "a1", "reply_text": "回你一句"})


@pytest.mark.asyncio
async def test_reading_progress_without_book_id_lists_the_shelf(probes):
    """不传 book_id = 列书架，是 reading_progress 的既有契约，合并后不能丢。"""
    await s.reading(action="progress")
    assert probes[-1] == ("reading_progress", {"book_id": ""})


@pytest.mark.asyncio
async def test_speak_routes_voice_and_push(probes):
    await s.speak(action="voice", text="晚安", stability=0.4, style=0.8, speed=1.1)
    assert probes[-1] == ("_speak_voice",
                          {"text": "晚安", "stability": 0.4, "style": 0.8, "speed": 1.1})

    await s.speak(action="push", title="克", body="记得吃饭", icon="https://x/i.png")
    assert probes[-1] == ("bark_push",
                          {"title": "克", "body": "记得吃饭", "icon": "https://x/i.png"})


@pytest.mark.asyncio
async def test_speak_voice_keeps_none_defaults(probes):
    """stability/style/speed 不传时必须是 None，不能被合并入口填成 0——
    speak 内部靠 None 回落到 0.34/0.84/1.2，填 0 会把声音改掉。"""
    await s.speak(action="voice", text="嗯")
    assert probes[-1][1] == {"text": "嗯", "stability": None, "style": None, "speed": None}


@pytest.mark.asyncio
async def test_push_keeps_the_icon_url(probes):
    """bark_push 的图标 URL 是合并时最容易顺手漏掉的参数。"""
    await s.speak(action="push", title="t", body="b", icon="https://example.com/i.png")
    assert probes[-1][1]["icon"] == "https://example.com/i.png"


@pytest.mark.parametrize("tool,valid", [
    ("diary", "read"),
    ("letter", "write"),
    ("reading", "progress"),
    ("speak", "voice"),
])
@pytest.mark.asyncio
async def test_unknown_action_says_what_is_available(tool, valid, probes):
    """未知 action 不能静默失败——模型只有从回话里看到可选值才改得对。"""
    out = await getattr(s, tool)(action="不存在的动作")
    assert "未知 action" in out
    assert valid in out
    assert not probes, "未知 action 不该调到任何薄壳"
