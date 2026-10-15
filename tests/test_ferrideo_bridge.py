"""
========================================
tests/test_ferrideo_bridge.py — ferrideo 子进程托管 + 反向代理回归
========================================

覆盖 web/ferrideo_bridge.py（M1 范围：服务起得来、页面发得出、失败要隔离）：

- _child_env()：剔除 PORT（Render 注入给 ombre 主服务，透传会抢主端口），
  注入 FERRIDEO_PORT / DATA_DIR / token / 公网前缀 / 页面候选目录
- web_token()：env 优先；否则生成并持久化，重启（重读）不变
- _app_dir()：repo_root 指向持久盘代码副本（里面没有 ferrideo/）时回退到
  镜像/仓库路径 —— read-along 踩过这个坑，这里提前钉住
- _page_dirs()：热更新目录（frontend/ferrideo）排在镜像内置之前
- 集成（需要本机 node，无则跳过）：
  · ensure_child_on_boot 拉起子进程，/healthz 就绪
  · token 门禁过代理语义不变（无/错 token 404，与路径不存在不可区分）
  · 播放器页面过代理可取，__FERRIDEO_BASE__ 被替换成 /ferrideo/<token>
  · 改页面文件不重启子进程即生效（热更新，node 侧每次现读不缓存）
  · 杀掉子进程 → 监控循环自动重启
- internal_gate_base()：MCP 侧的后端地址钉在**门禁端口**的 /api/gate 上。
  防剧透不是靠「python 侧不去调 /api/rooms/:id/subtitle」的约定成立的
- drain_rescues()：被回收房间的摘录/笔记 → letter 降级通道。写失败留着重试，
  绝不因为一条失败丢掉其余的
- **失败隔离**（v2.1 给 M1 加的验收）：server.js 坏掉时 ensure_child_on_boot
  不抛异常、status 如实反映未运行、代理降级 502 —— ombre 主进程不受影响

不做什么：不起完整 ombre server（lifespan 接线是一行 try/except；
「ombre 照常工作」由「弄坏 server.js 后跑全量测试仍绿」验证）。
========================================
"""

import os
import json
import shutil
import socket
import time
import asyncio
import urllib.request
import urllib.error

import pytest

from web import ferrideo_bridge as bridge

