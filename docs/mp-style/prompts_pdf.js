// PDF «Стиль карточек AMURA для ChatGPT»: node docs/mp-style/prompts_pdf.js → docs/mp-style/out/AMURA_prompts_VT_PDRN.pdf
let chromium; try { ({ chromium } = require('playwright')); } catch (e) { ({ chromium } = require('/opt/node22/lib/node_modules/playwright')); }
const path = require('path'), fs = require('fs');
(async () => {
  const out = path.join(__dirname, 'out'); fs.mkdirSync(out, { recursive: true });
  const b = await chromium.launch(), p = await b.newPage();
  await p.goto('file://' + path.join(__dirname, 'prompts-vt-pdrn.html')); await p.evaluate(() => document.fonts.ready);
  await p.pdf({ path: path.join(out, 'AMURA_prompts_VT_PDRN.pdf'), format: 'A4', printBackground: true, margin: { top: '16mm', bottom: '16mm', left: '14mm', right: '14mm' } });
  await b.close();
})();
