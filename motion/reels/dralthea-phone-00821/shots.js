/* Экраны amura.kz для телефона в ролике: node motion/reels/dralthea-phone-00821/shots.js motion/reels/dralthea-phone-00821  (390×844 @3x, цены скрыты). Плюс cream.png — фото товара без фона (cards/cutout.py, crop 0,215,600,800). */
let c;try{({chromium:c}=require('playwright'))}catch(e){({chromium:c}=require('/opt/node22/lib/node_modules/playwright'))}
const D=process.argv[2];
const hide=async p=>p.evaluate(()=>{document.querySelectorAll('body *').forEach(e=>{if(e.children.length===0&&/₸/.test(e.textContent))e.style.visibility='hidden'})});
(async()=>{const b=await c.launch({args:['--no-sandbox']});const p=await b.newPage({viewport:{width:390,height:844},deviceScaleFactor:3,isMobile:true,hasTouch:true});
await p.goto('https://amura.kz/',{waitUntil:'networkidle',timeout:60000});await p.waitForTimeout(1200);
await p.getByText('Да, верно').click().catch(()=>{});await p.waitForTimeout(800);
await hide(p);await p.screenshot({path:D+'/s1_home.png'});
const inp=p.getByPlaceholder('Бренд, товар или артикул').first();await inp.click();await p.waitForTimeout(400);
await p.screenshot({path:D+'/s2_focus.png'});
await inp.type('00821',{delay:120});await p.waitForTimeout(2000);await hide(p);
await p.screenshot({path:D+'/s3_search.png'});
await p.keyboard.press('Enter');await p.waitForTimeout(2500);await hide(p);
await p.screenshot({path:D+'/s4_product.png'});
const btn=p.getByText(/В корзину/).first();const bb=await btn.boundingBox();console.log('btn',JSON.stringify(bb));
await btn.click().catch(e=>console.log('cart',e.message));await p.waitForTimeout(1500);await hide(p);
await p.screenshot({path:D+'/s5_added.png'});
await b.close()})();
