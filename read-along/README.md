# read-along 共读后端（内嵌于 ombre-brain）

人机共读系统的后端 + 手机阅读器。**部署形态：不单独建服务**——由
`src/web/reading_bridge.py` 在 ombre-brain 启动时以 node 子进程方式拉起，
与 ombre 跑在同一个 Render 服务里、共用同一块持久盘，零新增费用。
配套 MCP 工具是 `src/tools/reading/` 的 5 个 `reading_*`。

Vendor 自 [luoluo-1121/read-along](https://github.com/luoluo-1121/read-along)
（MIT，见 [LICENSE](LICENSE)，基于上游 commit `5043a65`），改动（门禁/推送逻辑未动）：

| 改动 | 说明 |
|---|---|
| 监听 | `process.env.PORT` 存在时绑 `0.0.0.0:$PORT`；内嵌模式下 bridge 会剔除 PORT、注入 `READING_PORT`，实际绑 `127.0.0.1:18004`（不对外） |
| 前端托管 | Node 自己发 `web/reader.html`，页面里的 `API` 常量按 token（和代理前缀）在服务时改写 |
| 访问控制 | `READING_WEB_TOKEN`：除 `/health` 外所有路径挂 `/<token>/` 下，无/错 token 一律 404（与路径不存在不可区分） |
| 代理前缀 | `READING_PUBLIC_PREFIX`（bridge 注入 `/reading`）：只影响前端 API 常量，配合 ombre 侧 `/reading/*` 反向代理 |
| 数据目录 | `DATA_DIR` 环境变量；内嵌模式默认 `<buckets_dir>/read-along`，即持久盘子目录 |

## 运行架构

```
手机浏览器 ── https://<ombre域名>/reading/<token>/ ──┐
                                                    │ 反向代理（流式，50MB 传书可过）
Render Web Service (ombre-brain, Docker)            ▼
├─ Python: src/server.py（MCP + Dashboard + /reading/* 代理）
├─ Node 子进程: read-along/server.js ── 127.0.0.1:18004（不对外）
│    · 崩溃自动重启（指数退避 1→60s，稳定 60s 复位）
│    · 启停失败只降级 warning，绝不拖垮 ombre
└─ 持久盘 /app/buckets
     ├─ （记忆桶 .md、peek/、night_fall/ …）
     └─ read-along/   ← 书、进度、批注、outbox.log、.web-token
```

`reading_*` MCP 工具走内部环回 `http://127.0.0.1:18004/<token>`，不出公网
（bridge 在子进程起来后自动写 `READING_API_BASE`，也可显式覆盖）。

## 环境变量（全部可选，留空走默认）

| 变量 | 默认 | 说明 |
|---|---|---|
| `READING_WEB_TOKEN` | 首启自动生成 | 访问 token（限字母/数字/`_`/`-`）。自动生成的持久化在 `<DATA_DIR>/.web-token`，重启不变 |
| `READING_INTERNAL_PORT` | `18004` | 子进程内部端口（127.0.0.1） |
| `READING_DATA_DIR` | `<buckets_dir>/read-along` | 数据目录；默认已在持久盘上 |
| `READING_API_BASE` | 自动=内部环回 | MCP 工具的后端地址，一般不用设 |
| `READING_DWELL_MS` / `READING_IDLE_MS` / `READING_READER_NAME` | 同上游 | 停留阈值 / 空闲合卷 / 读者称呼 |
| `READING_PUSH_ENABLED` / `READING_PUSH_WEBHOOK` | **保持不设** | DRY-RUN：推送只写 `<DATA_DIR>/outbox.log`，不外发。bridge 构造子进程环境时会主动剔除这两个变量 |

github_sync 只同步 `.md` 文件，`read-along/` 下的书（epub/JSON/封面）不会被
备份到 GitHub 仓库。

## 部署

镜像由根目录 `Dockerfile` 构建：python:3.12-slim + `apt install nodejs npm`
（Debian bookworm 的 Node 18，满足 ≥18），构建期对 read-along 执行
`npm install --omit=dev`。ombre 原有启动方式（entrypoint.sh → src/server.py）不变。

- **现有服务已是 Docker 运行时**（本仓库线上形态）：合并本次代码、
  auto-deploy 重建镜像即可，什么都不用配。
- **若服务是 Python 原生运行时**：Render 原生 Python 环境不保证有 node，
  且无法原地更换运行时——需按根目录 `render.yaml` 新建 Docker 服务，
  把持久盘数据迁移过去（或重新导入记忆包）。node 缺失时 ombre 一切照旧，
  只是共读功能不可用，日志里有 `[reading]` 说明。

部署后自检（Render 日志应有 `[reading] 共读子进程已启动`）：

```bash
curl -s https://<ombre域名>/reading/health     # {"ok":true,"pushEnabled":false}
# token 看日志或 Render Shell: cat /app/buckets/read-along/.web-token
curl -s https://<ombre域名>/reading/<token>/api/books
```

`pushEnabled` 必须是 `false`（DRY-RUN）。

## 验收清单

1. ombre 原有 20 个工具全部正常（含 bark_push / phone_activity_query）。
2. 手机打开 `https://<ombre域名>/reading/<token>/`，「＋导入」上传
   epub/txt，能翻页阅读。
3. 停在某页超过阈值（默认 15 秒），Render Shell 里
   `tail /app/buckets/read-along/outbox.log` 出现 `[DRY-RUN]` 记录。
4. 五个 `reading_*` 工具全通（走内部环回，不出公网）；未解锁章节在任何
   工具输出里都不出现（连标题都没有）。
5. Manual Deploy 重启服务：书、进度、批注、token 都还在（持久盘验收）。

## 本地开发

```bash
# 独立跑（无 token，根路径即阅读器）：
cd read-along && npm install && node server.js     # 127.0.0.1:18004

# 或直接跑 ombre（bridge 会拉起子进程）：python src/server.py
node import-book.js 书.epub --id mybook
```

## 排错

- **/reading/... 全部 502** — 子进程没起来：看 ombre 日志 `[reading]` 行
  （node 缺失 / server.js 缺失 / 启动失败原因都在），子进程自身日志在
  `<DATA_DIR>/reading-server.log`。
- **手机打开 404** — token 抄错（区分大小写）；`/reading/health` 通而带
  token 的路径 404 就是这个原因。
- **MCP 工具报「连不上」** — 按提示看 `[reading]` 日志与
  `READING_API_BASE`；崩溃循环时监控会持续退避重启。
- **重启后书没了** — `READING_DATA_DIR` 被改到了持久盘之外；默认值
  （`<buckets_dir>/read-along`）本身就在盘上。
- **批注 404/409** — 404：quote 与原文非逐字一致（全角/半角标点）或内容
  未解锁；409：引文出现多次，换更长的句子。
- **换 token** — 设 `READING_WEB_TOKEN` 重启（或删 `<DATA_DIR>/.web-token`
  重启自动换新）；手机书签同步更新，MCP 侧自动跟随。
