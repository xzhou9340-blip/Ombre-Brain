# ferrideo —— 双人观影房间（内嵌于 ombre-brain）

一个人在 iPad 上看电影，AI 在手机那头跟着看同一场：读得到进度和当前台词、
能往画面上发弹幕、需要时抓一帧看画面，散场后出一张票根存进记忆。

**部署形态：不单独建服务**——由 `src/web/ferrideo_bridge.py` 在 ombre-brain
启动时以 node 子进程方式拉起，与 ombre 和 read-along 跑在同一个 Render 服务里、
共用同一块持久盘，零新增费用。配套 MCP 工具是 `src/tools/ferrideo/` 的 7 个
`ferrideo_*`。

> **不提供任何影视资源。不上传、不存储、不分发任何视频文件。**
> 视频只存在于用户自己的设备上（浏览器里的 `blob:` URL，退出即释放）。

---

## 怎么进去（地址长什么样）

```
https://<你 ombre 的域名>/ferrideo/<token>/
```

域名就是你连 MCP 用的那个（`https://<服务名>.onrender.com`，或你自己的
Cloudflare Tunnel 域名）。

**token 怎么来，两条路：**

| 做法 | 结果 |
|---|---|
| **自己定**（推荐）：在 Render 面板设 `FERRIDEO_WEB_TOKEN=<自定一串>` | 地址是你事先就知道的，可以直接存成 iPad 主屏图标 |
| 不设 | 首启随机生成并持久化到 `<盘>/ferrideo/.web-token`。重启不变，但**没有任何界面会显示它**——日志里也只打印 `<token>` 占位符，不打印真值（不把密钥写进日志） |

所以第一次部署建议直接设 `FERRIDEO_WEB_TOKEN`，20 位以上，只用字母、数字、
`-`、`_`。它就是唯一的门禁：无 token / 错 token 的请求一律 404，与「路径不存在」
不可区分。

**第一次在 iPad 上打开之后**，用 Safari 的「分享 → 添加到主屏幕」存成图标，
以后点图标就进。同时后端会从那次请求里学到对外域名并记在盘上，之后
`ferrideo_create_room` 就能直接回一个能点开的完整地址，不用再拼。

真机验收照着 [`docs/iPad验收清单.md`](docs/iPad验收清单.md) 走一遍。

---

## 运行架构

```
iPad Safari ── https://<ombre域名>/ferrideo/<token>/ ──┐
                                                       │ 反向代理（双向流式）
Render Web Service (ombre-brain, Docker)               ▼
├─ Python: src/server.py（MCP + Dashboard + /ferrideo/* 代理）
├─ Node 子进程: ferrideo/server.js
│    ├─ 127.0.0.1:18005  播放器 + /api/*     ← 只有这个经代理对外
│    └─ 127.0.0.1:18006  AI 门禁 /api/gate/* ← 公网不可达
│    · 崩溃自动重启（指数退避 1→60s，稳定 60s 复位）
│    · 启停失败只降级 warning，绝不拖垮 ombre
└─ 持久盘
     └─ ferrideo/
          ├─ frames/<房间号>/*.jpg   抓下来的帧（内存里只留元信息）
          ├─ rooms/<房间号>.json     房间快照（30 秒一次，原子写）
          ├─ .web-token              自动生成时的 token
          ├─ .public-base            学到的对外域名
          ├─ rescued.jsonl           被回收房间的摘录/笔记，等排空成 letter
          └─ ferrideo-server.log     子进程日志
```

### 为什么门禁要单独一个端口

防剧透必须是服务端强制的，不能是约定。播放器那套 API 里有一条能取字幕全文
（`GET /api/rooms/:id/subtitle`，播放器加入已有房间时要用）。如果 gate 和它
挂在同一个 app 上，「AI 侧不去调那条」就只是个约定——而约定会在某个赶时间的
深夜被绕过去。

所以 gate 跑在自己的端口上，播放器的路由压根不注册在那里：MCP 侧就算把路径
拼成 `../rooms/xxx/subtitle` 也够不到，因为那个端口上没有这条路由。
`ferrideo/gate.test.js` 用裸 socket 发带 `../` 的原始路径钉住了这一条。

---

## 环境变量（全部可选，留空走默认）

| 变量 | 默认 | 说明 |
|---|---|---|
| `FERRIDEO_WEB_TOKEN` | 首启自动生成 | 访问 token（只允许字母/数字/`_`/`-`）。**建议显式设置**，见上文 |
| `FERRIDEO_INTERNAL_PORT` | `18005` | 播放器端口（127.0.0.1，经代理对外） |
| `FERRIDEO_GATE_PORT` | `18006` | AI 门禁端口（127.0.0.1，**不对外**） |
| `FERRIDEO_DATA_DIR` | `<盘>/ferrideo` | 帧、快照、日志；默认已在持久盘上 |
| `FERRIDEO_APP_DIR` | 自动探测 | `server.js` 所在目录的显式覆盖（线上走镜像内置 `/app/ferrideo`） |
| `FERRIDEO_API_BASE` | 自动＝门禁环回 | MCP 工具的后端地址，一般不用设 |

