// node docs/mp-style/render.js (сам вызывает prep.py) → docs/mp-style/out/c{1..4}_{cover,inci,how}.png (900×1200)
let chromium; try { ({ chromium } = require('playwright')); } catch (e) { ({ chromium } = require('/opt/node22/lib/node_modules/playwright')); }
const path = require('path'), fs = require('fs');
require('child_process').execFileSync('python3', [path.join(__dirname, 'prep.py')]);   // флакон из фото каталога
(async () => {
  const out = path.join(__dirname, 'out'); fs.mkdirSync(out, { recursive: true });
  const b = await chromium.launch(), p = await b.newPage({ viewport: { width: 900, height: 1200 } });
  for (const c of [1, 2, 3, 4]) for (const s of ['cover', 'inci', 'how']) {
    await p.goto('file://' + path.join(__dirname, 'style.html') + `?c=${c}&s=${s}`); await p.evaluate(() => document.fonts.ready); await p.waitForTimeout(150);
    await p.screenshot({ path: path.join(out, `c${c}_${s}.png`) });
  }
  await b.close();
})();
