#!/usr/bin/env python3
"""Звук ролика AMURA Motion: голос + фирменный бит + звуки фишек по меткам (cues.json от render.js).

python3 motion/tools/mix.py motion/reels/<папка>  → <папка>/mix.wav

Звуки синтезируются здесь же (numpy), без сторонних файлов — у всех роликов одинаковые:
  hit — глухой удар (тряска, «удар» словом)     whoosh — порыв воздуха (переходы, «взлёт»)
  swish — короткий свист («влёт сбоку»)          pop — «поп» (чипы, плашки)
  tick — щелчок (набор текста, цифры)            shine — звон-блик («блик», «ирис»)
  drop — «бульк» (капля)
Бит: 120 BPM, мягкая бочка + аккорд-подложка; под голосом бит приглушается."""
import json
import os
import subprocess
import sys
import wave

import numpy as np

SR = 48000
rng = np.random.default_rng(7)


def env(n, a=.005, r=.2):
    t = np.arange(n) / SR
    return np.minimum(1, t / a) * np.exp(-t / r)


def tone(f0, f1, d, r, vol=1.0):
    n = int(d * SR); t = np.arange(n) / SR
    f = f0 * (f1 / f0) ** (t / d)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, .002, r) * vol


def noise(d, lo, hi, vol, shape='swell'):
    n = int(d * SR); x = rng.standard_normal(n)
    X = np.fft.rfft(x); fr = np.fft.rfftfreq(n, 1 / SR); X[(fr < lo) | (fr > hi)] = 0; x = np.fft.irfft(X, n)
    x /= np.abs(x).max() + 1e-9
    t = np.linspace(0, 1, n)
    e = np.sin(np.pi * t) ** 2 if shape == 'swell' else np.exp(-t * 8)
    return x * e * vol


def lay(*xs):
    out = np.zeros(max(len(x) for x in xs))
    for x in xs: out[:len(x)] += x
    return out


SFX = {
    'hit': lambda: lay(tone(120, 45, .35, .12, .9), noise(.05, 1500, 8000, .15, 'dec')),
    'whoosh': lambda: noise(.45, 400, 6000, .35),
    'swish': lambda: noise(.22, 2000, 9000, .22),
    'pop': lambda: tone(500, 1100, .09, .03, .35),
    'tick': lambda: tone(2200, 1800, .03, .008, .25),
    'shine': lambda: lay(tone(2400, 2400, .6, .25, .12), tone(3600, 3600, .6, .2, .08)),
    'drop': lambda: tone(900, 260, .18, .06, .45),
}


def bed(dur):
    n = int(dur * SR); t = np.arange(n) / SR; x = np.zeros(n)
    beat = .5
    for k in range(int(dur / beat) + 1):           # бочка
        s = tone(110, 45, .3, .09, .55); i = int(k * beat * SR); x[i:i + len(s)] += s[:max(0, n - i)]
    for f, v in ((110, .07), (164.81, .05), (220, .04), (329.63, .025)):   # подложка A-минор, «дышит»
        x += np.sin(2 * np.pi * f * t) * v * (.6 + .4 * np.sin(2 * np.pi * .25 * t))
    fade = np.minimum(1, np.minimum(t / 1.0, (dur - t) / 2.0)).clip(0, 1)
    return x * fade


def load_voice(d, dur):
    for name in ('voice.wav', 'voice.mp3', 'voice.m4a'):
        p = os.path.join(d, name)
        if os.path.exists(p):
            raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', p, '-f', 's16le', '-ac', '1', '-ar', str(SR), '-'], capture_output=True, check=True).stdout
            v = np.frombuffer(raw, np.int16).astype(np.float64) / 32768
            out = np.zeros(int(dur * SR)); out[:min(len(v), len(out))] = v[:len(out)]
            return out
    print('Голоса нет (voice.wav / voice.mp3) — только музыка и звуки')
    return np.zeros(int(dur * SR))


def main(d):
    c = json.load(open(os.path.join(d, 'cues.json')))
    dur = c['end']; n = int(dur * SR)
    voice = load_voice(d, dur)
    fx = np.zeros(n)
    last = {}
    for q in sorted(c['cues'], key=lambda q: q['t']):
        k = q['kind']
        if k not in SFX or q['t'] - last.get(k, -9) < .07:   # не накладывать одинаковые звуки чаще 70 мс
            continue
        last[k] = q['t']; s = SFX[k](); i = int(max(0, q['t']) * SR)
        fx[i:i + len(s)] += s[:max(0, n - i)]
    # ducking: где говорит голос, бит тише в 2,5 раза
    lvl = np.convolve(np.abs(voice), np.ones(2400) / 2400, mode='same')
    duck = 1 - .6 * np.clip(lvl / .04, 0, 1)
    duck = np.convolve(duck, np.ones(4800) / 4800, mode='same')
    mix = voice * 1.0 + bed(dur) * .55 * duck + fx * .5
    mix /= max(1, np.abs(mix).max() / .95)
    pcm = (np.repeat(mix[:, None], 2, axis=1) * 32767).astype(np.int16)
    with wave.open(os.path.join(d, 'mix.wav'), 'wb') as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())
    print(f'Звук: {dur:.2f} с, звуков фишек {len(c["cues"])}')


if __name__ == '__main__':
    main(sys.argv[1])
