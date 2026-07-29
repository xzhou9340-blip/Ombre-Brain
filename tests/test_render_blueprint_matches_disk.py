# ============================================================
# render.yaml 自洽性测试（交接事项 b）
#
# 背景：render.yaml 长期与线上服务对不上——蓝图写 mountPath=/app/buckets，
# 线上实际挂在 /opt/render/project/src/buckets（README.md 一直是对的，
# 蓝图是错的）。拿错的蓝图重建服务，会建出「盘挂在 A、数据写在 B」的服务：
# 表面一切正常，重启后数据没了。
#
# 这里钉住的不是「值等于某个字符串」，而是**蓝图内部自洽**：
#   ① 所有路径类环境变量都落在 disk.mountPath 下
#   ② OMBRE_VAULT_DIR / OMBRE_BUCKETS_DIR 同值（两个名字打架时无从判断谁生效）
#   ③ OMBRE_CONFIG_PATH 在盘上——entrypoint.sh 用它的父目录决定代码播种到哪，
#      配到临时层等于热更新活不过一次重启
#   ④ 蓝图与 README 里写给用户的挂载点一致
# ============================================================

import os
import re

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _service() -> dict:
    with open(os.path.join(_ROOT, "render.yaml"), encoding="utf-8") as f:
        blueprint = yaml.safe_load(f)
    services = blueprint["services"]
    ombre = next(s for s in services if s.get("name") == "ombre-brain")
    return ombre


def _env(service: dict) -> dict[str, str]:
    return {
        e["key"]: str(e["value"])
        for e in service.get("envVars", [])
        if "value" in e
    }


def test_path_env_vars_live_under_the_mounted_disk():
    service = _service()
    mount = service["disk"]["mountPath"].rstrip("/")
    env = _env(service)

    for key in ("OMBRE_VAULT_DIR", "OMBRE_BUCKETS_DIR", "OMBRE_CONFIG_PATH", "NIGHT_FALL_DATA_DIR"):
        assert key in env, f"render.yaml 没声明 {key}；Dockerfile 里烧死的是 /app/... 会落到临时层"
        value = env[key]
        assert value == mount or value.startswith(mount + "/"), (
            f"{key}={value} 不在挂载点 {mount} 下——数据会写进容器临时层，重启即丢"
        )


def test_vault_dir_and_buckets_dir_agree():
    env = _env(_service())
    assert env["OMBRE_VAULT_DIR"] == env["OMBRE_BUCKETS_DIR"], (
        "两个变量指向不同目录时，谁生效取决于读取顺序，等于埋雷"
    )


def test_config_path_is_a_file_on_the_disk():
    """entrypoint.sh:84 CODE_DIR=$(dirname $CONFIG)/_app —— 代码副本跟着它走。"""
    service = _service()
    mount = service["disk"]["mountPath"].rstrip("/")
    config_path = _env(service)["OMBRE_CONFIG_PATH"]

    assert config_path.endswith(".yaml")
    assert os.path.dirname(config_path) == mount, (
        f"config 的父目录是 {os.path.dirname(config_path)}，代码会被播种到那儿的 _app/，"
        f"必须就是挂载点 {mount}"
    )


def test_blueprint_mount_point_matches_readme():
    mount = _service()["disk"]["mountPath"].rstrip("/")
    with open(os.path.join(_ROOT, "README.md"), encoding="utf-8") as f:
        readme = f.read()

    documented = set(re.findall(r"`(/opt/render/project/src/[^`]*)`", readme))
    assert mount in documented, (
        f"README 里写给用户的挂载点是 {sorted(documented)}，蓝图写的是 {mount}"
    )


def test_disk_size_is_declared():
    """sizeGB 是要花钱的字段，写错会在重建时悄悄改配额。"""
    disk = _service()["disk"]
    assert isinstance(disk.get("sizeGB"), int) and disk["sizeGB"] >= 1
