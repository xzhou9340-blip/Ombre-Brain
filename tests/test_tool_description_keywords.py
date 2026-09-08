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
    "diary": ["最近怎么样", "这几天", "近况", "在忙什么", "记一下今天", "日常进展"],
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


# ------------------------------------------------------------
# iter 2.3 瘦身：24 -> 14
#
# 原来这里断言 `len(docs) >= 23`。合并入口之后个数会掉，光看个数既拦不住
# 「谁被误删了」也拦不住「谁被误加回去了」，所以改成钉住名单本身。
#
# server.py 里带 @mcp*.tool() 的一共 16 个；anchor / release 装饰器留着
# （代码不动、随时能回退），但在启动入口处按 _DISABLED_TOOLS 从注册表 del
# 掉，所以实际对外是 14 个。night_fall 由外部包注册，不在本文件源码里。
# ------------------------------------------------------------

# 源码里仍带 @mcp*.tool() 装饰器的
DECORATED = {
    "breath", "hold", "grow", "trace", "dream",
    "peek", "phone_activity_query", "pulse", "plan", "I",
    "anchor", "release",                      # 装饰器留着，启动时才摘
    "diary", "letter", "reading", "speak",    # 4 个合并入口
}
# 合并进上面 4 个入口、不再单独注册的薄壳
MERGED_AWAY = {
    "diary_read", "diary_write",
    "letter_read", "letter_write",
    "reading_progress", "reading_text", "reading_search",
    "reading_annotate", "reading_annotations",
    "bark_push",
}


def test_registered_tool_set_is_exactly_the_slimmed_list():
    names = set(_docstrings())
    assert names == DECORATED, (
        f"多出: {sorted(names - DECORATED)} / 少了: {sorted(DECORATED - names)}"
    )


def test_merged_shells_are_no_longer_registered():
    """薄壳函数必须还在源码里，但不能再挂 @mcp*.tool()——挂上就等于瘦身白做。"""
    src = _source()
    names = set(_docstrings())
    for shell in sorted(MERGED_AWAY):
        assert f"async def {shell}(" in src, f"{shell} 的函数被删了，合并入口会调空"
        assert shell not in names, f"{shell} 又被单独注册了，工具数会涨回去"
    # speak 与合并入口重名，内层改成了 _speak_voice
    assert "async def _speak_voice(" in src


def test_disabled_tools_are_pinned():
    """摘除名单是回退开关：改它要有意识，别顺手动。"""
    src = _source()
    assert '_DISABLED_TOOLS = ("night_fall", "anchor", "release")' in src
    assert "del mcp._tool_manager._tools[_dead]" in src


def test_every_registered_tool_has_a_synonym_prefix():
    docs = _docstrings()

    missing = [n for n, d in docs.items() if not d.startswith("【")]
    assert not missing, f"以下工具的 description 没有同义词前缀: {missing}"


# 合并之后，被摘掉的工具的口语词只剩合并入口这一处出口。
# 少一个词，用户那句话就再也搜不到对应的工具了——这里逐个钉住。
MERGED_PREFIX_WORDS = {
    "diary": ["最近怎么样", "这几天", "近况", "在忙什么", "交接班",   # 原 diary_read
              "记一下今天", "最近在忙", "日常进展", "正在发生"],       # 原 diary_write
    "letter": ["读信", "看以前的信", "翻旧信",                         # 原 letter_read
               "写信", "留一封信", "给她写", "给下一个我"],            # 原 letter_write
    "reading": ["读到哪了", "书架", "在读什么书",     # 原 reading_progress
                "看原文", "回看", "正文", "段落",     # 原 reading_text
                "书里搜", "找那句话", "全文检索",     # 原 reading_search
                "划线", "批注", "标注",               # 原 reading_annotate
                "看批注", "回批注"],                  # 原 reading_annotations
    "speak": ["发语音", "说话", "念给她听", "语音消息", "配音",        # 原 speak
              "推送", "发通知", "提醒她", "手机弹窗"],                 # 原 bark_push
}

MERGED_ACTIONS = {
    "diary": ["read", "write"],
    "letter": ["read", "write"],
    "reading": ["progress", "text", "search", "annotate", "annotations"],
    "speak": ["voice", "push"],
}


