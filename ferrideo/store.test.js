/* ============================================================
 * ferrideo/store.test.js — 内存 store + 快照回归
 * 跑法：cd ferrideo && node --test
 * ============================================================
 *
 * 钉住的东西（每条都对应一次真实的翻车方式）：
 * - 房间号 6 位、且不含 0 O 1 I L —— 这六位要在 iPad 上手输
 * - 帧超量丢最旧的，留下的是最新的那些，文件也真的被删掉
 * - 快照原子写：临时文件不残留；写到一半被杀不会留下半个 JSON
 * - 坏快照不阻止启动：改名 .corrupt + 跳过，好房间照常恢复
 * - /finish 幂等：重复调不重置 watchedMs
 * - 弹幕 ack：没 ack 的继续下发，超过 5 次丢弃
 * - 磁盘剩余不足时硬停写帧（保护 ombre 的热更新/回滚路径）
 * ============================================================ */

'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

function freshStore(dataDir) {
  delete require.cache[require.resolve('./store')];
  const store = require('./store');
  store.init({ dataDir, timers: false });   // 测试里不要后台定时器
  return store;
}

function tmpDir(tag) {
  return fs.mkdtempSync(path.join(os.tmpdir(), `ferrideo-${tag}-`));
}

// ============================================================
// 房间号
// ============================================================
test('房间号是 6 位，且不含会看错的字符 0 O 1 I L', () => {
  const store = freshStore(tmpDir('id'));
  const seen = new Set();
  for (let i = 0; i < 2000; i++) {
    const id = store._internals.newId();
    assert.strictEqual(id.length, 6);
    assert.match(id, /^[A-Z0-9]{6}$/);
    for (const ch of id) {
      assert.ok(!'0O1IL'.includes(ch), `房间号里出现了会看错的字符：${ch}（${id}）`);
      seen.add(ch);
    }
  }
  assert.strictEqual(seen.size, store._internals.ID_ALPHABET.length, '字母表没被均匀取到');
});

test('建房返回 6 位 id；房间数封顶', () => {
  const store = freshStore(tmpDir('rooms'));
  const room = store.createRoom('挪威的森林');
  assert.match(room.id, /^[A-Z0-9]{6}$/);
  assert.strictEqual(room.title, '挪威的森林');
  for (let i = 1; i < store._internals.MAX_ROOMS; i++) store.createRoom(`片子${i}`);
  assert.throws(() => store.createRoom('第六部'), /最多/);
});

test('名额满了：挤掉已散场的房间，但不挤还没生成票根的新鲜尸体', () => {
  const store = freshStore(tmpDir('evict'));
  const cap = store._internals.MAX_ROOMS;
  const first = store.createRoom('第一部');
  for (let i = 1; i < cap; i++) store.createRoom(`第${i + 1}部`);

  // 全都在放映中 → 挤不动，明确报错
  assert.throws(() => store.createRoom('第六部'), /没有已经散场的可以回收/);

  // 第一部散场了，但票根还没聊出来、也没过一小时 → 仍然不许挤
  store.finish(first, 7200000);
  assert.throws(() => store.createRoom('第六部'), /没有已经散场的可以回收/);

  // 票根生成了 → 可以挤
  store.setTicket(first, { title: '第一部', mood: '安静' });
  const sixth = store.createRoom('第六部');
  assert.ok(sixth.id);
  assert.strictEqual(store.getRoom(first.id), null, '票根已生成的旧房间应被回收');
});

test('淘汰没票根的房间前，先把摘录和笔记抢救进降级通道', () => {
  const dir = tmpDir('rescue');
  const store = freshStore(dir);
  const cap = store._internals.MAX_ROOMS;
  const first = store.createRoom('没聊完就散了');
  store.addQuote(first, { text: '如果我多一张船票', positionMs: 1834000 });
  store.addNote(first, { text: '她一直没回头' });
  for (let i = 1; i < cap; i++) store.createRoom(`第${i + 1}部`);
  store.finish(first, 7200000);
  first.finishedAt = new Date(Date.now() - store._internals.FINISHED_GRACE_MS - 1000).toISOString();

  store.createRoom('新的一部');
  assert.strictEqual(store.getRoom(first.id), null, '房间应被回收');

  const rescued = fs.readFileSync(path.join(dir, store._internals.RESCUE_FILE), 'utf8').trim();
  const rec = JSON.parse(rescued);
  assert.strictEqual(rec.roomId, first.id);
  assert.strictEqual(rec.quotes[0].text, '如果我多一张船票');
  assert.strictEqual(rec.notes[0].text, '她一直没回头');
});

