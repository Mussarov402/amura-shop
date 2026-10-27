#!/usr/bin/env python3
"""Туба в сценах ElevenLabs — настоящая: мелкий текст на сгенерированной этикетке бывает с ошибками,
поэтому тело тубы с фото МойСклад (tube.png, без колпачка) натягивается по 4 углам на тубу в сцене
(перспективное преобразование), цвет подгоняется под свет сцены, пальцы (если держат тубу) остаются сверху.
python3 tubefix.py → bg/s_*.png"""
import os
import numpy as np
from PIL import Image, ImageFilter
D = os.path.dirname(os.path.abspath(__file__))
REAL = [(0, 0), (342, 0), (66, 895), (274, 895)]          # углы тела тубы на tube.png: верх шва и стык с колпачком
# сцена → (файл генерации, углы тубы в сцене TL TR BL BR, оставить пальцы)
SCENES = {
    's_podium': ('7rjIA315kDaIV2ZevFYj', [(508, 214), (728, 214), (556, 814), (687, 814)], False),
    's_shelf': ('SNWFujgBrlgfK223VpTp', [(526, 505), (680, 505), (556, 940), (647, 940)], False),
    'w_podium': ('agwj3gBrOOByQUCH0oUN', [(922, 75), (1070, 75), (950, 492), (1040, 492)], False),
    'w_swirl': ('vE450qVKJFmXGA8D82V2', [(888, 84), (1054, 84), (920, 540), (1027, 540)], False),
}
KEEP = {'w_model': 'UOLFyNjlDLko1g27jP5q', 's_splash': 'VjBO18H14tweXq41gLwi', 's_face': '44C4WPV5VKRuErEILExG', 's_face2': 'DgMQBembx50riIgbCFAf',
        'w_face': 'WQnhHFT6HNs0Vy8wueT5'}   # этикетка верная или тубы нет; w_model — только кадр лица справа от тубы (x ≥ 900)


def coeffs(dst, src):
    # коэффициенты PIL PERSPECTIVE: точка результата (dst) → точка исходника (src)
    A, B = [], []
    for (x, y), (u, v) in zip(dst, src):
        A += [[x, y, 1, 0, 0, 0, -u * x, -u * y], [0, 0, 0, x, y, 1, -v * x, -v * y]]; B += [u, v]
    return np.linalg.solve(np.array(A, float), np.array(B, float))


def fix(gen, pts, fingers):
    g = Image.open(os.path.join(D, 'raw2', gen + '.png')).convert('RGB')
    r = Image.open(os.path.join(D, 'tube.png')).convert('RGBA').crop((0, 0, 342, 895))
    w = r.transform(g.size, Image.PERSPECTIVE, tuple(coeffs(pts, REAL)), Image.BICUBIC)
    wa = np.asarray(w).astype(float); ga = np.asarray(g).astype(float)
    a = wa[:, :, 3] / 255
    a = np.asarray(Image.fromarray((a * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1))) / 255
    m = a > .5                                                   # подгонка света: белый пластик тубы в сцене и на фото
    wm = m & (wa[:, :, :3].min(2) > 200); gm = m & (ga.min(2) > 180)
    if wm.sum() > 50 and gm.sum() > 50:
        wa[:, :, :3] *= np.median(ga[gm], 0) / np.median(wa[wm][:, :3], 0)
    if fingers:                                                  # кожа поверх тубы остаётся из сцены
        R, G_, B = ga[:, :, 0], ga[:, :, 1], ga[:, :, 2]
        skin = (R > G_ + 12) & (R > B + 20) & (R > 120)
        skin = np.asarray(Image.fromarray((skin * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(2))) / 255
        a = a * (1 - skin)
    out = ga * (1 - a[:, :, None]) + np.clip(wa[:, :, :3], 0, 255) * a[:, :, None]
    return Image.fromarray(out.astype(np.uint8))


if __name__ == '__main__':
    os.makedirs(os.path.join(D, 'bg'), exist_ok=True)
    for k, (gen, pts, f) in SCENES.items():
        fix(gen, pts, f).save(os.path.join(D, 'bg', k + '.png'))
    for k, gen in KEEP.items():
        Image.open(os.path.join(D, 'raw2', gen + '.png')).convert('RGB').save(os.path.join(D, 'bg', k + '.png'))
    print('Готово: bg/s_*, bg/w_*')
