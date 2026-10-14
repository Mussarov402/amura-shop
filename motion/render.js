#!/usr/bin/env node
/* Рендер ролика AMURA Motion: node motion/render.js motion/reels/<папка> [--workers 4] [--fps 30] [--snap 1,5.5,12]
   1) открывает index.html в Chromium (Playwright), перематывает анимации покадрово и снимает кадры;
   2) сохраняет звуковые метки фишек (cues.json) и собирает звук tools/mix.py: голос + бит + звуки фишек;
   3) склеивает out.mp4 (H.264, 1080×1920, 30 к/с). Голос: voice.wav / voice.mp3 в папке ролика (в git не храним). */
const fs = require('fs'), path = require('path'), { execFileSync, fork } = require('child_process');
let chromium;
try { ({ chromium } = require('playwright')); } catch (e) { ({ chromium } = require('/opt/node22/lib/node_modules/playwright')); }

const args = process.argv.slice(2), opt = k => { const i = args.indexOf('--' + k); return i >= 0 ? args[i + 1] : null; };
const dir = path.resolve(args[0] || '.'), fps = +(opt('fps') || 30), workers = +(opt('workers') || 4);
const page = 'file://' + path.join(dir, 'index.html'), frames = path.join(dir, 'frames');

async function open() {
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 1080, height: 1920 } });
  p.on('pageerror', e => { console.error('Ошибка на странице:', e.message); process.exitCode = 1; });
  await p.goto(page); await p.evaluate(() => document.fonts.ready); await p.waitForTimeout(400);
  return [b, p];
}

async function worker(from, to) {
  const [b, p] = await open();
  for (let f = from; f < to; f++) {
    await p.evaluate(ms => seek(ms), f * 1000 / fps);
    await p.screenshot({ path: path.join(frames, String(f).padStart(5, '0') + '.jpg'), type: 'jpeg', quality: 90 });
  }
  await b.close();
}

(async () => {
  if (process.env.AM_RANGE) { const [a, z] = process.env.AM_RANGE.split(',').map(Number); return worker(a, z); }
  const [b, p] = await open();
  const end = await p.evaluate(() => window.REEL_END), cues = await p.evaluate(() => AM.cues);
  if (opt('snap')) {   // быстрые кадры-проверки: --snap 1,5.5,12
    for (const t of opt('snap').split(',').map(Number)) { await p.evaluate(ms => seek(ms), t * 1000); await p.waitForTimeout(30); await p.screenshot({ path: path.join(dir, `snap_${t}.jpg`), type: 'jpeg', quality: 60 }); }
    await b.close(); return console.log('Кадры-проверки сохранены в', dir);
  }
  await b.close();
  fs.writeFileSync(path.join(dir, 'cues.json'), JSON.stringify({ end, cues }));
  fs.rmSync(frames, { recursive: true, force: true }); fs.mkdirSync(frames);
  const total = Math.ceil(end * fps), step = Math.ceil(total / workers);
  console.log(`Длина ${end.toFixed(2)} с, кадров ${total}, потоков ${workers}`);
  await Promise.all([...Array(workers)].map((_, i) => new Promise((ok, bad) => {
    const c = fork(__filename, [dir, "--fps", String(fps)], { env: { ...process.env, AM_RANGE: `${i * step},${Math.min(total, (i + 1) * step)}` } });
    c.on('exit', code => (code ? bad(new Error('поток ' + i)) : ok()));
  })));
  const mix = path.join(dir, 'mix.wav');
  execFileSync('python3', [path.join(__dirname, 'tools', 'mix.py'), dir], { stdio: 'inherit' });
  const out = path.join(dir, 'out.mp4');
  execFileSync('ffmpeg', ['-v', 'error', '-y', '-framerate', String(fps), '-i', path.join(frames, '%05d.jpg'), '-i', mix,
    '-c:v', 'libx264', '-preset', 'medium', '-crf', '19', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '160k', '-af', 'loudnorm=I=-14:TP=-1.5',
    '-shortest', '-movflags', '+faststart', out], { stdio: 'inherit' });
  console.log('Готово:', out);
})();