test('抢救写不进去就不许淘汰——宁可建房失败也不能无声无息丢掉', () => {
  const dir = tmpDir('rescuefail');
  const store = freshStore(dir);
  const cap = store._internals.MAX_ROOMS;
  const first = store.createRoom('有摘录的');
  store.addQuote(first, { text: '不能丢的一句', positionMs: 1000 });
  for (let i = 1; i < cap; i++) store.createRoom(`第${i + 1}部`);
  store.finish(first, 1000);
  first.finishedAt = new Date(Date.now() - store._internals.FINISHED_GRACE_MS - 1000).toISOString();

  const realOpen = fs.openSync;
  fs.openSync = (f, ...rest) => {
    if (String(f).endsWith(store._internals.RESCUE_FILE)) throw new Error('盘满了');
    return realOpen(f, ...rest);
  };
  try {
    assert.throws(() => store.createRoom('新的一部'), /没有已经散场的可以回收/);
  } finally {
    fs.openSync = realOpen;
  }
  assert.ok(store.getRoom(first.id), '抢救失败时房间必须留着');
  assert.strictEqual(store.getRoom(first.id).quotes[0].text, '不能丢的一句');
});

test('票根已生成的房间被淘汰时不需要抢救（内容已经进记忆了）', () => {
  const dir = tmpDir('noresc');
  const store = freshStore(dir);
  const cap = store._internals.MAX_ROOMS;
  const first = store.createRoom('已出票根');
  store.addQuote(first, { text: '这句已经在票根里了', positionMs: 1000 });
  for (let i = 1; i < cap; i++) store.createRoom(`第${i + 1}部`);
  store.finish(first, 1000);
  store.setTicket(first, { title: '已出票根' });

  store.createRoom('新的一部');
  assert.strictEqual(store.getRoom(first.id), null);
  assert.ok(!fs.existsSync(path.join(dir, store._internals.RESCUE_FILE)), '不该产生抢救文件');
});

test('散场超过一小时的房间，即使没票根也可以被挤掉', () => {
  const store = freshStore(tmpDir('evictold'));
  const cap = store._internals.MAX_ROOMS;
  const first = store.createRoom('很久以前');
  for (let i = 1; i < cap; i++) store.createRoom(`第${i + 1}部`);
  store.finish(first, 1000);
  first.finishedAt = new Date(Date.now() - store._internals.FINISHED_GRACE_MS - 1000).toISOString();
  assert.ok(store.createRoom('新的一部').id);
  assert.strictEqual(store.getRoom(first.id), null);
});

test('可以指定房间号重建（心跳 404 后播放器拿回原来那个号）', () => {
  const store = freshStore(tmpDir('rebuild'));
  const room = store.createRoom('重建', 'K7X2M9');
  assert.strictEqual(room.id, 'K7X2M9');
  assert.throws(() => store.createRoom('撞号', 'K7X2M9'), /已被占用/);
});

// ============================================================
// 帧
// ============================================================
test('帧超过上限丢最旧的，留下的是最新的，文件也真删了', () => {
  const dir = tmpDir('frames');
  const store = freshStore(dir);
  const room = store.createRoom('帧测试');
  const cap = store._internals.MAX_FRAMES_PER_ROOM;
  const metas = [];
  for (let i = 0; i < cap + 1; i++) {
    metas.push(store.saveFrame(room, { buffer: Buffer.from(`帧${i}`), positionMs: i * 1000 }));
  }
  assert.strictEqual(room.frames.length, cap);
  assert.strictEqual(room.frames[0].positionMs, 1000, '留下的应该是最新的那些');
  assert.strictEqual(room.frames.at(-1).positionMs, cap * 1000);
  assert.ok(!fs.existsSync(metas[0].file), '被挤掉的帧文件应该从盘上删掉');
  assert.ok(fs.existsSync(room.frames.at(-1).file));
  // 内存里只有元信息，没有二进制
  assert.ok(!('dataUrl' in room.frames[0]) && !('buffer' in room.frames[0]));
});

test('单帧超过 500 KB 拒收，返回 413', () => {
  const store = freshStore(tmpDir('big'));
  const room = store.createRoom('大帧');
  const big = Buffer.alloc(store._internals.MAX_FRAME_BYTES + 1, 0x41);
  assert.throws(() => store.saveFrame(room, { buffer: big }), (e) => e.status === 413);
});

test('整盘剩余不足时硬停写帧（保护 ombre 的回滚路径）', () => {
  const dir = tmpDir('disk');
  const store = freshStore(dir);
  const room = store.createRoom('磁盘');
  const realStatfs = fs.statfsSync;
  fs.statfsSync = () => ({ bavail: 1, bsize: 4096 });   // 假装只剩 4 KB
  try {
    assert.throws(() => store.saveFrame(room, { buffer: Buffer.from('x') }), (e) => e.status === 507);
  } finally {
    fs.statfsSync = realStatfs;
  }
  assert.strictEqual(room.frameMode, 'unavailable');
  assert.strictEqual(room.frameFailure.kind, 'disk');
  assert.strictEqual(store.stats().diskBlocked, true);
});

