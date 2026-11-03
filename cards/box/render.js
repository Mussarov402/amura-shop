// Превью концепций коробки: node cards/box/render.js → cards/box/out/box_{a,b,c}.png
let chromium; try { ({ chromium } = require('playwright')); } catch (e) { ({ chromium } = require('/opt/node22/lib/node_modules/playwright')); }
const path = require('path'), fs = require('fs');
(async () => {
  const out = path.join(__dirname, 'out'); fs.mkdirSync(out, { recursive: true });
  const b = await chromium.launch(), p = await b.newPage({ viewport: { width: 1800, height: 1100 } });
  for (const v of ['a', 'b', 'c']) { await p.goto('file://' + path.join(__dirname, 'box.html') + '?v=' + v); await p.evaluate(() => document.fonts.ready); await p.waitForTimeout(200); await p.screenshot({ path: path.join(out, `box_${v}.png`) }); }
  await b.close();
})();
