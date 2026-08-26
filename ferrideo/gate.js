/* ============================================================
 * ferrideo/gate.js — AI 侧的门禁 API（防剧透在这一层强制）
 * ============================================================
 *
 * 这是 MCP 工具唯一能碰到的东西。
 *
 * **为什么是单独一个监听端口，而不是同一个 app 上的一组路由：**
 * 防剧透必须是服务端强制，不是约定。「python 侧不去调 /api/rooms/:id/subtitle」
 * 是约定——约定会在某个赶时间的深夜被绕过去。这里把 gate 放在自己的
 * 127.0.0.1:<GATE_PORT> 上，播放器的 /api/rooms/* 压根不注册在这个 app 里：
 * MCP 那侧就算把路径拼成 ../rooms/xxx/subtitle 也够不到，因为那个端口上
 * 没有这条路由。结构上做不到，不是靠自觉。
 *
 * 这个端口**不经过 ombre 的 /ferrideo 反向代理**，公网无法访问。
 *
 * 门禁规则（逐条对应原规格书 6.2）：
 * - 只返回 currentIndex 及之前的 cue
 * - recentLines 最多回看 3 句
 * - nextLineExists 只给布尔值，不给内容
 * - 没有任何一条路由能按下标或时间范围取任意字幕
 * - 播放头往回拖时按**历史最远播放位置**判定，已经放过的不算剧透
 * - 字幕没加载时不报错，改 subtitleMode:"none" 并提示去抓帧
 *
 * 与 routes.js 的关系：**两套 handler，不共用**。共用的只有 store。
 *
 * 不做什么（边界）：
 * - 不返回字幕全文、不返回帧的 base64（帧要单张、现读现编码）
 * - 不做任何能按下标/时间取字幕的参数
 * ============================================================ */

'use strict';

const express = require('express');
const store = require('./store');

const RECENT_LINES = 3;            // 回看句数上限（硬上限，不接受调用方指定）
const STALE_WARN_SECONDS = 15;     // 超过这么久没心跳就在文案里提示

/** 毫秒 → 人类可读时间串（不给毫秒数，AI 拿到要能直接说出口）。 */
function hms(ms) {
  const total = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  const h = String(Math.floor(total / 3600)).padStart(2, '0');
  const m = String(Math.floor((total % 3600) / 60)).padStart(2, '0');
  const s = String(total % 60).padStart(2, '0');
  return `${h}:${m}:${s}`;
}

function fail(res, status, msg) {
  return res.status(status).json({ error: msg });
}

function withRoom(req, res, next) {
  const room = store.getRoom(req.params.id);
  if (!room) return fail(res, 404, '房间不存在，或者已经过期了');
  req.room = room;
  return next();
}

/**
 * 门禁核心：可见的字幕上界。
 * 取 currentIndex 与 furthestIndex 的较大者——用户往回拖时不收回已经放过的内容。
 * 任何返回台词的地方都必须先过这里。
 */
function visibleLimit(room) {
  const { cues } = room.subtitle;
  if (!cues.length) return -1;
  const limit = Math.max(room.subtitle.currentIndex, room.furthestIndex);
  return Math.min(limit, cues.length - 1);
}