// ============================================================
// 快照
// ============================================================
test('快照原子写：不留临时文件，内容可解析', () => {
  const dir = tmpDir('snap');
  const store = freshStore(dir);
  const room = store.createRoom('快照');
  store.addQuote(room, { text: '那一晚', positionMs: 1000 });
  store.flushSnapshots();
  const roomsDir = path.join(dir, 'rooms');
  const files = fs.readdirSync(roomsDir);
  assert.deepStrictEqual(files, [`${room.id}.json`], `目录里不该有临时文件：${files}`);
  const back = JSON.parse(fs.readFileSync(path.join(roomsDir, files[0]), 'utf8'));
  assert.strictEqual(back.quotes[0].text, '那一晚');
});

test('进程重启后从快照恢复', () => {
  const dir = tmpDir('restore');
  let store = freshStore(dir);
  const room = store.createRoom('恢复');
  store.heartbeat(room, { state: 'playing', positionMs: 12345, durationMs: 7260000, subtitleIndex: 7 });
  store.addDanmaku(room, { text: '这里好看', author: '克' });
  store.flushSnapshots();

  store = freshStore(dir);                       // 模拟重启
  const back = store.getRoom(room.id);
  assert.ok(back, '房间应该被恢复');
  assert.strictEqual(back.title, '恢复');
  assert.strictEqual(back.playback.positionMs, 12345);
  assert.strictEqual(back.furthestIndex, 7);
  assert.strictEqual(back.danmaku[0].text, '这里好看');
});

test('写到一半被杀：残留的临时文件不会被当成房间读回来', () => {
  const dir = tmpDir('halfwrite');
  let store = freshStore(dir);
  const room = store.createRoom('半截');
  store.flushSnapshots();
  // 原子写的中间态：临时文件已存在、rename 还没发生。这正是进程被杀时盘上的样子
  const roomsDir = path.join(dir, 'rooms');
  fs.writeFileSync(path.join(roomsDir, 'ZZZZZZ.json.tmp-999-1'), '{"id":"ZZZZZZ","play', 'utf8');

  store = freshStore(dir);
  assert.ok(store.getRoom(room.id), '好房间照常恢复');
  assert.strictEqual(store.listRooms().length, 1, '半截的临时文件不该变成房间');
  // 它既不会被读、也不会被误判成坏快照去改名——恢复只认 .json
  assert.ok(fs.existsSync(path.join(roomsDir, 'ZZZZZZ.json.tmp-999-1')));
});

test('坏快照不阻止启动：改名 .corrupt 并跳过，好房间照常恢复', () => {
  const dir = tmpDir('corrupt');
  let store = freshStore(dir);
  const good = store.createRoom('好房间');
  store.flushSnapshots();
  // 半个 JSON —— 正是「写到一半被杀」会留下的东西
  fs.writeFileSync(path.join(dir, 'rooms', 'BADBAD.json'), '{"id":"BADBAD","play', 'utf8');

  store = freshStore(dir);                       // 不许抛
  assert.ok(store.getRoom(good.id), '好房间必须照常恢复');
  assert.strictEqual(store.getRoom('BADBAD'), null);
  const left = fs.readdirSync(path.join(dir, 'rooms'));
  assert.ok(left.includes('BADBAD.json.corrupt'), `坏快照应被隔离，实际：${left}`);
  assert.ok(!left.includes('BADBAD.json'));
});

// ============================================================
// 心跳 / 弹幕 ack
// ============================================================
test('门禁按历史最远播放位置：往回拖不收回', () => {
  const store = freshStore(tmpDir('gate'));
  const room = store.createRoom('回看');
  store.heartbeat(room, { state: 'playing', positionMs: 600000, subtitleIndex: 300 });
  store.heartbeat(room, { state: 'playing', positionMs: 60000, subtitleIndex: 30 });   // 拖回去
  assert.strictEqual(room.playback.positionMs, 60000);
  assert.strictEqual(room.furthestPositionMs, 600000);
  assert.strictEqual(room.furthestIndex, 300);
});

test('弹幕要 ack：没 ack 的继续下发，ack 了就不再下发', () => {
  const store = freshStore(tmpDir('ack'));
  const room = store.createRoom('弹幕');
  const d = store.addDanmaku(room, { text: '她要哭了', author: '克' });

  let r = store.heartbeat(room, {});
  assert.strictEqual(r.pendingDanmaku.length, 1);
  r = store.heartbeat(room, {});                       // 没 ack：继续下发
  assert.strictEqual(r.pendingDanmaku.length, 1);
  // ack 在同一次心跳里先于「算待发」处理，所以带 ack 的那一轮就不会再下发了
  r = store.heartbeat(room, { ackedDanmaku: [d.id] });
  assert.strictEqual(r.pendingDanmaku.length, 0, '带 ack 的那一轮就不该再下发');
  r = store.heartbeat(room, {});
  assert.strictEqual(r.pendingDanmaku.length, 0, 'ack 过的不该再下发');
});