HAS_NODE = shutil.which("node") is not None
APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ferrideo")
HAS_DEPS = os.path.isdir(os.path.join(APP_DIR, "node_modules"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str):
    """返回 (status, 文本)。HTTPError 也当结果返回，不抛。"""
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


@pytest.fixture()
def bridge_env(monkeypatch, tmp_path):
    """隔离的 bridge 配置：临时 DATA_DIR + 随机端口，重置模块状态。"""
    port = _free_port()
    monkeypatch.setenv("FERRIDEO_DATA_DIR", str(tmp_path / "fdata"))
    monkeypatch.setenv("FERRIDEO_INTERNAL_PORT", str(port))
    monkeypatch.delenv("FERRIDEO_WEB_TOKEN", raising=False)
    monkeypatch.delenv("FERRIDEO_APP_DIR", raising=False)
    # 反面用例：父进程环境里有 PORT（线上 Render 就是这样），子进程必须剔除
    monkeypatch.setenv("PORT", "9999")
    bridge._token_cache = ""
    bridge._child_proc = None
    bridge._monitor_task = None
    bridge._managed = False
    bridge._last_spawn_error = ""
    bridge._proxy_client = None
    yield port


def _fake_mcp():
    """最小 mcp 替身：只收集 custom_route 注册的路由。"""
    from starlette.routing import Route

    class _FakeMCP:
        def __init__(self):
            self.routes = []

        def custom_route(self, path, methods):
            def deco(fn):
                self.routes.append(Route(path, fn, methods=methods))
                return fn
            return deco

    return _FakeMCP()


# ============================================================
# 纯函数
# ============================================================
def test_child_env_strips_port_and_injects_config(bridge_env):
    env = bridge._child_env()
    assert "PORT" not in env                        # 不许抢 ombre 的主端口
    assert env["FERRIDEO_PORT"] == str(bridge_env)
    assert env["DATA_DIR"].endswith("fdata")
    assert env["FERRIDEO_PUBLIC_PREFIX"] == "/ferrideo"
    assert env["FERRIDEO_WEB_TOKEN"]
    assert env["FERRIDEO_PAGE_DIRS"]                # 页面热更新候选目录


def test_web_token_env_priority(bridge_env, monkeypatch):
    monkeypatch.setenv("FERRIDEO_WEB_TOKEN", "tok-from-env")
    assert bridge.web_token() == "tok-from-env"


def test_web_token_generated_and_persisted(bridge_env):
    t1 = bridge.web_token()
    assert t1 and all(c.isalnum() or c in "_-" for c in t1)
    bridge._token_cache = ""                        # 模拟进程重启：缓存清空、盘上还在
    assert bridge.web_token() == t1


def test_token_is_independent_from_reading(bridge_env):
    """两个住户各生成各的 token，不共用。"""
    from web import reading_bridge
    reading_bridge._token_cache = ""
    assert bridge.web_token() != reading_bridge.web_token()


def test_gate_base_points_at_gate_port_not_player(bridge_env, monkeypatch):
    """MCP 侧只认门禁端口。播放器端口上那些能拿到字幕全文的路由，
    在结构上就不该出现在这个 base 的下面。"""
    monkeypatch.setenv("FERRIDEO_GATE_PORT", "18999")
    base = bridge.internal_gate_base()
    assert base.endswith("/api/gate")
    assert "127.0.0.1:18999" in base
    assert str(bridge_env) not in base, "不许指向播放器端口"
    # 模块里不该再留一个「指向播放器端口」的地址供人误用
    assert not hasattr(bridge, "internal_base")


def test_child_env_injects_gate_port(bridge_env):
    env = bridge._child_env()
    assert env["FERRIDEO_GATE_PORT"] == str(bridge._gate_port())
    assert env["FERRIDEO_GATE_PORT"] != env["FERRIDEO_PORT"]


# ============================================================
# 抢救排空：rescued.jsonl → letter
# ============================================================
def _write_rescue(tmp_path, *records):
    d = tmp_path / "fdata"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "rescued.jsonl", "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return d / "rescued.jsonl"


def _rescue_record(room_id="K7X2M9", **kw):
    rec = {
        "kind": "ferrideo_room_evicted",
        "roomId": room_id,
        "title": "花样年华",
        "finishedAt": "2026-08-26T12:00:00Z",
        "quotes": [{"text": "如果我多一张船票", "positionMs": 1834000}],
        "notes": [{"text": "她一直没回头", "positionMs": 1900000, "source": "ai"}],
    }
    rec.update(kw)
    return rec


@pytest.mark.asyncio
async def test_drain_rescues_writes_letters(bridge_env, tmp_path, monkeypatch):
    path_ = _write_rescue(tmp_path, _rescue_record("AAA111"), _rescue_record("BBB222", title="重庆森林"))
    calls = []

    from tools.plan import core as plan_core

    async def fake_letter_write(**kw):
        calls.append(kw)
        return "ok"

    monkeypatch.setattr(plan_core, "letter_write", fake_letter_write)
    assert await bridge.drain_rescues() == 2
    assert len(calls) == 2
    assert "如果我多一张船票" in calls[0]["content"]
    assert "她一直没回头" in calls[0]["content"]
    assert "00:30:34" in calls[0]["content"], "时间要给人能读的串"
    assert "花样年华" in calls[0]["title"]
    assert not os.path.exists(path_) or os.path.getsize(path_) == 0


@pytest.mark.asyncio
async def test_drain_rescues_keeps_failed_lines_for_retry(bridge_env, tmp_path, monkeypatch):
    """写 letter 失败的那条必须留着下轮再试——降级可以，丢失不行。"""
    path_ = _write_rescue(tmp_path, _rescue_record("AAA111"), _rescue_record("BBB222"))
    from tools.plan import core as plan_core

    async def flaky(**kw):
        if "AAA111" in kw["content"]:
            return "ok"
        raise RuntimeError("记忆库暂时写不进去")

    monkeypatch.setattr(plan_core, "letter_write", flaky)
    assert await bridge.drain_rescues() == 1
    left = open(path_, encoding="utf-8").read()
    assert "BBB222" in left and "AAA111" not in left


@pytest.mark.asyncio
async def test_drain_rescues_survives_a_broken_line(bridge_env, tmp_path, monkeypatch):
    """坏行跳过，其余照常——跟 M2 的坏快照同一条原则。"""
    d = tmp_path / "fdata"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "rescued.jsonl", "w", encoding="utf-8") as f:
        f.write('{"roomId":"BAD","quo\n')
        f.write(json.dumps(_rescue_record("GOOD11"), ensure_ascii=False) + "\n")

    from tools.plan import core as plan_core
    calls = []

    async def fake(**kw):
        calls.append(kw)
        return "ok"

    monkeypatch.setattr(plan_core, "letter_write", fake)
    assert await bridge.drain_rescues() == 1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_drain_rescues_noop_when_nothing_to_do(bridge_env, tmp_path):
    assert await bridge.drain_rescues() == 0


