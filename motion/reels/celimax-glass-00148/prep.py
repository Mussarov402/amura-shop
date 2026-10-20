#!/usr/bin/env python3
"""Фото товара без фона → product.png (медиа в git не храним). python3 motion/reels/celimax-glass-00148/prep.py"""
import os
import sys
here = os.path.dirname(os.path.abspath(__file__)); root = os.path.join(here, '../../..')
sys.path.insert(0, os.path.join(root, 'cards'))
from cutout import cut  # noqa: E402
print(cut(os.path.join(root, 'img/7147ca1b-2ff1-11f0-0a80-08e2001f36e3.webp'), os.path.join(here, 'product.png')))
