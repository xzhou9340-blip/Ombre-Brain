/* ============================================================
 * ferrideo/layout.e2e.js — 放映页在真实设备尺寸下的布局回归
 * ============================================================
 *
 * 跑法：node layout.e2e.js <chromium 可执行文件>
 *
 * 为什么要有这个文件：M3 交付时我没量过尺寸，结果**控件条浮出来会盖住字幕**，
 * 而且在 iPad 横屏上就盖住了（实测重叠 40px，正好一整行字）。靠读代码看不出来，
 * 只有量才知道。所以把那次测量钉成回归测试。
 *
 * 支持范围（用户决定）：iPad 全尺寸 + iPhone **横屏**。
 * iPhone 竖屏不保证好看，但不许溢出——横着拿本来就是看片的姿势。
 *
 * 量的是布局，不需要真的解码视频，所以直接构造状态进放映页。
 * ============================================================ */

'use strict';

const { chromium } = require(process.env.PW_PATH || 'playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');

const CHROME = process.argv[2];
const TOKEN = 'layout-token';
const PORT = 18091;
const GATE_PORT = 18092;

// [名字, 宽, 高, 是否在支持范围内]
const VIEWPORTS = [
  ['iPad Pro 11 横', 1194, 834, true],
  ['iPad Pro 11 竖', 834, 1194, true],
  ['iPad mini 横', 1133, 744, true],
  ['iPad mini 竖', 744, 1133, true],
  ['iPhone 15 Pro 横', 852, 393, true],
  ['iPhone SE 横', 667, 375, true],
  ['iPhone 15 Pro 竖', 393, 852, false],
  ['iPhone SE 竖', 375, 667, false],
];

const MAX_CTL_PCT = 26;          // 控件条最多吃掉这么多屏高（支持范围内）
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let passed = 0;
const failures = [];
function ok(cond, name, extra = '') {
  if (cond) { passed += 1; console.log(`  ok  ${name}`); }
  else { failures.push(`${name}${extra ? ' — ' + extra : ''}`); console.log(`  FAIL ${name}${extra ? ' — ' + extra : ''}`); }
}

function waitHealth() {
  return new Promise((resolve, reject) => {
    let tries = 0;
    const tick = () => {
      http.get(`http://127.0.0.1:${PORT}/healthz`, (res) => (res.statusCode === 200 ? resolve() : retry()))
        .on('error', retry);
    };
    const retry = () => { if (++tries > 50) reject(new Error('服务起不来')); else setTimeout(tick, 200); };
    tick();
  });
}

async function main() {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ferrideo-layout-'));
  const child = spawn('node', ['server.js'], {
    cwd: __dirname,
    env: {
      ...process.env,
      PORT: '', FERRIDEO_PORT: String(PORT), FERRIDEO_GATE_PORT: String(GATE_PORT),
      FERRIDEO_WEB_TOKEN: TOKEN, DATA_DIR: dataDir,
      FERRIDEO_PAGE_DIRS: path.join(__dirname, '..', 'frontend', 'ferrideo'),
    },
    stdio: 'ignore',
  });
  await waitHealth();
  const browser = await chromium.launch({ executablePath: CHROME });

  try {
    for (const [name, w, h, supported] of VIEWPORTS) {
      const ctx = await browser.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: 2, hasTouch: true });
      const page = await ctx.newPage();
      await page.goto(`http://127.0.0.1:${PORT}/${TOKEN}/`);
      await page.click('#setupDone');
      const lobbyOverflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

      // 直接进放映页：量布局不需要解码
      await page.evaluate(() => {
        S.videoFile = new File([new Uint8Array(1024)], 'layout.mp4', { type: 'video/mp4' });
        S.cues = [{ startMs: 0, endMs: 60000, text: '如果我多一张船票，你会不会跟我一起走' }];
        S.roomId = 'AAAAAA';
        S.title = '量尺寸';
        enterScreen();
        document.getElementById('subtitle').textContent = S.cues[0].text;
      });
      await page.click('#screen');          // 唤出控件（走真实路径，不是手动加 class）
      await sleep(400);

      const m = await page.evaluate(() => {
        const ctl = document.getElementById('controls');
        const ctlBox = ctl.getBoundingClientRect();
        const btns = [...ctl.querySelectorAll('button, input')].map((b) => b.getBoundingClientRect());
        const sub = document.getElementById('subtitle').getBoundingClientRect();
        const rows = [...ctl.querySelectorAll('.row')].map((r) => {
          const real = [...r.querySelectorAll('button, input, span')].filter((k) => k.getBoundingClientRect().height > 16);
          const tallest = Math.max(...real.map((k) => k.getBoundingClientRect().height));
          return { h: Math.round(r.getBoundingClientRect().height), wrapped: r.getBoundingClientRect().height > tallest + 6 };
        });
        return {
          ctlH: Math.round(ctlBox.height),
          ctlPct: Math.round(ctlBox.height / window.innerHeight * 100),
          btnTop: Math.round(Math.min(...btns.map((b) => b.top))),
          subBottom: Math.round(sub.bottom),
          subTop: Math.round(sub.top),
          overlap: Math.round(sub.bottom - Math.min(...btns.map((b) => b.top))),
          rows,
          overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          subVisible: sub.top >= 0 && sub.bottom <= window.innerHeight,
          ctlVar: document.getElementById('screen').style.getPropertyValue('--ctl-h'),
        };
      });

      console.log(`\n=== ${name}  ${w}×${h}${supported ? '' : '（支持范围外：只要求不溢出、字幕可见）'}`);
      ok(lobbyOverflow === 0 && m.overflow === 0, `${name}：没有横向滚动`, `大厅 ${lobbyOverflow}px / 放映 ${m.overflow}px`);
      ok(m.overlap <= 0, `${name}：控件条浮出时字幕没被按钮压住`, m.overlap > 0 ? `重叠 ${m.overlap}px` : '');
      ok(m.subVisible, `${name}：字幕完整在屏内`, `top ${m.subTop} bottom ${m.subBottom}`);
      ok(m.ctlVar !== '', `${name}：控件条高度被写进 --ctl-h`, m.ctlVar);
      if (supported) {
        ok(m.ctlPct <= MAX_CTL_PCT, `${name}：控件条不超过屏高 ${MAX_CTL_PCT}%`, `实测 ${m.ctlPct}%（${m.ctlH}px）`);
        ok(!m.rows.some((r) => r.wrapped), `${name}：控件条不换行`, m.rows.map((r) => r.h + 'px').join(' / '));
      } else {
        console.log(`  （参考）控件条 ${m.ctlH}px＝屏高 ${m.ctlPct}%，换行：${m.rows.map((r) => r.wrapped ? '是' : '否').join('/')}`);
      }
      await ctx.close();
    }
  } finally {
    await browser.close();
    child.kill('SIGTERM');
    await sleep(300);
  }

  console.log(`\n通过 ${passed}，失败 ${failures.length}`);
  if (failures.length) { failures.forEach((f) => console.log('  · ' + f)); process.exit(1); }
}

main().catch((e) => { console.error(e); process.exit(1); });
