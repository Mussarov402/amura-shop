// Превью коробки и вкладышей: node cards/box/render.js → cards/box/out/box_{a,b,c}.png, inserts.png
let chromium; try { ({ chromium } = require('playwright')); } catch (e) { ({ chromium } = require('/opt/node22/lib/node_modules/playwright')); }
const path = require('path'), fs = require('fs');
(async () => {
  const out = path.join(__dirname, 'out'); fs.mkdirSync(out, { recursive: true });
  const b = await chromium.launch(), p = await b.newPage({ viewport: { width: 1800, height: 1100 } });
  for (const v of ['a', 'b', 'c']) { await p.goto('file://' + path.join(__dirname, 'box.html') + '?v=' + v); await p.evaluate(() => document.fonts.ready); await p.waitForTimeout(200); await p.screenshot({ path: path.join(out, `box_${v}.png`) }); }
  await p.goto('file://' + path.join(__dirname, 'insert.html')); await p.evaluate(() => document.fonts.ready); await p.waitForTimeout(200); await p.screenshot({ path: path.join(out, 'inserts.png') });
  await b.close();
})();
// развёртки (после python3 cards/box/dieline.py): превью PNG по 4 px на мм
(async () => {
  const files = ['die_S', 'die_M', 'bag'].filter(n => fs.existsSync(path.join(__dirname, 'out', n + '.svg'))); if (!files.length) return;
  const b = await chromium.launch(), p = await b.newPage();
  for (const n of files) {
    const svg = fs.readFileSync(path.join(__dirname, 'out', n + '.svg'), 'utf8'), vb = svg.match(/viewBox="([^"]+)"/)[1].split(' ').map(Number), k = 4;
    await p.setViewportSize({ width: Math.round(vb[2] * k), height: Math.round(vb[3] * k) });
    await p.setContent(`<html><head><link rel="stylesheet" href="file://${path.join(__dirname, '..', 'fonts.css')}"><style>body{margin:0}svg{width:${vb[2] * k}px;height:${vb[3] * k}px;display:block}</style></head><body>${svg}</body></html>`);
    await p.evaluate(() => document.fonts.ready); await p.waitForTimeout(150);
    await p.screenshot({ path: path.join(__dirname, 'out', n + '.png') });
  }
  await b.close();
})();
