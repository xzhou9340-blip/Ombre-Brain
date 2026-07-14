"""
========================================
tests/test_reading_bridge.py — 共读子进程托管 + 反向代理回归
========================================

覆盖 web/reading_bridge.py：

- _child_env()：剔除 PORT / 两个推送开关（DRY-RUN 硬要求），注入
  READING_PORT / DATA_DIR / token / 公网前缀
- web_token()：env 优先；否则生成并持久化，重启（重读）不变
- 集成（需要本机 node，无则跳过）：
  · ensure_child_on_boot 拉起子进程，/health 就绪，READING_API_BASE 自动接线
  · token 门禁在代理后语义不变（无/错 token 404）
  · /reading/<token>/* 反代全链路：reader.html（API 常量含 /reading 前缀）、
    上传导书、心跳；停留超阈值 → [DRY-RUN] 落 DATA_DIR/outbox.log
  · reading_* 工具走内部地址全通，未解锁内容不可见
  · 杀掉子进程 → 监控循环自动重启
  · 「重启」后（新子进程、同 DATA_DIR）书与进度仍在

不做什么：不起完整 ombre server（lifespan 接线是一行 try/except，
由 test_server_import 侧的完整导入冒烟覆盖注册路径）。
========================================
"""

import os
import json
import shutil
import socket
import time
import asyncio
import urllib.request

import pytest

from web import reading_bridge as bridge

