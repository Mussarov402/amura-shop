#!/usr/bin/env python3
"""Этикетка Celimax 00148 в клипе — настоящая, с фото МойСклад (real.jpg): Veo нарисовал мелкий текст с ошибками.
Вырезка этикетки с фото (без перерисовки) масштабируется под этикетку в кадре, цвет подгоняется, правая часть
(«FOR SENSITIVE / DRY SKIN» — на фото её заслоняет коробка) продолжается фоном этикетки. Накладывается с момента, когда флакон стоит неподвижно.
python3 labelfix.py → clips/V2_fixed.mp4 (клип V2 — Veo 3.1, один непрерывный план, 25.10)."""
import os, subprocess
import numpy as np
from PIL import Image, ImageFilter
D = os.path.dirname(os.path.abspath(__file__))
SRC, OUT = 'V2.mp4', 'V2_fixed.mp4'
X, Y, W_, H_ = 392, 1192, 326, 296            # этикетка в кадре клипа (1080×1920)
T0, FADE = 3.2, .5                            # этикетка проявляется по мере наполнения — в это же время ставим настоящую
PROBE = 7.2                                   # кадр, по которому берём цвет этикетки


def patch(gen_gray):
    r = Image.open(os.path.join(D, 'real.jpg')).convert('RGB')
    p = r.crop((213, 921, 380, 1221)); k = H_ / 300                 # до «FOR»: правее на фото этикетку заслоняет коробка
    p = p.resize((round(p.width * k), H_), Image.LANCZOS)
    a = np.asarray(p).astype(float)
    real_gray = np.median(a[150:200, 20:60].reshape(-1, 3), 0)
    a = np.clip(a * (np.array(gen_gray) / real_gray), 0, 255)
    edge = np.median(a[:, -8:-2], 1)                                  # продолжаем фон этикетки и линию последним столбцом
    out = np.repeat(edge[:, None, :], W_, 1); out[:, :a.shape[1]] = a
    m = np.ones((H_, W_)) * 255                                   # мягкий край 3 px, чтобы не было шва
    im = Image.fromarray(out.astype(np.uint8)); mask = Image.fromarray(m.astype(np.uint8))
    mask = Image.fromarray((np.pad(np.ones((H_ - 6, W_ - 6)) * 255, 3)).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.5))
    im.save(os.path.join(D, 'frames', 'label_patch.png')); mask.save(os.path.join(D, 'frames', 'label_mask.png'))


if __name__ == '__main__':
    os.makedirs(os.path.join(D, 'frames'), exist_ok=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', str(PROBE), '-i', os.path.join(D, 'clips', SRC), '-frames:v', '1', os.path.join(D, 'frames', 'last.png')], check=True)
    g = np.asarray(Image.open(os.path.join(D, 'frames', 'last.png')).convert('RGB')).astype(float)
    patch(np.median(g[1300:1360, 420:460].reshape(-1, 3), 0))
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', os.path.join(D, 'clips', SRC),
                    '-loop', '1', '-i', os.path.join(D, 'frames', 'label_patch.png'), '-loop', '1', '-i', os.path.join(D, 'frames', 'label_mask.png'),
                    '-filter_complex', f"[2]format=gray[m];[1][m]alphamerge,format=rgba,fade=in:st={T0}:d={FADE}:alpha=1[p];[0][p]overlay={X}:{Y}:shortest=1[v]",
                    '-map', '[v]', '-c:v', 'libx264', '-crf', '14', '-pix_fmt', 'yuv420p', os.path.join(D, 'clips', OUT)], check=True)
    print('Готово: clips/' + OUT)
