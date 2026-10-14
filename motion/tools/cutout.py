#!/usr/bin/env python3
"""Фото товара на белом фоне → PNG с прозрачным фоном для роликов.

python3 motion/tools/cutout.py фото.jpg товар.png [--crop x1,y1,x2,y2] [--scale 3]
Белый фон, касающийся краёв, становится прозрачным; товар увеличивается (--scale) с подрезкой резкости.
Нужны: pip install pillow numpy scipy. Печатает ratio = ширина/высота для data.js."""
import argparse

import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage

ap = argparse.ArgumentParser(); ap.add_argument('src'); ap.add_argument('dst')
ap.add_argument('--crop'); ap.add_argument('--scale', type=float, default=3)
a = ap.parse_args()
im = Image.open(a.src).convert('RGB')
if a.crop: im = im.crop(tuple(int(v) for v in a.crop.split(',')))
im = im.resize((int(im.width * a.scale), int(im.height * a.scale)), Image.LANCZOS).filter(ImageFilter.UnsharpMask(radius=2, percent=80, threshold=2))
px = np.asarray(im).astype(int)
white = (px.min(axis=2) > 236) & ((px.max(axis=2) - px.min(axis=2)) < 14)
lab, _ = ndimage.label(white)
edge = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))) - {0}
keep = ndimage.binary_opening(~np.isin(lab, list(edge)), iterations=2)
alpha = Image.fromarray((keep * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.2))
out = im.convert('RGBA'); out.putalpha(alpha); out = out.crop(out.getbbox()); out.save(a.dst)
print(f'{a.dst}: {out.width}×{out.height}, ratio: {out.width} / {out.height}')