function buildContext(room, includeFrame) {
  const { subtitle, playback } = room;
  const staleSeconds = Math.max(0, Math.round((Date.now() - new Date(playback.updatedAt).getTime()) / 1000));
  const out = {
    roomId: room.id,
    title: room.title,
    state: playback.state,
    position: hms(playback.positionMs),
    duration: playback.durationMs ? hms(playback.durationMs) : null,
    progressPercent: playback.durationMs
      ? Math.min(100, Math.round((playback.positionMs / playback.durationMs) * 100))
      : null,
    staleSeconds,
    danmakuCount: room.danmaku.length,
    quoteCount: room.quotes.length,
    noteCount: room.notes.length,
    finished: Boolean(room.finishedAt),
    hints: [],
  };

  if (staleSeconds > STALE_WARN_SECONDS) {
    out.hints.push(
      `已经 ${staleSeconds} 秒没有心跳了——播放器可能不在前台（她切出去了，或者 iPad 锁屏了）。` +
      '上面这些数字是那一刻的，不是现在的。',
    );
  }

  // ---- 字幕 ----
  if (!subtitle.loaded) {
    // 无字幕降级：不报错，改成明确告诉她该怎么办
    out.subtitleMode = 'none';
    out.nextLineExists = false;
    out.hints.push(
      '这一场没有字幕轨（她没传字幕，或者字幕是烧死在画面上的硬字幕）。' +
      '要知道台词请用 ferrideo_request_frame 抓一帧看画面。',
    );
  } else {
    out.subtitleMode = 'track';
    const limit = visibleLimit(room);
    const cur = Math.min(subtitle.currentIndex, limit);
    if (cur >= 0) {
      out.currentLine = subtitle.cues[cur].text;
      out.recentLines = subtitle.cues
        .slice(Math.max(0, cur - RECENT_LINES), cur)
        .map((c) => c.text);
    } else {
      out.recentLines = [];
      out.hints.push('字幕已经加载，但还没走到第一句（可能在片头）。');
    }
    // 只给布尔值。后面那句是什么，这里永远不说
    out.nextLineExists = cur + 1 < subtitle.cues.length;
  }

  // ---- 帧 ----
  out.frameMode = room.frameMode;
  if (room.frameMode !== 'ok' && room.frameFailure) {
    out.hints.push(
      room.frameFailure.kind === 'disk'
        ? `抓不到画面：磁盘空间不足（${room.frameFailure.detail}）。这台机器上 ombre 的记忆也在同一块盘上，先去清盘。`
        : `这个片源抓不到画面（${room.frameFailure.kind}）。${subtitle.loaded ? '' : '而且这一场没有字幕——这一场你基本是瞎的，跟她直说。'}`,
    );
  }
  // 无字幕时 includeFrame 默认为 true：抓帧是这时唯一的上下文来源
  const wantFrame = includeFrame === undefined ? !subtitle.loaded : includeFrame;
  if (wantFrame && room.frameMode === 'ok') {
    const got = store.readFrame(room);
    if (got) {
      out.frame = `data:image/jpeg;base64,${got.buffer.toString('base64')}`;
      out.frameAt = hms(got.meta.positionMs);
      const ageSec = Math.round((Date.now() - new Date(got.meta.createdAt).getTime()) / 1000);
      out.frameAgeSeconds = ageSec;
      if (ageSec > 60) out.hints.push(`这帧是 ${ageSec} 秒前的，不是此刻的画面。要新的就再调一次 ferrideo_request_frame。`);
    } else if (room.frameRequest) {
      out.hints.push('已经跟播放器要了一帧，但还没送上来——再等几秒重新调一次。');
    } else {
      out.hints.push('这个房间还没有任何一帧。先调 ferrideo_request_frame，等 3-5 秒再来取。');
    }
  }
  return out;
}

/** 房间全貌（6.6）：笔记、摘录、弹幕。不含字幕全文、不含帧的 base64。 */
function buildRoomOverview(room) {
  return {
    roomId: room.id,
    title: room.title,
    createdAt: room.createdAt,
    state: room.playback.state,
    position: hms(room.playback.positionMs),
    duration: room.playback.durationMs ? hms(room.playback.durationMs) : null,
    finished: Boolean(room.finishedAt),
    watchedFor: room.watchedMs ? hms(room.watchedMs) : null,
    subtitleMode: room.subtitle.loaded ? 'track' : 'none',
    quotes: room.quotes.map((q) => ({ text: q.text, at: hms(q.positionMs), createdAt: q.createdAt })),
    notes: room.notes.map((n) => ({ text: n.text, at: hms(n.positionMs), source: n.source, createdAt: n.createdAt })),
    danmaku: room.danmaku.map((d) => ({ text: d.text, author: d.author, createdAt: d.createdAt, delivered: d.delivered })),
    frameCount: room.frames.length,
    frameMode: room.frameMode,
    ticket: room.ticket,
  };
}

