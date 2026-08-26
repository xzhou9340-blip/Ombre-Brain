/* ============================================================
 * ferrideo/routes.js — HTTP API（播放器 ↔ 后端）
 * ============================================================
 *
 * 全部挂在 /<token>/api/* 下（门禁在 server.js，这里拿到的请求已经过了 token）。
 * 原规格书 5.0 的 Bearer token 方案作废（v2 §1.2：照 read-along 走路径 token）。
 *
 * 端点：
 *   POST /api/rooms                 建房（body.id 可选：心跳 404 后重建同号房间）
 *   GET  /api/rooms/:id             房间状态（不含字幕全文、不含帧二进制）
 *   GET  /api/rooms/:id/subtitle    字幕 cues（播放器加入已有房间时取回，见下）
 *   POST /api/rooms/:id/heartbeat   心跳（3 秒一次，捎回待办弹幕 + 抓帧请求）
 *   POST /api/rooms/:id/subtitle    上传字幕
 *   POST /api/rooms/:id/frame       上传帧（raw JPEG body，不走 JSON 解析器）
 *   POST /api/rooms/:id/quotes      记下这句
 *   POST /api/rooms/:id/danmaku     用户自己发弹幕
 *   POST /api/rooms/:id/failure     播放器上报失败（选片预检 / 黑帧）
 *   POST /api/rooms/:id/finish      这一场结束（幂等，不生成票根）
 *
 * 两处与原规格书 5.2 的偏离，理由都是「响应体不要爆炸」：
 * - GET 房间不返回 subtitle.cues 全文（5000 条能到几百 KB），改成
 *   loaded/cueCount/currentIndex；要 cues 走单独的 GET .../subtitle。
 *   原规格书自己就为 frames 开了同样的口子
 * - 上传字幕走 raw body 自己 JSON.parse，不走 express.json——全局 JSON
 *   解析器按 v2 §1.4 锁在 1 MB，不为字幕撑大它
 *
 * 不做什么（边界）：
 * - 没有票根端点。票根只能由 AI 调 ferrideo_generate_ticket 生成（v2 §Q1）：
 *   票根里的 mood/note 是聊出来的，不是一个退出按钮能吐出来的
 * ============================================================ */

'use strict';

const express = require('express');
const store = require('./store');

const MAX_SUBTITLE_BODY = '4mb';   // 字幕 raw body 上限（5000 条中文字幕的量级）
const MAX_FRAME_BODY = '600kb';    // 帧 raw body 上限；真正的 500 KB 判定在 store

/** 统一错误响应：中文、带状态码。 */
function fail(res, status, msg) {
  return res.status(status).json({ error: msg });
}

/** 取房间，取不到就 404。 */
function withRoom(req, res, next) {
  const room = store.getRoom(req.params.id);
  if (!room) return fail(res, 404, '房间不存在，或者已经过期了');
  req.room = room;
  return next();
}

/** 对外的房间视图：不含字幕全文、不含帧二进制。 */
function roomView(room) {
  return {
    id: room.id,
    title: room.title,
    createdAt: room.createdAt,
    playback: room.playback,
    subtitle: {
      loaded: room.subtitle.loaded,
      cueCount: room.subtitle.cues.length,
      currentIndex: room.subtitle.currentIndex,
      truncated: room.subtitle.truncated,
    },
    danmaku: room.danmaku.map(({ id, text, author, source, createdAt, delivered }) =>
      ({ id, text, author, source, createdAt, delivered })),
    notes: room.notes,
    quotes: room.quotes,
    frames: room.frames.map(({ id, positionMs, createdAt }) => ({ id, positionMs, createdAt })),
    frameRequest: room.frameRequest,
    frameMode: room.frameMode,
    frameFailure: room.frameFailure,
    playbackFailure: room.playbackFailure,
    finishedAt: room.finishedAt,
    watchedMs: room.watchedMs,
    ticket: room.ticket,
  };
}

