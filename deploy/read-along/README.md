# read-along 共读系统 · 部署 + 接入 Ombre MCP

把 [read-along](https://github.com/luoluo-1121/read-along) 共读后端部署到与
Ombre Brain 同一台服务器，并通过本仓库新增的 5 个 `reading_*` MCP 工具接入
ombre 体系。上游完整文档见其 `docs/DEPLOY.md` / `docs/AI-GUIDE.md`；本文是
针对本机器（ombre 已在跑）的落地清单。

## 一、部署后端（脚本一把梭）

```bash
# 在 VPS 上，root：
bash deploy/read-along/deploy.sh
```

脚本做的事（幂等，可重复跑）：

| 步骤 | 内容 |
|---|---|
| Node 检查 | 需要 Node ≥ 18（内置 fetch） |
| 安装 | clone/更新到 `/opt/read-along`，`npm install`，数据落 `data/`（本地 JSON，无数据库） |
| 常驻 | `pm2 start server.js --name reading`，**DRY-RUN**：`READING_PUSH_ENABLED` / `READING_PUSH_WEBHOOK` 都不设，推送只写 `data/outbox.log`，不外发（webhook 常驻分身桥是后续任务，本次不接通） |
| 访问控制 | 生成随机路径 `/reading-<token>/`（token 存 `/opt/read-along/.web-token`），`reader.html` 拷到 `/var/www/reading-<token>/` 并同步改写其中的 `API` 常量 |
| nginx | 按 token 渲染 `nginx.conf.example` → `/etc/nginx/read-along.locations` |

之后手工两步：

```bash
# 1. 在已有 HTTPS server 块里 include 渲染好的片段
#    （/etc/nginx/sites-enabled/xxx 的 server{} 里加一行）：
#        include /etc/nginx/read-along.locations;
nginx -t && systemctl reload nginx

# 2. 还没配 HTTPS 的话：
apt install -y certbot python3-certbot-nginx
certbot --nginx -d 你的域名
```

想在随机路径之上再叠一层 Basic Auth：见 `nginx.conf.example` 内注释
（`htpasswd` + 取消两处 `auth_basic` 注释）。AI 侧从本机直连
`127.0.0.1:18004`，不走 nginx，两种访问控制都不影响 MCP 工具。

## 二、接入 Ombre MCP

本仓库已内置 5 个工具（`src/tools/reading/`，注册在 `src/server.py`），
重启 ombre 服务后即在 `/mcp` 连接器里可见：

| 工具 | 包装的端点 | 用途 |
|---|---|---|
| `reading_progress` | `GET /api/gate/{bookId}`（不传 book_id 时 `GET /api/books`） | 书架 / 进度 / 已解锁章节 |
| `reading_text` | `GET /api/gate/{bookId}/text?from&to` | 回看已解锁正文 |
| `reading_search` | `GET /api/gate/{bookId}/search?q` | 只搜已解锁范围 |
| `reading_annotate` | `POST /api/annotate` | 划线写批注（quote 须逐字含标点；409 → 换更长的句子） |
| `reading_annotations` | `GET /api/annotations/{bookId}`、`POST …/{annoId}/comment` | 查批注 / 回复（author 固定 `ai`） |

防剧透门禁是 read-along **服务端**硬约束：未解锁章节连标题都不返回。
工具层只调 gate/annotate 端点做转译，不绕过、不改门禁逻辑。

### 后端地址

默认 `http://127.0.0.1:18004`，裸机部署的 ombre 不需要任何配置。
要改地址时设环境变量：

```
READING_API_BASE=http://127.0.0.1:18004
```

### Docker 网络（仅当 ombre 跑在 Docker 里）

read-along 只监听宿主机回环（`127.0.0.1:18004`），ombre 容器里的
`127.0.0.1` 是容器自己，直连不通。在宿主机加一个 socat 回环桥：

```bash
apt install -y socat
cat >/etc/systemd/system/reading-bridge.service <<'EOF'
[Unit]
Description=Bridge docker gateway -> read-along on loopback
After=network.target docker.service

[Service]
# 172.17.0.1 = 默认 docker0 网桥网关（docker network inspect bridge 可核对）
ExecStart=/usr/bin/socat TCP-LISTEN:18004,bind=172.17.0.1,fork,reuseaddr TCP:127.0.0.1:18004
Restart=always

[Install]
WantedBy=multi-user.target
EOF
systemctl enable --now reading-bridge
```

然后用本目录的 compose 覆盖文件把 `host.docker.internal` 映射进容器并设好
`READING_API_BASE`：

```bash
cp deploy/read-along/docker-compose.reading.example.yml deploy/docker-compose.reading.yml
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.reading.yml up -d
```

## 三、验收清单

1. **手机能读**：手机浏览器打开 `https://你的域名/reading-<token>/`
   （token 见 `cat /opt/read-along/.web-token`），点「＋导入」上传一本
   epub/txt，能翻页阅读。
2. **停留即推送（DRY-RUN）**：停在某页 ≥ 15 秒（阈值可在阅读器设置面板改），
   `tail -f /opt/read-along/data/outbox.log` 出现 `[DRY-RUN]` 开卷 + 页面记录。
3. **MCP 查得到进度和原文**：对 AI 说「看看我读到哪了」——
   `reading_progress` 应返回进度与已解锁章节；`reading_text` 能取出
   刚才那页的原文。
4. **MCP 批注上屏**：让 AI 用 `reading_annotate` 写一条批注（quote 逐字
   复制第 3 步取到的原文），手机阅读器该页页边出现划线圆点与留言。
5. **门禁**：`reading_progress` 的已解锁章节列表不含未读章节（连标题都没有）；
   对未解锁段号 `reading_text` 返回「未解锁」；`reading_search` 搜后文
   关键词无命中。

## 四、排错速查

- **工具报「共读服务连不上」** — `pm2 status reading`、
  `curl -s 127.0.0.1:18004/health`；Docker 部署检查 socat 桥
  （`systemctl status reading-bridge`）与 `READING_API_BASE`。
- **outbox.log 一直没记录** — 心跳没进来：查手机端控制台 / nginx 两段
  location；有 `[DRY-RUN]` 是正常的（本任务就保持 DRY-RUN）。
- **批注 404 / 409** — 404：quote 与原文不是逐字一致（全角/半角标点），或
  批的内容还没解锁；409：引文出现多次，换更长的句子。
- **重置某本书进度** — 停服务，删 `data/state.json` 里对应 bookId 条目，重启。
  批注独立于进度，不会丢。

数据备份：整个系统的全部状态就是 `/opt/read-along/data/`，定期打包即可。
