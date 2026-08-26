/* ============================================================
 * ferrideo/store.js — 房间状态（内存）+ 快照落盘 + 磁盘配额
 * ============================================================
 *
 * 内存里一个 Map<roomId, Room> 是运行时的工作存储；两处落盘：
 * - 帧：<DATA_DIR>/frames/<roomId>/<frameId>.jpg，内存只留元信息，**不留 base64**
 * - 房间快照：<DATA_DIR>/rooms/<roomId>.json，每 30 秒写一次（只写变过的），
 *   进程启动时扫目录恢复
 *
 * 三条硬规矩（都在这个文件里）：
 * 1. 快照原子写：临时文件 → fsync → rename → fsync 目录。一场电影两小时
 *    ≈ 240 次写，进程在写到一半被杀不是理论风险；留下半个 JSON，下次启动就炸
 * 2. 坏快照不许阻止启动：逐个文件解析，坏的改名成 .corrupt 并 warning 跳过，
 *    继续下一个。一个坏房间不能拖垮子进程
 * 3. 房间号避开会看错的字符：排除 0 O 1 I L。这六位是要在 iPad 上手输的
 *
 * 磁盘（这块盘只有 1G，还装着 ombre 的记忆桶和 _app/ 代码副本）：
 * - ferrideo 整个目录上限 100 MB，超了从最旧的房间开始删整个帧目录
 * - 写帧前查**整盘剩余**，低于 200 MB 硬停（不是降级）：不写帧、记 error 级日志、
 *   房间标记 frameMode=unavailable。这条不是 ferrideo 的自我约束，是保护 ombre
 *   的热更新/回滚路径——盘满了坏的不是 ferrideo，是 ombre 改不动也回滚不了
 *
 * 不做什么（边界）：
 * - 不接数据库、不做用户系统、不存视频文件
 * - 不在这里做防剧透门禁（那是 MCP 侧的事，但这里维护它需要的
 *   furthestIndex——播放头往回拖不收回已经放过的内容）
 * ============================================================ */

'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { execFileSync } = require('child_process');

// ============================================================
// 调参面板
// ============================================================
const ID_ALPHABET = '23456789ABCDEFGHJKMNPQRSTUVWXYZ';  // 排除 0 O 1 I L
const ID_LENGTH = 6;
const MAX_ROOMS = 5;                    // 同时存在的房间上限（内嵌形态，跟 ombre 抢内存）
const MAX_CUES = 5000;                  // 单房间字幕条数上限
const MAX_CUE_TEXT = 200;               // 单条字幕文本上限（防构造的超长行撑爆内存）
const MAX_DANMAKU = 200;                // 单房间弹幕条数上限
const MAX_DANMAKU_TEXT = 60;            // 单条弹幕字数上限
const MAX_FRAMES_PER_ROOM = 20;         // 单房间保留帧数
const MAX_FRAME_BYTES = 500 * 1024;     // 单帧上限 500 KB，超了 413
const MAX_DANMAKU_ATTEMPTS = 5;         // 下发多少次仍未 ack 就丢弃
const SNAPSHOT_INTERVAL_MS = 30 * 1000;
const CLEANUP_INTERVAL_MS = 30 * 60 * 1000;
const ROOM_TTL_MS = 12 * 60 * 60 * 1000;         // 12 小时无心跳即回收
const FINISHED_GRACE_MS = 60 * 60 * 1000;        // 已结束的房间留多久才可被挤掉（等票根聊出来）
const DISK_QUOTA_BYTES = 100 * 1024 * 1024;      // ferrideo 目录总配额
const MIN_FREE_BYTES = 200 * 1024 * 1024;        // 整盘剩余低于此值：硬停写帧
const FREE_SPACE_CACHE_MS = 10 * 1000;           // 剩余空间查询结果缓存

// ============================================================
// 模块状态
// ============================================================
const rooms = new Map();
const dirty = new Set();            // 有改动、待写快照的房间号
let dataDir = '';
let framesRoot = '';
let roomsRoot = '';
let usedBytes = 0;                  // 帧占用（增量维护，清理时重新校准）
let diskBlocked = false;            // 整盘剩余不足 → 硬停写帧
let freeSpaceCache = { at: 0, bytes: Infinity };
let timers = [];

