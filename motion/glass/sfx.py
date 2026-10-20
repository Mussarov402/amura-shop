#!/usr/bin/env python3
"""Звук ролика AMURA Glass: python3 motion/glass/sfx.py <папка> → <папка>/mix.wav
Всё синтезируется здесь (numpy), чужой музыки нет:
  click — щелчок клавиши (печать)   ding — стеклянный «динь» (появление карточки)
  ping — короткий «пинг» (пункт списка)   pop — мягкий «поп» (таблетка)   whoosh — воздух (смена формы)
  hit — удар-«бум» (слово на весь экран)   glitch — цифровой сбой
Подложка: электронный бит 100 BPM — бочка, хлопок, бас, арпеджио и мягкий аккорд; первые секунды (печать) — тише, в конце — затухание.
Голос: voice.wav / voice.mp3 в папке ролика (в git не храним) — под голосом музыка приглушается."""
import json
import os
import subprocess
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


def hit():
    n = int(.7 * SR); t = np.arange(n) / SR                                 # удар: низкий «бум» с падающей частотой + шум атаки
    f = 120 * np.exp(-t * 9) + 38
    x = np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, .001, .22)
    m = int(.05 * SR); x[:m] += rng.standard_normal(m) * env(m, .0005, .012) * .6
    return .9 * x


def glitch():
    n = int(.22 * SR); x = np.zeros(n)                                      # цифровой сбой: рваные пачки шума и писка
    for a in range(0, n, int(.03 * SR)):
        if rng.random() < .6:
            m = min(n - a, int(rng.uniform(.008, .025) * SR)); t = np.arange(m) / SR
            x[a:a + m] += np.sign(np.sin(2 * np.pi * rng.uniform(300, 2500) * t)) * .25 + rng.standard_normal(m) * .15
    return x


FX = {'click': click, 'ding': ding, 'ping': ping, 'pop': pop, 'whoosh': whoosh, 'hit': hit, 'glitch': glitch}


def bed(dur):
    n = int(dur * SR); out = np.zeros(n); beat = 60 / 100
    t = np.arange(n) / SR
    chords = [(220, 277.2, 329.6), (196, 246.9, 293.7), (174.6, 220, 261.6), (196, 246.9, 329.6)]
    roots = [110, 98, 87.3, 98]

    def put(snd, at):
        i = int(at * SR)
        if i >= n: return
        j = min(n, i + len(snd)); out[i:j] += snd[:j - i]

    bar = 4 * beat
    for b in range(int(dur / bar) + 1):
        ch, rt, a = chords[b % 4], roots[b % 4], b * bar
        m = (t >= a) & (t < a + bar); tt = t[m] - a                       # аккорд-подложка
        out[m] += sum(np.sin(2 * np.pi * f * tt) for f in ch) * .045 * np.minimum(1, tt / .3) * np.exp(-tt / 3)
        for k in range(8):                                                # бас на восьмые
            put(sine(rt, beat / 2 * .9, .18, .32, ((1, 1), (2, .3))), a + k * beat / 2)
        for k, step in enumerate([0, 1, 2, 1, 0, 2, 1, 2]):               # арпеджио-«плак»
            put(sine(ch[step] * 2, .22, .07, .12, ((1, 1), (2, .25), (3, .1))), a + k * beat / 2)
    k = 0
    while k * beat < dur:                                                 # бочка, хлопок на 2 и 4, хэт на половинки
        put(sine(55, .25, .12, .55), k * beat)
        if k % 2: put(rng.standard_normal(int(.12 * SR)) * env(int(.12 * SR), .001, .03) * .22, k * beat)
        put(rng.standard_normal(int(.04 * SR)) * env(int(.04 * SR), .001, .01) * .07, k * beat + beat / 2)
        k += 1
    return out


def load_voice(d, n):
    for name in ('voice.wav', 'voice.mp3'):
        p = os.path.join(d, name)
        if os.path.exists(p):
            raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', p, '-f', 's16le', '-ac', '1', '-ar', str(SR), '-'], capture_output=True, check=True).stdout
            v = np.frombuffer(raw, np.int16).astype(float) / 32768
            out = np.zeros(n); out[:min(n, len(v))] = v[:n]; return out
    return None


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
voice = load_voice(d, n)
if voice is not None:                                                    # под голосом музыка тише в ~3 раза
    lvl = np.convolve(np.abs(voice), np.ones(2400) / 2400, mode='same')
    duck = 1 - .65 * np.clip(lvl / .03, 0, 1); duck = np.convolve(duck, np.ones(9600) / 9600, mode='same')
    mix = voice * 1.6 + music * duck * .8 + fx * .45
else:
    mix = music + fx
mix = mix / max(1e-6, np.abs(mix).max()) * .9
pcm = (np.repeat(mix[:, None], 2, 1) * 32767).astype(np.int16)
with wave.open(os.path.join(d, 'mix.wav'), 'wb') as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())
print(f'Звук: {dur:.1f} с, звуков {len(cfg["cues"])}')