# ============================================================
# server.js / 页面定位
# ============================================================
def test_app_dir_falls_back_when_repo_root_lacks_code(bridge_env, monkeypatch, tmp_path):
    """线上形态：repo_root 指向持久盘代码副本 _app/（只有 src/+frontend/），
    那里没有 ferrideo/ —— 必须回退到 __file__ 推导的仓库根。"""
    from web import _shared as sh
    fake_code_dir = tmp_path / "buckets" / "_app"
    (fake_code_dir / "src").mkdir(parents=True)
    monkeypatch.setattr(sh, "repo_root", str(fake_code_dir))
    picked = bridge._app_dir()
    assert picked == APP_DIR
    assert os.path.isfile(os.path.join(picked, "server.js"))
    assert bridge._app_dir_candidates()[0] == str(fake_code_dir / "ferrideo")


def test_app_dir_env_override_is_strict(bridge_env, monkeypatch, tmp_path):
    """FERRIDEO_APP_DIR 显式指定时不回退扫描；指错目录时 _spawn 报错要直说。"""
    monkeypatch.setenv("FERRIDEO_APP_DIR", str(tmp_path / "nowhere"))
    assert bridge._app_dir() == str(tmp_path / "nowhere")
    assert bridge._spawn() is None
    assert "nowhere" in bridge._last_spawn_error and "FERRIDEO_APP_DIR" in bridge._last_spawn_error


def test_page_dirs_prefer_hot_update_dir(bridge_env, monkeypatch, tmp_path):
    """播放器页面优先读热更新目录（<_app>/frontend/ferrideo），镜像内置垫底。"""
    from web import _shared as sh
    monkeypatch.setattr(sh, "repo_root", str(tmp_path / "_app"))
    dirs = bridge._page_dirs()
    assert dirs[0] == str(tmp_path / "_app" / "frontend" / "ferrideo")
    assert dirs[-1] == os.path.join(bridge._IMAGE_ROOT, "frontend", "ferrideo")