const log = (...a) => console.log('[ferrideo]', ...a);
const warn = (...a) => console.warn('[ferrideo][warn]', ...a);
const error = (...a) => console.error('[ferrideo][error]', ...a);

// ============================================================
// 小工具
// ============================================================
function newId(len = ID_LENGTH) {
  let out = '';
  for (let i = 0; i < len; i++) out += ID_ALPHABET[crypto.randomInt(ID_ALPHABET.length)];
  return out;
}

function newRoomId() {
  for (let i = 0; i < 50; i++) {
    const id = newId();
    if (!rooms.has(id)) return id;
  }
  throw new Error('房间号生成失败（碰撞太多）');
}

function uid() {
  return crypto.randomBytes(8).toString('hex');
}

function nowIso() {
  return new Date().toISOString();
}

function clampText(s, max) {
  const t = String(s == null ? '' : s).trim();
  return t.length > max ? t.slice(0, max) : t;
}

/** 整盘剩余字节。Node 18.15 之前没有 fs.statfs，回退 df -Pk。 */
function freeBytes(dir) {
  const now = Date.now();
  if (now - freeSpaceCache.at < FREE_SPACE_CACHE_MS) return freeSpaceCache.bytes;
  let bytes = Infinity;
  try {
    if (typeof fs.statfsSync === 'function') {
      const st = fs.statfsSync(dir);
      bytes = st.bavail * st.bsize;
    } else {
      // Debian bookworm 的 node 是 18.13，没有 statfs——用 df 兜底。
      // 帧上传本来就是几分钟一次，spawn 一次 df 的代价可以忽略。
      const out = execFileSync('df', ['-Pk', dir], { encoding: 'utf8' });
      const cols = out.trim().split('\n').pop().split(/\s+/);
      bytes = parseInt(cols[3], 10) * 1024;
    }
  } catch (e) {
    warn('查不到磁盘剩余空间，按「够用」处理：', e.message);
    bytes = Infinity;
  }
  freeSpaceCache = { at: now, bytes };
  return bytes;
}

function dirSize(dir) {
  let total = 0;
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return 0;
  }
  for (const e of entries) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) total += dirSize(p);
    else {
      try { total += fs.statSync(p).size; } catch { /* 文件刚被删，忽略 */ }
    }
  }
  return total;
}

/** 原子写：临时文件 → fsync → rename → fsync 目录。 */
function atomicWrite(file, buf) {
  const tmp = `${file}.tmp-${process.pid}-${Date.now()}`;
  let fd;
  try {
    fd = fs.openSync(tmp, 'w');
    fs.writeFileSync(fd, buf);
    fs.fsyncSync(fd);           // 数据真的落盘，再 rename
  } finally {
    if (fd !== undefined) { try { fs.closeSync(fd); } catch { /* 已关 */ } }
  }
  fs.renameSync(tmp, file);     // rename 在同一文件系统上是原子的
  let dfd;
  try {
    dfd = fs.openSync(path.dirname(file), 'r');
    fs.fsyncSync(dfd);          // 目录项也要落盘，否则崩溃后可能看不到 rename
  } catch {
    /* 某些文件系统不支持对目录 fsync，忽略 */
  } finally {
    if (dfd !== undefined) { try { fs.closeSync(dfd); } catch { /* 已关 */ } }
  }
}

// ============================================================
// 房间
// ============================================================
function makeRoom(id, title) {
  return {
    id,
    title: clampText(title, 200) || '未命名',
    createdAt: nowIso(),
    playback: { state: 'idle', positionMs: 0, durationMs: 0, updatedAt: nowIso() },
    // furthestPositionMs / furthestIndex：防剧透门禁按**历史最远播放位置**判定，
    // 用户往回拖不收回已经放过的内容
    furthestPositionMs: 0,
    furthestIndex: -1,
    subtitle: { loaded: false, cues: [], currentIndex: -1, truncated: false },
    danmaku: [],
    notes: [],
    quotes: [],
    frames: [],                 // { id, file, bytes, positionMs, createdAt }
    frameRequest: null,
    frameMode: 'ok',            // ok | unavailable（黑帧/磁盘不足时由服务端置位）
    frameFailure: null,         // { kind, detail, at }
    playbackFailure: null,      // 选片预检失败上报（R1），给我看她卡在哪
    finishedAt: null,
    watchedMs: 0,
    ticket: null,
  };
}

