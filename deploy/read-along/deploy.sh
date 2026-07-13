#!/usr/bin/env bash
# ============================================================
# read-along 共读系统 · 服务器部署脚本（在 VPS 上以 root 运行）
#
# 做什么：
#   1. clone/更新 https://github.com/luoluo-1121/read-along 到 /opt/read-along
#   2. npm install（要求 Node ≥ 18），数据落 /opt/read-along/data/
#   3. pm2 常驻，进程名 reading —— 保持 DRY-RUN：
#      不设 READING_PUSH_ENABLED / READING_PUSH_WEBHOOK，推送只写 data/outbox.log
#   4. 生成不可猜测的随机路径 /reading-<token>/，把 reader.html 拷到
#      /var/www/reading-<token>/ 并改写其中的 API 常量（访问控制·必做）
#   5. 按该 token 渲染 nginx 配置片段，输出到 /etc/nginx/read-along.locations
#      （需要你 include 进已有 HTTPS server 块，见脚本结尾提示与 README.md）
#
# 不做什么：
#   - 不改 nginx 主配置、不申请证书（certbot 命令见结尾提示）
#   - 不开任何外发推送（webhook 桥是后续任务）
#
# 幂等：重复运行会更新代码并 pm2 restart；随机 token 首次生成后
# 存在 /opt/read-along/.web-token，之后复用（路径不会每次变掉）。
# ============================================================
set -euo pipefail

REPO_URL="${READ_ALONG_REPO:-https://github.com/luoluo-1121/read-along.git}"
APP_DIR="/opt/read-along"
TOKEN_FILE="$APP_DIR/.web-token"
NGINX_SNIPPET="/etc/nginx/read-along.locations"

# ---- 0. Node ≥ 18 ------------------------------------------------
if ! command -v node >/dev/null 2>&1; then
  echo "✗ 没有 node。先装 Node.js ≥ 18（如 https://deb.nodesource.com）再跑本脚本。" >&2
  exit 1
fi
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
if [ "$NODE_MAJOR" -lt 18 ]; then
  echo "✗ Node 版本过低（$(node -v)），需要 ≥ 18（内置 fetch）。" >&2
  exit 1
fi
echo "✓ node $(node -v)"

# ---- 1. clone / 更新 ----------------------------------------------
if [ -d "$APP_DIR/.git" ]; then
  echo "→ 更新 $APP_DIR"
  git -C "$APP_DIR" pull --ff-only
else
  echo "→ clone 到 $APP_DIR"
  git clone "$REPO_URL" "$APP_DIR"
fi

# ---- 2. npm install + data/ --------------------------------------
cd "$APP_DIR"
npm install --omit=dev
mkdir -p data
echo "✓ 依赖安装完成，数据目录 $APP_DIR/data/"

# ---- 3. pm2 常驻（DRY-RUN） ---------------------------------------
if ! command -v pm2 >/dev/null 2>&1; then
  echo "→ 安装 pm2"
  npm install -g pm2
fi
# 显式确保两个推送开关都不进入 pm2 保存的环境（保持 DRY-RUN，只写 outbox.log）
if pm2 describe reading >/dev/null 2>&1; then
  pm2 delete reading
fi
env -u READING_PUSH_ENABLED -u READING_PUSH_WEBHOOK pm2 start server.js --name reading
pm2 save
echo "✓ pm2 进程 reading 已启动（DRY-RUN：推送只写 data/outbox.log，不外发）"

# ---- 自检 ---------------------------------------------------------
sleep 1
HEALTH="$(curl -sf http://127.0.0.1:18004/health || true)"
if echo "$HEALTH" | grep -q '"ok":true'; then
  echo "✓ 后端自检通过：$HEALTH"
  echo "$HEALTH" | grep -q '"pushEnabled":false' \
    || echo "⚠ pushEnabled 不是 false —— 检查环境里是否残留了推送开关变量！"
else
  echo "✗ http://127.0.0.1:18004/health 不通，看日志：pm2 logs reading" >&2
  exit 1
fi

# ---- 4. 随机路径 + 前端静态文件（访问控制·必做） -------------------
if [ -f "$TOKEN_FILE" ]; then
  TOKEN="$(cat "$TOKEN_FILE")"
else
  TOKEN="$(tr -dc 'a-z0-9' </dev/urandom | head -c 10)"
  printf '%s' "$TOKEN" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
fi
WEB_ROOT="/var/www/reading-$TOKEN"
mkdir -p "$WEB_ROOT"
# 改写 reader.html 里的 API 常量：/reading/api → /reading-<token>/api
sed "s|const API = '/reading/api'|const API = '/reading-$TOKEN/api'|" \
  web/reader.html > "$WEB_ROOT/reader.html"
if ! grep -q "reading-$TOKEN/api" "$WEB_ROOT/reader.html"; then
  echo "✗ reader.html 的 API 常量改写失败（上游文件结构变了？），手工核对 $WEB_ROOT/reader.html" >&2
  exit 1
fi
echo "✓ 前端已发布到 $WEB_ROOT（随机路径 /reading-$TOKEN/）"

# ---- 5. 渲染 nginx 片段 -------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/nginx.conf.example" ]; then
  sed "s/__TOKEN__/$TOKEN/g" "$SCRIPT_DIR/nginx.conf.example" > "$NGINX_SNIPPET"
  echo "✓ nginx 片段已渲染到 $NGINX_SNIPPET"
else
  echo "⚠ 找不到 $SCRIPT_DIR/nginx.conf.example，跳过 nginx 片段渲染"
fi

cat <<EOF

============================================================
 剩下的手工步骤
============================================================
1. 把片段 include 进你已有的 HTTPS server 块（/etc/nginx/sites-*/…）：
       include /etc/nginx/read-along.locations;
   然后 nginx -t && systemctl reload nginx

2. 还没有 HTTPS 的话：
       apt install -y certbot python3-certbot-nginx
       certbot --nginx -d 你的域名

3. （可选加固）在随机路径之上再叠一层 Basic Auth，见片段内注释。

4. 手机打开  https://你的域名/reading-$TOKEN/  验收：
   上传一本 epub/txt → 翻页停留 ≥15 秒 →
   tail -f $APP_DIR/data/outbox.log 应出现 [DRY-RUN] 记录。

5. 开机自启：pm2 startup（按它输出的提示执行一次）。

Ombre MCP 侧无需额外配置（裸机默认直连 127.0.0.1:18004）；
Ombre 跑在 Docker 里时见 deploy/read-along/README.md 的网络说明。
============================================================
EOF
