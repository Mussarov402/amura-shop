// node cards/medicube-ager/render.js [pink|black] → cards/medicube-ager/out/<цвет>_<1..6>.png (900×1200)
let chromium; try { ({ chromium } = require('playwright')); } catch (e) { ({ chromium } = require('/opt/node22/lib/node_modules/playwright')); }
const path = require('path'), fs = require('fs');
(async () => {
  const cols = process.argv[2] ? [process.argv[2]] : ['pink', 'black'], out = path.join(__dirname, 'out'); fs.mkdirSync(out, { recursive: true });
  const b = await chromium.launch(), p = await b.newPage({ viewport: { width: 900, height: 1200 } });
  for (const c of cols) { if (!fs.existsSync(path.join(__dirname, 'src', c + '_cut.png'))) { console.log('нет фото:', c); continue; }
    for (let s = 1; s <= 6; s++) { await p.goto('file://' + path.join(__dirname, 'slides.html') + `?c=${c}&s=${s}`); await p.evaluate(() => document.fonts.ready); await p.waitForTimeout(150); await p.screenshot({ path: path.join(out, `${c}_${s}.png`) }); } }
  await b.close();
})();
