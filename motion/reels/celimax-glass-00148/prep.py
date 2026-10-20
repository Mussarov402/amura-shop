#!/usr/bin/env python3
"""Фото товара без фона → product.png (медиа в git не храним). python3 motion/reels/celimax-glass-00148/prep.py
Лучше брать оригинал из МойСклад (1086×1448, вдвое крупнее фото сайта): сохранить его как source.jpg рядом.
Без source.jpg берётся img/<id>.webp сайта."""
import os
import sys
here = os.path.dirname(os.path.abspath(__file__)); root = os.path.join(here, '../../..')
sys.path.insert(0, os.path.join(root, 'cards'))
from cutout import cut  # noqa: E402
src = os.path.join(here, 'source.jpg')
if os.path.exists(src):   # белый флакон на белом фоне: порог 248, кадр без надписей сверху
    out = os.path.join(here, 'product.png')
    print(cut(src, out, crop=[0, 400, 1086, 1340], thr=248))   # низ 1340 — без тени-подложки под товаром
    import numpy as np
    from PIL import Image
    im = Image.open(out); a = np.asarray(im).copy(); al = a[..., 3]
    box_top = next(y for y in range(al.shape[0]) if (al[y, int(al.shape[1] * .75)] > 128))
    for y in range(box_top):                                    # над коробкой — только крышка: ровный прямоугольник
        xs = np.where(al[y] > 128)[0]
        if len(xs) > 20: al[y, xs.min():xs.max() + 1] = np.maximum(al[y, xs.min():xs.max() + 1], 255)
    h = al.shape[0]                                             # низ: остатки светлой тени убрать
    low = a[h - 60:, :, :3].min(2) > 232; al[h - 60:][low] = 0
    Image.fromarray(a).save(out)
else:
    print(cut(os.path.join(root, 'img/7147ca1b-2ff1-11f0-0a80-08e2001f36e3.webp'), os.path.join(here, 'product.png')))
