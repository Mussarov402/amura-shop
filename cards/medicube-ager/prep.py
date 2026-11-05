#!/usr/bin/env python3
"""Вырезка аппарата и коробки Medicube AGE-R Booster Pro из фото МойСклад.
Розовый (01122): фото МС 9bfbdbea… (800×800) → src/pink.jpg; результат src/pink_cut.png и src/pink_dev.png (только аппарат)."""
import os
from PIL import Image, ImageDraw
d = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src')
im = Image.open(os.path.join(d, 'pink.jpg')).convert('RGB')
S = 4
def mask(parts):
    m = Image.new('L', (800 * S, 800 * S), 0); dr = ImageDraw.Draw(m)
    for kind, box, r in parts:
        b = [v * S for v in box]
        (dr.rounded_rectangle(b, radius=r * S, fill=255) if kind == 'rr' else dr.rectangle(b, fill=255))
    return m.resize((800, 800), Image.LANCZOS)
dev = ('rr', (547, 192, 651, 762), 40)
for name, parts in (('pink_cut', [('r', (176, 48, 560, 758), 0), dev]), ('pink_dev', [dev])):
    m = mask(parts); r = im.copy(); r.putalpha(m); r.crop(m.getbbox()).save(os.path.join(d, name + '.png'))
