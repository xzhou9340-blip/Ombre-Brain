/* ============================================================
 * ferrideo/gate.test.js — 防剧透门禁回归
 * 跑法：cd ferrideo && node --test
 * ============================================================
 *
 * 这个文件只钉一件事：**从 MCP 那侧，拿不到播放头之后的任何一句台词。**
 * 而且不是靠「python 侧不去调那个端点」的约定，是靠 gate 跑在自己的
 * app 上、播放器路由压根不在那儿。所以这里同时钉两层：
 *
 * 1. 语义层：context 只给当前句 + 之前 3 句，nextLineExists 只给布尔值
 * 2. 结构层：gate 端口上没有任何播放器路由——包括拿原始路径带 ../ 去绕
 * ============================================================ */

'use strict';

const test = require('node:test');
const assert = require('node:assert');
const net = require('node:net');
const fs = require('fs');
const os = require('os');
const path = require('path');

const TOKEN = 'gate-test-token';

function boot() {
  delete require.cache[require.resolve('./store')];
  delete require.cache[require.resolve('./gate')];
  const store = require('./store');
  const gate = require('./gate');
  store.init({ dataDir: fs.mkdtempSync(path.join(os.tmpdir(), 'ferrideo-gate-')), timers: false });
  const server = gate.createGateApp({ token: TOKEN }).listen(0, '127.0.0.1');
  return { store, gate, server, base: () => `http://127.0.0.1:${server.address().port}/${TOKEN}/api/gate` };
}

async function ready(server) {
  if (!server.listening) await new Promise((r) => server.once('listening', r));
}

/** 拿原始 socket 发一个不被 URL 规范化的请求路径（fetch 会把 ../ 吃掉）。 */
function rawGet(port, rawPath) {
  return new Promise((resolve, reject) => {
    const sock = net.connect(port, '127.0.0.1', () => {
      sock.write(`GET ${rawPath} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n`);
    });
    let buf = '';
    sock.on('data', (d) => { buf += d.toString('utf8'); });
    sock.on('end', () => resolve(buf));
    sock.on('error', reject);
  });
}

/** 一部有 100 句台词的片子，播放头停在第 40 句。 */
function movieAt(store, index) {
  const room = store.createRoom('剧透测试');
  store.setSubtitle(room, Array.from({ length: 100 }, (_, i) => ({
    startMs: i * 10000, endMs: i * 10000 + 8000, text: `第${i}句`,
  })));
  store.heartbeat(room, { state: 'playing', positionMs: index * 10000, durationMs: 1000000, subtitleIndex: index });
  return room;
}

// ============================================================
// 语义层
// ============================================================
test('context 只给当前句和之前 3 句，后面的一个字都不给', async (t) => {
  const { store, server, base } = boot();
  t.after(() => server.close());
  await ready(server);
  const room = movieAt(store, 40);

  const ctx = await (await fetch(`${base()}/rooms/${room.id}/context`)).json();
  assert.strictEqual(ctx.currentLine, '第40句');
  assert.deepStrictEqual(ctx.recentLines, ['第37句', '第38句', '第39句']);
  assert.strictEqual(ctx.nextLineExists, true);

  // 整个响应体里不许出现播放头之后的任何一句
  const body = JSON.stringify(ctx);
  for (let i = 41; i < 100; i++) {
    assert.ok(!body.includes(`第${i}句`), `响应里泄漏了第${i}句`);
  }
  // 时间给人类可读串，不给毫秒
  assert.match(ctx.position, /^\d{2}:\d{2}:\d{2}$/);
  assert.match(ctx.duration, /^\d{2}:\d{2}:\d{2}$/);
});

test('最后一句时 nextLineExists 为 false，且仍然不给内容', async (t) => {
  const { store, server, base } = boot();
  t.after(() => server.close());
  await ready(server);
  const room = movieAt(store, 99);
  const ctx = await (await fetch(`${base()}/rooms/${room.id}/context`)).json();
  assert.strictEqual(ctx.currentLine, '第99句');
  assert.strictEqual(ctx.nextLineExists, false);
});

test('往回拖：不收回已经放过的，但也绝不越过历史最远处', async (t) => {
  const { store, server, base } = boot();
  t.after(() => server.close());
  await ready(server);
  const room = movieAt(store, 60);
  store.heartbeat(room, { state: 'playing', positionMs: 100000, subtitleIndex: 10 });  // 拖回第 10 句

  const ctx = await (await fetch(`${base()}/rooms/${room.id}/context`)).json();
  assert.strictEqual(ctx.currentLine, '第10句', '当前句跟着播放头走');
  const body = JSON.stringify(ctx);
  for (let i = 61; i < 100; i++) assert.ok(!body.includes(`第${i}句`), `泄漏了第${i}句`);
  assert.strictEqual(store._internals ? 60 : 60, room.furthestIndex, '历史最远处不该被拖回去');
});

test('没有字幕轨：不报错，明说要去抓帧', async (t) => {
  const { store, server, base } = boot();
  t.after(() => server.close());
  await ready(server);
  const room = store.createRoom('硬字幕片');
  store.heartbeat(room, { state: 'playing', positionMs: 60000, durationMs: 600000 });

  const res = await fetch(`${base()}/rooms/${room.id}/context`);
  assert.strictEqual(res.status, 200, '无字幕不许报错');
  const ctx = await res.json();
  assert.strictEqual(ctx.subtitleMode, 'none');
  assert.ok(!('currentLine' in ctx) && !('recentLines' in ctx));
  assert.ok(ctx.hints.some((h) => h.includes('ferrideo_request_frame')), '要提示去抓帧');
});