### 磁盘配额（硬性）

- ferrideo 整个目录上限 **100 MB**；超了从最旧的房间开始清帧目录
- 写帧前查**整盘剩余**，低于 **200 MB** 硬停：不写帧、记 `error` 级日志、
  `ferrideo_get_context` 返回「磁盘空间不足」

这条预检保护的不是 ferrideo 自己，是 **ombre 的热更新和回滚路径**——`_app/`
代码副本也在这块盘上，盘满了坏的不是观影，是 ombre 改不动也回滚不了。
这块盘线上只有 1 GB，三个住户共用。

### 其他上限

同时 5 个房间（名额满时只回收「已散场且票根已出」或「散场超一小时」的，
回收前先把摘录/笔记抢救进 letter）· 单帧 500 KB · 每房间 20 帧 ·
单房间字幕 5000 条 · 弹幕 200 条 / 单条 60 字 · 房间 12 小时无心跳即回收

---

## 七个 MCP 工具

| 工具 | 做什么 |
|---|---|
| `ferrideo_create_room` | 开一场。返回房间号 + 她要打开的地址 |
| `ferrideo_get_context` | **最重要的一个。** 放到第几分钟、当前这句台词、前三句；可带一帧画面（走 image content block）。播放头之后的台词永远拿不到 |
| `ferrideo_send_danmaku` | 往她画面上发一条弹幕，3 秒内飘过去。上限 60 字 |
| `ferrideo_request_frame` | 跟播放器要一帧。**异步**：等 3-5 秒再用 `get_context` 带 `include_frame` 取 |
| `ferrideo_add_note` | 记一笔，自动绑当前播放时间。她在画面上看不到 |
| `ferrideo_get_room` | 房间全貌：摘录、笔记、所有弹幕。不含字幕全文、不含画面 |
| `ferrideo_generate_ticket` | 出票根，并直接存成一条 `#观影` 记忆（跳过打标、绝不与旧桶合并） |

### 防剧透门禁（服务端强制）

字幕全文存在后端，但 MCP 侧永远拿不到播放头之后的任何一句：

- 只返回当前句及之前的 cue，`recentLines` 最多回看 3 句
- `nextLineExists` 只给布尔值，不给内容
- 没有任何工具能按下标或时间范围取任意字幕
- 播放头往回拖时按**历史最远播放位置**判定——已经放过的不算剧透

### 没有字幕的那一场

用户没传字幕、或者字幕烧死在画面上时，`get_context` 不报错：返回
`subtitleMode: "none"`，默认带上一帧，并明说「要知道台词请抓帧看画面」。
这时抓帧是唯一的上下文来源。

---

## 本机怎么跑

```bash
cd ferrideo
npm install
FERRIDEO_WEB_TOKEN=devtoken DATA_DIR=./data npm start
# 播放器： http://127.0.0.1:18005/devtoken/
# 门禁：   http://127.0.0.1:18006/devtoken/api/gate/...
```

页面本体在 `../frontend/ferrideo/index.html`（放在 `frontend/` 下是为了跟着
ombre 的 `do-update` 热更新走——改页面不用重建镜像，node 每次请求现读）。
本机跑时 node 会自己找到它。

### 测试

```bash
cd ferrideo
node --test                                      # store + gate 单测
node player.e2e.js  <chromium 可执行文件>         # 播放器全流程（真浏览器）
node layout.e2e.js  <chromium 可执行文件>         # 8 种设备尺寸的布局回归
```

`player.e2e.js` 不需要准备视频文件——没给就用浏览器的 MediaRecorder 现录一段。

python 侧：`pytest tests/test_ferrideo_bridge.py tests/test_ferrideo_tools.py`

---

## 部署

镜像由仓库根目录 `Dockerfile` 构建，构建期对 ferrideo 执行
`npm install --omit=dev`。ombre 原有启动方式不变。

- **现有服务已是 Docker 运行时**（本仓库线上形态）：合并代码、auto-deploy
  重建镜像即可。强烈建议同时在面板上设好 `FERRIDEO_WEB_TOKEN`
- `ferrideo/` 里的服务端代码**不参与热更新**（entrypoint 只播种 `src/` 和
  `frontend/`），改它要重建镜像。播放器页面在 `frontend/ferrideo/` 下，
  走 `do-update` 即可生效

### 支持的设备

iPad Safari（主场景，横竖都可）+ iPhone Safari **横屏**。iPhone 竖屏不保证好看。

**别想着一台 iPhone 既看片又跟 AI 聊**：切到聊天那一刻 iOS 会把网页挂起，
片子会停、心跳会断。双设备分工是这个东西的前提，不是偶然。

### iPad 放不了的片子

Safari 只认 MP4/MOV 容器里的常见编码。`.mkv` 容器、AC3/DTS 音轨一律不放。
播放器在选片时会预检并给出明确提示，不会让人对着黑屏猜。转一下就行：

```bash
ffmpeg -i input.mkv -c:v copy -c:a aac -movflags +faststart output.mp4
```

视频流大概率能直接 copy，只转音轨，几分钟就完。只有 10-bit HEVC 才需要真转码。
