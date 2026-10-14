/* Celimax Retinal Shot First Essence Toner, арт. 01330 — розница amura.kz. Голос: Virale (ElevenLabs Sarah), темп ×1.15. */
window.REEL = {
  product: { img: 'bottle.png', ratio: 430 / 1898 },
  scenes: [
    { type: 'hook', at: 0, lines: [
      { at: 0, html: 'Поры<br><span class="lime">расширены?</span>', top: 640 },
      { at: '@Морщинки', html: 'Морщинки?', top: 700 },
      { at: '@Кожа', html: 'Кожа теряет<br><span class="lime">упругость?</span>', top: 620, size: 104 } ] },
    { type: 'reveal', at: '@Знакомьтесь', kick: 'ЗНАКОМЬТЕСЬ', caption: { text: 'тонер-эссенция с ретиналем', at: '@тонер' } },
    { type: 'compare', at: '@Ретиналь', in: 'whip', kick: 'СЛЕДУЮЩАЯ СТУПЕНЬ',
      from: { label: 'было', title: 'Ретинол', at: '@после' }, to: { label: 'теперь', title: 'Ретиналь', at: '@Ретиналь+0.1' },
      chips: [{ text: '⚡ активнее', at: '@активнее' }, { text: '🪶 но мягко', at: '@мягко' }] },
    { type: 'facts', at: '@Пантенол-0.1', in: 'wipe',
      cards: [{ label: 'Ретиналь', value: '0,01%', at: '@Пантенол+0.1' }, { label: 'Пантенол · успокаивает', value: '1%', at: '@Пантенол+0.5' }, { label: 'RETINOGIN™ · плотность', value: '3%', at: '@RETINOGIN' }],
      chips: [{ text: 'плотнее', at: '@плотнее' }, { text: 'глаже', at: '@глаже' }] },
    { type: 'texture', at: '@Текстура-0.1', in: 'iris', y: 680, kick: 'ТЕКСТУРА',
      scale: { left: 'тонер', right: 'сыворотка', pos: .5, at: '@между' },
      steps: [{ text: 'Умывание', at: '@Наносите' }, { text: 'Retinal Shot', at: '@первым' }] },
    { type: 'cta', at: '@Celimax-0.05', in: 'zoom', name: 'Celimax<br>Retinal Shot',
      article: { code: '01330', at: '@Артикул', digits: [24.82, 25.33, 25.94, 26.15, 26.35] },
      desc: { text: 'также в описании', at: '@он-0.05' },
      profile: { text: 'Ссылка на магазин —<br><span class="lime">в шапке профиля</span>', at: '@Ссылка-0.05' },
      search: { at: '@Введите-0.05', title: 'Celimax Retinal Shot<br>First Essence Toner' } },
  ],
};
