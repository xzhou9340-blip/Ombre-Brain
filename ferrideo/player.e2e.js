/* ============================================================
 * ferrideo/player.e2e.js — 播放器端到端（Chromium 驱动真页面）
 * ============================================================
 *
 * 跑法：node player.e2e.js <chromium 可执行文件> <一个能播的视频文件>
 * 例：node player.e2e.js /opt/pw-browsers/chromium-1194/chrome-linux/chrome /tmp/movie.webm
 *
 * 这一节的最后一步只能在 iPad Safari 上跑（见 docs/ipad-验收清单）。
 * 这个脚本负责在 iPad 之前把能自动化的都跑掉：真起 node 服务、真开浏览器、
 * 真选文件、真播、真心跳、真上屏——不是读代码判断。
 *
 * Chromium 不等于 iOS Safari：playsinline 接管、wakeLock、HEVC 解码、
 * 切后台挂起这几条它测不出来，那几条在人工清单里。
 *
 * 一个测试工具的坑（不是产品的坑）：Playwright 的 setInputFiles 遇到**非 ASCII
 * 路径会静默失败**——不报错，但文件根本没进 input，change 也不触发。所以喂文件
 * 一律用 ASCII 文件名；中文片名这条真实场景改用页面内构造 File + 派发 change
 * 来覆盖（走的是同一个 handler）。
 * ============================================================ */

'use strict';

const { chromium } = require(process.env.PW_PATH || 'playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');

const CHROME = process.argv[2];
const MOVIE = process.argv[3];
const TOKEN = 'e2e-token';
const PORT = 18077;
const GATE_PORT = 18078;
const BASE = `http://127.0.0.1:${PORT}/${TOKEN}`;
const GATE = `http://127.0.0.1:${GATE_PORT}/${TOKEN}/api/gate`;

let passed = 0;
const failures = [];
function ok(cond, name, extra = '') {
  if (cond) { passed += 1; console.log(`  ok  ${name}`); }
  else { failures.push(name + (extra ? ` — ${extra}` : '')); console.log(`  FAIL ${name}${extra ? ' — ' + extra : ''}`); }
}

