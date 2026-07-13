# read-along 共读后端（Render 版）

人机共读系统的后端 + 手机阅读器，作为**独立 Render Web Service** 部署，
与 Ombre Brain（`src/tools/reading/` 的 5 个 `reading_*` MCP 工具）配套。

Vendor 自 [luoluo-1121/read-along](https://github.com/luoluo-1121/read-along)
（MIT，见 [LICENSE](LICENSE)，基于上游 commit `5043a65`），针对无 nginx、
无宿主机的 PaaS 环境做了三处适配，其余逻辑（含防剧透门禁）与上游一致：

| 改动 | 上游 | 本版 |
|---|---|---|
| 监听 | 仅 `127.0.0.1:18004` | `process.env.PORT` 存在时监听 `0.0.0.0:$PORT`（Render 硬性要求）；本地开发不变 |
| 前端托管 | nginx 静态托管 + 反代 | Node 自己发 `web/reader.html`，并把页面里的 `API` 常量按 token 前缀改写 |
| 访问控制 | nginx Basic Auth / 随机路径 | `READING_WEB_TOKEN`：除 `/health` 外所有路径挂在 `/<token>/` 下，不带或带错一律 404 |
| 数据目录 | 项目内 `data/` | `DATA_DIR` 环境变量指向 persistent disk 挂载点（不设时回退 `data/`，仅限本地开发） |

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `PORT` | Render 注入 | 存在即监听 `0.0.0.0`；本地不设则 `127.0.0.1:18004` |
| `READING_WEB_TOKEN` | 公网必填 | 访问 token（限字母/数字/`_`/`-`），阅读器与 API 的路径前缀。PaaS 环境不设会打启动警告 |
| `DATA_DIR` | 公网必填 | 数据目录。**Render 容器磁盘临时、每次部署/重启清空**，必须指到持久盘挂载点（如 `/var/data`），否则书/进度/批注全部蒸发 |
| `READING_PUSH_ENABLED` / `READING_PUSH_WEBHOOK` | 不设 | **保持 DRY-RUN**：都不设时推送只写 `<DATA_DIR>/outbox.log`，不外发（webhook 桥是后续任务） |
| `READING_DWELL_MS` / `READING_IDLE_MS` / `READING_READER_NAME` | 可选 | 同上游：停留阈值 / 空闲合卷 / 读者称呼 |

## 部署到 Render

仓库根目录的 `render.yaml` 已包含 `read-along` 服务定义
（`rootDir: read-along`、node 运行时、1GB 持久盘挂 `/var/data`、
`READING_WEB_TOKEN` 自动生成随机值）。两种方式：

- **Blueprint**：Render → New → Blueprint → 选本仓库，会按 render.yaml 建出/同步服务；
- **手动**：New → Web Service → 本仓库，Root Directory 填 `read-along`，
  Build `npm install --omit=dev`，Start `node server.js`，
  Advanced 里 **Add Disk**（mount `/var/data`，1GB），
  环境变量照上表填（token 自己生成一串随机字符）。

> 挂盘需要付费计划（Starter 起）；免费计划没有持久盘，数据必丢，不要用。

部署完成后自检：

```bash
curl -s https://<服务>.onrender.com/health          # {"ok":true,"pushEnabled":false}
curl -s https://<服务>.onrender.com/<token>/api/books
```

`pushEnabled` 必须是 `false`（DRY-RUN）。token 在服务的 Environment 页可见。

## 接到 Ombre Brain

在 **ombre-brain 服务**（Render Dashboard → Environment）加一个环境变量并重启：

```
READING_API_BASE=https://<read-along服务>.onrender.com/<token>
```

含 token 路径、结尾不带斜杠。`reading_*` 五个工具每次调用现读该变量，
在其后直接拼 `/api/...`，改完即生效，无需改代码。

## 验收清单

1. 手机浏览器打开 `https://<服务>.onrender.com/<token>/`，
   「＋导入」上传一本 epub/txt，能翻页阅读。
2. 停在某页超过阈值（默认 15 秒），Render Shell 里
   `tail /var/data/outbox.log` 出现 `[DRY-RUN]` 开卷 + 页面记录。
3. 对 AI：`reading_progress` 查到进度与已解锁章节，`reading_text`
   取出刚才那页原文。
4. 让 AI `reading_annotate` 写一条批注（quote 逐字复制第 3 步的原文），
   手机阅读器该页页边出现划线与留言。
5. 门禁：未解锁章节在任何工具输出里都不出现（连标题都没有）；
   对未解锁段号 `reading_text` 返回「未解锁」，`reading_search`
   搜后文关键词无命中。
6. （持久性）Render 上 Manual Deploy 重启一次服务，书和进度还在
   （在 → 盘挂对了；丢了 → `DATA_DIR` 没指到挂载点）。

## 本地开发

```bash
cd read-along && npm install
node server.js                 # 127.0.0.1:18004，无 token，根路径即阅读器
node import-book.js 书.epub --id mybook
```

## 排错

- **手机打开 404** — token 路径抄错（区分大小写）；token 在 Render
  Environment 页。`/health` 通而带 token 的路径 404 就是这个原因。
- **MCP 工具报「连不上」** — 按提示查 `/health`、核对 `READING_API_BASE`
  是否含 token 且与服务端 `READING_WEB_TOKEN` 一致。
- **重启后书全没了** — 数据落在了临时盘：确认服务挂了 Disk 且
  `DATA_DIR` 等于挂载点，重新导入。
- **批注 404/409** — 404：quote 与原文非逐字一致（全角/半角标点）或内容
  未解锁；409：引文出现多次，换更长的句子。
- **换 token** — 改 `READING_WEB_TOKEN` 重启，同时更新 ombre 侧
  `READING_API_BASE`；手机书签也要换。
