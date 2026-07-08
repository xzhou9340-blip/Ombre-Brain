#!/bin/sh
# entrypoint.sh — 容器启动入口
#
# 职责：确保 config 文件是一个可用的**普通文件**再启动服务，否则宁可 FATAL 退出，
# 也不让应用带着坏配置进入无限崩溃重启。不做其他事（不改业务逻辑）。
#
# 问题背景（Windows/WSL2 fresh install 崩溃重启）：
#   旧 compose 用单文件 bind mount `./config.yaml:/app/config.yaml`。若宿主
#   ./config.yaml 不存在，Docker（尤其 Windows/WSL2）会把它当成目录创建并挂进来，
#   /app/config.yaml 于是是个**目录**而非文件，应用读它直接 IsADirectoryError 崩溃。
#   更糟的是：bind mount 的挂载点在容器内**删不掉**（rm 报 "Device or resource busy"）。
#   根治办法是不再单文件挂载 config，改用 $OMBRE_CONFIG_PATH 把配置放进已经是目录挂载
#   的数据卷里（见 docker-compose.user.yml）。本脚本是最后一道防线。
#
# 处理逻辑：
#   1. 配置路径取 $OMBRE_CONFIG_PATH，未设则退回 /app/config.yaml（老行为，兼容现有部署）。
#   2. 确保父目录存在。
#   3. 若该路径是目录（Docker 副作用）：rmdir / rm -rf 常规删除；删不掉就用
#      `find -mindepth 1 -delete` 清空内容兜底（即便目录本身是挂载点删不掉），再试 rmdir。
#   4. 删成功（路径已不存在）→ 从内置默认模板初始化一份。
#   5. 最终校验：路径必须是普通文件，否则打印清晰指引并 FATAL 退出（不带病启动）。

CONFIG="${OMBRE_CONFIG_PATH:-/app/config.yaml}"
DEFAULT=/app/config.default.yaml

mkdir -p "$(dirname "$CONFIG")" 2>/dev/null || true

# --- 3. 若是目录，尽全力把它清掉 ---
if [ -d "$CONFIG" ]; then
    echo "[entrypoint] '$CONFIG' is a directory (Docker created it because the host file was missing)."
    echo "[entrypoint] Trying to remove it and re-initialize from defaults..."
    rmdir "$CONFIG" 2>/dev/null || rm -rf "$CONFIG" 2>/dev/null || true
    if [ -d "$CONFIG" ]; then
        # 直接删除失败（多半是活动 bind mount，挂载点自身删不掉）。
        # 兜底：清空目录内容（mindepth 1 = 不碰目录自身），再试着删掉空目录。
        echo "[entrypoint] Direct removal failed; clearing its contents as a fallback..."
        find "$CONFIG" -mindepth 1 -delete 2>/dev/null || true
        rmdir "$CONFIG" 2>/dev/null || true
    fi
fi

# --- 4. 不存在则从默认模板初始化（上面删成功后会走到这；纯缺文件也走这）---
if [ ! -e "$CONFIG" ]; then
    echo "[entrypoint] Initializing config from defaults at '$CONFIG'..."
    cp "$DEFAULT" "$CONFIG"
fi

# --- 5. 最终校验：必须是普通文件，否则别启动去无限崩溃刷屏 ---
if [ ! -f "$CONFIG" ]; then
    echo "[entrypoint] FATAL: could not prepare a usable config file at '$CONFIG'."
    echo "[entrypoint] Two known causes:"
    echo "[entrypoint]   (a) compose single-file-mounts a missing config (Docker makes it a directory):"
    echo "[entrypoint]         volumes:  - ./config.yaml:/app/config.yaml   <-- remove this line"
    echo "[entrypoint]   (b) the path sits on a read-only / non-writable filesystem (many PaaS, e.g."
    echo "[entrypoint]         Zeabur, use a read-only rootfs — only the mounted volume is writable)."
    echo "[entrypoint] Fix: point config at the writable data volume:"
    echo "[entrypoint]     environment:  - OMBRE_CONFIG_PATH=/app/buckets/config.yaml"
    echo "[entrypoint]     volumes:      - ./buckets:/app/buckets   (PaaS: mount the volume at /app/buckets)"
    echo "[entrypoint] The image already defaults OMBRE_CONFIG_PATH to /app/buckets/config.yaml;"
    echo "[entrypoint] this FATAL means it was overridden to an unwritable location."
    exit 1
fi

echo "[entrypoint] config ready at '$CONFIG'."

# ============================================================
# 持久化代码 bootstrap（#4a ①②）
# ------------------------------------------------------------
# 问题：容器平台(Docker/Render/Zeabur)代码烤在镜像只读层，/api/do-update 的
# 热更新写在可写临时层，容器一重建就回退到旧镜像 → 更新"留不住"。
# 方案：把 src/frontend 播种到数据卷上的 CODE_DIR，从那里运行；repo_root 随之
# 指向 CODE_DIR，热更新直接写持久盘，容器重建也还在。
#   · 镜像代码变化(正经重建) → 重新播种，让重建覆盖旧热更新。判定用两条：
#       (a) VERSION 号变了；或 (b) src/+frontend/ 内容指纹变了（防漏 bump VERSION，
#       Night-Fall 首次集成即撞过这坑 — 镜像换了新代码但 VERSION 未 bump，卷上
#       .seeded_image_version 相同，重播种不触发，新代码等于没生效）。
#   · OMBRE_CODE_RESEED=1 → 无条件重播种一次（运维应急开关；用完记得取消或
#     它会每次启动都覆盖热更新）。
#   · 连续启动失败 → 回滚到 _prev（do-update 覆盖前留的上一版），防坏更新锁死。
#   · 任何环节失败 → 回退到镜像内置 /app/src，绝不 brick。
# 裸机(非 Docker)不走本脚本，直接从仓库目录跑，本就是持久的。
# ============================================================
IMAGE_ROOT=/app
CODE_DIR="${OMBRE_CODE_DIR:-$(dirname "$CONFIG")/_app}"
RUN_ROOT="$IMAGE_ROOT"
ROLLBACK_THRESHOLD=2