function register(router) {
  // ---- 建房 ----------------------------------------------------------
  router.post('/api/rooms', (req, res) => {
    const { title, id } = req.body || {};
    try {
      return res.status(201).json(roomView(store.createRoom(title, id)));
    } catch (e) {
      return fail(res, e.status || 500, e.message);
    }
  });

  // ---- 房间状态 ------------------------------------------------------
  router.get('/api/rooms/:id', withRoom, (req, res) => res.json(roomView(req.room)));

  // ---- 字幕：取回（加入已有房间时，本机没选字幕就用房间里的，v2 §Q3）----
  router.get('/api/rooms/:id/subtitle', withRoom, (req, res) => {
    const { subtitle } = req.room;
    res.json({ loaded: subtitle.loaded, truncated: subtitle.truncated, cues: subtitle.cues });
  });

  // ---- 心跳 ----------------------------------------------------------
  router.post('/api/rooms/:id/heartbeat', withRoom, (req, res) => {
    res.json(store.heartbeat(req.room, req.body || {}));
  });

  // ---- 字幕：上传（raw body，自己 parse）------------------------------
  router.post(
    '/api/rooms/:id/subtitle',
    withRoom,
    express.raw({ type: () => true, limit: MAX_SUBTITLE_BODY }),
    (req, res) => {
      let body;
      try {
        // 正常路径拿到的是 Buffer；万一上游解析器变了，对象也照收，不静默变成空字幕
        body = Buffer.isBuffer(req.body) ? JSON.parse(req.body.toString('utf8')) : (req.body || {});
      } catch (e) {
        return fail(res, 400, `字幕 JSON 解析不了：${e.message}`);
      }
      const out = store.setSubtitle(req.room, body.cues);
      const note = out.truncated
        ? `字幕超过 ${out.max} 条，已截断到 ${out.count} 条（后面的不会出现在这一场里）`
        : null;
      return res.json({ ...out, note });
    },
  );

  // ---- 帧上传（raw JPEG）---------------------------------------------
  router.post(
    '/api/rooms/:id/frame',
    withRoom,
    express.raw({ type: () => true, limit: MAX_FRAME_BODY }),
    (req, res) => {
      try {
        const meta = store.saveFrame(req.room, {
          buffer: req.body,
          positionMs: req.query.positionMs,
        });
        return res.json({ ok: true, frame: { id: meta.id, positionMs: meta.positionMs, bytes: meta.bytes } });
      } catch (e) {
        return fail(res, e.status || 500, e.message);
      }
    },
  );

  // ---- 记下这句 ------------------------------------------------------
  router.post('/api/rooms/:id/quotes', withRoom, (req, res) => {
    try {
      return res.status(201).json(store.addQuote(req.room, req.body || {}));
    } catch (e) {
      return fail(res, e.status || 500, e.message);
    }
  });

  // ---- 用户自己发弹幕 ------------------------------------------------
  router.post('/api/rooms/:id/danmaku', withRoom, (req, res) => {
    try {
      const { text, author } = req.body || {};
      return res.status(201).json(store.addDanmaku(req.room, { text, author, source: 'user' }));
    } catch (e) {
      return fail(res, e.status || 500, e.message);
    }
  });

  // ---- 失败上报（R1 选片预检 / R2 黑帧）------------------------------
  // 走后端而不是 console：这样我能看到她卡在哪，不用她自己描述
  router.post('/api/rooms/:id/failure', withRoom, (req, res) => {
    const { scope, kind, detail } = req.body || {};
    return res.status(201).json(store.reportFailure(req.room, { scope, kind, detail }));
  });

  // ---- 还没建房就失败了（选片预检挂在这里）----------------------------
  // 没有这条的话，R1 的信号永远到不了后端：预检失败时根本没有房间可挂
  router.post('/api/failures', (req, res) => {
    const { scope, kind, detail } = req.body || {};
    return res.status(201).json(store.reportOrphanFailure({ scope, kind, detail }));
  });

  // ---- 这一场结束（幂等）---------------------------------------------
  router.post('/api/rooms/:id/finish', withRoom, (req, res) => {
    const { watchedMs } = req.body || {};
    return res.json(store.finish(req.room, watchedMs));
  });
}

module.exports = { register, roomView };