function touch(room) {
  dirty.add(room.id);
}

function frameDir(roomId) {
  return path.join(framesRoot, roomId);
}

function snapshotPath(roomId) {
  return path.join(roomsRoot, `${roomId}.json`);
}

/** 房间快照：帧只存元信息，不存二进制。 */
function toSnapshot(room) {
  return { ...room, frames: room.frames.map((f) => ({ ...f })) };
}

function dropRoomFiles(roomId) {
  try { fs.rmSync(frameDir(roomId), { recursive: true, force: true }); } catch (e) { warn('删帧目录失败', roomId, e.message); }
  try { fs.rmSync(snapshotPath(roomId), { force: true }); } catch (e) { warn('删快照失败', roomId, e.message); }
}

// ============================================================
// 启动 / 恢复
// ============================================================
function restore() {
  let files = [];
  try {
    files = fs.readdirSync(roomsRoot).filter((f) => f.endsWith('.json'));
  } catch {
    return;
  }
  for (const f of files) {
    const full = path.join(roomsRoot, f);
    try {
      const room = JSON.parse(fs.readFileSync(full, 'utf8'));
      if (!room || typeof room.id !== 'string' || !room.playback) {
        throw new Error('快照结构不对');
      }
      // 恢复后补齐可能缺的新字段（老快照跨版本）
      rooms.set(room.id, { ...makeRoom(room.id, room.title), ...room });
    } catch (e) {
      // 坏快照不许阻止启动：改名隔离 + warning，继续下一个
      const bad = `${full}.corrupt`;
      try { fs.renameSync(full, bad); } catch { /* 改名也失败就算了，反正跳过 */ }
      warn(`快照坏了，已隔离为 ${path.basename(bad)}，跳过：${e.message}`);
    }
  }
  usedBytes = dirSize(framesRoot);
  if (rooms.size) log(`从快照恢复 ${rooms.size} 个房间：${[...rooms.keys()].join('、')}`);
}

function init(opts = {}) {
  dataDir = opts.dataDir || path.join(__dirname, 'data');
  framesRoot = path.join(dataDir, 'frames');
  roomsRoot = path.join(dataDir, 'rooms');
  fs.mkdirSync(framesRoot, { recursive: true });
  fs.mkdirSync(roomsRoot, { recursive: true });
  restore();
  // 定时器 unref：它们不该单独把进程吊着活（HTTP server 才决定进程生命周期）
  if (opts.timers !== false) {
    for (const [fn, ms] of [[flushSnapshots, SNAPSHOT_INTERVAL_MS], [cleanup, CLEANUP_INTERVAL_MS]]) {
      const t = setInterval(fn, ms);
      if (typeof t.unref === 'function') t.unref();
      timers.push(t);
    }
  }
}

function stop() {
  for (const t of timers) clearInterval(t);
  timers = [];
  flushSnapshots();
}

// ============================================================
// 快照 / 清理
// ============================================================
function flushSnapshots() {
  for (const id of [...dirty]) {
    const room = rooms.get(id);
    dirty.delete(id);
    if (!room) continue;
    try {
      atomicWrite(snapshotPath(id), Buffer.from(JSON.stringify(toSnapshot(room)), 'utf8'));
    } catch (e) {
      warn(`写快照失败 ${id}：${e.message}`);
    }
  }
}

function cleanup() {
  const cutoff = Date.now() - ROOM_TTL_MS;
  for (const [id, room] of [...rooms]) {
    if (new Date(room.playback.updatedAt).getTime() < cutoff) {
      rooms.delete(id);
      dirty.delete(id);
      dropRoomFiles(id);
      log(`房间 ${id} 超过 12 小时没有心跳，已回收`);
    }
  }
  usedBytes = dirSize(framesRoot);   // 校准增量维护的偏差
  enforceQuota();
}

/** 超配额时，按最后心跳时间从最旧的房间开始删整个帧目录。 */
function enforceQuota() {
  if (usedBytes <= DISK_QUOTA_BYTES) return;
  const byOldest = [...rooms.values()].sort(
    (a, b) => new Date(a.playback.updatedAt) - new Date(b.playback.updatedAt),
  );
  for (const room of byOldest) {
    if (usedBytes <= DISK_QUOTA_BYTES) break;
    if (!room.frames.length) continue;
    const freed = room.frames.reduce((s, f) => s + f.bytes, 0);
    try { fs.rmSync(frameDir(room.id), { recursive: true, force: true }); } catch { /* 已经没了 */ }
    room.frames = [];
    usedBytes = Math.max(0, usedBytes - freed);
    touch(room);
    warn(`ferrideo 目录超过 ${Math.round(DISK_QUOTA_BYTES / 1048576)} MB，已清空房间 ${room.id} 的帧目录`);
  }
}

