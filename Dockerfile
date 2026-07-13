# ============================================================
# Ombre Brain Docker Build
# Docker 构建文件
#
# Build:
#   docker build -t ombre-brain .
# 本地运行（最小必填项）:
#   docker run \
#     -e OMBRE_COMPRESS_API_KEY=your-llm-key \
#     -e OMBRE_EMBED_API_KEY=your-gemini-key \
#     -e OMBRE_DASHBOARD_PASSWORD=xxx \
#     -p 18001:8000 ombre-brain          # 对外 18001 → 容器内 8000
# 推荐用 deploy/docker-compose.yml（开发）或 deploy/docker-compose.user.yml（用户）启动。
# ============================================================

FROM python:3.12-slim

WORKDIR /app

# Install cloudflared + curl (for downloading cloudflared)
# 安装 cloudflared（用于 Tunnel 一键管理功能）
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && ARCH=$(dpkg --print-architecture) \
    && curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}" \
       -o /usr/local/bin/cloudflared \
    && chmod +x /usr/local/bin/cloudflared \
    && apt-get remove -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Node.js（read-along 共读后端以子进程方式跑在本服务里，见 src/web/reading_bridge.py）
# Debian bookworm 的 nodejs 是 18.x，满足 read-along 的 Node ≥ 18（内置 fetch）要求。
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && node --version

# Install dependencies first (leverage Docker cache)
# 先装依赖（利用 Docker 缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# read-along 的 node 依赖也先装（同样吃缓存：package*.json 不变就不重装）
COPY read-along/package.json read-along/package-lock.json ./read-along/
RUN cd read-along && npm install --omit=dev && npm cache clean --force

# Copy project files / 复制项目文件
COPY src/ ./src/
COPY frontend/ ./frontend/
COPY tools/ ./tools/
# read-along 代码（node_modules 已在上面装好，.dockerignore 排除了本地的
# node_modules/ 与 data/，COPY 是合并语义、不会覆盖掉装好的依赖）
COPY read-along/ ./read-along/
COPY VERSION ./VERSION
COPY config.example.yaml ./config.default.yaml
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# Persistent mount point: bucket data
# 持久化挂载点：记忆数据
VOLUME ["/app/buckets"]

# Default to streamable-http for container (remote access)
# 容器场景默认用 streamable-http
ENV OMBRE_TRANSPORT=streamable-http
# 容器内固定监听 8000；对外通过 host 端口映射 18001:8000 暴露（保持 Cloudflare
# ingress 指向 :8000 不变）。裸机（非容器）不读此 ENV，走 server.py 默认 18001。
ENV OMBRE_PORT=8000
ENV OMBRE_VAULT_DIR=/app/buckets
# config 默认落在持久卷 /app/buckets 里，而不是镜像可写层 /app/config.yaml。
# 关键：很多 PaaS（Zeabur / 部分 Render 配置等）用**只读根文件系统**，只有挂载的卷可写——
# 这时 entrypoint 往 /app/config.yaml 写默认配置会 "Read-only file system" 失败 → FATAL →
# 无限崩溃重启（本地 root + 可写 /app 复现不出，平台上才炸）。放到 /app/buckets 既避开只读根，
# 又让 Dashboard 改的 key 落在卷上、重启/重部署不丢。VPS（deploy/docker-compose.yml）显式覆盖回
# /app/config.yaml 保持原有文件挂载不变。
ENV OMBRE_CONFIG_PATH=/app/buckets/config.yaml
# Embedding 使用 API 后端（Gemini）
# 必须通过运行时 -e 或 docker-compose environment 传入 OMBRE_EMBED_API_KEY
ENV OMBRE_EMBED_BACKEND=api

EXPOSE 8000

# ── Night-Fall extension (path 0：原地扩展) ────────────────────────────────
# 安装 Night-Fall 包本身（需要 git 才能 pip 装 git+ 源）。
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir git+https://github.com/ysuu525/Night-Fall.git

# 集成方式：把 Night-Fall 作为**库**挂到本 fork 的 server.py 上
# （register_night_fall 在 src/server.py 里调用），**不**走 night_fall.launcher。
# 原因：本 fork 的启动栈高度定制——ENTRYPOINT=entrypoint.sh（配置自愈 + 持久卷
# 代码 bootstrap + .boot_fails 回滚），server.py 的 __main__ 里还挂了 OAuth 鉴权
# 中间件、Accept 头补丁（Claude.ai 连接器可连的关键）、decay 引擎启动、.boot_fails
# 复位、mcp_extra 的 7 个工具回灌。night_fall.launcher 自带一套裸 serving
# （mcp.streamable_http_app() + CORS + uvicorn），会把上述全部丢掉。因此**保留**
# 原 ENTRYPOINT / 启动命令不变，只在 server.py 里以库方式注册工具与自动浮梦钩子。
#
# OMBRE_HOME 指向真正存放 server.py 的目录（本 fork 在 src/ 下，非仓库根）；
# Night-Fall 的 config 加载器会校验 $OMBRE_HOME/server.py 是否存在。
ENV OMBRE_HOME=/app/src
# 潜梦数据目录：落在持久数据卷 /app/buckets 下的子目录，容器重建后潜梦保留。
# 只**新增** night_fall/ 子目录，不触碰卷内已有的记忆桶数据。
ENV NIGHT_FALL_DATA_DIR=/app/buckets/night_fall

ENTRYPOINT ["./entrypoint.sh"]
