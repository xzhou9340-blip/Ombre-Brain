/* ============================================================
 * ferrideo/server.js — 观影房间后端（ombre-brain 内嵌子进程）
 * ============================================================
 *
 * 形态与 read-along 完全一致：不单独建 Render 服务，由
 * src/web/ferrideo_bridge.py 在 ombre 启动时以 node 子进程拉起，
 * 绑 127.0.0.1 内部端口（不对外），公网入口是 ombre 侧的
 * /ferrideo/<token>/* 反向代理。
 *
 * 关键行为：
 * - 监听：env PORT 存在时绑 0.0.0.0:$PORT（独立跑/本地开发）；否则绑
 *   127.0.0.1:$FERRIDEO_PORT（内嵌形态，bridge 会剔除 PORT 并注入 FERRIDEO_PORT）
 * - 访问控制：除 /healthz 外所有路径都挂在 /<token>/ 下。无 token / 错 token
 *   一律 404，且与「路径不存在」不可区分（照抄 read-along 的门禁语义）
 * - 播放器页面：每次请求现读文件，按候选链找 index.html，找到就把
 *   __FERRIDEO_BASE__ 替换成 <公网前缀>/<token>。**不缓存**——页面放在
 *   frontend/ferrideo/ 下，跟着 ombre 的 do-update 热更新走，缓存了热更新就白做
 * - token 不合法（非 URL 安全字符）直接拒绝启动，不静默降级成无门禁
 *
 * 不做什么（边界）：
 * - 不碰 ombre 的记忆桶、不碰 read-along 的 data/
 * - 不做用户系统、不存视频文件（视频只存在于用户自己的设备上）
 *
 * 房间状态见 store.js，HTTP API 见 routes.js。
 * ============================================================ */

'use strict';

const express = require('express');
const fs = require('fs');
const path = require('path');

const store = require('./store');
const routes = require('./routes');

// ============================================================
// 调参面板
// ============================================================
const DEFAULT_INTERNAL_PORT = 18005;      // 内部端口（read-along 占了 18004）
const TOKEN_RE = /^[\w-]+$/;              // URL 安全字符，与 bridge 侧一致
const PAGE_PLACEHOLDER = '__FERRIDEO_BASE__';  // 页面里被替换成 <前缀>/<token>
const JSON_BODY_LIMIT = '1mb';            // 帧不走 JSON（走 raw），这里不需要放大

// ============================================================
// 环境
// ============================================================
const TOKEN = (process.env.FERRIDEO_WEB_TOKEN || '').trim();
const PUBLIC_PREFIX = (process.env.FERRIDEO_PUBLIC_PREFIX || '').replace(/\/+$/, '');
const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, 'data');

if (!TOKEN || !TOKEN_RE.test(TOKEN)) {
  // 没有合法 token 就不启动：宁可子进程起不来（ombre 侧只降级 warning），
  // 也不要跑出一个没有门禁的公网可达服务。
  console.error(
    '[ferrideo] FERRIDEO_WEB_TOKEN 缺失或不合法（只允许字母/数字/_/-）；拒绝启动。'
  );
  process.exit(1);
}

/** 播放器页面的候选目录，按优先级。前三个是热更新目录（frontend/ 跟着
 *  ombre 的代码播种走），最后一个是镜像内置兜底。bridge 会用
 *  FERRIDEO_PAGE_DIRS 显式注入前面几个（冒号分隔）。 */
function pageDirs() {
  const injected = (process.env.FERRIDEO_PAGE_DIRS || '')
    .split(':')
    .map((s) => s.trim())
    .filter(Boolean);
  return [...injected, path.join(__dirname, 'public')];
}

/** 现读页面文件。返回 null 表示候选链里一个都没有。 */
function readPage() {
  for (const dir of pageDirs()) {
    const f = path.join(dir, 'index.html');
    try {
      return fs.readFileSync(f, 'utf8');
    } catch (e) {
      if (e.code !== 'ENOENT') {
        console.error(`[ferrideo] 读页面失败 ${f}: ${e.message}`);
      }
    }
  }
  return null;
}

// ============================================================
// 应用
// ============================================================
const app = express();
app.disable('x-powered-by');

// 全局 JSON 解析器按 v2 §1.4 锁在 1 MB，且**跳过两条走 raw body 的路径**：
// 字幕（5000 条中文能到几 MB）和帧（二进制）。不跳过的话它会先把 body 吃掉，
// 后面路由上的 express.raw 只会拿到一个已解析的对象——第一次 curl 验收就栽在这。
const RAW_BODY_ROUTES = /\/(subtitle|frame)$/;
app.use(express.json({
  limit: JSON_BODY_LIMIT,
  type: (req) => !(req.method === 'POST' && RAW_BODY_ROUTES.test((req.url || '').split('?')[0]))
    && /json/i.test(req.headers['content-type'] || ''),
}));

// 健康检查：不带 token，给 bridge / Render healthCheck / 排查用
app.get('/healthz', (req, res) => {
  res.type('text/plain').send('ok');
});

// ---- token 门禁 ----------------------------------------------------------
// 挂在 /:token 下。token 不对直接 404，与路径不存在不可区分。
const gated = express.Router({ mergeParams: true });

app.use('/:token', (req, res, next) => {
  if (req.params.token !== TOKEN) return res.status(404).end();
  return gated(req, res, next);
});

// HTTP API（/<token>/api/*）
routes.register(gated);

// 播放器页面
gated.get('/', (req, res) => {
  const html = readPage();
  if (html === null) {
    console.error('[ferrideo] 候选目录里都没有 index.html：' + pageDirs().join('、'));
    return res.status(500).type('text/plain').send('播放器页面找不到，看 ombre 日志里的 [ferrideo] 行');
  }
  // 页面自己不知道公网前缀和 token，服务时注入
  const base = `${PUBLIC_PREFIX}/${TOKEN}`;
  res
    .type('text/html; charset=utf-8')
    .set('Cache-Control', 'no-store')  // 热更新的页面不许被中间层缓存
    .send(html.split(PAGE_PLACEHOLDER).join(base));
});

// 门禁内的未知路径同样 404（不泄漏「token 猜对了」这个信息差之外的东西）
gated.use((req, res) => res.status(404).end());

// ============================================================
// 启动
// ============================================================
function main() {
  try {
    store.init({ dataDir: DATA_DIR });
  } catch (e) {
    // 存储起不来就别装作能用：子进程退出，ombre 侧只降级 warning
    console.error(`[ferrideo] 存储初始化失败 ${DATA_DIR}: ${e.message}`);
    process.exit(1);
  }
  const envPort = parseInt(process.env.PORT || '', 10);
  const port = Number.isInteger(envPort)
    ? envPort
    : parseInt(process.env.FERRIDEO_PORT || '', 10) || DEFAULT_INTERNAL_PORT;
  const host = Number.isInteger(envPort) ? '0.0.0.0' : '127.0.0.1';

  const server = app.listen(port, host, () => {
    console.log(`[ferrideo] 已启动 http://${host}:${port}  data=${DATA_DIR}  页面候选=${pageDirs().join('、')}`);
  });
  server.on('error', (e) => {
    console.error(`[ferrideo] 监听 ${host}:${port} 失败: ${e.message}`);
    process.exit(1);
  });
  // 被 bridge terminate 时干净退出（不留孤儿端口）
  for (const sig of ['SIGTERM', 'SIGINT']) {
    process.on(sig, () => {
      store.stop();          // 退出前把待写的快照落盘
      server.close(() => process.exit(0));
    });
  }
}

main();
