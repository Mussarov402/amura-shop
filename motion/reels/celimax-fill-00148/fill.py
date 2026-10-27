#!/usr/bin/env python3
"""Celimax 00148 — ролик «наполнение» по референсу владельца (Reels DdnndJ1t7l5): пустой прозрачный флакон
наполняется молочной текстурой и становится настоящим флаконом; в конце — плашка AMURA и петля к началу.
python3 motion/reels/celimax-fill-00148/fill.py → out.mp4 (1080×1920, 30 к/с, AAC 48 кГц).

Исходники (в git не храним): clips/V.mp4 — Veo 3.1 Fast из ElevenLabs (flow ULD2C8p6BJpmj14eW6rG, старт — пустой флакон,
конец — настоящий флакон по фото МойСклад); ov_price.png / ov_end.png — накладки из overlay.html.
Клип V2 (Veo 3.1, 25.10): один непрерывный план наполнения; в 5,7 с Veo подменяет кувшин крышкой — режем до этого
и мягко растворяем в готовый флакон (7,0 с)."""
import os
import subprocess
import wave

import numpy as np

D = os.path.dirname(os.path.abspath(__file__))
FPS, SR = 30, 48000
JUMP_A, JUMP_B, FLASH = 5.60, 7.00, .60          # V2: до смены «кувшин → крышка» / готовый флакон / мягкое растворение
HOLD, LOOP = 2.6, .8                             # стоим на готовом флаконе / растворяемся обратно в начало
CAP_AT = 5.6                                      # крышка садится на флакон (по клипу)


def run(*a):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *a], check=True)


def env(n, a=.002, r=.1):
    t = np.arange(n) / SR
    return np.minimum(1, t / a) * np.exp(-t / r)


def audio(total, pour, chime, cap, end):
    rng = np.random.default_rng(5); n = int(total * SR); out = np.zeros(n); t = np.arange(n) / SR

    def put(s, at, v=1.):
        i = int(at * SR)
        if 0 <= i < n: j = min(n, i + len(s)); out[i:j] += v * s[:j - i]
    # мягкая подложка: аккорды и тихий «плак» 84 BPM
    beat = 60 / 84
    for k, ch in enumerate([(220, 277.2, 329.6), (196, 246.9, 329.6), (174.6, 220, 261.6), (196, 246.9, 293.7)] * 3):
        a = k * 4 * beat; m = (t >= a) & (t < a + 4 * beat); tt = t[m] - a
        out[m] += sum(np.sin(2 * np.pi * f * tt) for f in ch) * .05 * np.minimum(1, tt / .4) * np.exp(-tt / 4)
        for s in range(4):
            d = int(.25 * SR); tn = np.arange(d) / SR
            put(np.sin(2 * np.pi * ch[s % 3] * 2 * tn) * env(d, .002, .08), a + s * beat, .1)
    # наливание: шум, отфильтрованный в «журчание», с лёгким бульканьем
    a, b = pour; m = (t >= a) & (t < b)
    x = rng.standard_normal(m.sum()); x = np.convolve(x, np.ones(40) / 40, 'same')
    x *= (1 + .5 * np.sin(2 * np.pi * 7 * (t[m] - a))) * np.minimum(1, (t[m] - a) / .3) * np.minimum(1, (b - t[m]) / .3)
    out[m] += x * 1.6
    d = int(1.6 * SR); tn = np.arange(d) / SR                                       # «динь» на волшебном моменте
    put((np.sin(2 * np.pi * 1568 * tn) + .4 * np.sin(2 * np.pi * 2352 * tn)) * env(d, .002, .5), chime, .35)
    d = int(.06 * SR); put(rng.standard_normal(d) * env(d, .0005, .01), cap, .7)     # щелчок крышки
    d = int(1.4 * SR); put((np.sin(2 * np.pi * 1318.5 * tn[:d]) + .5 * np.sin(2 * np.pi * 1975.5 * tn[:d])) * env(d, .002, .45), end, .25)
    out *= np.clip((total - t) / .8, 0, 1)
    out = out / max(1e-6, np.abs(out).max()) * .9
    with wave.open(os.path.join(D, 'mix.wav'), 'wb') as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((np.repeat(out[:, None], 2, 1) * 32767).astype(np.int16).tobytes())


if __name__ == '__main__':
    V = os.path.join(D, 'clips', 'V2_fixed.mp4'); F = os.path.join(D, 'frames'); os.makedirs(F, exist_ok=True)
    clip_end = 7.7                                         # в последних кадрах V2 снова мелькает кувшин
    a_len = JUMP_A                                           # часть до скачка
    b_len = clip_end - JUMP_B                                # часть после скачка
    main = a_len + b_len - FLASH                             # длина после вспышки-перехода
    total = main + HOLD                                      # плюс стоим на готовом флаконе (петля — внутри HOLD)
    end_at = main - .2
    sc = 'scale=1080:1920,fps=30,format=yuv420p,settb=AVTB'
    run('-i', V, '-loop', '1', '-i', os.path.join(D, 'ov_price.png'), '-loop', '1', '-i', os.path.join(D, 'ov_end.png'),
        '-filter_complex',
        f"[0]trim=0:{JUMP_A},setpts=PTS-STARTPTS,{sc}[a];[0]trim={JUMP_B}:{clip_end},setpts=PTS-STARTPTS,{sc}[b];"
        f"[a][b]xfade=transition=fade:duration={FLASH}:offset={a_len - FLASH}[m];"
        f"[m]tpad=stop_mode=clone:stop_duration={HOLD}[h];"
        f"[0]trim=0:0.05,setpts=PTS-STARTPTS,{sc},tpad=stop_mode=clone:stop_duration={LOOP + .1}[s];"
        f"[h][s]xfade=transition=fade:duration={LOOP}:offset={total - LOOP}[v0];"
        f"[v0][1]overlay=shortest=1:enable='lt(t,{end_at:.2f})'[v1];"
        f"[2]format=rgba,fade=in:st={end_at:.2f}:d=0.35:alpha=1,fade=out:st={total - LOOP:.2f}:d={LOOP / 2:.2f}:alpha=1[e];[v1][e]overlay=shortest=1[v]",
        '-map', '[v]', '-t', f'{total:.3f}', '-c:v', 'libx264', '-crf', '17', '-pix_fmt', 'yuv420p', '-an', os.path.join(F, 'video.mp4'))
    audio(total, pour=(1.6, JUMP_A - .2), chime=3.4, cap=a_len - FLASH / 2, end=end_at)
    run('-i', os.path.join(F, 'video.mp4'), '-i', os.path.join(D, 'mix.wav'), '-map', '0:v', '-map', '1:a', '-c:v', 'copy',
        '-c:a', 'aac', '-b:a', '160k', '-ar', '48000', '-af', 'loudnorm=I=-14:TP=-1.5', '-t', f'{total:.3f}', '-movflags', '+faststart', os.path.join(D, 'out.mp4'))
    print(f'Готово: out.mp4, {total:.1f} с')