test('弹幕重发超过 5 次就丢弃，不无限重发', () => {
  const store = freshStore(tmpDir('attempts'));
  const room = store.createRoom('重发');
  store.addDanmaku(room, { text: '丢不掉的一句' });
  const max = store._internals.MAX_DANMAKU_ATTEMPTS;
  for (let i = 0; i < max; i++) {
    assert.strictEqual(store.heartbeat(room, {}).pendingDanmaku.length, 1, `第 ${i + 1} 次应仍下发`);
  }
  assert.strictEqual(store.heartbeat(room, {}).pendingDanmaku.length, 0, '超过上限应丢弃');
});

test('弹幕条数封顶，丢最旧的', () => {
  const store = freshStore(tmpDir('cap'));
  const room = store.createRoom('刷屏');
  const cap = store._internals.MAX_DANMAKU;
  for (let i = 0; i < cap + 10; i++) store.addDanmaku(room, { text: `第${i}条` });
  assert.strictEqual(room.danmaku.length, cap);
  assert.strictEqual(room.danmaku[0].text, '第10条');
});

test('弹幕文本超 60 字截断', () => {
  const store = freshStore(tmpDir('long'));
  const room = store.createRoom('长弹幕');
  const d = store.addDanmaku(room, { text: '啊'.repeat(200) });
  assert.strictEqual(d.text.length, store._internals.MAX_DANMAKU_TEXT);
});

// ============================================================
// 字幕 / finish
// ============================================================
test('字幕超过 5000 条截断并说明', () => {
  const store = freshStore(tmpDir('cues'));
  const room = store.createRoom('长片');
  const cues = Array.from({ length: 5100 }, (_, i) => ({ startMs: i * 1000, endMs: i * 1000 + 500, text: `第${i}句` }));
  const out = store.setSubtitle(room, cues);
  assert.strictEqual(out.count, store._internals.MAX_CUES);
  assert.strictEqual(out.truncated, true);
  assert.strictEqual(room.subtitle.loaded, true);
});

test('没有字幕时 loaded 为 false（无字幕降级的前提）', () => {
  const store = freshStore(tmpDir('nosub'));
  const room = store.createRoom('硬字幕片');
  assert.strictEqual(room.subtitle.loaded, false);
  store.setSubtitle(room, []);
  assert.strictEqual(room.subtitle.loaded, false);
});

test('/finish 幂等：重复调不重置 watchedMs', () => {
  const store = freshStore(tmpDir('finish'));
  const room = store.createRoom('散场');
  const first = store.finish(room, 7092000);
  assert.strictEqual(first.watchedMs, 7092000);
  assert.strictEqual(first.repeated, false);
  const again = store.finish(room, 0);                  // 重复调，且带了个 0
  assert.strictEqual(again.watchedMs, 7092000, '重复调不许把 watchedMs 冲掉');
  assert.strictEqual(again.repeated, true);
  assert.strictEqual(again.finishedAt, first.finishedAt, 'finishedAt 保留第一次');
  const longer = store.finish(room, 7200000);           // 真的又看了一会儿
  assert.strictEqual(longer.watchedMs, 7200000, '只增不减');
});

test('房间结束立刻删帧目录，不等 12 小时清理', () => {
  const dir = tmpDir('finishframes');
  const store = freshStore(dir);
  const room = store.createRoom('删帧');
  const meta = store.saveFrame(room, { buffer: Buffer.from('画面'), positionMs: 1000 });
  assert.ok(fs.existsSync(meta.file));
  store.finish(room, 1000);
  assert.strictEqual(room.frames.length, 0);
  assert.ok(!fs.existsSync(store._internals.frameDir(room.id)), '帧目录应该整个删掉');
});

test('12 小时没有心跳的房间被回收，帧和快照一起删', () => {
  const dir = tmpDir('ttl');
  const store = freshStore(dir);
  const room = store.createRoom('过期');
  store.saveFrame(room, { buffer: Buffer.from('画面') });
  store.flushSnapshots();
  room.playback.updatedAt = new Date(Date.now() - store._internals.ROOM_TTL_MS - 1000).toISOString();
  store.cleanup();
  assert.strictEqual(store.getRoom(room.id), null);
  assert.ok(!fs.existsSync(store._internals.snapshotPath(room.id)));
  assert.ok(!fs.existsSync(store._internals.frameDir(room.id)));
});