@pytest.mark.parametrize("tool", sorted(MERGED_PREFIX_WORDS))
def test_merged_entry_keeps_every_swallowed_synonym(tool):
    prefix = _docstrings()[tool]
    prefix = prefix[:prefix.index("】")]
    missing = [w for w in MERGED_PREFIX_WORDS[tool] if w not in prefix]
    assert not missing, f"{tool} 吞掉了原工具的口语词: {missing}"


@pytest.mark.parametrize("tool", sorted(MERGED_ACTIONS))
def test_merged_entry_documents_every_action(tool):
    """action 是唯一的分发依据，描述里没写清就等于这个功能消失了。"""
    doc = _docstrings()[tool]
    for act in MERGED_ACTIONS[tool]:
        assert f'action="{act}"' in doc, f"{tool} 的描述没写 action={act!r}"


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
    assert "YYYY-MM-DD" in docs["diary"]
    assert "明天还在不在" in docs["diary"]         # diary 的判断标准不能被挤掉
    assert "delete=True" in docs["trace"]


# ------------------------------------------------------------
# 2026-07-29 人工验收发现的选错工具（补钉）
#
# 用户实测：问「我最近怎么样」不调任何工具，直接拿上下文里已有的内容总结；
# 「回忆」调到了 dream；日常进展和一句话事实分不清 diary_write / hold。
# 光有同义词不够——三个工具的口语词天然重叠，还得在描述里写清彼此的边界，
# 尤其是「刚调过 X 不等于读过 Y」这句：它治的是「不调工具」而不是「调错工具」。
# ------------------------------------------------------------

def test_time_window_tools_declare_their_boundaries():
    """breath(全库) / dream(48h 窗口) / diary_read(最近几天) 必须互相指路。"""
    docs = _docstrings()

    assert "全库翻找" in docs["breath"]
    assert "dream" in docs["breath"] and 'diary(action="read")' in docs["breath"]

    assert "不含 diary" in docs["dream"]
    assert 'diary(action="read")' in docs["dream"]
    assert "breath" in docs["dream"]


def test_diary_read_defers_to_the_session_start_hook():
    """连贯性优先于「多调工具」——但只在钩子真的存在的客户端上。

    2026-07-29 第一版写的是「被问到近况先调这个」——逼模型多调一次工具。
    用户否掉了这个方向：要的是开窗即在场，而不是每次现查。所以 diary 最近
    3 天改由 SessionStart 钩子带进来，本工具退成「钩子没覆盖到时才用」。

    2026-07-31 补丁：上面那条只对了一半。手机 App / 网页版**根本没有
    SessionStart 钩子**，「=== 最近几天 ===」那一段永远不会出现，于是
    「钩子会带给我」变成了「谁都没带」——用户实测里模型全程不调 diary_read，
    要她自己打出「自己看 dairy」才去读。所以描述必须分两种客户端说清楚：
    看得见那一段就别重复调（原方向不变），看不见就主动调（新增的那一半）。
    这条同时钉住两个方向，缺一边都算回退。"""
    doc = _docstrings()["diary"]

    assert "唯一的读取路径" in doc
    assert "SessionStart" in doc
    # 有钩子时不重复调 —— 2026-07-29 用户定的方向，不许掉回去
    assert "就别重复调本工具" in doc
    assert "先调这个" not in doc, "又掉回「逼模型多调工具」的写法了"
    # 没钩子时必须主动调 —— 2026-07-31 补的另一半
    assert "手机 App" in doc, "没写清手机端没有钩子，模型会以为钩子总在"
    assert "开窗第一件事就调它" in doc, "缺了「无钩子客户端要主动调」这一半"


def test_hold_and_diary_write_point_at_each_other():
    """边界此前只写在 diary_write 一侧，单向的指路只能挡住一个方向。

    iter 2.3：diary_write 并进了 diary(action="write")，指路的写法要跟着换成
    模型真能照着调的形式——写旧工具名等于指到一个不存在的工具上。"""
    docs = _docstrings()

    assert 'diary(action="write")' in docs["hold"]
    assert "不要拿 diary 替代 hold" in docs["diary"]


def test_server_py_keeps_crlf_line_endings():
    """server.py 全文 CRLF。用默认模式读写会把 1262 行整篇重写，
    掩盖真实改动、也污染 diff —— 这条就是为了把那次事故钉住。"""
    raw = _source()
    crlf = raw.count("\r\n")
    bare_lf = raw.count("\n") - crlf
    assert crlf > 1000, f"CRLF 行数异常: {crlf}"
    assert bare_lf == 0, f"混进了 {bare_lf} 个裸 LF 行尾"