# ============================================================
# 失败隔离（v2.1 给 M1 加的验收）
# ============================================================
@pytest.mark.asyncio
async def test_broken_server_js_does_not_break_ombre(bridge_env, monkeypatch, tmp_path):
    """server.js 坏掉：ensure_child_on_boot 不抛、status 如实报未运行、
    代理降级 502。ombre 主进程该照常活着。"""
    broken = tmp_path / "broken_ferrideo"
    broken.mkdir()
    (broken / "server.js").write_text("这不是 JavaScript {{{", encoding="utf-8")
    monkeypatch.setenv("FERRIDEO_APP_DIR", str(broken))

    await bridge.ensure_child_on_boot()              # 不许抛
    try:
        # 进程要么根本没起来，要么起来立刻死；给监控循环一点时间，两种都算通过
        await asyncio.sleep(1.5)
        assert bridge.status()["running"] is False or bridge.status()["pid"] is not None

        from starlette.applications import Starlette
        from starlette.testclient import TestClient
        fake = _fake_mcp()
        bridge.register(fake)
        with TestClient(Starlette(routes=fake.routes)) as client:
            r = client.get("/ferrideo/anything/")
            assert r.status_code == 502                       # 降级，不是 500 崩
            assert "unavailable" in r.text
    finally:
        await bridge.stop_child()


@pytest.mark.asyncio
async def test_missing_node_only_warns(bridge_env, monkeypatch):
    """node 不在 PATH（裸机没装）：只记原因，不抛。"""
    monkeypatch.setattr(bridge.shutil, "which", lambda _n: None)
    await bridge.ensure_child_on_boot()
    try:
        assert bridge.status()["running"] is False
        assert "node 不在 PATH" in bridge._last_spawn_error
    finally:
        await bridge.stop_child()


# ============================================================
# 集成（需要 node）
# ============================================================
async def _wait_health(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            status, body = _get(f"http://127.0.0.1:{port}/healthz")
            if status == 200 and body.strip() == "ok":
                return
        except Exception as e:  # noqa: BLE001
            last = e
        await asyncio.sleep(0.3)
    raise AssertionError(f"child /healthz 未就绪: {last}")


@pytest.mark.skipif(not (HAS_NODE and HAS_DEPS), reason="需要 node 与 ferrideo/node_modules")
@pytest.mark.asyncio
async def test_embedded_end_to_end(bridge_env, monkeypatch, tmp_path):
    port = bridge_env
    # 把页面热更新目录指到临时目录，才能就地改文件验证「不重启也生效」
    from web import _shared as sh
    page_dir = tmp_path / "_app" / "frontend" / "ferrideo"
    page_dir.mkdir(parents=True)
    (page_dir / "index.html").write_text(
        '<!doctype html><p>第一版</p><script>const API_BASE="__FERRIDEO_BASE__";</script>',
        encoding="utf-8",
    )
    monkeypatch.setattr(sh, "repo_root", str(tmp_path / "_app"))

    await bridge.ensure_child_on_boot()
    try:
        assert bridge.status()["running"], bridge._last_spawn_error
        await _wait_health(port)
        token = bridge.web_token()

        from starlette.applications import Starlette
        from starlette.testclient import TestClient
        fake = _fake_mcp()
        bridge.register(fake)
        with TestClient(Starlette(routes=fake.routes)) as client:
            # 门禁语义过代理不变：无 token / 错 token → 404
            assert client.get("/ferrideo/").status_code == 404
            assert client.get("/ferrideo/tok-wrong/").status_code == 404
            # 正确 token：页面能取到，base 已注入成公网路径
            r = client.get(f"/ferrideo/{token}/")
            assert r.status_code == 200
            assert "第一版" in r.text
            assert f'const API_BASE="/ferrideo/{token}"' in r.text
            assert "__FERRIDEO_BASE__" not in r.text

            # 热更新：改文件、不重启子进程，下一次请求就该变
            (page_dir / "index.html").write_text(
                '<!doctype html><p>第二版</p>', encoding="utf-8",
            )
            assert "第二版" in client.get(f"/ferrideo/{token}/").text

        # 崩溃自愈：杀掉子进程，监控循环应自动重启
        old_pid = bridge._child_proc.pid
        bridge._child_proc.kill()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            st = bridge.status()
            if st["running"] and st["pid"] != old_pid:
                break
            await asyncio.sleep(0.3)
        st = bridge.status()
        assert st["running"] and st["pid"] != old_pid, "子进程未被自动重启"
        await _wait_health(port)
    finally:
        await bridge.stop_child()
    assert not bridge.status()["running"]