// ============================================================
// 对外操作
// ============================================================
/**
 * 名额满了先腾一个：只挤「已经散场的」房间，优先挤票根已生成的，
 * 其次挤散场超过一小时的。
 *
 * 为什么不是散场就删：票根由 AI 事后调 ferrideo_generate_ticket 生成，
 * 里面的 mood/note 是聊出来的。散场即删 = 还没聊完就把摘录和笔记扔了。
 * 为什么不能不挤：一天看第六部片子就建不了房，等 12 小时 TTL 太荒唐。
 */
function evictOneFinished() {
  const now = Date.now();
  const finished = [...rooms.values()]
    .filter((r) => r.finishedAt)
    .sort((a, b) => new Date(a.finishedAt) - new Date(b.finishedAt));
  const victim = finished.find((r) => r.ticket)
    || finished.find((r) => now - new Date(r.finishedAt).getTime() > FINISHED_GRACE_MS);
  if (!victim) return false;
  rooms.delete(victim.id);
  dirty.delete(victim.id);
  dropRoomFiles(victim.id);
  log(`名额满了，回收已散场的房间 ${victim.id}「${victim.title}」（票根${victim.ticket ? '已生成' : '未生成但已过一小时'}）`);
  return true;
}

function createRoom(title, wantedId) {
  if (rooms.size >= MAX_ROOMS) evictOneFinished();
  if (rooms.size >= MAX_ROOMS) {
    const err = new Error(
      `同时最多 ${MAX_ROOMS} 个房间，而且没有已经散场的可以回收。先退出一场（调 /finish）再开`,
    );
    err.status = 409;
    throw err;
  }
  // wantedId：播放器在心跳 404 后重建同名房间用（R3）。房间号是 AI 手里的凭据，
  // 重建必须能拿回原来那个号，否则「房间还在」对她那边不成立。
  let id;
  if (wantedId) {
    id = String(wantedId).toUpperCase();
    if (!/^[A-Z0-9]{6}$/.test(id) || rooms.has(id)) {
      const err = new Error('房间号不合法或已被占用');
      err.status = 409;
      throw err;
    }
  } else {
    id = newRoomId();
  }
  const room = makeRoom(id, title);
  rooms.set(id, room);
  touch(room);
  log(`建房 ${id}「${room.title}」`);
  return room;
}

function getRoom(id) {
  return rooms.get(String(id || '').toUpperCase()) || null;
}

function listRooms() {
  return [...rooms.values()];
}

function heartbeat(room, body = {}) {
  const state = ['playing', 'paused', 'idle'].includes(body.state) ? body.state : 'idle';
  const positionMs = Math.max(0, Number(body.positionMs) || 0);
  const durationMs = Math.max(0, Number(body.durationMs) || 0);
  const idx = Number.isInteger(body.subtitleIndex) ? body.subtitleIndex : -1;

  room.playback = { state, positionMs, durationMs, updatedAt: nowIso() };
  room.subtitle.currentIndex = idx;
  // 门禁按历史最远播放位置判定：往回拖不收回
  if (positionMs > room.furthestPositionMs) room.furthestPositionMs = positionMs;
  if (idx > room.furthestIndex) room.furthestIndex = idx;

  // ack：上一轮下发的弹幕，播放器上屏后在这一轮回执
  const acked = Array.isArray(body.ackedDanmaku) ? body.ackedDanmaku : [];
  if (acked.length) {
    const set = new Set(acked.map(String));
    for (const d of room.danmaku) if (set.has(d.id)) d.delivered = true;
  }

  // 下发未 ack 的；attempts 超限就丢弃（防无限重发）
  const pending = [];
  for (const d of room.danmaku) {
    if (d.delivered) continue;
    if (d.attempts >= MAX_DANMAKU_ATTEMPTS) {
      if (!d.dropped) {
        d.dropped = true;
        warn(`弹幕 ${d.id} 下发 ${d.attempts} 次仍未 ack，丢弃：${d.text}`);
      }
      continue;
    }
    d.attempts += 1;
    pending.push({ id: d.id, text: d.text, author: d.author });
  }
  touch(room);
  return { ok: true, pendingDanmaku: pending, frameRequest: room.frameRequest };
}

