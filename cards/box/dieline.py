#!/usr/bin/env python3
"""Развёртки фирменной коробки AMURA (самосборная почтовая, по типу FEFCO 0427) и почтового пакета.
python3 cards/box/dieline.py → cards/box/out/die_S.svg, die_M.svg, bag.svg (мм, 1:1) + превью PNG через render.js.
Внутренние размеры от Аналитика (03.11.2026): S 17×8×8, M 23×10×10 см; пакет для масок ~26×19 см.
Это схема для согласования дизайна: точный штамп (замки, клапаны, допуски под толщину картона) даёт типография —
наш дизайн кладётся на их штамп. Красная линия — рез, синий пунктир — сгиб, серая — вылет под обрез 3 мм. Дизайн — вариант B."""
import os
OUT = os.path.join(os.path.dirname(__file__), 'out'); os.makedirs(OUT, exist_ok=True)
GREEN, BRASS, LIGHT = '#0E3B2C', '#C9A86A', '#E9D7AE'
LOGO = ('<g fill="none" stroke="{c}" stroke-width="1.5"><path transform="translate(0 0.5)" d="M1 24L10 1L19 24"/><path transform="translate(31 0.5)" d="M1 24V1L11 17L21 1V24"/>'
        '<path transform="translate(64 0.5)" d="M1 1V15a8 8 0 0 0 16 0V1"/><path transform="translate(93 0.5)" d="M1 24V1H9a6.5 6.5 0 0 1 0 13H1M9 14L16 24"/><path transform="translate(121 0.5)" d="M1 24L10 1L19 24"/></g>')
T = 1.5   # толщина микрогофры E, мм (запас на сгибы)


def logo(cx, cy, w, rot=0, c=BRASS, sub=True):
    k = w / 141
    s = f'<g transform="translate({cx} {cy}) rotate({rot})"><g transform="translate({-w/2} {-12.75*k - (3 if sub else 0)}) scale({k})">{LOGO.format(c=c)}</g>'
    if sub: s += f'<text x="0" y="{14.5*k + 4}" text-anchor="middle" font-family="Inter" font-size="{w*.045:.2f}" letter-spacing="{w*.016:.2f}" fill="{c}">COSMETICS</text>'
    return s + '</g>'


def text(cx, cy, t, size, rot=0, c=BRASS, w=500):
    lines = t.split('\n')
    s = f'<g transform="translate({cx} {cy}) rotate({rot})">'
    for i, ln in enumerate(lines):
        s += f'<text x="0" y="{(i - (len(lines)-1)/2) * size*1.3 + size*.35:.2f}" text-anchor="middle" font-family="Inter" font-weight="{w}" font-size="{size}" fill="{c}">{ln}</text>'
    return s + '</g>'


def box(name, L, W, H):
    L, W, H = L + 2*T, W + 2*T, H + T          # наружные размеры панелей
    tuck, side_in, dust = H*.8, H - T, W*.45     # язык крышки, внутренняя часть боковины, пылевые клапаны
    x0 = side_in + H + 10                        # левый край основания
    rows = {'tuck': 10, 'lid': 10 + tuck, 'back': 10 + tuck + W, 'base': 10 + tuck + W + H, 'front': 10 + tuck + 2*W + H, 'fin': 10 + tuck + 2*W + 2*H}
    Wt, Ht = x0*2 + L, rows['fin'] + H - T + 10
    cut, fold, art = [], [], []
    R = lambda x, y, w, h: f'M{x} {y}h{w}v{h}h{-w}z'
    # фон печати с вылетом 3 мм — по всем наружным панелям
    panels = [(x0, rows['lid'], L, W), (x0, rows['back'], L, H), (x0, rows['base'], L, W), (x0, rows['front'], L, H),
              (x0 - H, rows['base'], H, W), (x0 + L, rows['base'], H, W), (x0, rows['tuck'], L, tuck)]
    bg = ''.join(f'<rect x="{x-3}" y="{y-3}" width="{w+6}" height="{h+6}" fill="{GREEN}"/>' for x, y, w, h in panels)
    # контур реза
    o = [(x0 + 6, rows['tuck']), (x0 + L - 6, rows['tuck']), (x0 + L, rows['tuck'] + 6), (x0 + L, rows['lid']),
         (x0 + L + H*.9, rows['lid'] + 4), (x0 + L + H*.9, rows['lid'] + W - 4), (x0 + L, rows['lid'] + W),
         (x0 + L, rows['back']), (x0 + L + dust, rows['back'] + 4), (x0 + L + dust, rows['back'] + H - 2), (x0 + L, rows['base']),
         (x0 + L + H, rows['base']), (x0 + L + H + side_in, rows['base'] + 6), (x0 + L + H + side_in, rows['base'] + W - 6), (x0 + L + H, rows['base'] + W),
         (x0 + L, rows['base'] + W), (x0 + L, rows['fin']), (x0 + L - 4, rows['fin'] + H - T), (x0 + 4, rows['fin'] + H - T), (x0, rows['fin']),
         (x0, rows['base'] + W), (x0 - H, rows['base'] + W), (x0 - H - side_in, rows['base'] + W - 6), (x0 - H - side_in, rows['base'] + 6), (x0 - H, rows['base']),
         (x0, rows['base']), (x0 - dust, rows['back'] + H - 2), (x0 - dust, rows['back'] + 4), (x0, rows['back']),
         (x0, rows['lid'] + W), (x0 - H*.9, rows['lid'] + W - 4), (x0 - H*.9, rows['lid'] + 4), (x0, rows['lid']), (x0, rows['tuck'] + 6)]
    cut.append('M' + ' L'.join(f'{x:.1f} {y:.1f}' for x, y in o) + 'Z')
    for y in (rows['lid'], rows['back'], rows['base'], rows['front'], rows['fin']):
        fold.append(f'M{x0} {y}h{L}')
    for x in (x0, x0 + L):
        fold += [f'M{x} {rows["lid"]}v{W}', f'M{x} {rows["back"]}v{H}', f'M{x} {rows["base"]}v{W}']
    fold += [f'M{x0 - H} {rows["base"]}v{W}', f'M{x0 + L + H} {rows["base"]}v{W}']
    # дизайн B: крышка — логотип и Cosmetics; лицевая (передняя стенка) — логотип; боковины — слоган
    art.append(logo(x0 + L/2, rows['lid'] + W/2, L*.62))
    art.append(f'<line x1="{x0 + L/2 - L*.1}" x2="{x0 + L/2 + L*.1}" y1="{rows["lid"] + W/2 + W*.22}" y2="{rows["lid"] + W/2 + W*.22}" stroke="{BRASS}" stroke-width=".4"/>')
    art.append(logo(x0 + L/2, rows['front'] + H/2, L*.42, rot=180))
    sl = 'Спасибо, что выбираете\nкорейский уход'
    art.append(text(x0 - H/2, rows['base'] + W/2, sl, min(H, W)*.075, rot=90))
    art.append(text(x0 + L + H/2, rows['base'] + W/2, sl, min(H, W)*.075, rot=-90))
    art.append(logo(x0 + L/2, rows['back'] + H/2, L*.28, sub=False, c=BRASS))
    lbl = f'{name}: внутр. {(L-2*T)/10:g}×{(W-2*T)/10:g}×{(H-T)/10:g} см · лист {Wt/10:.1f}×{Ht/10:.1f} см · микрогофра E, печать снаружи'
    cap = [('крышка', rows['lid'] + 4), ('задняя стенка', rows['back'] + 4), ('дно (внутри — без печати)', rows['base'] + 4), ('передняя стенка', rows['front'] + 4)]
    caps = ''.join(f'<text x="{x0 + 3}" y="{y + 3}" font-family="Inter" font-size="3" fill="#ffffff" opacity=".55">{t}</text>' for t, y in cap)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{Wt}mm" height="{Ht + 14}mm" viewBox="0 -14 {Wt} {Ht + 14}"><rect x="0" y="-14" width="{Wt}" height="{Ht + 14}" fill="#fff"/>'
           f'<text x="10" y="-5" font-family="Inter" font-weight="700" font-size="5" fill="#111">{lbl}</text>{bg}{"".join(art)}{caps}'
           f'<path d="{" ".join(cut)}" fill="none" stroke="#E5322D" stroke-width=".5"/><path d="{" ".join(fold)}" fill="none" stroke="#2D6BE5" stroke-width=".4" stroke-dasharray="3 2"/></svg>')
    open(os.path.join(OUT, f'die_{name}.svg'), 'w').write(svg)
    print(name, f'лист {Wt:.0f}×{Ht:.0f} мм')