# 计算镜像内 src/+frontend/ 的内容指纹（16 位十六进制）。用 sha256sum 逐文件
# 摘要后再整体摘要，输出里含文件名 → 改名/移动同样会变。任何失败返回 "unknown"，
# 交给 VERSION 检查兜底（防指纹功能自身炸掉时静默不触发重播种）。
_image_code_fingerprint() {
    fp="$(cd "$IMAGE_ROOT" 2>/dev/null && \
        find src frontend -type f -exec sha256sum {} + 2>/dev/null \
        | LC_ALL=C sort | sha256sum 2>/dev/null | cut -c1-16)"
    [ -n "$fp" ] || fp="unknown"
    printf %s "$fp"
}

_bootstrap_code() {
    # CODE_DIR 必须在可写持久卷上；试探写权限，失败就回退镜像代码。
    mkdir -p "$CODE_DIR" 2>/dev/null || return 1
    ( : > "$CODE_DIR/.wtest" ) 2>/dev/null || return 1
    rm -f "$CODE_DIR/.wtest" 2>/dev/null || true

    IMG_VER="$(cat "$IMAGE_ROOT/VERSION" 2>/dev/null || echo unknown)"
    SEEDED_VER="$(cat "$CODE_DIR/.seeded_image_version" 2>/dev/null || echo none)"
    IMG_FP="$(_image_code_fingerprint)"
    SEEDED_FP="$(cat "$CODE_DIR/.seeded_image_fingerprint" 2>/dev/null || echo none)"
    FORCE_RESEED="${OMBRE_CODE_RESEED:-0}"

    # --- ② 崩溃自愈：上一次启动没被 server.py 标记成功 → 计数累加；超阈值且有 _prev 则回滚 ---
    FAILS="$(cat "$CODE_DIR/.boot_fails" 2>/dev/null || echo 0)"
    case "$FAILS" in ''|*[!0-9]*) FAILS=0 ;; esac
    if [ "$FAILS" -ge "$ROLLBACK_THRESHOLD" ] && [ -f "$CODE_DIR/_prev/src/server.py" ]; then
        echo "[entrypoint] 连续 $FAILS 次启动失败 → 回滚到上一版代码 (_prev)"
        rm -rf "$CODE_DIR/src" "$CODE_DIR/frontend" 2>/dev/null
        cp -a "$CODE_DIR/_prev/src" "$CODE_DIR/src" 2>/dev/null || return 1
        cp -a "$CODE_DIR/_prev/frontend" "$CODE_DIR/frontend" 2>/dev/null || true
        [ -f "$CODE_DIR/_prev/VERSION" ] && cp -a "$CODE_DIR/_prev/VERSION" "$CODE_DIR/VERSION" 2>/dev/null
        rm -rf "$CODE_DIR/_prev" 2>/dev/null
        FAILS=0
        echo 0 > "$CODE_DIR/.boot_fails" 2>/dev/null || true
    fi

    # --- ① 播种 / 重建覆盖：首次；镜像版本变了；镜像代码指纹变了；或强制刷新开关 ---
    if [ ! -f "$CODE_DIR/src/server.py" ] \
       || [ "$IMG_VER" != "$SEEDED_VER" ] \
       || [ "$IMG_FP" != "$SEEDED_FP" ] \
       || [ "$FORCE_RESEED" = "1" ]; then
        if [ "$FORCE_RESEED" = "1" ]; then
            echo "[entrypoint] OMBRE_CODE_RESEED=1 → 强制重播种到 $CODE_DIR (image=v$IMG_VER fp=$IMG_FP)"
        else
            echo "[entrypoint] 播种代码到持久卷 $CODE_DIR (image=v$IMG_VER fp=$IMG_FP, 卷上 seeded=v$SEEDED_VER fp=$SEEDED_FP)"
        fi
        rm -rf "$CODE_DIR/src" "$CODE_DIR/frontend" 2>/dev/null
        cp -a "$IMAGE_ROOT/src" "$CODE_DIR/src" 2>/dev/null || return 1
        cp -a "$IMAGE_ROOT/frontend" "$CODE_DIR/frontend" 2>/dev/null || return 1
        cp -a "$IMAGE_ROOT/VERSION" "$CODE_DIR/VERSION" 2>/dev/null || true
        rm -rf "$CODE_DIR/_prev" 2>/dev/null
        echo "$IMG_VER" > "$CODE_DIR/.seeded_image_version" 2>/dev/null || true
        echo "$IMG_FP" > "$CODE_DIR/.seeded_image_fingerprint" 2>/dev/null || true
        FAILS=0
    fi

    [ -f "$CODE_DIR/src/server.py" ] || return 1

    # 预增启动失败计数；启动成功后 server.py 会清零，崩溃则保留 → 下次累加直至回滚。
    echo $((FAILS + 1)) > "$CODE_DIR/.boot_fails" 2>/dev/null || true
    RUN_ROOT="$CODE_DIR"
    return 0
}

if _bootstrap_code; then
    echo "[entrypoint] 从持久卷运行: $RUN_ROOT/src/server.py"
else
    echo "[entrypoint] 持久卷代码不可用，回退到镜像内置代码 /app/src（不影响本次运行）"
    RUN_ROOT="$IMAGE_ROOT"
fi

cd "$RUN_ROOT" 2>/dev/null || cd /app
exec python src/server.py
