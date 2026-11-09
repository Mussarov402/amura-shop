// node cards/banners-site/render.js → cards/banners-site/out/b<1..5>_<d|m>.png (d 1500×500, m 1280×720)
let chromium; try { ({ chromium } = require('playwright')); } catch (e) { ({ chromium } = require('/opt/node22/lib/node_modules/playwright')); }
const path = require('path'), fs = require('fs');
(async () => {
  const out = path.join(__dirname, 'out'); fs.mkdirSync(out, { recursive: true });
  const b = await chromium.launch();
  for (const [f, w, h] of [['d', 1500, 500], ['m', 1280, 720]]) {
    const p = await b.newPage({ viewport: { width: w, height: h } });
    for (let n = 1; n <= 5; n++) { await p.goto('file://' + path.join(__dirname, 'banners.html') + `?b=${n}&f=${f}`); await p.waitForSelector('body[data-ready]'); await p.evaluate(() => document.fonts.ready); await p.waitForTimeout(150);
      await p.screenshot({ path: path.join(out, `b${n}_${f}.png`) }); }
    await p.close();
  }
  await b.close(); console.log('готово:', out);
})();
