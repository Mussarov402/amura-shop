#!/usr/bin/env python3
"""Axis-Y 00259 — 3D-ролик по референсу (Reels DdFuO32JyPL): монтаж по такту.
python3 motion/reels/axisy-3d-00259/edit.py → out.mp4 (1080×1920, 30 к/с).

Исходники (в git не храним): clips/C1..C8.mp4 — Veo 3.1 Fast из ElevenLabs (flow vgflC2RhcyXdat46NcmM),
source.jpg — оригинал фото из МойСклад (макро этикетки — по настоящему фото, текст не перерисовываем),
ov_price.png / ov_end.png — накладки из overlay.html.
Звук синтезируется здесь: бит 120 BPM, «вжух» на каждой склейке, удар на ключевых кадрах."""
import json
import os
import subprocess
import sys
import wave

import numpy as np

D = os.path.dirname(os.path.abspath(__file__))
W, H, FPS, BPM = 1080, 1920, 30, 120
BEAT = 60 / BPM

# план: (источник, с какой секунды, длительность в долях такта, скорость, удар)
PLAN = json.load(open(os.path.join(D, 'plan.json')))


def run(*a):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *a], check=True)


def seg(i, p):
    out = os.path.join(D, 'frames', f'{i:02d}.mp4')
    dur = p['beats'] * BEAT
    vf = f'scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS}'
    if p['src'].startswith('label'):
        # макро этикетки по настоящему фото: окно 1080×1920 плавно едет по тюбику (кадры — PIL, без перерисовки текста)
        from PIL import Image
        sw, x0, y0, x1, y1 = p['pan']
        im = Image.open(os.path.join(D, 'label.jpg')).convert('RGB')
        im = im.resize((sw, round(im.height * sw / im.width)), Image.LANCZOS)
        n = round(dur * FPS); tmp = os.path.join(D, 'frames', f'lab{i:02d}')
        os.makedirs(tmp, exist_ok=True)
        c = 540 * sw / 1086                                              # тюбик на фото ровно по центру, x = 540
        for f in range(n):
            k = f / max(1, n - 1); k = k * k * (3 - 2 * k)
            y = round(y0 + (y1 - y0) * k)
            x = round(c - W / 2 + x0 + (x1 - x0) * k)               # x0/x1 — сдвиг от центра тюбика
            x = max(0, min(im.width - W, x))
            im.crop((x, y, x + W, y + H)).save(os.path.join(tmp, f'{f:03d}.jpg'), quality=95)
        run('-framerate', str(FPS), '-i', os.path.join(tmp, '%03d.jpg'), '-c:v', 'libx264', '-crf', '16', '-pix_fmt', 'yuv420p', out)
    elif p['src'] == 'flash':
        run('-f', 'lavfi', '-i', f'color=white:s={W}x{H}:r={FPS}', '-t', f'{dur:.3f}', '-c:v', 'libx264', '-crf', '16', '-pix_fmt', 'yuv420p', out)
    else:
        sp = p.get('speed', 1)
        run('-ss', str(p['in']), '-i', os.path.join(D, 'clips', p['src'] + '.mp4'), '-vf', f'setpts=PTS/{sp},{vf}',
            '-t', f'{dur:.3f}', '-an', '-c:v', 'libx264', '-crf', '16', '-pix_fmt', 'yuv420p', out)
    return dur


SR = 48000
rng = np.random.default_rng(7)


def env(n, a=.002, r=.1):
    t = np.arange(n) / SR
    return np.minimum(1, t / a) * np.exp(-t / r)


def tone(f, d, r, v=1.):
    n = int(d * SR); t = np.arange(n) / SR
    return v * env(n, .002, r) * np.sin(2 * np.pi * f * t)


def kick():
    n = int(.35 * SR); t = np.arange(n) / SR
    f = 140 * np.exp(-t * 25) + 45
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, .001, .12)


def whoosh(d=.35):
    n = int(d * SR); x = rng.standard_normal(n)
    x = np.convolve(x, np.ones(18) / 18, 'same') * np.hanning(n)
    return .5 * x


def impact():
    n = int(.8 * SR); t = np.arange(n) / SR
    f = 90 * np.exp(-t * 6) + 32
    x = np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, .001, .3)
    m = int(.04 * SR); x[:m] += rng.standard_normal(m) * env(m, .0005, .01)
    return x


def audio(total, cuts, hits):
    n = int(total * SR) + SR; out = np.zeros(n)

    def put(s, at, v=1.):
        i = int(at * SR)
        if 0 <= i < n: j = min(n, i + len(s)); out[i:j] += v * s[:j - i]
    roots = [55, 55, 49, 41.2]
    k = 0
    while k * BEAT < total:
        t = k * BEAT
        put(kick(), t, .9)
        put(rng.standard_normal(int(.03 * SR)) * env(int(.03 * SR), .0005, .008), t + BEAT / 2, .18)   # хэт
        if k % 2: put(rng.standard_normal(int(.15 * SR)) * env(int(.15 * SR), .001, .04), t, .3)        # хлопок
        r = roots[(k // 4) % 4]
        put(tone(r * 2, BEAT * .9, .2, .35), t)                                                            # бас
        put(tone(r * 8 * [1, 1.5, 1.26, 2][k % 4], .18, .06, .08), t + BEAT / 2)                          # «плак»
        k += 1
    for c in cuts: put(whoosh(), max(0, c - .17), .55)
    for h in hits: put(impact(), h, .8)
    t = np.arange(n) / SR
    out *= np.clip((total - t) / 1.2, 0, 1) * np.clip(t / .05, 0, 1)
    out = out / max(1e-6, np.abs(out).max()) * .9
    pcm = (np.repeat(out[:, None], 2, 1) * 32767).astype(np.int16)
    with wave.open(os.path.join(D, 'mix.wav'), 'wb') as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())


if __name__ == '__main__':
    os.makedirs(os.path.join(D, 'frames'), exist_ok=True)
    t, cuts, hits, lst = 0, [], [], []
    for i, p in enumerate(PLAN):
        d = seg(i, p); lst.append(f"file '{os.path.join(D, 'frames', f'{i:02d}.mp4')}'")
        if i: cuts.append(t)
        if p.get('hit'): hits.append(t)
        t += d
    open(os.path.join(D, 'frames', 'list.txt'), 'w').write('\n'.join(lst))
    run('-f', 'concat', '-safe', '0', '-i', os.path.join(D, 'frames', 'list.txt'), '-c', 'copy', os.path.join(D, 'frames', 'cut.mp4'))
    audio(t, cuts, hits)
    end_at = t - PLAN[-1]['beats'] * BEAT                                # финальная плашка — на последнем плане
    run('-i', os.path.join(D, 'frames', 'cut.mp4'), '-loop', '1', '-i', os.path.join(D, 'ov_price.png'), '-loop', '1', '-i', os.path.join(D, 'ov_end.png'), '-i', os.path.join(D, 'mix.wav'),
        '-filter_complex', f"[0][1]overlay=shortest=1:enable='lt(t,{end_at:.2f})'[a];[2]format=rgba,fade=in:st={end_at:.2f}:d=0.35:alpha=1[e];[a][e]overlay=shortest=1[v]",
        '-map', '[v]', '-map', '3:a', '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p', '-r', str(FPS),
        '-c:a', 'aac', '-b:a', '160k', '-ar', '48000', '-af', 'loudnorm=I=-14:TP=-1.5', '-shortest', '-movflags', '+faststart', os.path.join(D, 'out.mp4'))
    print(f'Готово: out.mp4, {t:.1f} с, склеек {len(cuts)}')
