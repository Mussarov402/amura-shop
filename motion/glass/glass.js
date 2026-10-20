/* AMURA Glass — движок кадра. Время в секундах; render.js перематывает анимации покадрово (seek).
   G.A(el, keyframes, t, dur, easing) · G.type(el, text, t, step) — печать по буквам со щелчками ·
   G.cue(t, kind) — звук: click | ding | ping | pop | whoosh (синтез в sfx.py) · G.bg(end) — фон «хром». */
window.G = (() => {
  const E = { out: 'cubic-bezier(.2,.8,.2,1)', back: 'cubic-bezier(.34,1.4,.64,1)', io: 'cubic-bezier(.65,0,.35,1)' };
  const cues = [], cue = (t, kind) => cues.push({ t: +t.toFixed(3), kind });
  const make = (html, parent = document.body) => { const t = document.createElement('template'); t.innerHTML = html.trim(); const n = t.content.firstChild; parent.appendChild(n); return n; };
  const A = (el, kf, t, d, e = E.out, fill = 'both') => el.animate(kf, { delay: t * 1000, duration: Math.max(1, d * 1000), fill, easing: e });
  const show = (el, a, b, fade = .3) => A(el, [{ opacity: 0 }, { opacity: 1, offset: Math.min(.45, fade / (b - a)) }, { opacity: 1, offset: Math.max(.55, 1 - fade / (b - a)) }, { opacity: 0 }], a, b - a, 'linear', 'none');

  /* печать по буквам: каждая буква — span, появляется в свой момент; щелчок на каждую */
  function type(el, text, t, step = .06) {
    let i = 0;
    for (const part of text.split(/(<br>)/)) {
      if (part === '<br>') { make('<br>', el); continue; }
      for (const ch of part) { const s = make(`<span style="opacity:0">${ch === ' ' ? '&nbsp;' : ch}</span>`, el); A(s, [{ opacity: 0 }, { opacity: 1 }], t + i * step, .01, 'linear', 'forwards'); if (ch !== ' ') cue(t + i * step, 'click'); i++; }
    }
    return t + i * step;
  }
  function caret(el, a, b) { A(el, [{ opacity: 1 }, { opacity: 1, offset: .5 }, { opacity: 0, offset: .51 }, { opacity: 0 }], a, .9, 'linear', 'none').effect.updateTiming({ iterations: Math.ceil((b - a) / .9) }); }

  /* фон: изогнутые хромовые ленты + радужная призма; медленно «дышит» */
  function bg(end) {
    const b = make('<div id="g-bg"></div>');
    const ribbon = (d, w, o, id) => `<path d="${d}" fill="none" stroke="url(#${id})" stroke-width="${w}" stroke-linecap="round" opacity="${o}"/>`;
    make(`<svg viewBox="0 0 1480 2320"><defs>
      <linearGradient id="c1" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#15171a"/><stop offset=".35" stop-color="#8d939b"/><stop offset=".5" stop-color="#eef1f4"/><stop offset=".62" stop-color="#7d838b"/><stop offset="1" stop-color="#121315"/></linearGradient>
      <linearGradient id="c2" x1="1" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#0f1012"/><stop offset=".4" stop-color="#5d636b"/><stop offset=".52" stop-color="#d4d8dd"/><stop offset=".66" stop-color="#4b5057"/><stop offset="1" stop-color="#0d0e10"/></linearGradient>
      <filter id="sf"><feGaussianBlur stdDeviation="3"/></filter></defs>
      <g filter="url(#sf)">
      ${ribbon('M-100 700 C 300 380, 700 980, 1100 560 S 1600 300, 1700 520', 230, .9, 'c1')}
      ${ribbon('M-150 1500 C 250 1150, 650 1850, 1150 1450 S 1550 1200, 1700 1350', 260, .85, 'c2')}
      ${ribbon('M 200 2400 C 400 2000, 900 2200, 1200 1800', 160, .6, 'c1')}
      ${ribbon('M 1500 100 C 1200 300, 900 0, 600 200', 140, .5, 'c2')}
      </g></svg>`, b);
    make('<div id="g-prism"></div>', b); make('<div id="g-vig"></div>', b);
    A(b.querySelector('svg'), [{ transform: 'scale(1) rotate(0deg)' }, { transform: 'scale(1.06) rotate(1.5deg)' }], 0, end, 'linear');
    A(b.querySelector('#g-prism'), [{ transform: 'rotate(-32deg) translateX(-80px)', opacity: .35 }, { transform: 'rotate(-32deg) translateX(80px)', opacity: .6 }], 0, end, 'ease-in-out');
    document.body.prepend(b);
  }

  window.seek = ms => document.getAnimations().forEach(a => { a.pause(); a.currentTime = ms; });
  return { E, A, make, show, type, caret, bg, cue, cues };
})();
