#!/usr/bin/env python3
"""Озвучка → слова с таймингами для ролика.

python3 motion/tools/align.py <папка ролика> <голос.mp3> <выравнивание.json> [--tempo 1.15] [--fix "Селимакс=Celimax"] [--join "ноль один три три ноль=01330"]

- выравнивание: JSON ElevenLabs/Virale с characters + character_start_times_seconds (или words от Scribe);
- --tempo ускоряет голос без изменения высоты (1.1–1.2 — живее для Reels), тайминги пересчитываются;
- --fix заменяет написание слова в субтитрах, --join склеивает несколько слов в одно (артикул цифрами).
Пишет <папка>/voice.wav и <папка>/words.js."""
import argparse
import json
import os
import re
import subprocess

ap = argparse.ArgumentParser()
ap.add_argument('dir'); ap.add_argument('voice'); ap.add_argument('alignment')
ap.add_argument('--tempo', type=float, default=1.0)
ap.add_argument('--fix', action='append', default=[]); ap.add_argument('--join', action='append', default=[])
a = ap.parse_args()

d = json.load(open(a.alignment))
words = []
if isinstance(d, dict) and 'characters' in d:
    cur, cs, pe = '', 0, 0
    for ch, s, e in zip(d['characters'], d['character_start_times_seconds'], d['character_end_times_seconds']):
        if ch.isspace():
            if cur: words.append([cur, cs, pe]); cur = ''
            continue
        if not cur: cs = s
        cur += ch; pe = e
    if cur: words.append([cur, cs, pe])
else:
    for w in (d['words'] if isinstance(d, dict) else d):
        if w.get('type', 'word') == 'word': words.append([w['text'], w['start'], w['end']])

words = [[w, s / a.tempo, e / a.tempo] for w, s, e in words if w not in ('—', '-', '–')]
fix = dict(f.split('=', 1) for f in a.fix)
for w in words:
    k = re.sub(r'[.,:;!?]+$', '', w[0])
    if k in fix: w[0] = fix[k] + w[0][len(k):]
norm = lambda s: re.sub(r'[^\wё-]', '', s.lower())
for j in a.join:
    src, dst = j.split('='); seq = [norm(x) for x in src.split()]
    i = 0
    while i <= len(words) - len(seq):
        if [norm(w[0]) for w in words[i:i + len(seq)]] == seq:
            tail = re.search(r'[.,:;!?]*$', words[i + len(seq) - 1][0]).group(0)
            words[i:i + len(seq)] = [[dst + tail, words[i][1], words[i + len(seq) - 1][2]]]
        i += 1
words = [[w, round(s, 3), round(e, 3)] for w, s, e in words]
open(os.path.join(a.dir, 'words.js'), 'w').write('window.WORDS=' + json.dumps(words, ensure_ascii=False) + ';\n')
subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', a.voice, '-af', f'atempo={a.tempo}', '-ar', '48000', os.path.join(a.dir, 'voice.wav')], check=True)
print(f'Слов: {len(words)}, длина голоса ≈ {words[-1][2]:.2f} с → words.js, voice.wav')
