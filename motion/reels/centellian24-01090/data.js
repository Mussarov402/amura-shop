/* Centellian24 Madeca Cream Time Reverse 50ml, арт. 01090 — розница amura.kz. Голос: Virale (ElevenLabs Sarah), темп ×1.15. Артикул голосом не читаем — только на экране. */
window.REEL = {
  end: 36.3,
  product: { img: 'tube.png', ratio: 408 / 1600 },
  scenes: [
    { type: 'hook', at: 0, lines: [
      { at: 0, html: '<span class="lime">Морщинки?</span>', top: 700 },
      { at: '@Кожа', html: 'Кожа потеряла<br><span class="lime">упругость?</span>', top: 620, size: 100 },
      { at: '@Тон', html: 'Тон<br><span class="lime">тусклый?</span>', top: 640 } ] },
    { type: 'reveal', at: '@Знакомьтесь', kick: 'ЗНАКОМЬТЕСЬ', height: 1040, top: 300, caption: { text: 'антивозрастной крем', at: '@антивозрастной' } },
    { type: 'facts', at: '@Комплекс-0.1', in: 'wipe',
      cards: [{ label: 'Active-TECA™ · коллаген', value: 'TECA™', at: '@Active' },
              { label: 'восстанавливает барьер', value: 'Центелла', at: '@центелла' },
              { label: 'гиалуронка · ниацинамид', value: 'Коллаген', at: '@Коллаген' }],
      chips: [{ text: '💧 увлажняют', at: '@увлажняют' }, { text: '✨ ровный тон', at: '@выравнивают' }] },
    { type: 'statement', at: '@становится-0.15', in: 'whip', top: 640, size: 100,
      html: 'Кожа<br><mark>плотнее</mark>,<br><mark>глаже</mark><br>и <mark>сияет</mark>' },
    { type: 'statement', at: '@Подходит-0.1', in: 'zoom', top: 700, size: 84,
      html: 'Для <mark>всех</mark><br>типов кожи —<br>даже <mark>чувствительной</mark>' },
    { type: 'cta', at: '@Madeca>-0.6', in: 'zoom', name: 'Centellian24<br>Madeca Cream',
      article: { code: '01090', at: '@Артикул' },
      desc: { text: 'также в описании', at: '@он-0.05' },
      profile: { text: 'Ссылка на магазин —<br><span class="lime">в шапке профиля</span>', at: '@Ссылка-0.05' },
      search: { at: '@Введите-0.05', title: 'Centellian24 Madeca Cream<br>Time Reverse 50ml' } },
  ],
};
