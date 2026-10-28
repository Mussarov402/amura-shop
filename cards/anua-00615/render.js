// node cards/anua-00615/render.js [g1 r2 ...] → out/<id>.png (галерея 900×1200, рич 1440×900)
const path = require('path'), fs = require('fs');
const { chromium } = require('/opt/node22/lib/node_modules/playwright');
(async () => {
  const dir = __dirname, out = path.join(dir, 'out'); fs.mkdirSync(out, { recursive: true });
  const ids = process.argv.slice(2).length ? process.argv.slice(2) : ['g1', 'g1a', 'g1b', 'g1c', 'g1o', 'g2', 'g3', 'g4', 'g5', 'g6', 'g7', 'r1', 'r2', 'r3', 'r4'];
  const b = await chromium.launch(), p = await b.newPage({ viewport: { width: 1440, height: 1200 } });
  for (const id of ids) {
    await p.goto('file://' + path.join(dir, 'slides.html') + '?s=' + id); await p.evaluate(() => document.fonts.ready); await p.waitForTimeout(250);
    const [w, h] = await p.evaluate(() => window.SIZE);
    await p.screenshot({ path: path.join(out, id + '.png'), clip: { x: 0, y: 0, width: w, height: h } });
  }
  await b.close(); console.log('Готово:', out);
})();
