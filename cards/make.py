#!/usr/bin/env python3
"""Карточка товара AMURA без ИИ — утверждённый дизайн docs/CARDS.md, PNG 1086×1448.

python3 cards/make.py 00148                    → cards/out/00148.png (цены из catalog.json)
python3 cards/make.py 00167 --opt 2250 --rtl 4590   → свои цены
python3 cards/make.py <id товара> [--no-opt]   → по id; --no-opt — карточка только с розничной ценой

Тексты (название, объём, тип, 4 преимущества) — в cards/benefits.json по артикулу: владелец правит их там.
Фото — img/<id>.webp, товар вырезается с белого фона (cards/cutout.py); не вышло — фото целиком в светлой рамке.
Фон нарисован; положите фото магазина в cards/bg.jpg — встанет вместо нарисованного."""
import argparse
import json
import os
import subprocess
import sys
from urllib.parse import quote

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from cutout import cut  # noqa: E402

ap = argparse.ArgumentParser(); ap.add_argument('key', help='артикул (00148) или id товара')
ap.add_argument('--opt', type=int); ap.add_argument('--rtl', type=int); ap.add_argument('--no-opt', action='store_true')
a = ap.parse_args()

cat = json.load(open(os.path.join(ROOT, 'catalog.json')))
items = cat if isinstance(cat, list) else cat.get('items', list(cat.values()))
p = next((x for x in items if a.key in (x.get('code'), x.get('id'), x.get('article'))), None)
if not p: sys.exit(f'Нет товара {a.key} в catalog.json')
code = p['code']; texts = json.load(open(os.path.join(HERE, 'benefits.json')))
t = texts.get(code)
if not t:
    tpl = {code: {'title': [p['name'].replace(p.get('brand', ''), '').strip(), ''], 'spec': '', 'type': '',
                  'benefits': [{'icon': 'drop', 'title': '', 'text': ''} for _ in range(4)]}}
    sys.exit(f'Нет текстов для {code} «{p["name"]}» в cards/benefits.json — добавьте (только правда из описания):\n'
             + json.dumps(tpl, ensure_ascii=False, indent=1) + '\n\nОписание:\n' + p.get('desc', ''))

out = os.path.join(HERE, 'out'); os.makedirs(out, exist_ok=True)
src = os.path.join(ROOT, 'img', p['id'] + '.webp')
frame = bool(t.get('frame'))
if not frame:
    res = cut(src, os.path.join(out, code + '_prod.png'), t.get('crop'))
    frame = not (res and res['ok'])
    if frame: print('Фон с фото не отделился чисто — ставлю фото в рамке')
if frame:
    img, (w, h) = '../img/' + p['id'] + '.webp', Image.open(src).size
else:
    img, (w, h) = 'out/' + code + '_prod.png', Image.open(os.path.join(out, code + '_prod.png')).size

opt = None if a.no_opt else (a.opt or p.get('opt')); rtl = a.rtl or p.get('rtl')
d = {'brand': (p.get('brand') or '').upper(), 'title': t['title'], 'spec': t.get('spec'), 'type': t.get('type'),
     'benefits': t['benefits'], 'opt': opt, 'rtl': rtl, 'img': img, 'ratio': w / h, 'frame': frame,
     'bg': 'bg.jpg' if os.path.exists(os.path.join(HERE, 'bg.jpg')) else None}
url = 'file://' + os.path.join(HERE, 'card.html') + '?d=' + quote(json.dumps(d, ensure_ascii=False))
png = os.path.join(out, code + '.png')
js = ("let c;try{({chromium:c}=require('playwright'))}catch(e){({chromium:c}=require('/opt/node22/lib/node_modules/playwright'))}"
      "(async()=>{const b=await c.launch({args:['--no-sandbox']});const p=await b.newPage({viewport:{width:1086,height:1448}});"
      "p.on('pageerror',e=>{console.error(e.message);process.exitCode=1});"
      f"await p.goto({json.dumps(url)});await p.evaluate(()=>document.fonts.ready);await p.waitForTimeout(300);"
      # проверка: заголовок и преимущества не вылезли за свои места
      "const bad=await p.evaluate(()=>{const r=[];const t=document.getElementById('title').getBoundingClientRect();if(t.bottom>470)r.push('заголовок слишком длинный');"
      "const b=document.getElementById('ben').getBoundingClientRect();if(b.bottom>1135)r.push('преимущества не влезают — сократите тексты');return r});"
      "bad.forEach(x=>console.error('Внимание: '+x));"
      f"await p.screenshot({{path:{json.dumps(png)}}});await b.close()}})()")
subprocess.run(['node', '-e', js], check=True)
print(f'Готово: {png} — {p["name"]}, опт {opt or "—"} ₸, розница {rtl} ₸')