HAS_NODE = shutil.which("node") is not None
APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "read-along")
HAS_DEPS = os.path.isdir(os.path.join(APP_DIR, "node_modules"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(method: str, url: str, body: bytes | None = None, ctype: str = "application/json"):
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture()
def bridge_env(monkeypatch, tmp_path):
    """隔离的 bridge 配置：临时 DATA_DIR + 随机端口 + 快速 dwell，重置模块状态。"""
    port = _free_port()
    monkeypatch.setenv("READING_DATA_DIR", str(tmp_path / "rdata"))
    monkeypatch.setenv("READING_INTERNAL_PORT", str(port))
    monkeypatch.setenv("READING_DWELL_MS", "1000")
    monkeypatch.delenv("READING_API_BASE", raising=False)
    monkeypatch.delenv("READING_WEB_TOKEN", raising=False)
    # DRY-RUN 硬要求的反面用例：故意在父进程环境放上开关，子进程必须剔除
    monkeypatch.setenv("READING_PUSH_ENABLED", "1")
    monkeypatch.setenv("READING_PUSH_WEBHOOK", "http://example.com/hook")
    monkeypatch.setenv("PORT", "9999")
    bridge._token_cache = ""
    bridge._child_proc = None
    bridge._monitor_task = None
    bridge._managed = False
    bridge._last_spawn_error = ""
    bridge._proxy_client = None
    yield port


# ============================================================
# 纯函数
# ============================================================
def test_child_env_strips_push_switches_and_port(bridge_env):
    env = bridge._child_env()
    assert "READING_PUSH_ENABLED" not in env        # DRY-RUN：开关绝不透传
    assert "READING_PUSH_WEBHOOK" not in env
    assert "PORT" not in env                        # Render 注入的主端口不透传
    assert env["READING_PORT"] == str(bridge_env)
    assert env["DATA_DIR"].endswith("rdata")
    assert env["READING_PUBLIC_PREFIX"] == "/reading"
    assert env["READING_WEB_TOKEN"]


def test_web_token_env_priority(bridge_env, monkeypatch):
    monkeypatch.setenv("READING_WEB_TOKEN", "tok-from-env")
    assert bridge.web_token() == "tok-from-env"


def test_web_token_generated_and_persisted(bridge_env):
    t1 = bridge.web_token()
    assert t1 and all(c.isalnum() or c in "_-" for c in t1)
    bridge._token_cache = ""                        # 模拟进程重启：缓存清空、盘上还在
    assert bridge.web_token() == t1


# ============================================================
# server.js 定位（回归：线上 repo_root 指向持久盘代码副本 _app/，
# 副本里只有 src/+frontend/、没有 read-along/ —— 曾去
# <buckets>/_app/read-along 找代码导致子进程永远起不来）
# ============================================================
def test_app_dir_falls_back_when_repo_root_lacks_code(bridge_env, monkeypatch, tmp_path):
    """代码目录与 buckets_dir/repo_root 分离：repo_root 下没有 read-along 时，
    必须回退到 __file__ 推导的仓库根（本仓库里真实存在 server.js）。"""
    from web import _shared as sh
    fake_code_dir = tmp_path / "buckets" / "_app"        # 模拟 entrypoint 的 CODE_DIR
    (fake_code_dir / "src").mkdir(parents=True)
    monkeypatch.setattr(sh, "repo_root", str(fake_code_dir))
    picked = bridge._app_dir()
    assert picked == APP_DIR                             # 落到真实仓库的 read-along/
    assert os.path.isfile(os.path.join(picked, "server.js"))
    # 候选链首位仍是 repo_root（保持「代码副本若真带了 read-along 就优先用」的语义）
    assert bridge._app_dir_candidates()[0] == str(fake_code_dir / "read-along")


def test_app_dir_prefers_repo_root_when_it_has_code(bridge_env, monkeypatch, tmp_path):
    from web import _shared as sh
    root = tmp_path / "seeded"
    (root / "read-along").mkdir(parents=True)
    (root / "read-along" / "server.js").write_text("// stub", encoding="utf-8")
    monkeypatch.setattr(sh, "repo_root", str(root))
    assert bridge._app_dir() == str(root / "read-along")


def test_app_dir_env_override_is_strict(bridge_env, monkeypatch, tmp_path):
    """READING_APP_DIR 显式指定时不回退扫描；指错目录时 _spawn 报错要直说。"""
    monkeypatch.setenv("READING_APP_DIR", str(tmp_path / "nowhere"))
    assert bridge._app_dir() == str(tmp_path / "nowhere")
    assert bridge._spawn() is None
    assert "nowhere" in bridge._last_spawn_error and "READING_APP_DIR" in bridge._last_spawn_error


def test_spawn_error_lists_all_candidates(bridge_env, monkeypatch, tmp_path):
    """哪儿都找不到 server.js 时，错误信息带完整候选清单（含镜像内置路径）。"""
    from web import _shared as sh
    monkeypatch.setattr(sh, "repo_root", str(tmp_path / "empty1"))
    monkeypatch.setattr(bridge, "_IMAGE_APP_DIR", str(tmp_path / "empty2"))
    monkeypatch.setattr(bridge, "_app_dir_candidates", lambda: [
        str(tmp_path / "empty1" / "read-along"), str(tmp_path / "empty2"),
    ])
    assert bridge._spawn() is None
    assert "empty1" in bridge._last_spawn_error and "empty2" in bridge._last_spawn_error


# ============================================================
# 集成（需要 node）
# ============================================================
async def _wait_health(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            status, data = _http("GET", f"http://127.0.0.1:{port}/health")
            if status == 200 and data.get("ok"):
                assert data.get("pushEnabled") is False, "子进程必须处于 DRY-RUN"
                return
        except Exception as e:  # noqa: BLE001
            last = e
        await asyncio.sleep(0.3)
    raise AssertionError(f"child /health 未就绪: {last}")


@pytest.mark.skipif(not (HAS_NODE and HAS_DEPS), reason="需要 node 与 read-along/node_modules")
@pytest.mark.asyncio
async def test_embedded_end_to_end(bridge_env, tmp_path):
    port = bridge_env
    await bridge.ensure_child_on_boot()
    try:
        assert bridge.status()["running"], bridge._last_spawn_error
        await _wait_health(port)
        token = bridge.web_token()

        # --- READING_API_BASE 自动接线到内部环回 ---
        assert os.environ["READING_API_BASE"] == f"http://127.0.0.1:{port}/{token}"

        # --- 反向代理（等价于挂在 mcp 上的 custom_route）---
        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.testclient import TestClient

        class _FakeMCP:
            def __init__(self):
                self.routes = []

            def custom_route(self, path, methods):
                def deco(fn):
                    self.routes.append(Route(path, fn, methods=methods))
                    return fn
                return deco

        fake = _FakeMCP()
        bridge.register(fake)
        with TestClient(Starlette(routes=fake.routes)) as client:
            # 门禁语义过代理不变：无 token / 错 token → 404
            assert client.get("/reading/api/books").status_code == 404
            assert client.get("/reading/tok-wrong/api/books").status_code == 404
            # 正确 token：书架 + 阅读器（API 常量已带 /reading 前缀）
            assert client.get(f"/reading/{token}/api/books").json() == {"books": []}
            html = client.get(f"/reading/{token}/").text
            assert f"const API = '/reading/{token}/api'" in html

            # 上传导书走代理（流式 POST，阅读器「＋导入」同款端点）
            book = ("第一章 起点\n\n" + "\n\n".join(f"第一章第{i}段。代理链路验证正文{i}。" for i in range(1, 6))
                    + "\n\n第二章 秘密结局\n\n后文不可见。").encode("utf-8")
            r = client.post(f"/reading/{token}/api/import?filename=proxybook.txt&id=proxybook", content=book)
            assert r.status_code == 200 and r.json()["ok"], r.text

            # 心跳 + 停留超阈值（dwell=1s）→ [DRY-RUN] 落 DATA_DIR/outbox.log
            beat = {"bookId": "proxybook", "event": "page", "chapter": 0,
                    "seqStart": 0, "seqEnd": 2, "pageKey": "p1"}
            client.post(f"/reading/{token}/api/beat", json={"bookId": "proxybook", "event": "open"})
            client.post(f"/reading/{token}/api/beat", json=beat)
            await asyncio.sleep(1.5)
            client.post(f"/reading/{token}/api/beat", json={**beat, "event": "beat"})
        outbox = tmp_path / "rdata" / "outbox.log"
        assert outbox.exists()
        log = outbox.read_text(encoding="utf-8")
        assert "[DRY-RUN]" in log and "[SENT]" not in log

        # --- reading_* 工具走内部地址 ---
        from tools.reading import core as rt
        out = await rt.progress("proxybook")
        assert "第一章 起点" in out and "秘密结局" not in out       # 门禁：未解锁章节不可见
        out = await rt.text("proxybook", 0, 2)
        assert "代理链路验证正文1" in out
        out = await rt.search("proxybook", "后文不可见")
        assert "没有命中" in out

        # --- 崩溃自愈：杀掉子进程，监控循环应自动重启 ---
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

        # --- 「重启」后同 DATA_DIR：书和进度仍在（持久盘语义）---
        status, data = _http("GET", f"http://127.0.0.1:{port}/{token}/api/gate/proxybook")
        assert status == 200 and data["furthestSeq"] >= 2
    finally:
        await bridge.stop_child()
    assert not bridge.status()["running"]
