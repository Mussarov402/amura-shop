#!/usr/bin/env python3
"""Фото товара → serum.png (тюбик + коробка без белого фона) для ролика.
python3 motion/reels/skin1004-spf-window-00355/prep.py   (из корня репозитория)
Белый тюбик на белом фоне cutout.py «съедает», поэтому маска строится по контуру: тюбик — по строкам, коробка — прямоугольник."""
import os

import numpy as np
from PIL import Image, ImageFilter

here = os.path.dirname(os.path.abspath(__file__))
im = Image.open(os.path.join(here, '../../../img/0c3c71e7-3ee3-11f0-0a80-1753006a5bc5.webp')).convert('RGB')
m = np.asarray(im).astype(int).min(2)
mask = np.zeros(m.shape, np.uint8)
for y in range(250, 768):                       # тюбик: от первой до последней не-белой точки в строке
    xs = np.where(m[y, 60:300] < 250)[0] + 60
    if len(xs): mask[y, xs.min():xs.max() + 1] = 255
c = m[300:740, 300:560].mean(0); nz = np.where(c < 249)[0] + 300; L, R = nz.min(), nz.max()
r = m[240:790, L + 3:R - 3].mean(1); nr = np.where(r < 249)[0] + 240; T, B = nr.min(), nr.max()
mask[T:B + 1, L:R + 1] = 255                    # коробка
box = (85, 248, R + 4, B + 4); im = im.crop(box); mk = Image.fromarray(mask).crop(box)
im = im.resize((im.width * 3, im.height * 3), Image.LANCZOS).filter(ImageFilter.UnsharpMask(2, 80, 2))
mk = mk.resize(im.size, Image.LANCZOS).filter(ImageFilter.GaussianBlur(1.5))
out = im.convert('RGBA'); out.putalpha(mk); out = out.crop(out.getbbox())
out.save(os.path.join(here, 'serum.png')); print(f'serum.png: {out.width}×{out.height}, ratio: {out.width} / {out.height}')