def bag(Wb=190, Hb=260):
    """Почтовый пакет для тканевых масок ~26×19 см: лицевая и оборотная сторона рядом, клапан со скотчем сверху."""
    g, flap = 20, 40
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{2*Wb + 3*g}mm" height="{Hb + flap + 2*g + 14}mm" viewBox="0 -14 {2*Wb + 3*g} {Hb + flap + 2*g + 14}"><rect x="0" y="-14" width="{2*Wb+3*g}" height="{Hb+flap+2*g+14}" fill="#fff"/>'
    svg += f'<text x="{g}" y="-5" font-family="Inter" font-weight="700" font-size="6" fill="#111">Пакет для тканевых масок {Hb/10:g}×{Wb/10:g} см · лицевая и оборот · клапан со скотчем {flap/10:g} см</text>'
    fx, bx, y = g, 2*g + Wb, g + flap
    svg += f'<rect x="{fx}" y="{y}" width="{Wb}" height="{Hb}" fill="{GREEN}"/>' + logo(fx + Wb/2, y + Hb*.42, Wb*.62) + text(fx + Wb/2, y + Hb*.72, 'Спасибо, что выбираете\nкорейский уход', 7.5)
    svg += f'<rect x="{bx}" y="{g}" width="{Wb}" height="{flap}" fill="{GREEN}" opacity=".85"/><rect x="{bx + 10}" y="{g + 8}" width="{Wb - 20}" height="8" fill="#ddd" opacity=".7"/>'
    svg += f'<text x="{bx + Wb/2}" y="{g + 30}" text-anchor="middle" font-family="Inter" font-size="4" fill="{LIGHT}">клапан · скотч</text>'
    svg += f'<rect x="{bx}" y="{y}" width="{Wb}" height="{Hb}" fill="{GREEN}"/>'
    pat = ''.join(f'<path d="M{bx + 14 + i*25.5} {y + 26 + j*28}l7 -14l7 14" fill="none" stroke="{BRASS}" stroke-width=".6" opacity=".35"/>' for i in range(7) for j in range(9))
    svg += pat + f'<rect x="{bx + Wb/2 - 50}" y="{y + Hb/2 - 32}" width="100" height="64" rx="3" fill="#fff"/><text x="{bx + Wb/2}" y="{y + Hb/2 + 2}" text-anchor="middle" font-family="Inter" font-size="5" fill="#888">место для этикетки WB / Ozon</text>'
    svg += f'<line x1="{bx}" x2="{bx + Wb}" y1="{y}" y2="{y}" stroke="#2D6BE5" stroke-width=".4" stroke-dasharray="3 2"/></svg>'
    open(os.path.join(OUT, 'bag.svg'), 'w').write(svg)


box('S', 170, 80, 80)
box('M', 230, 100, 100)
bag()
