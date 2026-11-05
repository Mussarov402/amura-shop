#!/usr/bin/env python3
"""Вырезает флакон VT PDRN Essence 100 из фото каталога (img/3a575134…webp, 800×800) → docs/mp-style/vt_cut.png.
Запускать перед render.js: python3 docs/mp-style/prep.py. Контур — по замерам фото (колпачок, горлышко, корпус)."""
import os
from PIL import Image, ImageDraw
root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
im = Image.open(os.path.join(root, 'img', '3a575134-c90a-11f0-0a80-06de0071cafc.webp')).convert('RGB')
S = 4; m = Image.new('L', (800 * S, 800 * S), 0); d = ImageDraw.Draw(m)
d.rounded_rectangle([301 * S, 63 * S, 499 * S, 192 * S], radius=26 * S, fill=255)
d.rectangle([319 * S, 185 * S, 481 * S, 258 * S], fill=255)
d.rounded_rectangle([271 * S, 250 * S, 528 * S, 756 * S], radius=34 * S, fill=255)
m = m.resize((800, 800), Image.LANCZOS)
im.putalpha(m); im.crop(m.getbbox()).save(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vt_cut.png'))
