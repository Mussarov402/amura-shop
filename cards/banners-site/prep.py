#!/usr/bin/env python3
"""Фото товаров для баннеров сайта: вырезка из фото каталога (img/<id>.webp) → src/<код>.png.
VT PDRN Essence и Medicube AGE-R берём из готовых вырезок (docs/mp-style/prep.py, cards/medicube-ager/prep.py)."""
import os, sys, shutil, subprocess
H = os.path.dirname(os.path.abspath(__file__)); R = os.path.dirname(os.path.dirname(H))
sys.path.insert(0, os.path.join(R, 'cards')); from cutout import cut
os.makedirs(os.path.join(H, 'src'), exist_ok=True)
P = {'00615': '2c7d8772-8aec-11f0-0a80-118e0037a53a', '00661': 'e9c356cf-ae57-11f0-0a80-1a880005bbe3', '00668': '7d476d2b-af01-11f0-0a80-1c6f000240d0',
     '00690': '7d422fe5-af01-11f0-0a80-1c6f000240cc', '01250': '4e504b44-8b4c-11f1-0a80-1d12001a3c77', '01335': '9f44b546-b656-11f1-0a80-03fc001b502d'}
CROP = {'01335': (0, 150, 600, 800)}     # у Torriden сверху надпись на фото — отрезаем
for c, i in P.items(): cut(os.path.join(R, 'img', i + '.webp'), os.path.join(H, 'src', c + '.png'), crop=CROP.get(c))
subprocess.run([sys.executable, os.path.join(R, 'docs/mp-style/prep.py')], check=True)
shutil.copy(os.path.join(R, 'docs/mp-style/vt_cut.png'), os.path.join(H, 'src', '00784.png'))
m = os.path.join(R, 'cards/medicube-ager/src/pink_cut.png')
if not os.path.exists(m): subprocess.run([sys.executable, os.path.join(R, 'cards/medicube-ager/prep.py')], check=True)
shutil.copy(m, os.path.join(H, 'src', '01122.png'))
print('готово:', sorted(os.listdir(os.path.join(H, 'src'))))
