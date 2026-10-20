#!/usr/bin/env node
/* Рендер ролика AMURA Glass: node motion/glass/render.js motion/reels/<папка> [--snap 1,4.5,9] [--workers 4]
   Chromium (Playwright) перематывает анимации покадрово → кадры JPEG → звук sfx.py (щелчки, динь, пинг, поп + тихий бит) → out.mp4 1080×1920, 30 к/с. */
const fs = require('fs'), path = require('path'), { execFileSync, fork } = require('child_process');
let chromium; try { ({ chromium } = require('playwright')); } catch (e) { ({ chromium } = require('/opt/node22/lib/node_modules/playwright')); }
const args = process.argv.slice(2), opt = k => { const i = args.indexOf('--' + k); return i >= 0 ? args[i + 1] : null; };
const dir = path.resolve(args[0] || '.'), fps = 30, workers = +(opt('workers') || 4);
const page = 'file://' + path.join(dir, 'index.html'), frames = path.join(dir, 'frames');

async function open() {
  const b = await chromium.launch({ args: ['--no-sandbox'] }); const p = await b.newPage({ viewport: { width: 1080, height: 1920 } });
  p.on('pageerror', e => { console.error('Ошибка на странице:', e.message); process.exitCode = 1; });
  await p.goto(page); await p.evaluate(() => document.fonts.ready); await p.evaluate(() => { const v = document.querySelector('video'); return v && v.readyState < 2 ? new Promise(ok => v.addEventListener('loadeddata', ok, { once: true })) : 0; }); await p.waitForTimeout(400); return [b, p];
}
async function worker(a, z) {
  const [b, p] = await open();
  for (let f = a; f < z; f++) { await p.evaluate(ms => seek(ms), f * 1000 / fps); await p.screenshot({ path: path.join(frames, String(f).padStart(5, '0') + '.jpg'), type: 'jpeg', quality: 92 }); }
  await b.close();
}
(async () => {
  if (process.env.G_RANGE) { const [a, z] = process.env.G_RANGE.split(',').map(Number); return worker(a, z); }
  const [b, p] = await open(); const end = await p.evaluate(() => window.REEL_END), cues = await p.evaluate(() => G.cues);
  if (opt('snap')) { for (const t of opt('snap').split(',').map(Number)) { await p.evaluate(ms => seek(ms), t * 1000); await p.waitForTimeout(30); await p.screenshot({ path: path.join(dir, `snap_${t}.jpg`), type: 'jpeg', quality: 70 }); } await b.close(); return console.log('Кадры-проверки:', dir); }
  await b.close();
  fs.writeFileSync(path.join(dir, 'cues.json'), JSON.stringify({ end, cues }));
  fs.rmSync(frames, { recursive: true, force: true }); fs.mkdirSync(frames);
  const total = Math.ceil(end * fps), step = Math.ceil(total / workers);
  await Promise.all([...Array(workers)].map((_, i) => new Promise((ok, bad) => { const c = fork(__filename, [dir], { env: { ...process.env, G_RANGE: `${i * step},${Math.min(total, (i + 1) * step)}` } }); c.on('exit', x => (x ? bad(new Error('поток ' + i)) : ok())); })));
  execFileSync('python3', [path.join(__dirname, 'sfx.py'), dir], { stdio: 'inherit' });
  const out = path.join(dir, 'out.mp4');
  execFileSync('ffmpeg', ['-v', 'error', '-y', '-framerate', String(fps), '-i', path.join(frames, '%05d.jpg'), '-i', path.join(dir, 'mix.wav'),
    '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '160k', '-af', 'loudnorm=I=-14:TP=-1.5', '-shortest', '-movflags', '+faststart', out], { stdio: 'inherit' });
  console.log('Готово:', out, `(${end.toFixed(1)} с)`);
})();