function setSubtitle(room, cues) {
  const list = Array.isArray(cues) ? cues : [];
  const truncated = list.length > MAX_CUES;
  room.subtitle.cues = list.slice(0, MAX_CUES).map((c) => ({
    startMs: Math.max(0, Number(c.startMs) || 0),
    endMs: Math.max(0, Number(c.endMs) || 0),
    text: clampText(c.text, MAX_CUE_TEXT),
  }));
  room.subtitle.loaded = room.subtitle.cues.length > 0;
  room.subtitle.truncated = truncated;
  touch(room);
  return { ok: true, count: room.subtitle.cues.length, truncated, max: MAX_CUES };
}

function addDanmaku(room, { text, author, source = 'user' }) {
  const t = clampText(text, MAX_DANMAKU_TEXT);
  if (!t) {
    const err = new Error('弹幕不能是空的');
    err.status = 400;
    throw err;
  }
  const item = {
    id: uid(), text: t, author: clampText(author, 20) || '匿名',
    source, createdAt: nowIso(), delivered: false, attempts: 0,
  };
  room.danmaku.push(item);
  if (room.danmaku.length > MAX_DANMAKU) room.danmaku.splice(0, room.danmaku.length - MAX_DANMAKU);
  touch(room);
  return item;
}

function addQuote(room, { text, positionMs }) {
  const t = clampText(text, 500);
  if (!t) {
    const err = new Error('摘录不能是空的');
    err.status = 400;
    throw err;
  }
  const item = { id: uid(), text: t, positionMs: Math.max(0, Number(positionMs) || 0), createdAt: nowIso() };
  room.quotes.push(item);
  touch(room);
  return item;
}

function addNote(room, { text, source = 'ai' }) {
  const t = clampText(text, 1000);
  if (!t) {
    const err = new Error('笔记不能是空的');
    err.status = 400;
    throw err;
  }
  const item = { id: uid(), text: t, positionMs: room.playback.positionMs, source, createdAt: nowIso() };
  room.notes.push(item);
  touch(room);
  return item;
}

function requestFrame(room, requestedBy) {
  room.frameRequest = { id: uid(), requestedBy: clampText(requestedBy, 20) || '克', requestedAt: nowIso() };
  touch(room);
  return room.frameRequest;
}

/** 帧写盘。内存里只留元信息，不留 base64。 */
function saveFrame(room, { buffer, positionMs }) {
  if (!buffer || !buffer.length) {
    const err = new Error('帧是空的');
    err.status = 400;
    throw err;
  }
  if (buffer.length > MAX_FRAME_BYTES) {
    const err = new Error(`单帧上限 ${Math.round(MAX_FRAME_BYTES / 1024)} KB，这张 ${Math.round(buffer.length / 1024)} KB`);
    err.status = 413;
    throw err;
  }
  // 硬停：整盘剩余不足。这条保护的是 ombre 的热更新/回滚路径，不是 ferrideo 自己
  const free = freeBytes(dataDir);
  if (free < MIN_FREE_BYTES) {
    diskBlocked = true;
    room.frameMode = 'unavailable';
    room.frameFailure = { kind: 'disk', detail: `整盘剩余仅 ${Math.round(free / 1048576)} MB`, at: nowIso() };
    room.frameRequest = null;
    touch(room);
    error(`磁盘剩余 ${Math.round(free / 1048576)} MB，低于 ${Math.round(MIN_FREE_BYTES / 1048576)} MB 下限：停止写帧。` +
          '这块盘还装着 ombre 的记忆桶和 _app/ 代码副本，写满会导致 ombre 无法热更新或回滚。');
    const err = new Error('磁盘空间不足，已停止写帧');
    err.status = 507;
    throw err;
  }
  diskBlocked = false;

  const dir = frameDir(room.id);
  fs.mkdirSync(dir, { recursive: true });
  const id = uid();
  const file = path.join(dir, `${id}.jpg`);
  atomicWrite(file, buffer);

  const meta = { id, file, bytes: buffer.length, positionMs: Math.max(0, Number(positionMs) || 0), createdAt: nowIso() };
  room.frames.push(meta);
  usedBytes += buffer.length;

  while (room.frames.length > MAX_FRAMES_PER_ROOM) {
    const old = room.frames.shift();
    try { fs.rmSync(old.file, { force: true }); } catch { /* 已经没了 */ }
    usedBytes = Math.max(0, usedBytes - old.bytes);
  }
  room.frameRequest = null;
  room.frameMode = 'ok';
  room.frameFailure = null;
  touch(room);
  enforceQuota();
  return meta;
}

