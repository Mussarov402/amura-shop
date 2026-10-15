/* Запись оптового сайта для ролика: node motion/reels/opt-site-01/capture.js (сайт из opt/index.html отдаётся локально на :8765
   вместе с catalog.json, img и т.д.). Кадры и метки действий → site/ (в git не храним). Заказ не отправляется — палец только «нажимает». */
let chromium; try { ({ chromium } = require('playwright')); } catch (e) { ({ chromium } = require('/opt/node22/lib/node_modules/playwright')); }
const fs = require('fs'); const OUT = require('path').join(__dirname, 'site'); fs.rmSync(OUT, { recursive: true, force: true }); fs.mkdirSync(OUT);
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2.5, isMobile: true, hasTouch: true, locale: 'ru-RU' });
  const p = await ctx.newPage();
  await p.goto('http://127.0.0.1:8765/index.html'); await sleep(5000);
  await p.addStyleTag({ content: `#tapf{position:fixed;z-index:99999;width:46px;height:46px;margin:-23px 0 0 -23px;border-radius:50%;background:rgba(255,255,255,.55);border:3px solid rgba(31,106,80,.9);box-shadow:0 4px 18px rgba(0,0,0,.35);pointer-events:none;opacity:0;transition:left .45s cubic-bezier(.3,.8,.3,1),top .45s cubic-bezier(.3,.8,.3,1),opacity .25s,transform .15s}#tapf.on{opacity:1}#tapf.down{transform:scale(.7);background:rgba(215,234,74,.8)} .cbtn{visibility:hidden!important}` });
  await p.evaluate(() => { const d = document.createElement('div'); d.id = 'tapf'; d.style.left = '200px'; d.style.top = '700px'; document.body.appendChild(d); });
  const cdp = await ctx.newCDPSession(p); const frames = [];
  cdp.on('Page.screencastFrame', async f => { frames.push(f.metadata.timestamp); fs.writeFileSync(`${OUT}/${String(frames.length).padStart(5, '0')}.jpg`, Buffer.from(f.data, 'base64')); try { await cdp.send('Page.screencastFrameAck', { sessionId: f.sessionId }); } catch (e) {} });
  const marks = {}; const t0 = Date.now(); const mark = k => { marks[k] = (Date.now() - t0) / 1000; };
  await cdp.send('Page.startScreencast', { format: 'jpeg', quality: 92, maxWidth: 975, maxHeight: 2110, everyNthFrame: 1 });
  // keep frames flowing: tiny invisible animation
  await p.addStyleTag({ content: '@keyframes kk{from{opacity:.99}to{opacity:1}} body::after{content:"";position:fixed;left:0;top:0;width:1px;height:1px;animation:kk .1s infinite alternate}' });
  const finger = async (sel, opt = {}) => {
    const el = typeof sel === 'string' ? p.locator(sel).first() : sel; const bb = await el.boundingBox();
    const x = bb.x + bb.width * (opt.fx ?? .5), y = bb.y + bb.height * (opt.fy ?? .5);
    await p.evaluate(([x, y]) => { const f = document.getElementById('tapf'); f.classList.add('on'); f.style.left = x + 'px'; f.style.top = y + 'px'; }, [x, y]);
    await sleep(500); await p.evaluate(() => document.getElementById('tapf').classList.add('down')); await sleep(140);
    if (!opt.noClick) await p.mouse.click(x, y); await p.evaluate(() => document.getElementById('tapf').classList.remove('down')); await sleep(opt.after ?? 350);
  };
  const hideF = () => p.evaluate(() => document.getElementById('tapf').classList.remove('on'));
  const smooth = async (dy, ms, sel) => { await p.evaluate(([dy, ms, sel]) => new Promise(r => { const el = sel ? document.querySelector(sel) : null; const s0 = el ? el.scrollTop : scrollY; const t = performance.now(); const st = () => { const k = Math.min(1, (performance.now() - t) / ms), e = k < .5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2; el ? el.scrollTop = s0 + dy * e : scrollTo(0, s0 + dy * e); k < 1 ? requestAnimationFrame(st) : r(); }; st(); }), [dy, ms, sel]); };

  mark('home'); await sleep(1500);
  mark('scroll'); await smooth(1500, 2600); await sleep(400); await smooth(-1500, 1200); await sleep(300);
  mark('search'); await finger('#q'); await p.keyboard.type('Celimax', { delay: 110 }); await sleep(900); await hideF();
  await smooth(250, 700); await sleep(900);
  mark('open'); const card = p.locator('article.card:has-text("Brightening Eye Patch") [data-open]').first(); await finger(card); await sleep(1300);
  const sh = '#sheet'; await smooth(330, 900, '#modal'); await sleep(200); await smooth(330, 900, '#sheet'); await sleep(600);
  mark('qty'); const inc = p.locator('#sheet [data-inc]').first(); await finger(inc, { after: 120 });
  for (let i = 0; i < 8; i++) { await p.evaluate(() => document.getElementById('tapf').classList.add('down')); await sleep(90); await inc.click(); await p.evaluate(() => document.getElementById('tapf').classList.remove('down')); await sleep(160); }
  await sleep(900);
  mark('add'); await finger('#sheet [data-add]', { after: 900 });
  await p.evaluate(() => { document.querySelector('#sheet').scrollTop = 0; document.querySelector('#modal').scrollTop = 0; }); await sleep(250); await finger('#closeModal', { after: 500 });
  mark('cart'); await finger(p.locator('button[data-view="cart"]:visible').last(), { after: 1100 });
  mark('form'); await finger('#fName', { after: 150 }); await p.keyboard.type('Айгерим', { delay: 70 });
  await finger('#fPhone', { after: 150 }); await p.keyboard.type('7012345678', { delay: 55 });
  await finger('#fCity', { after: 150 }); await p.keyboard.type('Караганда', { delay: 65 }); await p.evaluate(() => document.activeElement.blur());
  await smooth(330, 800); await sleep(200);
  mark('ship'); await finger('.ship [data-m="kamaz"]', { after: 500 });
  await p.locator('#send').scrollIntoViewIfNeeded().catch(() => {}); await sleep(300);
  await smooth(0, 10); const sb = await p.locator('#send').boundingBox(); if (sb.y > 650) await smooth(sb.y - 520, 900); await sleep(400);
  mark('send'); await finger('#send', { noClick: true, after: 1500 });
  mark('end'); await sleep(300);
  await cdp.send('Page.stopScreencast'); await sleep(300);
  fs.writeFileSync(`${OUT}/meta.js`, 'window.SITE=' + JSON.stringify({ frames, marks }) + ';\n');
  console.log('frames', frames.length, 'dur', (frames[frames.length - 1] - frames[0]).toFixed(2), marks);
  await b.close();
})().catch(e => { console.error(e); process.exit(1); });