test('房间全貌不含字幕全文、不含帧的 base64', async (t) => {
  const { store, server, base } = boot();
  t.after(() => server.close());
  await ready(server);
  const room = movieAt(store, 40);
  store.addQuote(room, { text: '如果我多一张船票', positionMs: 400000 });
  store.saveFrame(room, { buffer: Buffer.from('画面'), positionMs: 400000 });

  const body = await (await fetch(`${base()}/rooms/${room.id}/room`)).text();
  assert.ok(body.includes('如果我多一张船票'));
  assert.ok(!body.includes('第41句') && !body.includes('第50句'), '不许带字幕全文');
  assert.ok(!body.includes('base64'), '不许带帧的 base64');
  assert.ok(!body.includes('cues'));
});

// ============================================================
// 结构层：gate 端口上根本没有播放器的路由
// ============================================================
test('gate 端口上没有播放器 API——连建房那条都不是同一个', async (t) => {
  const { store, server, base } = boot();
  t.after(() => server.close());
  await ready(server);
  const room = movieAt(store, 40);
  const root = base().replace('/api/gate', '');

  for (const p of [
    `/api/rooms/${room.id}`,                 // 播放器的房间视图
    `/api/rooms/${room.id}/subtitle`,        // 字幕全文——最想防的那条
    `/api/rooms/${room.id}/heartbeat`,
    `/api/gate/rooms/${room.id}/subtitle`,   // 门禁下不存在这条
    `/api/gate/rooms/${room.id}/cues`,
  ]) {
    const res = await fetch(root + p);
    assert.strictEqual(res.status, 404, `${p} 居然通了`);
  }
});

test('拿 ../ 去绕也够不到播放器 API（原始路径，不经 URL 规范化）', async (t) => {
  const { store, server, base } = boot();
  t.after(() => server.close());
  await ready(server);
  const room = movieAt(store, 40);
  const port = server.address().port;

  for (const raw of [
    `/${TOKEN}/api/gate/rooms/../../rooms/${room.id}/subtitle`,
    `/${TOKEN}/api/gate/../api/rooms/${room.id}/subtitle`,
    `/${TOKEN}/api/gate/rooms/${room.id}/../../../api/rooms/${room.id}/subtitle`,
  ]) {
    const resp = await rawGet(port, raw);
    const status = resp.split(' ')[1];
    assert.ok(['404', '400', '301'].includes(status), `${raw} 返回了 ${status}`);
    assert.ok(!resp.includes('第41句') && !resp.includes('"cues"'), `${raw} 漏出了字幕`);
  }
});

test('错 token 一律 404', async (t) => {
  const { store, server } = boot();
  t.after(() => server.close());
  await ready(server);
  const room = movieAt(store, 40);
  const port = server.address().port;
  const res = await fetch(`http://127.0.0.1:${port}/WRONG/api/gate/rooms/${room.id}/context`);
  assert.strictEqual(res.status, 404);
});

// ============================================================
// 写入类
// ============================================================
test('发弹幕 / 要一帧 / 记一笔', async (t) => {
  const { store, server, base } = boot();
  t.after(() => server.close());
  await ready(server);
  const room = movieAt(store, 40);
  const post = (p, body) => fetch(base() + p, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });

  const d = await (await post(`/rooms/${room.id}/danmaku`, { text: '她要哭了' })).json();
  assert.strictEqual(d.author, '克', 'author 默认是克');

  const long = await (await post(`/rooms/${room.id}/danmaku`, { text: '啊'.repeat(100) })).json();
  assert.strictEqual(long.text.length, 60, '超 60 字截断');
  assert.strictEqual(long.truncated, true);

  const fr = await (await post(`/rooms/${room.id}/frame-request`, {})).json();
  assert.ok(fr.tip.includes('3-5 秒'), '要说清这是异步的');
  assert.ok(store.getRoom(room.id).frameRequest);

  const n = await (await post(`/rooms/${room.id}/note`, { text: '这段配乐是梅林茂' })).json();
  assert.strictEqual(n.at, '00:06:40', '笔记自动绑当前时间（第 40 句 = 400 秒）');
});

test('票根：拿到摘录和笔记，不含字幕全文', async (t) => {
  const { store, server, base } = boot();
  t.after(() => server.close());
  await ready(server);
  const room = movieAt(store, 99);
  store.addQuote(room, { text: '如果我多一张船票', positionMs: 990000 });
  store.addNote(room, { text: '她一直没回头' });
  store.finish(room, 1000000);

  const ticket = await (await fetch(`${base()}/rooms/${room.id}/ticket`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mood: '安静', note: '看完没说话' }),
  })).json();
  assert.strictEqual(ticket.title, '剧透测试');
  assert.deepStrictEqual(ticket.quotes, ['如果我多一张船票']);
  assert.deepStrictEqual(ticket.notes, ['她一直没回头']);
  assert.strictEqual(ticket.mood, '安静');
  assert.match(ticket.watchedFor, /^\d{2}:\d{2}:\d{2}$/);
  assert.ok(!JSON.stringify(ticket).includes('第50句'));
  assert.ok(store.getRoom(room.id).ticket, '票根要存进房间');
});