/** 读回某一帧的二进制（MCP 侧现读现编码用）。 */
function readFrame(room, frameId) {
  const meta = frameId ? room.frames.find((f) => f.id === frameId) : room.frames[room.frames.length - 1];
  if (!meta) return null;
  try {
    return { meta, buffer: fs.readFileSync(meta.file) };
  } catch (e) {
    warn(`帧文件读不到 ${meta.file}：${e.message}`);
    return null;
  }
}

/** 播放器上报抓帧失败（黑帧 / toDataURL 抛异常）或选片预检失败。 */
function reportFailure(room, { scope = 'frame', kind, detail }) {
  const rec = { kind: clampText(kind, 40) || 'unknown', detail: clampText(detail, 500), at: nowIso() };
  if (scope === 'playback') {
    room.playbackFailure = rec;
  } else {
    room.frameFailure = rec;
    room.frameMode = 'unavailable';
    room.frameRequest = null;
  }
  touch(room);
  warn(`房间 ${room.id} 上报${scope === 'playback' ? '选片预检' : '抓帧'}失败：${rec.kind} ${rec.detail}`);
  return rec;
}

/**
 * 这一场结束。**幂等**：重复调不重置 watchedMs（取较大值，只增不减），
 * finishedAt 保留第一次。按 v2.1，房间结束立刻删帧目录，不等 12 小时清理。
 */
function finish(room, watchedMs) {
  const incoming = Math.max(0, Number(watchedMs) || 0);
  const first = room.finishedAt === null;
  room.watchedMs = Math.max(room.watchedMs || 0, incoming);
  if (first) room.finishedAt = nowIso();
  room.playback.state = 'idle';
  room.playback.updatedAt = nowIso();
  if (room.frames.length) {
    const freed = room.frames.reduce((s, f) => s + f.bytes, 0);
    try { fs.rmSync(frameDir(room.id), { recursive: true, force: true }); } catch { /* 已经没了 */ }
    room.frames = [];
    usedBytes = Math.max(0, usedBytes - freed);
  }
  touch(room);
  flushSnapshots();               // 结束这一刻的状态值得立刻落盘
  log(`房间 ${room.id} 结束，观看时长 ${Math.round(room.watchedMs / 1000)}s${first ? '' : '（重复调用，未重置）'}`);
  return { ok: true, watchedMs: room.watchedMs, finishedAt: room.finishedAt, repeated: !first };
}

function setTicket(room, ticket) {
  room.ticket = ticket;
  touch(room);
  flushSnapshots();
  return ticket;
}

function stats() {
  return {
    rooms: rooms.size,
    usedBytes,
    quotaBytes: DISK_QUOTA_BYTES,
    freeBytes: freeBytes(dataDir),
    diskBlocked,
  };
}

module.exports = {
  init, stop, flushSnapshots, cleanup, enforceQuota,
  createRoom, getRoom, listRooms,
  heartbeat, setSubtitle, addDanmaku, addQuote, addNote,
  requestFrame, saveFrame, readFrame, reportFailure, finish, setTicket, evictOneFinished,
  stats,
  // 给测试和路由用的常量/内部件
  _internals: {
    rooms, dirty, newId, newRoomId, atomicWrite, dirSize, ID_ALPHABET,
    MAX_ROOMS, MAX_CUES, MAX_DANMAKU, MAX_FRAMES_PER_ROOM, MAX_FRAME_BYTES,
    MAX_DANMAKU_ATTEMPTS, MAX_DANMAKU_TEXT, ROOM_TTL_MS, FINISHED_GRACE_MS,
    DISK_QUOTA_BYTES, MIN_FREE_BYTES,
    frameDir, snapshotPath,
  },
};
