// node motion/posts/brand-puns/render.js → out/01.png … (1080×1350): обложка, 8 пар, финал
const path = require('path'), fs = require('fs');
const { chromium } = require('/opt/node22/lib/node_modules/playwright');
(async () => {
  const dir = __dirname, out = path.join(dir, 'out'); fs.mkdirSync(out, { recursive: true });
  const b = await chromium.launch(), p = await b.newPage({ viewport: { width: 1080, height: 1350 } });
  const pages = ['cover', ...[...Array(8).keys()].map(String), 'end'];
  for (const [k, i] of pages.entries()) {
    await p.goto('file://' + path.join(dir, 'index.html') + '?i=' + i); await p.evaluate(() => document.fonts.ready);
    await p.waitForLoadState('networkidle'); await p.waitForTimeout(200);
    await p.screenshot({ path: path.join(out, String(k + 1).padStart(2, '0') + '.png') });
  }
  await b.close(); console.log('Готово:', out);
})();