function req(url, { method = 'GET', body } = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const r = http.request({
      hostname: u.hostname, port: u.port, path: u.pathname + u.search, method,
      headers: body ? { 'Content-Type': 'application/json' } : {},
    }, (res) => {
      let buf = '';
      res.on('data', (d) => { buf += d; });
      res.on('end', () => { try { resolve({ status: res.statusCode, body: buf ? JSON.parse(buf) : null }); } catch { resolve({ status: res.statusCode, body: buf }); } });
    });
    r.on('error', reject);
    if (body) r.write(JSON.stringify(body));
    r.end();
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ferrideo-e2e-'));
  const child = spawn('node', ['server.js'], {
    cwd: __dirname,
    env: {
      ...process.env,
      PORT: '', FERRIDEO_PORT: String(PORT), FERRIDEO_GATE_PORT: String(GATE_PORT),
      FERRIDEO_WEB_TOKEN: TOKEN, FERRIDEO_PUBLIC_PREFIX: '',
      DATA_DIR: dataDir,
      FERRIDEO_PAGE_DIRS: path.join(__dirname, '..', 'frontend', 'ferrideo'),
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const serverLog = [];
  child.stdout.on('data', (d) => serverLog.push(d.toString()));
  child.stderr.on('data', (d) => serverLog.push(d.toString()));
  for (let i = 0; i < 50; i++) {
    try { if ((await req(`http://127.0.0.1:${PORT}/healthz`)).status === 200) break; } catch { /* 还没起来 */ }
    await sleep(200);
  }

  const browser = await chromium.launch({ executablePath: CHROME, args: ['--autoplay-policy=no-user-gesture-required'] });
  const page = await (await browser.newContext({ viewport: { width: 1024, height: 768 } })).newPage();
  const pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(e.message));

  try {
    await page.goto(`${BASE}/`);

    // ---- 设置页 ----
    ok(await page.isVisible('#setup'), '首次打开落在设置页');
    ok(!(await page.$('input[type=password]')) && !(await page.$('#token')), '设置页没有 token 输入框（token 在路径里）');
    await page.fill('#nick', '她');
    await page.fill('#aiName', '克');
    await page.click('#setupDone');
    ok(await page.isVisible('#lobby'), '设置完进大厅');

    // ---- 选片预检：喂一个假 mkv ----
    const mkv = path.join(dataDir, 'fake.mkv');
    fs.writeFileSync(mkv, Buffer.alloc(2048, 7));
    await page.setInputFiles('#videoFile', mkv);
    await page.waitForFunction(() => document.getElementById('videoErr').textContent.length > 0, null, { timeout: 8000 });
    const errText = await page.textContent('#videoErr');
    ok(/MKV|AC3|MP4/.test(errText), '放不了的文件给出具体可执行的提示', errText.slice(0, 40));
    ok(await page.getAttribute('#start', 'disabled') !== null, '预检没过时「开始」不可点');
    await sleep(300);
    const orphan = serverLog.join('').includes('播放器上报失败（还没建房）');
    ok(orphan, '预检失败被上报到后端（不是只写 console）');

    // ---- 中文片名（她的片子基本都是中文名）----
    // 走页面内构造 File + 派发 change，绕开 setInputFiles 对非 ASCII 路径的静默失败
    await page.evaluate(() => {
      const f = new File([new Uint8Array(2048)], '花样年华 2000 BluRay.mkv', { type: 'video/x-matroska' });
      const dt = new DataTransfer();
      dt.items.add(f);
      const input = document.getElementById('videoFile');
      input.files = dt.files;
      input.dispatchEvent(new Event('change'));
    });
    await page.waitForFunction(() => document.getElementById('videoPicked').textContent.includes('花样年华'), null, { timeout: 8000 });
    ok((await page.textContent('#videoPicked')).includes('花样年华 2000 BluRay.mkv'), '中文片名能正常显示');
    await page.waitForFunction(() => document.getElementById('videoErr').textContent.length > 0, null, { timeout: 8000 });
    ok(true, '中文片名的不可播文件同样给出提示');

    // ---- 选真视频 + 字幕 ----
    await page.setInputFiles('#videoFile', MOVIE);
    await page.waitForFunction(() => document.getElementById('videoState').textContent.includes('×'), null, { timeout: 15000 });
    ok(true, '能播的文件通过预检并显示分辨率');

    const srt = path.join(dataDir, 'sub.srt');
    fs.writeFileSync(srt,
      '1\n00:00:00,000 --> 00:00:02,000\n<i>第一句</i>\n\n' +
      '2\n00:00:02,000 --> 00:00:04,000\n{\\an8}第二句\n\n' +
      '3\n00:00:04,000 --> 00:00:06,000\n第三句\n\n' +
      '4\n00:00:10,000 --> 00:00:30,000\n这句在播放头后面，谁都不许看见\n', 'utf8');
    await page.setInputFiles('#subFile', srt);
    await page.waitForFunction(() => document.getElementById('subState').textContent.includes('句'), null, { timeout: 5000 });
    ok((await page.textContent('#subState')).includes('4 句'), 'srt 解析出 4 句');
    const cues = await page.evaluate(() => S.cues);
    ok(cues[0].text === '第一句', 'HTML 标签被清掉', JSON.stringify(cues[0]));
    ok(cues[1].text === '第二句', '{...} 样式块被清掉', JSON.stringify(cues[1]));

    // ---- 开始 ----
    await page.fill('#title', '端到端测试片');
    await page.click('#start');
    await page.waitForSelector('#screen.on', { timeout: 8000 });
    const roomId = await page.evaluate(() => S.roomId);
    ok(/^[A-Z0-9]{6}$/.test(roomId), '建房拿到 6 位房间号', roomId);
    ok(!/[0O1IL]/.test(roomId), '房间号不含会看错的字符', roomId);

    // ---- 心跳 ----
    await page.evaluate(() => document.getElementById('video').play());
    await sleep(4000);
    const room = (await req(`${BASE}/api/rooms/${roomId}`)).body;
    ok(room.playback.positionMs > 0, '后端看到 positionMs 在动', String(room.playback.positionMs));
    ok(room.playback.state === 'playing', '后端看到状态是 playing');
    ok(room.subtitle.cueCount === 4, '字幕上传到后端');

    // ---- 字幕跟得上（先暂停，让播放头停在一个确定的位置再断言）----
    await page.evaluate(() => { const v = document.getElementById('video'); v.pause(); v.currentTime = 2.5; });
    await sleep(4000);   // 等一次心跳，让后端的 currentIndex 也跟上
    ok((await page.textContent('#subtitle')) === '第二句', '画面上的字幕跟着播放头走', await page.textContent('#subtitle'));

    // ---- AI 从 gate 发弹幕 → 3 秒内上屏 ----
    await req(`${GATE}/rooms/${roomId}/danmaku`, { method: 'POST', body: { text: '这段配乐是梅林茂' } });
    await page.waitForFunction(() => document.querySelectorAll('.danmaku').length > 0, null, { timeout: 6000 });
    const dm = await page.textContent('.danmaku');
    ok(dm.includes('这段配乐是梅林茂') && dm.includes('克'), '弹幕带署名浮上画面', dm);

    // ---- ack：上屏后下一次心跳回执，后端标 delivered ----
    await sleep(4000);
    const afterAck = (await req(`${BASE}/api/rooms/${roomId}`)).body;
    ok(afterAck.danmaku.every((d) => d.delivered), '上屏后的弹幕被 ack 掉了');

    // ---- 记下这句 ----
    await page.click('#screen');                  // 唤出控件
    await page.click('#quote');
    await sleep(500);
    const quoted = (await req(`${BASE}/api/rooms/${roomId}`)).body.quotes;
    ok(quoted.length === 1 && quoted[0].text === '第二句', '「记下这句」把当前台词存进后端', JSON.stringify(quoted));

    // ---- 抓帧：gate 要一帧 → 播放器上传 → 现读现编码取回 ----
    // 保持暂停：播放头不动，下面对台词的断言才是确定的
    await req(`${GATE}/rooms/${roomId}/frame-request`, { method: 'POST', body: {} });
    await sleep(5000);
    const ctx = (await req(`${GATE}/rooms/${roomId}/context?includeFrame=1`)).body;
    ok(typeof ctx.frame === 'string' && ctx.frame.startsWith('data:image/jpeg;base64,'), '抓到的帧能从 gate 取回', String(ctx.frame).slice(0, 30));
    ok(ctx.currentLine === '第二句', 'gate 看到的台词跟画面上一致', String(ctx.currentLine));
    ok(!JSON.stringify(ctx).includes('谁都不许看见'), 'gate 拿不到播放头之后的台词');
    ok(ctx.nextLineExists === true, '只告诉后面还有，不告诉是什么');

    // ---- 字幕对轴 ----
    await page.click('#offPlus');
    await sleep(300);
    ok((await page.textContent('#offsetBox')).includes('0.5'), '对轴按钮改得动偏移量', await page.textContent('#offsetBox'));
    ok(await page.isVisible('#alignPeek.on'), '对轴时画面中央显示当前这句');

    // ---- 掉房重建（R3）----
    await page.evaluate((id) => { S.roomId = id; }, 'ZZZZZZ');
    await sleep(4500);
    const rebuilt = await page.evaluate(() => S.roomId);
    const rebuiltRoom = (await req(`${BASE}/api/rooms/ZZZZZZ`)).body;
    ok(rebuilt === 'ZZZZZZ' && rebuiltRoom && rebuiltRoom.id === 'ZZZZZZ', '心跳 404 后自动重建同号房间');
    ok(rebuiltRoom.subtitle.cueCount === 4, '重建时字幕重传了一次');

    // ---- 退出 ----
    await page.evaluate((id) => { S.roomId = id; }, roomId);
    await page.click('#screen');
    await page.click('#exit');
    await page.waitForSelector('#lobby.on', { timeout: 5000 });
    const finished = (await req(`${BASE}/api/rooms/${roomId}`)).body;
    ok(finished.finishedAt !== null, '退出时调了 /finish');
    ok(finished.watchedMs > 0, '记下了实际观看时长', String(finished.watchedMs));
    ok(finished.ticket === null, '退出不生成票根（票根只能 AI 调工具生成）');

    ok(pageErrors.length === 0, '整个过程没有未捕获的 JS 报错', pageErrors.join(' | '));
  } finally {
    await browser.close();
    child.kill('SIGTERM');
    await sleep(300);
  }

  console.log(`\n通过 ${passed}，失败 ${failures.length}`);
  if (failures.length) { failures.forEach((f) => console.log('  · ' + f)); process.exit(1); }
}

main().catch((e) => { console.error(e); process.exit(1); });
