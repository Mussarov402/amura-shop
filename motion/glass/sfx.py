#!/usr/bin/env python3
"""Звук ролика AMURA Glass: python3 motion/glass/sfx.py <папка> → <папка>/mix.wav
Всё синтезируется здесь (numpy), чужой музыки нет:
  click — щелчок клавиши (печать)   ding — стеклянный «динь» (появление карточки)
  ping — короткий «пинг» (пункт списка)   pop — мягкий «поп» (таблетка)   whoosh — воздух (смена формы)
Подложка: лёгкий электронный бит 100 BPM с мягким аккордом; первые секунды (печать) — тише, в конце — затухание."""
import json
import os
import sys
import wave

import numpy as np

SR = 48000
rng = np.random.default_rng(3)


def env(n, a=.003, r=.15):
    t = np.arange(n) / SR
    return np.minimum(1, t / a) * np.exp(-t / r)


def sine(f, d, r, vol=1.0, partials=((1, 1),)):
    n = int(d * SR); t = np.arange(n) / SR
    return vol * env(n, .002, r) * sum(a * np.sin(2 * np.pi * f * m * t) for m, a in partials)


def click():
    n = int(.03 * SR); x = rng.standard_normal(n) * env(n, .0005, .006)
    x = np.diff(x, prepend=0)                       # высокие частоты — сухой щелчок
    tone = sine(2400 + rng.integers(-300, 300), .02, .004, .15)
    x[:len(tone)] += tone / .35
    return .35 * x


def ding():
    x = sine(1568, 1.4, .45, .5, ((1, 1), (2.01, .35), (3.02, .15)))
    hi = sine(2349, 1.0, .3, .2); x[:len(hi)] += hi
    return x


def ping():
    return sine(1975, .5, .14, .4, ((1, 1), (2, .2)))


def pop():
    n = int(.12 * SR); t = np.arange(n) / SR
    f = 900 * np.exp(-t * 28) + 180
    return .6 * np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, .001, .04)


def whoosh():
    n = int(.6 * SR); x = rng.standard_normal(n)
    k = np.hanning(n); x = np.convolve(x, np.ones(30) / 30, mode='same')
    return .25 * x * k


FX = {'click': click, 'ding': ding, 'ping': ping, 'pop': pop, 'whoosh': whoosh}


def bed(dur):
    n = int(dur * SR); out = np.zeros(n); beat = 60 / 100
    t = np.arange(n) / SR
    chords = [(220, 277.2, 329.6), (196, 246.9, 293.7), (174.6, 220, 261.6), (196, 246.9, 329.6)]
    for i, ch in enumerate(chords * 8):                                # аккорд-подложка, смена каждые 4 доли
        a = i * 4 * beat
        if a >= dur: break
        m = (t >= a) & (t < a + 4 * beat); tt = t[m] - a
        out[m] += sum(np.sin(2 * np.pi * f * tt) for f in ch) * .05 * np.minimum(1, tt / .3) * np.exp(-tt / 3)
    k = 0
    while k * beat < dur:                                              # бочка на каждую долю, хэт на половинки
        i = int(k * beat * SR)
        kick = sine(55, .25, .12, .5); hat = rng.standard_normal(int(.04 * SR)) * env(int(.04 * SR), .001, .01) * .06
        for snd, at in ((kick, i), (hat, i + int(beat / 2 * SR))):
            j = min(n, at + len(snd)); out[at:j] += snd[:j - at] if at < n else 0
        k += 1
    return out


d = sys.argv[1]; cfg = json.load(open(os.path.join(d, 'cues.json'))); dur = cfg['end']
n = int(dur * SR) + SR
music = bed(dur + 1)[:n]
first = min((c['t'] for c in cfg['cues'] if c['kind'] == 'ding'), default=0)
t = np.arange(n) / SR
music *= np.where(t < first, .35, 1.0)                                  # во время печати бит тихий
music *= np.clip((dur - t) / 1.5, 0, 1)                                 # затухание в конце
fx = np.zeros(n)
for c in cfg['cues']:
    s = FX[c['kind']](); i = int(c['t'] * SR); j = min(n, i + len(s)); fx[i:j] += s[:j - i]
mix = music + fx
mix = mix / max(1e-6, np.abs(mix).max()) * .9
pcm = (np.repeat(mix[:, None], 2, 1) * 32767).astype(np.int16)
with wave.open(os.path.join(d, 'mix.wav'), 'wb') as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())
print(f'Звук: {dur:.1f} с, звуков {len(cfg["cues"])}')
