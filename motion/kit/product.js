/* Шаблон товарного ролика AMURA: сцены собираются из описания REEL (data.js) и слов озвучки (words.js).
   Время любой сцены/элемента — число секунд или '@слово' (момент, когда голос произносит слово).
   Типы сцен: hook, reveal, compare, facts, texture, statement, cta. Цену в роликах не показываем. */
AM.productReel = function (R) {
  const { make, at } = AM;
  AM.setWords(window.WORDS || []);
  const world = AM.stage({ brand: R.brand ?? 'AMURA' });
  const img = R.product.img, ar = R.product.ratio;          // ratio = ширина / высота картинки товара
  const P = (h, l, t, extra = '') => make(`<div class="am-product" style="left:${l}px;top:${t}px;width:${Math.round(h * ar)}px;height:${h}px;background-image:url(${img});${extra}"></div>`);
  const words = AM.words(), lastWord = words.length ? words[words.length - 1][2] : 0;
  const END = R.end ?? lastWord + 1.6;

  let cursor = 0;
  const T = s => (s === undefined ? undefined : at(s, cursor));

  function chips(el, arr, x, y, gap = 110, solid = false) {
    (arr || []).forEach((c, k) => { const n = make(`<div class="ab" style="left:${x}px;top:${y + k * gap}px"><span class="am-chip${solid || c.solid ? ' solid' : ''}">${c.text}</span></div>`, el); AM.pop(n, T(c.at)); });
  }

  const BUILD = {
    /* Зацепка: 1–3 вопроса «ударом» по голосу, фон — «поры» */
    hook(el, s, a, b) {
      const end = b(); AM.dots(a, end + .2);
      s.lines.forEach((l, k) => {
        const t0 = T(l.at), t1 = s.lines[k + 1] ? at(s.lines[k + 1].at, t0) : end;
        const n = make(`<div class="am-big" style="top:${l.top ?? 660}px;${l.size ? 'font-size:' + l.size + 'px' : ''}">${l.html}</div>`, el);
        AM.slamWin(n, t0, t1);
      });
    },
    /* Появление товара: «взлёт» + свечение + кольцо + блик, подпись-чип */
    reveal(el, s, a, b) {
      const h = s.height ?? 1006, l = Math.round(540 - h * ar / 2), top = s.top ?? 330;
      const g = make(`<div class="am-glow" style="left:140px;top:${top + 90}px;width:800px;height:800px"></div>`, el);
      const r = make(`<div class="am-ring" style="left:290px;top:${top + 240}px;width:500px;height:500px"></div>`, el);
      const p = P(h, l, top); el.appendChild(p);
      AM.rise(p, a); AM.sway(p, a + .7, b()); AM.glow(g, a + .35); AM.ring(r, a + .45); AM.sweep(p, a + .9);
      if (s.caption) { const c = make(`<div class="ab" style="left:0;right:0;top:${top + h + 25}px;text-align:center"><span class="am-chip">${s.caption.text}</span></div>`, el); AM.up(c, T(s.caption.at), .5, 40); }
    },
    /* Сравнение «было → стало»: тусклая карточка, стрелка, яркая карточка, чипы-выводы */
    compare(el, s, a, b) {
      const f = make(`<div class="am-card" style="left:90px;top:780px;width:400px;height:260px;display:flex;flex-direction:column;align-items:center;justify-content:center"><div style="font:700 30px Manrope;opacity:.6">${s.from.label}</div><div style="font:800 56px Unbounded;opacity:.75">${s.from.title}</div></div>`, el);
      const arr = make(`<svg class="ab" style="left:470px;top:820px" width="140" height="180" viewBox="0 0 140 180"><path d="M20 160 C 60 150, 90 110, 110 30" fill="none" stroke="#D7EA4A" stroke-width="12" stroke-linecap="round" stroke-dasharray="220" stroke-dashoffset="220"/><path d="M84 40 L112 22 L124 56" fill="none" stroke="#D7EA4A" stroke-width="12" stroke-linecap="round" stroke-linejoin="round" opacity="0"/></svg>`, el);
      const to = make(`<div class="am-card solid" style="left:590px;top:560px;width:420px;height:320px;display:flex;flex-direction:column;align-items:center;justify-content:center"><div style="font:800 30px Manrope">${s.to.label}</div><div style="font:800 60px Unbounded">${s.to.title}</div></div>`, el);
      const tf = T(s.from.at), tt = T(s.to.at);
      AM.slide(f, tf, -1); AM.A(arr.firstChild, [{ strokeDashoffset: 220 }, { strokeDashoffset: 0 }], tf + .4, .5); AM.fade(arr.lastChild, tf + .85, .2);
      AM.A(to, [{ opacity: 0, transform: 'translateY(120px) scale(.7)' }, { opacity: 1, transform: 'none' }], tt, .55, AM.E.back); AM.flash(tt, .3); AM.cue(tt, 'pop');
      chips(el, s.chips, 600, 930);
    },
    /* Факты/состав: товар слева, карточки с процентами справа, чипы-эффекты */
    facts(el, s, a, b) {
      make('<div class="am-glow" style="left:-120px;top:420px;width:700px;height:900px"></div>', el);
      const p = P(1059, 90, 330); el.appendChild(p);
      AM.A(p, [{ opacity: 0, transform: 'translateX(-300px) rotate(-8deg)' }, { opacity: 1, transform: 'none' }], a, .6, AM.E.snap); AM.float(p, a + .6, b());
      s.cards.forEach((c, k) => { const n = make(`<div class="am-card" style="left:390px;top:${380 + k * 230}px;width:620px;height:200px;padding:28px 36px"><div style="font:700 30px Manrope;opacity:.7">${c.label}</div><div style="font:800 76px Unbounded" class="lime">${c.value}</div></div>`, el); AM.slide(n, T(c.at), 1, .45); AM.flash(T(c.at), .15); });
      const y = 380 + s.cards.length * 230 + 20;
      (s.chips || []).forEach((c, k) => { const n = make(`<div class="ab" style="left:${390 + k * 300}px;top:${y}px"><span class="am-chip solid" style="font-size:42px">${c.text}</span></div>`, el); AM.pop(n, T(c.at)); });
    },
    /* Текстура и способ: капля, шкала «лёгкое ↔ плотное», шаги ухода */
    texture(el, s, a, b) {
      AM.drop(el, a + .1);
      if (s.scale) {
        const sc = make(`<div class="ab" style="left:90px;top:860px;width:900px"><div style="display:flex;justify-content:space-between;font:800 34px Manrope;opacity:.85"><span>${s.scale.left}</span><span>${s.scale.right}</span></div><div style="position:relative;height:16px;border-radius:8px;background:rgba(255,255,255,.18);margin-top:24px"><div class="am-dot" style="position:absolute;left:0;top:-22px;width:60px;height:60px;border-radius:50%;background:#D7EA4A;box-shadow:0 0 30px rgba(215,234,74,.8)"></div></div></div>`, el);
        const t = T(s.scale.at); AM.up(sc, t, .4, 30); AM.A(sc.querySelector(".am-dot"), [{ left: '0px' }, { left: Math.round(840 * (s.scale.pos ?? .5)) + 'px' }], t + .3, 1.0, AM.E.back);
      }
      (s.steps || []).forEach((st, k) => {
        const on = k === s.steps.length - 1;
        const n = make(`<div class="ab" style="left:${90 + k * 480}px;top:1080px;width:420px;height:150px"><div class="am-card${on ? ' solid' : ''}" style="inset:0;display:flex;align-items:center;gap:22px;padding:0 30px"><span style="font:800 54px Unbounded;${on ? '' : 'opacity:.5'}">${k + 1}</span><span style="font:800 36px Manrope">${st.text}</span></div></div>`, el);
        AM.A(n, [{ opacity: 0, transform: 'translateY(40px) scale(.9)' }, { opacity: on ? 1 : .65, transform: 'none' }], T(st.at), .45, AM.E.back); AM.cue(T(st.at), 'pop');
      });
    },
    /* Утверждение: одна мысль крупно, ключевые слова — маркером */
    statement(el, s, a, b) {
      const n = make(`<div class="am-big" style="top:${s.top ?? 700}px;font-size:${s.size ?? 96}px">${s.html}</div>`, el);
      AM.slam(n, a + .05, { hit: s.hit ?? false });
      n.querySelectorAll('mark').forEach((m, k) => { m.style.background = 'none'; m.style.color = 'inherit'; AM.marker(m, a + .4 + k * .3); });
    },
    /* Призыв: товар, название, артикул по голосу, «в описании», «ссылка в шапке профиля», поиск по артикулу */
    cta(el, s, a, b) {
      make('<div class="am-glow" style="left:-60px;top:280px;width:700px;height:1100px"></div>', el);
      const p = P(1015, 110, 330); el.appendChild(p);
      AM.A(p, [{ opacity: 0, transform: 'translateY(600px) rotate(10deg)' }, { opacity: 1, transform: 'none' }], a, .6, AM.E.snap); AM.float(p, a + .6, END, 14);
      const name = make(`<div class="ab" style="left:390px;top:390px;width:640px;font:800 52px/1.15 Unbounded">${s.name}</div>`, el); AM.up(name, a + .25, .4, 30);
      const art = make(`<div class="ab" style="left:390px;top:580px;width:620px;height:190px;border-radius:32px;background:#D7EA4A;color:#0B3326;display:flex;flex-direction:column;justify-content:center;padding-left:36px;box-shadow:0 0 80px rgba(215,234,74,.45)"><div style="font:800 30px Manrope;letter-spacing:.2em">АРТИКУЛ</div><div style="font:800 92px Unbounded;letter-spacing:.04em"></div></div>`, el);
      const ta = T(s.article.at); AM.pop(art, ta);
      const dt = s.article.digits ? s.article.digits.map(x => T(x)) : [...s.article.code].map((_, k) => ta + .3 + k * .15);
      AM.digits(art.lastChild, s.article.code, dt);
      if (s.desc) { const n = make(`<div class="ab" style="left:390px;top:790px"><span class="am-chip" style="font-size:32px">↓ ${s.desc.text}</span></div>`, el); AM.pop(n, T(s.desc.at), .45); }
      if (s.profile) { const n = make(`<div class="ab" style="left:390px;top:890px;width:620px;height:150px;border-radius:30px;background:#1F6A50;display:flex;align-items:center;gap:22px;padding:0 30px;box-shadow:0 0 0 4px #D7EA4A"><span style="font:800 60px Manrope;color:#D7EA4A">↑</span><div style="font:800 36px/1.2 Manrope">${s.profile.text}</div></div>`, el); const t = T(s.profile.at); AM.pop(n, t); AM.pulse(n, t + .6); }
      if (s.search) {
        const t = T(s.search.at);
        const sb = make(`<div class="ab" style="left:390px;top:1075px;width:620px;height:110px;border-radius:55px;background:#fff;color:#16201B;display:flex;align-items:center;gap:18px;padding:0 30px;font:700 38px Manrope"><svg width="44" height="44" viewBox="0 0 100 100"><circle cx="42" cy="42" r="26" fill="none" stroke="#16201B" stroke-width="11"/><path d="M62 62 L86 86" stroke="#16201B" stroke-width="13" stroke-linecap="round"/></svg><span></span><span style="width:4px;height:46px;background:#1F6A50"></span></div>`, el);
        AM.up(sb, t, .4, 30); AM.type(sb.children[1], s.article.code, t + .5, .14); AM.caret(sb.lastChild, t, END);
        const rs = make(`<div class="ab" style="left:390px;top:1205px;width:620px;height:150px;border-radius:30px;background:rgba(255,255,255,.95);color:#16201B;display:flex;align-items:center;gap:22px;padding:0 24px"><div style="width:${Math.round(120 * ar)}px;height:120px;background:url(${img}) 0 0/100% 100%"></div><div style="flex:1;font:700 28px/1.25 Manrope">${s.search.title}</div><div style="font:800 26px Manrope;color:#1F6A50">${s.search.badge ?? 'в наличии'}</div></div>`, el);
        AM.A(rs, [{ opacity: 0, transform: 'translateY(-30px)' }, { opacity: 1, transform: 'none' }], t + .5 + s.article.code.length * .14 + .5, .45, AM.E.back);
      }
    },
  };

  const list = [];
  R.scenes.forEach((s, i) => {
    const a = T(s.at); cursor = a;
    const id = 'sc' + i, el = make(`<div class="sc" id="${id}"></div>`, world);
    list.push({ id, a, in: s.in, x: s.x, y: s.y });
    const b = () => (R.scenes[i + 1] ? at(R.scenes[i + 1].at, a) : END);
    if (s.kick) { const k = make(`<div class="am-kick">${s.kick}</div>`, el); AM.up(k, a + .05, .4, 30); }
    BUILD[s.type](el, s, a, b);
  });
  AM.scenes(list, END);
  AM.background(END);
  AM.karaoke(words, { top: R.subsTop ?? 1440 });
  window.REEL_END = END;
};
