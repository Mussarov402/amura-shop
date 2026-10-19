#!/usr/bin/env python3
"""Монтаж ASMR-ролика ухода (формат @jasminlily_): короткие планы под бит + ASMR-звуки по меткам + музыка + финал с артикулами.

python3 motion/asmr/edit.py <папка-ролика>            → <папка>/out.mp4
python3 motion/asmr/edit.py <папка-ролика> --test      → сначала делает пробные клипы и звуки (проверка шаблона без съёмки)

В папке ролика: edit.json (описание, в git), исходники с телефона, music.mp3 и sfx/*.wav (медиа, в git не кладём).
edit.json:
  bpm         — темп музыки; длина плана = beats × 60 / bpm (планы режутся точно по долям)
  music       — файл музыки (lo-fi/джаз без слов, с правом использования); music_start — с какой секунды брать
  items       — [["Название", "артикул"], ...] для финальной карточки (2 с). Цену не показываем.
  shots       — по порядку: {"clip": файл, "in": с какой секунды клипа, "beats": долей,
                 "audio": громкость живого звука клипа (0 — выкл, по умолчанию 1),
                 "sfx": [{"file": "sfx/pump.wav", "at": секунда от начала плана, "vol": 1}]}
Картинка: кадрирование в 1080×1920, 30 к/с, мягкая цветокоррекция (тёплая, чуть мягче контраст, лёгкое «сияние»)."""
import json
import os
import subprocess
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
FPS, W, H = 30, 1080, 1920
GRADE = ('scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={F},'
         'eq=contrast=0.97:brightness=0.015:saturation=1.06:gamma=1.02,'
         'colorbalance=rs=0.03:gs=0.01:bs=-0.03:rh=0.02:bh=-0.02,'
         'split[a][b];[b]gblur=sigma=18[g];[a][g]blend=all_mode=screen:all_opacity=0.12,'
         'unsharp=5:5:0.4,format=yuv420p').format(W=W, H=H, F=FPS)


def run(*a):
    subprocess.run(a, check=True)


def endcard(d, items):
    """PNG финальной карточки через Chromium (те же шрифты, что в motion/kit)."""
    from urllib.parse import quote
    png = os.path.join(d, 'endcard.png')
    url = 'file://' + os.path.join(KIT, 'endcard.html') + '?items=' + quote(';'.join(f'{n}|{a}' for n, a in items))
    js = ("let c;try{({chromium:c}=require('playwright'))}catch(e){({chromium:c}=require('/opt/node22/lib/node_modules/playwright'))}"
          "(async()=>{const b=await c.launch({args:['--no-sandbox']});const p=await b.newPage({viewport:{width:1080,height:1920}});"
          f"await p.goto({json.dumps(url)});await p.evaluate(()=>document.fonts.ready);await p.waitForTimeout(300);"
          f"await p.screenshot({{path:{json.dumps(png)}}});await b.close()}})()")
    run('node', '-e', js)
    return png


def make_test(d, cfg):
    """Пробные материалы: цветные клипы с таймером и щелчки вместо ASMR — чтобы проверить шаблон без съёмки."""
    os.makedirs(os.path.join(d, 'sfx'), exist_ok=True)
    cols = ['#E8CFC0', '#D9C2A8', '#CFE0E6', '#E6D9EE', '#F0E2C8', '#D8E8D2']
    for i, s in enumerate(cfg['shots']):
        p = os.path.join(d, s['clip'])
        if not os.path.exists(p):
            run('ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color=c={cols[i % len(cols)]}:s=720x1280:r=30:d=6',
                '-f', 'lavfi', '-i', 'anoisesrc=a=0.02:c=pink:d=6', '-vf', f"drawtext=text='{s['clip']} %{{pts\\:hms}}':fontsize=40:x=40:y=60:fontcolor=black",
                '-shortest', '-c:v', 'libx264', '-c:a', 'aac', p)
        for x in s.get('sfx', []):
            p = os.path.join(d, x['file'])
            if not os.path.exists(p):
                run('ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=f=1800:d=0.06', '-af', 'afade=out:st=0.01:d=0.05', p)
    m = os.path.join(d, cfg['music'])
    if not os.path.exists(m):
        run('ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f"aevalsrc='0.25*sin(2*PI*220*t)*exp(-6*mod(t,{60 / cfg['bpm']}))':d=60", m)


def main():
    d = os.path.abspath(sys.argv[1]); cfg = json.load(open(os.path.join(d, 'edit.json')))
    if '--test' in sys.argv: make_test(d, cfg)
    beat = 60 / cfg['bpm']; tmp = os.path.join(d, 'tmp'); os.makedirs(tmp, exist_ok=True)
    segs, aud, t = [], [], 0.0                       # aud: (файл, с какой секунды, задержка, длина, громкость)
    for i, s in enumerate(cfg['shots']):
        dur = round(s.get('beats', 2) * beat * FPS) / FPS
        src = os.path.join(d, s['clip']); seg = os.path.join(tmp, f'{i:02d}.mp4')
        run('ffmpeg', '-v', 'error', '-y', '-ss', str(s.get('in', 0)), '-t', f'{dur:.3f}', '-i', src,
            '-filter_complex', f'[0:v]{GRADE}', '-an', '-c:v', 'libx264', '-preset', 'fast', '-crf', '17', seg)
        segs.append(seg)
        if s.get('audio', 1): aud.append((src, s.get('in', 0), t, dur, s.get('audio', 1)))
        for x in s.get('sfx', []): aud.append((os.path.join(d, x['file']), 0, t + x['at'], None, x.get('vol', 1)))
        t += dur
    end = os.path.join(tmp, 'end.mp4')               # финал 2 с: лёгкий наезд на карточку с артикулами
    run('ffmpeg', '-v', 'error', '-y', '-loop', '1', '-t', '2', '-i', endcard(d, cfg['items']),
        '-vf', f"scale={W * 2}:{H * 2},zoompan=z='1+0.03*on/60':d=60:s={W}x{H}:fps={FPS},fade=in:d=0.25,format=yuv420p",
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '17', end)
    segs.append(end); total = t + 2
    lst = os.path.join(tmp, 'list.txt'); open(lst, 'w').write(''.join(f"file '{p}'\n" for p in segs))
    video = os.path.join(tmp, 'video.mp4'); run('ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', lst, '-c', 'copy', video)

    ins, fl = ['-i', video, '-ss', str(cfg.get('music_start', 0)), '-i', os.path.join(d, cfg['music'])], []
    fl.append(f"[1:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,volume={cfg.get('music_vol', 0.35)},afade=in:d=0.3,afade=out:st={total - 1.2:.3f}:d=1.2[m]")
    for k, (f, ss, dl, du, v) in enumerate(aud):
        ins += ['-ss', str(ss)] + (['-t', f'{du:.3f}'] if du else []) + ['-i', f]
        fl.append(f'[{k + 2}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={v},adelay={int(dl * 1000)}:all=1[s{k}]')
    fl.append('[m]' + ''.join(f'[s{k}]' for k in range(len(aud))) + f'amix=inputs={len(aud) + 1}:normalize=0:duration=first,loudnorm=I=-14:TP=-1.5[a]')
    out = os.path.join(d, 'out.mp4')
    run('ffmpeg', '-v', 'error', '-y', *ins, '-filter_complex', ';'.join(fl), '-map', '0:v', '-map', '[a]',
        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-t', f'{total:.3f}', '-movflags', '+faststart', out)
    print(f'Готово: {out} — {total:.1f} с, планов {len(cfg["shots"])}')


main()