/** 票根（6.7）。生成并存进 room.ticket，返回给调用方拿去存长期记忆。 */
function buildTicket(room, { mood, note }) {
  const ticket = {
    roomId: room.id,
    title: room.title,
    date: new Date().toISOString().slice(0, 10),
    duration: room.playback.durationMs ? hms(room.playback.durationMs) : null,
    watchedFor: hms(room.watchedMs || room.playback.positionMs),
    quotes: room.quotes.map((q) => q.text),
    notes: room.notes.map((n) => n.text),
    danmakuCount: room.danmaku.length,
    mood: (mood || '').trim() || null,
    note: (note || '').trim() || null,
  };
  store.setTicket(room, ticket);
  return ticket;
}

/**
 * 造一个只有 gate 路由的 express app。
 * 播放器的路由不在这里注册——这就是「结构上够不到」的全部实现。
 */
function createGateApp({ token }) {
  const app = express();
  app.disable('x-powered-by');
  app.use(express.json({ limit: '256kb' }));   // gate 侧只收小 body

  app.get('/healthz', (req, res) => res.type('text/plain').send('ok'));

  const gated = express.Router({ mergeParams: true });
  app.use('/:token', (req, res, next) => {
    if (req.params.token !== token) return res.status(404).end();
    return gated(req, res, next);
  });

  // 建房（ferrideo_create_room）
  gated.post('/api/gate/rooms', (req, res) => {
    try {
      const room = store.createRoom((req.body || {}).title);
      return res.status(201).json({
        roomId: room.id,
        title: room.title,
        tip: `房间号 ${room.id}，在 iPad 上输入这个号`,
      });
    } catch (e) {
      return fail(res, e.status || 500, e.message);
    }
  });

  // 现在放到哪了、这一句是什么（ferrideo_get_context）
  gated.get('/api/gate/rooms/:id/context', withRoom, (req, res) => {
    const raw = req.query.includeFrame;
    const includeFrame = raw === undefined ? undefined : ['1', 'true', 'yes'].includes(String(raw).toLowerCase());
    return res.json(buildContext(req.room, includeFrame));
  });

  // 房间全貌（ferrideo_get_room）
  gated.get('/api/gate/rooms/:id/room', withRoom, (req, res) => res.json(buildRoomOverview(req.room)));

  // 发弹幕（ferrideo_send_danmaku）
  gated.post('/api/gate/rooms/:id/danmaku', withRoom, (req, res) => {
    try {
      const { text, author } = req.body || {};
      const d = store.addDanmaku(req.room, { text, author: author || '克', source: 'ai' });
      return res.status(201).json({ ok: true, text: d.text, author: d.author, truncated: d.text.length < String(text || '').trim().length });
    } catch (e) {
      return fail(res, e.status || 500, e.message);
    }
  });

  // 要一帧（ferrideo_request_frame）
  gated.post('/api/gate/rooms/:id/frame-request', withRoom, (req, res) => {
    const fr = store.requestFrame(req.room, (req.body || {}).requestedBy);
    return res.status(202).json({
      ok: true,
      requestId: fr.id,
      tip: '已经跟播放器要了一帧，等 3-5 秒后用 ferrideo_get_context 带 includeFrame 取。',
    });
  });

  // 记一笔（ferrideo_add_note）
  gated.post('/api/gate/rooms/:id/note', withRoom, (req, res) => {
    try {
      const n = store.addNote(req.room, { text: (req.body || {}).text, source: 'ai' });
      return res.status(201).json({ ok: true, at: hms(n.positionMs), text: n.text });
    } catch (e) {
      return fail(res, e.status || 500, e.message);
    }
  });

  // 票根（ferrideo_generate_ticket）。存进长期记忆是 python 侧的事，这里只生成
  gated.post('/api/gate/rooms/:id/ticket', withRoom, (req, res) => {
    const { mood, note } = req.body || {};
    return res.json(buildTicket(req.room, { mood, note }));
  });

  // 门禁内的未知路径 404。播放器那套路由压根不在这个 app 上
  gated.use((req, res) => res.status(404).end());
  return app;
}

module.exports = { createGateApp, buildContext, buildRoomOverview, buildTicket, hms, visibleLimit };
