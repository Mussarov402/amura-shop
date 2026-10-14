/* AMURA Motion — набор фишек и переходов.
   Всё строится на Web Animations: время задаётся в секундах от начала ролика, render.js перематывает
   анимации покадрово (seek), поэтому каждый кадр одинаковый при любом рендере.
   Подключение: <link rel="stylesheet" href="kit/brand.css"> + <script src="kit/kit.js"></script>. */
window.AM = (() => {
  const E = {
    out: 'cubic-bezier(.2,.8,.2,1)',     // мягкая остановка — по умолчанию
    back: 'cubic-bezier(.34,1.56,.64,1)', // с перелётом — «поп»
    snap: 'cubic-bezier(.7,0,.2,1)',      // резкий старт и мягкая посадка — «взлёт», «свайп»
    slam: 'cubic-bezier(.2,.9,.3,1)',     // «удар» словом
  };
  const $ = x => (typeof x === 'string' ? document.getElementById(x) : x);
  const ms = s => Math.max(1, s * 1000);
  const cues = [];                         // звуковые метки для render.js: [{t, kind}]
  const cue = (t, kind) => cues.push({ t: +t.toFixed(3), kind });

  /* базовое: анимация, окно видимости, создание элемента */
  const A = (el, kf, t, d, e = E.out, fill = 'both') => $(el).animate(kf, { delay: t * 1000, duration: ms(d), fill, easing: e });
  const win = (el, a, b) => { el = $(el); el.style.opacity = 0; return el.animate([{ opacity: 1 }, { opacity: 1 }], { delay: a * 1000, duration: ms(b - a), fill: 'none' }); };
  const make = (html, parent) => { const t = document.createElement('template'); t.innerHTML = html.trim(); const n = t.content.firstChild; if (parent) $(parent).appendChild(n); return n; };

  /* сцена кадра: фон, бренд, слои вспышки/шторки/субтитров/прогресса */
  let world;
  function stage({ brand = 'AMURA' } = {}) {
    world = make('<div id="am-world" class="ab" style="inset:0"></div>', document.body);
    make('<div id="am-bg"></div>', world); make('<div id="am-dots"></div>', world); make('<div id="am-grain"></div>', world);
    ['am-flash', 'am-wipe'].forEach(id => make(`<div id="${id}"></div>`, document.body));
    if (brand) make(`<div id="am-brand">${brand}</div>`, document.body);   // над сценами и шторкой
    make('<div id="am-subs"></div>', document.body); make('<div id="am-bar"></div>', document.body);
    return world;
  }
  function background(end) {
    A('am-bg', [{ transform: 'scale(1) rotate(0deg)' }, { transform: 'scale(1.15) rotate(4deg)' }], 0, end, 'linear');
    A('am-bar', [{ transform: 'scaleX(0)' }, { transform: 'scaleX(1)' }], 0, end, 'linear');
  }
  /* «Поры»: сетка точек наезжает — фон для зацепки */
  const dots = (a, b) => A('am-dots', [{ opacity: 0, transform: 'scale(1)' }, { opacity: 1, transform: 'scale(1.25)', offset: .15 }, { opacity: 1, transform: 'scale(1.6)', offset: .85 }, { opacity: 0, transform: 'scale(1.8)' }], a, b - a, 'linear', 'none');

  /* ---------- ФИШКИ ---------- */
  const flash = (t, o = .45) => { A('am-flash', [{ opacity: o }, { opacity: 0 }], t, .35, 'ease-out', 'none'); };
  const shake = (t, f = 1) => { cue(t, 'hit'); A(world, [{ transform: 'translate(0,0)' }, { transform: `translate(${-14 * f}px,${8 * f}px)` }, { transform: `translate(${12 * f}px,${-10 * f}px)` }, { transform: `translate(${-8 * f}px,${-6 * f}px)` }, { transform: 'translate(0,0)' }], t, .28, 'linear', 'none'); };
  const M = {
    /* «Удар»: слово влетает из крупного плана с размытием, тряска + вспышка */
    slam(el, t, { d = .35, hit = true } = {}) { A(el, [{ opacity: 0, transform: 'scale(2.4)', filter: 'blur(18px)' }, { opacity: 1, transform: 'scale(1)', filter: 'blur(0px)' }], t, d, E.slam); if (hit) { shake(t + .08); flash(t, .3); } },
    /* «Удар» с окном: слово держится до b и растворяется (для серии вопросов-зацепок) */
    slamWin(el, a, b) {
      el = $(el); el.style.opacity = 0; const d = b - a;
      el.animate([{ opacity: 0, transform: 'scale(2.4)', filter: 'blur(18px)' }, { opacity: 1, transform: 'scale(1)', filter: 'blur(0px)', offset: Math.min(.3, .3 / d) }, { opacity: 1, transform: 'scale(1.06)', offset: .9 }, { opacity: 0, transform: 'scale(1.1)' }], { delay: a * 1000, duration: ms(d), fill: 'none', easing: 'linear' });
      shake(a + .08); flash(a, .3);
    },
    /* «Взлёт»: товар вылетает снизу с перелётом */
    rise(el, t, d = .7) { cue(t, 'whoosh'); A(el, [{ opacity: 0, transform: 'translateY(900px) scale(.8)' }, { opacity: 1, transform: 'translateY(-30px) scale(1.02)', offset: .75 }, { opacity: 1, transform: 'none' }], t, d, E.snap); },
    /* «Влёт сбоку»: from = -1 слева, 1 справа */
    slide(el, t, from = 1, d = .5) { cue(t, 'swish'); A(el, [{ opacity: 0, transform: `translateX(${from * 200}px)` }, { opacity: 1, transform: 'none' }], t, d, E.back); },
    up(el, t, d = .5, dy = 60) { A(el, [{ opacity: 0, transform: `translateY(${dy}px)` }, { opacity: 1, transform: 'none' }], t, d); },
    /* «Поп»: плашка/чип выпрыгивает */
    pop(el, t, d = .5) { cue(t, 'pop'); A(el, [{ opacity: 0, transform: 'scale(.5)' }, { opacity: 1, transform: 'scale(1)' }], t, d, E.back); },
    fade(el, t, d = .4) { A(el, [{ opacity: 0 }, { opacity: 1 }], t, d); },
    /* «Парение»: товар дышит, пока на экране (свойство translate — не мешает transform) */
    float(el, a, b, amp = 16) { A(el, [{ translate: '0 0' }, { translate: `0 ${-amp}px` }, { translate: '0 0' }], a, b - a, 'ease-in-out', 'none'); },
    sway(el, a, b, deg = 2) { A(el, [{ rotate: '0deg' }, { rotate: `${-deg}deg` }, { rotate: `${deg}deg` }, { rotate: '0deg' }], a, b - a, 'ease-in-out', 'none'); },
    /* «Свечение» за товаром и «Кольцо»-вспышка */
    glow(el, t, d = .6) { A(el, [{ opacity: 0, transform: 'scale(.3)' }, { opacity: 1, transform: 'scale(1)' }], t, d); },
    ring(el, t, d = .9) { A(el, [{ opacity: 1, transform: 'scale(.4)' }, { opacity: 0, transform: 'scale(2.2)' }], t, d, 'ease-out'); },
    /* «Блик»: полоса света пробегает по товару (маска = картинка товара) */
    sweep(product, t, d = .8) {
      product = $(product); const img = getComputedStyle(product).backgroundImage;
      const m = make(`<div class="ab" style="left:${product.style.left};top:${product.style.top};width:${product.style.width};height:${product.style.height};overflow:hidden;-webkit-mask:${img} 0 0/100% 100%;mask:${img} 0 0/100% 100%"><div style="position:absolute;left:-200px;top:-200px;width:120px;height:2400px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.85),transparent);transform:rotate(18deg)"></div></div>`);
      product.after(m); cue(t, 'shine');
      A(m.firstChild, [{ translate: '0 0' }, { translate: '700px 0' }], t, d, 'ease-in-out');
      return m;
    },
    /* «Счётчик»: целое число тикает от 0 (элемент с классом am-count) */
    count(el, t, to, d = .8) { el = $(el); el.classList.add('am-count'); A(el, [{ '--am-n': 0 }, { '--am-n': to }], t, d, E.out); },
    /* «Набор»: текст печатается по символу (поиск, артикул) */
    type(el, text, t, step = .12, sound = true) {
      el = $(el); el.textContent = '';
      [...text].forEach((ch, k) => { const s = make(`<span style="display:inline-block;overflow:hidden;vertical-align:bottom;white-space:pre">${ch}</span>`, el); A(s, [{ opacity: 0, maxWidth: '0px' }, { opacity: 1, maxWidth: '60px' }], t + k * step, .01, 'linear'); if (sound) cue(t + k * step, 'tick'); });
    },
    /* «Цифры по голосу»: каждый символ появляется в свой момент (артикул под озвучку) */
    digits(el, text, times) { el = $(el); el.textContent = ''; [...text].forEach((ch, k) => { const s = make(`<span style="display:inline-block">${ch}</span>`, el); A(s, [{ opacity: 0, transform: 'translateY(40px)' }, { opacity: 1, transform: 'none' }], times[k], .25, E.back); cue(times[k], 'tick'); }); },
    caret(el, a, b) { A(el, [{ opacity: 1 }, { opacity: 0 }, { opacity: 1 }], a, Math.min(.6, b - a), 'steps(2,end)', 'none'); },
    /* «Зачёркивание» (красное) и «Маркер» (салатовая подложка под словом) */
    strike(el, t, d = .4) { A(el, [{ transform: 'scaleX(0)' }, { transform: 'scaleX(1)' }], t, d); },
    marker(el, t, d = .4) { el = $(el); el.style.backgroundImage = 'linear-gradient(transparent 58%,rgba(215,234,74,.55) 58%)'; el.style.backgroundRepeat = 'no-repeat'; A(el, [{ backgroundSize: '0% 100%' }, { backgroundSize: '100% 100%' }], t, d); },
    /* «Капля»: капля падает и расходится лужицей с кольцами */
    drop(parent, t, x = 540, y = 680) {
      const d = make(`<div class="ab" style="left:${x - 50}px;top:${y - 380}px;width:100px;height:140px;border-radius:50% 50% 50% 50%/60% 60% 40% 40%;background:radial-gradient(circle at 35% 30%,#fff,#E4F28C 45%,#B9CC2E);box-shadow:0 0 50px rgba(215,234,74,.6)"></div>`, parent);
      const p = make(`<div class="ab" style="left:${x - 200}px;top:${y - 65}px;width:400px;height:130px;border-radius:50%;background:radial-gradient(closest-side,rgba(228,242,140,.9),rgba(215,234,74,.35) 60%,rgba(215,234,74,0))"></div>`, parent);
      const r1 = make(`<div class="am-ring" style="left:${x - 150}px;top:${y - 55}px;width:300px;height:110px"></div>`, parent);
      const r2 = make(`<div class="am-ring" style="left:${x - 250}px;top:${y - 95}px;width:500px;height:190px;border-width:4px"></div>`, parent);
      A(d, [{ opacity: 0, transform: 'translateY(-200px) scaleY(1.2)' }, { opacity: 1, transform: 'translateY(250px) scaleY(1.1)', offset: .85 }, { opacity: 0, transform: 'translateY(300px) scale(1.6,.2)' }], t, .9, 'ease-in');
      cue(t + .85, 'drop');
      A(p, [{ opacity: 0, transform: 'scale(.2)' }, { opacity: 1, transform: 'scale(1)' }], t + .85, .6, E.back);
      A(r1, [{ opacity: 1, transform: 'scale(.2)' }, { opacity: 0, transform: 'scale(1.6)' }], t + .85, .8, 'ease-out');
      A(r2, [{ opacity: .8, transform: 'scale(.2)' }, { opacity: 0, transform: 'scale(1.4)' }], t + 1.0, 1.0, 'ease-out');
    },
    /* «Пульс»: кнопка/призыв привлекает внимание */
    pulse(el, t, d = .8) { A(el, [{ transform: 'scale(1)' }, { transform: 'scale(1.06)' }, { transform: 'scale(1)' }], t, d, 'ease-in-out', 'none'); },
    shake, flash,
  };

  /* ---------- ПЕРЕХОДЫ между сценами ----------
     zoom  — «Зум-вспышка»: новая сцена из лёгкого приближения + вспышка (по умолчанию)
     whip  — «Свайп»: сцены проносятся вбок с размытием
     wipe  — «Шторка»: салатовая диагональ закрывает экран, за ней — новая сцена
     iris  — «Ирис»: новая сцена раскрывается кругом из точки (x, y)
     cut   — склейка без эффекта */
  const TIN = { zoom: .3, whip: .35, wipe: .01, iris: .55, cut: .01 };
  const TOUT = { zoom: .25, whip: .35, wipe: .01, iris: .55, cut: .01 };
  const FULL = { opacity: 1, transform: 'none', filter: 'blur(0px)', clipPath: 'circle(2400px at 540px 960px)' };
  function scenes(list, end) {
    list.forEach((s, i) => {
      const nx = list[i + 1], last = !nx, a = s.a, b = last ? end : nx.a;
      const kin = i ? (s.in || 'zoom') : 'cut', kout = last ? null : (nx.in || 'zoom');
      const x = s.x ?? 540, y = s.y ?? 960, full = { ...FULL, clipPath: `circle(2400px at ${x}px ${y}px)` };
      const in0 = {
        zoom: { ...full, opacity: 0, transform: 'scale(1.08)' },
        whip: { ...full, transform: 'translateX(1080px)', filter: 'blur(24px)' },
        iris: { ...full, clipPath: `circle(0px at ${x}px ${y}px)` },
        wipe: full, cut: full,
      }[kin];
      const out1 = kout ? { zoom: { ...full, opacity: 0, transform: 'scale(.94)' }, whip: { ...full, transform: 'translateX(-1080px)', filter: 'blur(24px)' }, iris: full, wipe: full, cut: full }[kout] : full;
      if (kin === 'iris') $(s.id).style.background = 'radial-gradient(60% 40% at 50% 45%, #13503B 0%, #0B3326 45%, #061A13 100%)'; // круг открывает непрозрачную сцену
      const tin = TIN[kin], tout = kout ? TOUT[kout] : 0, start = a, dur = (b + tout) - start;
      const kf = [{ ...in0, offset: 0 }, { ...full, offset: Math.min(.45, tin / dur) }, { ...full, offset: Math.max(.55, (b - start) / dur) }, { ...out1, offset: 1 }];
      $(s.id).animate(kf, { delay: start * 1000, duration: ms(dur), fill: last ? 'forwards' : 'none', easing: 'linear' });
      if (i) {
        if (kin === 'zoom') { flash(a, .35); cue(a, 'whoosh'); }
        if (kin === 'whip') cue(a, 'whoosh');
        if (kin === 'iris') cue(a, 'shine');
        if (kin === 'wipe') { A('am-wipe', [{ transform: 'translateX(-2400px) rotate(12deg)' }, { transform: 'translateX(0) rotate(12deg)', offset: .5 }, { transform: 'translateX(2400px) rotate(12deg)' }], a - .25, .55, 'cubic-bezier(.6,0,.4,1)', 'none'); cue(a - .25, 'whoosh'); }
      }
    });
  }

  /* ---------- СЛОВА и СУБТИТРЫ ---------- */
  let W = [];
  const norm = s => s.toLowerCase().replace(/[«»"“”.,:;!?—–()]/g, '').replace(/ё/g, 'е');
  /* время: число, '@слово', '@слово+0.2', '@слово>' (конец слова). cursor — искать начиная с этого времени */
  function at(spec, cursor = 0) {
    if (typeof spec === 'number') return spec;
    const m = /^@(.+?)(>)?([+-][\d.]+)?$/.exec(spec); if (!m) return +spec;
    const key = norm(m[1]), w = W.find(w => w[1] >= cursor - .01 && norm(w[0]).startsWith(key));
    if (!w) throw new Error('нет слова в озвучке: ' + spec);
    return (m[2] ? w[2] : w[1]) + (m[3] ? +m[3] : 0);
  }
  /* «Караоке»: фраза по 2–4 слова, текущее слово — салатовым */
  function karaoke(words, { top = 1440 } = {}) {
    const ch = []; let cur = [];
    words.forEach((w, i) => { cur.push(w); if ((/[.,:?!]$/.test(w[0]) && cur.length >= 2) || cur.length >= 4 || i === words.length - 1) { ch.push(cur); cur = []; } });
    ch.forEach((c, k) => {
      const d = make(`<div class="am-sub" style="top:${top}px"></div>`, 'am-subs');
      c.forEach((w, j) => { const s = make('<span></span>', d); s.textContent = (j ? ' ' : '') + w[0]; s.animate([{ color: '#D7EA4A' }, { color: '#D7EA4A' }], { delay: w[1] * 1000, duration: ms(w[2] - w[1]), fill: 'none' }); });
      const a = c[0][1] - .05, b = ch[k + 1] ? Math.min(ch[k + 1][0][1] - .05, c[c.length - 1][2] + .5) : c[c.length - 1][2] + .8;
      d.animate([{ opacity: 0, transform: 'translateY(20px) scale(.95)' }, { opacity: 1, transform: 'none', offset: Math.min(.5, .12 / (b - a)) }, { opacity: 1, transform: 'none' }], { delay: a * 1000, duration: ms(b - a), fill: 'none' });
    });
  }

  if (window.CSS && CSS.registerProperty) { try { CSS.registerProperty({ name: '--am-n', syntax: '<integer>', inherits: false, initialValue: '0' }); } catch (e) { } }
  const st = document.createElement('style'); st.textContent = '.am-count{counter-reset:am-n var(--am-n)}.am-count::before{content:counter(am-n)}'; document.head.appendChild(st);

  window.seek = msec => document.getAnimations().forEach(a => { a.pause(); a.currentTime = msec; });
  return { E, $, A, win, make, stage, background, dots, scenes, karaoke, at, cues, cue, ...M, setWords: w => { W = w; }, words: () => W };
})();
