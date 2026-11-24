"""Уход на розничном сайте (задача владельца через Хиро, 24.11.2026): «Подбор ухода», «Состав простым языком», «С чем сочетать»,
«Похожие дешевле». Только розница; модуль «care» (выключен, пока владелец не включит в «Настройки → Модули»).

Как устроено, чтобы ИИ не выдумывал товары и цены:
- Товары выбираются ПРАВИЛАМИ из каталога в наличии (oh.live): категория — по названию (cat_of), активы и задачи кожи —
  по названию, описанию и составу (tags), цены — из каталога. ИИ товары не выбирает.
- ИИ (OpenAI, ключ из inbox.py) пишет только тексты: «Состав простым языком» — заранее пачкой по каталогу в фоне, хранится
  в базе (care_ai), на просмотр карточки ИИ не вызывается; короткое пояснение к рутине — по уже выбранным товарам.
- «С чем сочетать / не сочетать» — правила по активам (ретиноиды, кислоты, витамин C …), не ИИ.
- Тексты ИИ проходят фильтр запрещённых слов (диагнозы, обещания результата, «оригинал»): нашли — текст не показываем.
- Анкета без ФИО и телефона; ответы не сохраняем."""
import hashlib
import json
import re
import threading
import time

import requests
from flask import Blueprint, jsonify, request

import bg
import inbox
import modules
import order_hook as oh

bp = Blueprint("care", __name__)
bp.after_request(oh.cors)

# тексты Юриста (24.11.2026): полный — под рекомендацией подбора, короткий — на карточке товара
DISCLAIMER = ("Рекомендации подготовлены искусственным интеллектом на основе ваших ответов и описаний производителей. Это не медицинская "
              "консультация, не диагноз и не назначение лечения. ИИ может ошибаться. Перед применением прочитайте состав и инструкцию на "
              "упаковке и сделайте тест на небольшом участке кожи. При покраснении, жжении, сыпи или зуде прекратите применение и обратитесь "
              "к дерматологу. При кожных заболеваниях, во время беременности и кормления грудью посоветуйтесь с врачом.")
DISCLAIMER_SHORT = "Подбор сделал ИИ. Это не медицинская консультация, ИИ может ошибаться. Если появилось раздражение, обратитесь к дерматологу."
BANNED = re.compile(r"вылеч|лечит|лечени|излеч|клиническ|гарантир|диагноз|оригинал|100\s*%|навсегда|избав\w*|чудо|заживл|антибакт|"
                    r"убива\w* бактер|противовоспал|рекомендован\w* дерматолог|одобрен\w* врач|дерматологически|гипоаллерген|некомедоген|"
                    r"без побочн|безопасн\w* для (беремен|дет)|подходит всем|№\s*1|лучш\w* (средств|крем)|омолажива\w* на|эффективн\w* на \d+|"
                    r"акне исчез|убирает морщин|натуральн|органическ|у вас акне", re.I)

# ---------- категории по названию (порядок важен: точное раньше общего) ----------
CATS = [
    ("other", r"\b(shampoo|conditioner|hair|шампун|кондиционер|для волос|бальзам для волос|гель для душа|душа|body|hand cream|hand|foot|heel|для рук|для ног|стоп|дезодорант|набор|age-r|booster pro|device|lip|губ)\b|для тела|подарочн|для ног|для рук"),
    ("makeup", r"тональн|консил|concealer|тушь|mascara|maskara|румян|blush|помад|карандаш|cushion|кушон|foundation|\bcc\b|\bbb\b|cake|праймер|primer|пудр|тени|lip tint|тинт|хайлайт|бронз|корректор"),
    ("oil", r"cleansing oil|cleansing balm|гидрофильн|очищающ\w* масл|очищающ\w* бальзам|cleansing water|мицелл|micellar"),
    ("spf", r"\bsun\b|sunscreen|sun cream|sun stick|sun serum|\bspf|солнцезащ"),
    ("cleanser", r"cleans|foam|\bwash\b|powder wash|enzyme wash|пенка|умыв|гель для умыв|очищающ"),
    ("exfol", r"peeling|\bpeel\b|пилинг|скраб|scrub|exfol|pad\b|пэд"),
    ("eye", r"\beye\b|для век|вокруг глаз|патч"),
    ("mask", r"\bmask\b|маск|sleeping pack"),
    ("toner", r"toner|тонер|тоник|\bmist\b|мист"),
    ("serum", r"serum|ampoule|essence|booster|сыворот|ампул|эссенц|\bshot\b|концентрат"),
    ("cream", r"cream|moistur|lotion|emulsion|крем|эмульс|лосьон|gel\b|гель"),
]
CAT_TITLE = {"oil": "Снятие макияжа", "cleanser": "Умывание", "toner": "Тонер", "serum": "Сыворотка",
             "cream": "Крем", "spf": "Солнцезащитный крем", "eye": "Крем для глаз", "exfol": "Пилинг", "mask": "Маска"}


def cat_of(name):
    n = str(name or "").lower()
    if "mask cleanser" in n or "cleansing" in n and "mask" in n:
        return "cleanser"
    if "tone up" in n and re.search(r"sun|spf", n):
        return "spf"
    if re.search(r"(ampoule|essence|serum|сыворот\w*|ампул\w*)[ -]+(cream|крем)", n) and not re.search(r"sun|spf", n):
        return "cream"
    for c, rx in CATS:
        if re.search(rx, n):
            return c
    return "other"


# ---------- активы и задачи кожи ----------
ACTIVES = {   # id: (название, regex, что делает — нейтрально, без обещаний)
    "retinoid": ("Ретиноиды", r"retin|ретин|bakuchiol|бакучиол", "помогают обновлению кожи и упругости"),
    "vitc": ("Витамин C", r"vitamin c|витамин c|витамин с|ascorb|аскорб|vita c", "выравнивает тон, придаёт сияние"),
    "acids": ("Кислоты AHA/BHA/PHA", r"\baha\b|\bbha\b|\bpha\b|salicyl|салицил|glycol|гликол|lactic|молочн|azelaic|азелаин|кислот", "отшелушивают, очищают поры"),
    "niacin": ("Ниацинамид", r"niacinamid|ниацинамид", "выравнивает тон, помогает с порами и жирностью"),
    "txa": ("Транексамовая кислота", r"\btxa\b|tranexam|транексам", "помогает с пятнами и неровным тоном"),
    "cica": ("Центелла", r"centella|cica|центелл|madecass|мадекасс", "успокаивает, снимает покраснение"),
    "hyal": ("Гиалуроновая кислота", r"hyal|гиалур", "удерживает влагу, увлажняет"),
    "ceramide": ("Керамиды", r"ceramid|керамид|barrier|барьер", "укрепляют защитный барьер кожи"),
    "panthenol": ("Пантенол", r"panthenol|пантенол|\bb5\b", "успокаивает и смягчает"),
    "peptide": ("Пептиды", r"peptide|пептид", "поддерживают упругость"),
    "collagen": ("Коллаген", r"collagen|коллаген", "увлажняет, смягчает"),
    "snail": ("Муцин улитки", r"snail|escargot|улит|муцин", "увлажняет, помогает восстановлению"),
    "heartleaf": ("Хауттюйния", r"heartleaf|houttuyn|хауттюйн|цветок сердца", "успокаивает, подходит для склонной к высыпаниям"),
    "teatree": ("Чайное дерево", r"tea tree|чайн\w* дерев", "очищает, подходит для склонной к высыпаниям"),
    "pdrn": ("PDRN / NAD+", r"pdrn|\bnad\b|nmn", "поддерживает восстановление и тонус"),
    "rice": ("Рис", r"\brice\b|рис\b|рисов", "смягчает, придаёт сияние"),
    "propolis": ("Прополис", r"propolis|прополис", "питает, успокаивает"),
    "clay": ("Глина", r"\bclay\b|глин", "очищает поры, убирает жирный блеск"),
}
CONCERNS = {  # задача анкеты: (подпись, активы, слова в описании)
    "acne": ("Высыпания", {"acids", "teatree", "heartleaf", "niacin", "cica"}, r"акне|высып|воспал|прыщ|blemish|trouble|spot\b|ac\b|anti-acne"),
    "pigment": ("Пятна и неровный тон", {"vitc", "niacin", "txa", "rice"}, r"пигмент|пятн|осветл|bright|white|тон кожи|dark spot|постакне"),
    "aging": ("Морщины, упругость", {"retinoid", "peptide", "collagen", "pdrn"}, r"морщ|упруг|лифт|anti-?aging|wrinkle|firm|возраст"),
    "dry": ("Сухость, шелушение", {"hyal", "ceramide", "panthenol", "snail", "collagen"}, r"сух|увлаж|moist|hydra|aqua|шелуш|питат"),
    "red": ("Покраснения, чувствительность", {"cica", "panthenol", "heartleaf", "propolis"}, r"покрасн|раздраж|чувствит|успок|sooth|calm|mild|sensitive"),
    "pores": ("Поры, чёрные точки", {"acids", "niacin", "clay"}, r"\bпор|чёрн\w* точ|черн\w* точ|pore|себум|жирн\w* блеск"),
    "dull": ("Тусклый цвет", {"vitc", "rice", "niacin", "acids"}, r"тускл|сияни|glow|radian|свеж"),
}
HARSH = {"retinoid", "acids", "vitc"}
SKINS = {"dry": "сухая", "oily": "жирная", "combo": "комбинированная", "normal": "нормальная"}


def _acts(t):
    act = [a for a, (_, rx, _) in ACTIVES.items() if re.search(rx, t)]
    if "acids" in act and "hyal" in act and not re.search(r"\baha\b|\bbha\b|\bpha\b|salicyl|салицил|glycol|гликол|lactic|azelaic|азелаин", t):
        act.remove("acids")                                           # «гиалуроновая кислота» — не кислотный пилинг
    return act


def tags(name, desc="", inci=""):
    """Активы и задачи. «key» — то, что видно в названии и составе (сильный признак), «concerns» — ещё и по описанию
    (описания у многих товаров перечисляют всё подряд, поэтому это слабый признак)."""
    n = str(name or "").lower()
    t = f"{name} {desc} {inci}".lower()
    act = _acts(t)
    strong = _acts(f"{n} {str(inci or '').lower()}")
    con = [c for c, (_, acts, rx) in CONCERNS.items() if set(act) & acts or re.search(rx, t)]
    key = [c for c, (_, acts, rx) in CONCERNS.items() if set(strong) & acts or re.search(rx, n)]
    skin = set()
    if re.search(r"сух|dry|питат|rich|интенсив|barrier|барьер", t):
        skin.add("dry")
    if re.search(r"жирн|oily|себум|матир|pore|пор\b|clay|глин", t):
        skin |= {"oily", "combo"}
    if re.search(r"чувствит|sensitive|mild|baby|мягк|деликат", t):
        skin.add("sens")
    return {"actives": act, "concerns": con, "key": key, "skin": sorted(skin)}


# ---------- каталог ----------
def catalog():
    """Товары розницы в наличии: id → {id, name, brand, price, img, cat, tags}. Цены — розничные из каталога."""
    live = oh.live()
    descs = (oh._cache.get("descs") or (0, {}))[1]
    incis = (oh._cache.get("incis") or (0, {}))[1]
    ai = ai_all()
    out = {}
    for i in live.get("items", []):
        price = int(i.get("rtl") or 0)
        if price <= 0 or int(i.get("qty") or 0) <= 0:
            continue
        d, inc = descs.get(i["id"], i.get("desc", "")) or "", incis.get(i["id"], i.get("inci", "")) or ""
        tg = tags(i.get("name", ""), d, inc)
        a = ai.get(i["id"]) or {}
        if a.get("concerns"):
            tg["key"] = sorted(set(tg["key"]) | {c for c in a["concerns"] if c in CONCERNS})
        for k in ("concerns", "skin"):                                # ИИ уточняет задачи и тип кожи, если разметил
            if a.get(k):
                tg[k] = sorted(set(tg[k]) | {x for x in a[k] if x in (CONCERNS if k == "concerns" else {**SKINS, "sens": 1})})
        out[i["id"]] = {"id": i["id"], "name": i.get("name", ""), "brand": i.get("brand", ""), "price": price,
                        "img": i.get("img") or "", "cat": a.get("cat") if a.get("cat") in CAT_TITLE else cat_of(i.get("name", "")), **tg}
    return out


# ---------- «С чем сочетать / не сочетать» — правила ----------
def pairing(actives, cat=""):
    ok, no = [], []
    a = set(actives)
    if "retinoid" in a:
        no.append("Не рекомендуется одновременно с кислотами (AHA/BHA) и витамином C — лучше чередовать вечера")
        ok.append("Хорошо с увлажняющими и успокаивающими средствами: гиалуроновая кислота, керамиды, пантенол, центелла")
        ok.append("Утром — обязательно солнцезащитный крем")
    if "acids" in a:
        no.append("Не рекомендуется одновременно с ретиноидами и в одной процедуре с витамином C")
        ok.append("После — увлажнение и успокаивающие средства; утром — солнцезащитный крем")
    if "vitc" in a:
        no.append("Не рекомендуется одновременно с ретиноидами и кислотами — их лучше наносить вечером")
        ok.append("Удобно утром под солнцезащитный крем; хорошо с увлажняющими средствами")
    if "niacin" in a and "vitc" not in a:
        ok.append("Хорошо с гиалуроновой кислотой, центеллой, пантенолом")
    if a & {"cica", "panthenol", "heartleaf", "ceramide", "hyal"} and not a & HARSH:
        ok.append("Мягкое средство: сочетается с большинством ухода, удобно после кислот и ретиноидов")
    if cat == "spf":
        ok.append("Последний шаг утреннего ухода; на солнце обновляйте каждые 2–3 часа")
    if a & HARSH:
        no.append("Если кожа щиплет или краснеет — прекратите применение; при раздражении обратитесь к дерматологу")
    seen = set()
    return [x for x in ok if not (x in seen or seen.add(x))][:4], no[:4]


def freq(item):
    a, c = set(item["actives"]), item["cat"]
    if "retinoid" in a:
        return "Вечером, начните с 2 раз в неделю"
    if c == "exfol" or (c != "cleanser" and "acids" in a and c in ("toner", "serum")):
        return "1–2 раза в неделю, вечером"
    if c == "mask":
        return "1–2 раза в неделю"
    if c == "spf":
        return "Каждое утро, последним шагом"
    if c == "oil":
        return "Вечером, первым шагом — снимает макияж и SPF"
    if c in ("cleanser", "toner", "cream", "eye"):
        return "Утром и вечером"
    return "Ежедневно"


# ---------- ИИ: «Состав простым языком» — заранее, пачкой ----------
_ready = [False]
_run = threading.Lock()
BATCH = 6


def db():
    d = inbox.db()
    if not _ready[0]:
        d.run("CREATE TABLE IF NOT EXISTS care_ai (id TEXT PRIMARY KEY, sig TEXT, data TEXT, at DOUBLE PRECISION)")
        d.c.commit()
        _ready[0] = True
    return d


def ai_all():
    def load():
        with db() as d:
            rows = d.run("SELECT id, data FROM care_ai", many=True) or []
        out = {}
        for i, s in rows:
            try:
                out[i] = json.loads(s or "{}")
            except ValueError:
                pass
        return out
    return oh.cached("care_ai_all", 300, load)


def _sig(name, desc, inci):
    return hashlib.sha1(f"{name}|{desc[:1500]}|{inci[:1500]}".encode()).hexdigest()[:16]


def clean(s, n=600):
    s = re.sub(r"\s+", " ", str(s or "")).strip()[:n]
    return "" if BANNED.search(s) else s


SYSTEM = ("Ты пишешь для интернет-магазина корейской косметики понятные тексты о составе. Пиши по-русски, просто, без медицинских "
          "терминов. Не ставь диагнозов: не «у вас акне», а «вы указали, что…». Не обещай лечебный эффект и не называй болезни кожи "
          "(акне, дерматит, розацеа, экзема) целью средства — пиши «для кожи, склонной к…». Нельзя: «лечит», «вылечивает», «избавляет», "
          "«заживляет», «противовоспалительное», «убивает бактерии», «клинически доказано», «рекомендовано дерматологами», "
          "«гарантированный результат», «без побочных эффектов», «100% безопасно», «подходит всем», «убирает морщины», «оригинал», "
          "цифры эффективности. Не пиши «гипоаллергенно», «некомедогенно», «подходит беременным/детям», «натуральное», «органическое», "
          "если этого нет в описании производителя; если есть — только дословно и с пометкой «по данным производителя». Не сравнивай с "
          "другими брендами, без «лучший» и «№1». Используй глаголы «увлажняет», «ухаживает», «помогает», «уменьшает видимость», "
          "«помогает поддерживать барьер». Состав описывай нейтрально: функция компонента. Опирайся ТОЛЬКО на название, описание и "
          "состав, которые даны; не выдумывай ингредиенты. Если состава нет — опиши по описанию и названию, осторожно. Ответ — JSON.")
SHAPE = ('{"items":[{"id":"…","simple":"2–3 предложения: что это за средство и что делает его состав",'
         '"actives":[{"name":"Ниацинамид","what":"выравнивает тон"}],"suits":"кому подойдёт","caution":"кому с осторожностью (или пусто)",'
         '"cat":"cleanser|oil|toner|serum|cream|spf|eye|mask|exfol|makeup|other",'
         '"skin":["dry","oily","combo","normal","sens"],"concerns":["acne","pigment","aging","dry","red","pores","dull"]}]}')


def _ask(items):
    r = requests.post("https://api.openai.com/v1/chat/completions", timeout=90,
                      headers={"Authorization": f"Bearer {inbox.OPENAI_KEY}"},
                      json={"model": inbox.OPENAI_MODEL, "temperature": 0.2, "max_tokens": 3500, "response_format": {"type": "json_object"},
                            "messages": [{"role": "system", "content": SYSTEM},
                                         {"role": "user", "content": "Формат ответа: " + SHAPE + "\nТовары:\n" + json.dumps(items, ensure_ascii=False)}]})
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"]).get("items") or []


def _norm(x):
    acts = [{"name": clean(a.get("name"), 60), "what": clean(a.get("what"), 120)} for a in (x.get("actives") or [])[:6] if isinstance(a, dict)]
    return {"simple": clean(x.get("simple")), "actives": [a for a in acts if a["name"] and a["what"]],
            "suits": clean(x.get("suits"), 240), "caution": clean(x.get("caution"), 240),
            "cat": x.get("cat") if x.get("cat") in CAT_TITLE or x.get("cat") in ("makeup", "other") else "",
            "skin": [s for s in (x.get("skin") or []) if s in (*SKINS, "sens")],
            "concerns": [c for c in (x.get("concerns") or []) if c in CONCERNS]}


def prepare(limit=60):
    """Тексты для товаров в наличии, у которых их нет или изменились описание / состав. → сколько подготовлено."""
    if not inbox.OPENAI_KEY or not on() or not _run.acquire(blocking=False):   # модуль выключен — OpenAI не тратим (владелец 24.11.2026)
        return 0
    try:
        live = oh.live()
        descs = (oh._cache.get("descs") or (0, {}))[1]
        incis = (oh._cache.get("incis") or (0, {}))[1]
        with db() as d:
            have = dict(d.run("SELECT id, sig FROM care_ai", many=True) or [])
        todo = []
        for i in live.get("items", []):
            if int(i.get("rtl") or 0) <= 0 or int(i.get("qty") or 0) <= 0:
                continue
            ds, inc = descs.get(i["id"], i.get("desc", "")) or "", incis.get(i["id"], i.get("inci", "")) or ""
            s = _sig(i.get("name", ""), ds, inc)
            if have.get(i["id"]) != s:
                todo.append((i["id"], s, {"id": i["id"], "name": i.get("name", ""), "description": ds[:1200], "inci": inc[:1500]}))
        done = 0
        for k in range(0, min(len(todo), limit), BATCH):
            part = todo[k:k + BATCH]
            try:
                got = {str(x.get("id")): x for x in _ask([p[2] for p in part]) if isinstance(x, dict)}
            except Exception as e:
                print("Уход: ИИ-тексты не получены:", str(e)[:200], flush=True)
                break
            with db() as d:
                for pid, s, _ in part:
                    if pid in got:
                        d.run("DELETE FROM care_ai WHERE id=%s", (pid,))
                        d.run("INSERT INTO care_ai (id, sig, data, at) VALUES (%s,%s,%s,%s)",
                              (pid, s, json.dumps(_norm(got[pid]), ensure_ascii=False), time.time()))
                        done += 1
            time.sleep(1)
        oh._cache.pop("care_ai_all", None)
        if done:
            print(f"Уход: подготовлены тексты для {done} товаров, осталось {max(0, len(todo) - done)}", flush=True)
        return done
    finally:
        _run.release()


def _loop():
    time.sleep(120)
    while True:
        try:
            while prepare() >= 60:                    # первый проход — весь каталог, пачками по 60
                time.sleep(5)
        except Exception as e:
            print("Уход: подготовка текстов:", str(e)[:200], flush=True)
        time.sleep(6 * 3600)


def start():
    bg.start(_loop)


# ---------- «Похожие дешевле» ----------
def similar(pid, cat=None, n=6):
    cat = cat or catalog()
    it = cat.get(pid)
    if not it or it["cat"] in ("other", "makeup"):
        return []
    same = [x for x in cat.values() if x["id"] != pid and x["cat"] == it["cat"] and x["price"] < it["price"]]
    key = lambda x: (-(len(set(x["actives"]) & set(it["actives"])) * 2 + len(set(x["concerns"]) & set(it["concerns"]))), x["price"])  # noqa: E731
    best = [x for x in sorted(same, key=key) if set(x["actives"]) & set(it["actives"]) or set(x["concerns"]) & set(it["concerns"])]
    return [x["id"] for x in best[:n]]


# ---------- подбор ухода ----------
BUDGETS = {"low": 15000, "mid": 30000, "high": 60000}
AM = ["cleanser", "toner", "serum", "cream", "spf"]
PM = ["oil", "cleanser", "toner", "serum", "cream", "eye"]
WEEK = ["exfol", "mask"]
LEVEL = {"min": {"cleanser", "cream", "spf"}, "std": {"cleanser", "toner", "serum", "cream", "spf"},
         "full": {"oil", "cleanser", "toner", "serum", "cream", "spf", "eye", "exfol", "mask"}}


def answers_ok(a):
    skin = a.get("skin") if a.get("skin") in SKINS else "normal"
    sens = a.get("sens") in ("yes", "some")                           # «skip» — не указывать: как «нет»
    con = [c for c in (a.get("concerns") or []) if c in CONCERNS][:3]
    if a.get("age") in ("35", "45") and "aging" not in con:
        con.append("aging")
    return {"skin": skin, "sens": sens, "very": a.get("sens") == "yes", "concerns": con, "age": a.get("age") or "",
            "budget": BUDGETS.get(a.get("budget"), BUDGETS["mid"]), "have": {h for h in (a.get("have") or []) if h in CAT_TITLE},
            "level": a.get("level") if a.get("level") in LEVEL else "std"}


def score(x, q, when="am"):
    s = 4 * len(set(x.get("key", [])) & set(q["concerns"])) + len(set(x["concerns"]) & set(q["concerns"]))
    s += 2 if q["skin"] in x["skin"] or (q["skin"] == "combo" and "oily" in x["skin"]) else 0
    s -= 2 if q["skin"] == "dry" and "oily" in x["skin"] and "dry" not in x["skin"] else 0
    s -= 2 if q["skin"] == "oily" and "dry" in x["skin"] and "oily" not in x["skin"] else 0
    a = set(x["actives"])
    if q["sens"]:
        s += 2 if "sens" in x["skin"] or a & {"cica", "panthenol", "heartleaf"} else 0
        s -= (6 if q["very"] else 3) * len(a & HARSH)
    if when == "am" and "retinoid" in a:
        s -= 20                                                       # ретиноиды — только вечером
    if when == "am" and "acids" in a and x["cat"] in ("serum", "toner"):
        s -= 6
    if when == "pm" and "vitc" in a:
        s -= 4                                                        # витамин C удобнее утром
    if x["cat"] == "serum" and not q["very"]:                         # «активная» сыворотка под задачу: ретиноид — вечером, витамин C — утром
        if when == "pm" and "retinoid" in a and "aging" in q["concerns"]:
            s += 4
        if when == "am" and "vitc" in a and {"pigment", "dull"} & set(q["concerns"]):
            s += 3
    return s


def _pick(cands, q, when, cap, used, eco=False, target=0):
    sc = [(score(x, q, when), x) for x in cands if x["price"] <= cap]
    if not sc:
        return None
    top = max(s for s, _ in sc)
    if eco:                                                           # эконом: самое дешёвое из подходящих
        ok = [x for s, x in sc if s >= min(4, top)] or [x for _, x in sc]   # хоть что-то под задачу или тип кожи — и самое дешёвое
        return min(ok, key=lambda x: (x["price"], x["id"] in used))
    good = [x for s, x in sc if s >= top - 1]                        # лучшие по задачам; из них — цена ближе к бюджету шага
    return min(good, key=lambda x: (abs(x["price"] - target), x["id"] in used))


def _why(x, q):
    hit = [CONCERNS[c][0].lower() for c in (x.get("key") or x["concerns"]) if c in q["concerns"]][:2]
    act = [ACTIVES[a][0] for a in x["actives"]][:2]
    parts = []
    if hit:
        parts.append("для задачи: " + ", ".join(hit))
    if act:
        parts.append("в составе: " + ", ".join(act))
    if not parts and (q["skin"] in x["skin"]):
        parts.append(f"для кожи: {SKINS[q['skin']]}")
    return "; ".join(parts).capitalize() if parts else "Базовый шаг ухода"


def routine(q, cat, eco=False):
    """{"am": [шаги], "pm": [...], "week": [...], "total": ₸, "missing": [шаги без товара]}. Только товары из cat."""
    by = {}
    for x in cat.values():
        by.setdefault(x["cat"], []).append(x)
    lvl = LEVEL["min"] if eco else LEVEL[q["level"]]
    budget = q["budget"] if not eco else min(q["budget"], BUDGETS["low"])
    n_steps = len([s for s in AM + PM + WEEK if s in lvl])
    used, out, missing = {}, {"am": [], "pm": [], "week": []}, []

    def step(c, when):
        if c not in lvl:
            return
        if c in used:                                                 # то же средство утром и вечером (умывание, тонер, крем)
            x = used[c] if not (c == "serum" and when == "pm") else None
            if x:
                out[when].append({**_card(x, q, c), "again": True})
                return
        share = budget / max(3, n_steps - 1)
        x = _pick(by.get(c, []), q, when, max(share * 1.8, 2500), {v["id"] for v in used.values()}, eco, target=share)
        if x is None:
            x = _pick(by.get(c, []), q, when, 10 ** 9, set(), True)  # в бюджет не влезло — самое дешёвое подходящее
        if x is None:
            missing.append(CAT_TITLE[c])
            return
        if c == "serum" and when == "pm" and x["id"] == (used.get("serum") or {}).get("id"):
            out[when].append({**_card(x, q, c), "again": True})
            return
        used.setdefault(c, x)
        out[when].append(_card(x, q, c))

    for c in AM:
        step(c, "am")
    used_pm = dict(used)
    for c in PM:
        if c == "serum":
            used.pop("serum", None)
        step(c, "pm")
    if "serum" in used_pm and "serum" not in used:
        used["serum"] = used_pm["serum"]
    for c in WEEK:
        if c in lvl:
            x = _pick(by.get(c, []), q, "pm", 10 ** 9, set(), eco)
            if x:
                out["week"].append(_card(x, q, c))
    ids = {s["id"] for k in out for s in out[k]}
    total = sum(cat[i]["price"] for i in ids if i in cat and not any(s["id"] == i and s["have"] for k in out for s in out[k]))
    return {**out, "total": total, "missing": sorted(set(missing))}


def _card(x, q, c):
    return {"step": c, "title": CAT_TITLE[c], "id": x["id"], "name": x["name"], "brand": x["brand"], "price": x["price"], "img": x["img"],
            "freq": freq(x), "why": _why(x, q), "have": c in q["have"]}


def notes(r):
    out = []
    pm_ids = [s["id"] for s in r["pm"]] + [s["id"] for s in r["week"]]
    try:
        cat = catalog()
    except Exception:
        cat = {}
    a = set()
    for i in pm_ids:
        a |= set((cat.get(i) or {}).get("actives", []))
    if "retinoid" in a and "acids" in a:
        out.append("Ретиноид и кислоты — в разные вечера: например, ретиноид пн/чт, пилинг в субботу")
    if a & HARSH:
        out.append("Активные средства вводите по одному, начиная с 1–2 раз в неделю")
    out.append("Утром солнцезащитный крем — обязательно, особенно с активными средствами")
    return out


def summary(q, r):
    """Короткое пояснение ИИ к уже выбранной рутине (товары не меняет). Не вышло или нашлось запрещённое слово — шаблон."""
    base = ("Наносите по порядку: от самой лёгкой текстуры к самой плотной — умывание, тонер, сыворотка, крем; утром последним "
            "шагом солнцезащитный крем." + (" Новые средства вводите по одному, чтобы видеть, как кожа на них реагирует." if q["sens"] else ""))
    if not inbox.OPENAI_KEY:
        return base
    key = "care_sum:" + hashlib.sha1(json.dumps([q["skin"], q["sens"], q["concerns"], [s["id"] for k in ("am", "pm", "week") for s in r[k]]],
                                                ensure_ascii=False).encode()).hexdigest()

    def ask():
        steps = [{"когда": k, "шаг": s["title"], "товар": s["name"]} for k in ("am", "pm", "week") for s in r[k]]
        msg = (f"Кожа: {SKINS[q['skin']]}{', чувствительная' if q['sens'] else ''}; задачи: {', '.join(CONCERNS[c][0] for c in q['concerns']) or 'поддерживающий уход'}. "
               f"Рутина (уже выбрана, не меняй и не добавляй товары): {json.dumps(steps, ensure_ascii=False)}. "
               "Напиши 2–3 коротких предложения, почему уход устроен так и в каком порядке наносить. Без обещаний результата и медицинских слов.")
        rr = requests.post("https://api.openai.com/v1/chat/completions", timeout=8, headers={"Authorization": f"Bearer {inbox.OPENAI_KEY}"},
                           json={"model": inbox.OPENAI_MODEL, "temperature": 0.3, "max_tokens": 220,
                                 "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": msg}]})
        rr.raise_for_status()
        return clean(rr.json()["choices"][0]["message"]["content"], 500)
    try:
        return oh.cached(key, 86400, ask) or base
    except Exception as e:
        print("Уход: пояснение ИИ:", str(e)[:150], flush=True)
        return base


# ---------- адреса для сайта ----------
def on():
    try:
        return modules.enabled("care")
    except Exception:
        return False


def _admin():
    try:
        import admin
        return bool(admin.who())
    except Exception:
        return False


@bp.get("/care/status")
def status_route():
    return jsonify(ok=True, on=on())


@bp.get("/care/info/<pid>")
def info_route(pid):
    """Карточка товара: «Состав простым языком» (заранее подготовлен ИИ), «С чем сочетать», «Похожие дешевле»."""
    if not (on() or _admin()):
        return jsonify(ok=False), 404
    cat = oh.cached("care_cat", 120, catalog)
    it = cat.get(pid)
    if not it:
        return jsonify(ok=True, found=False, similar=[])
    a = ai_all().get(pid) or {}
    ok, no = pairing(it["actives"], it["cat"])
    acts = a.get("actives") or [{"name": ACTIVES[x][0], "what": ACTIVES[x][2]} for x in it["actives"]][:5]
    resp = jsonify(ok=True, found=True, simple=a.get("simple", ""), actives=acts, suits=a.get("suits", ""), caution=a.get("caution", ""),
                   combine=ok, avoid=no, freq=freq(it), similar=similar(pid, cat), disclaimer=DISCLAIMER_SHORT, ai=bool(a.get("simple")))
    resp.headers["Cache-Control"] = "public, max-age=600"
    return resp


@bp.route("/care/quiz", methods=["POST", "OPTIONS"])
def quiz_route():
    """Анкета → рутина утро / вечер / раз в неделю и вариант «эконом». Ответы без ФИО и телефона, не сохраняем."""
    if request.method == "OPTIONS":
        return "", 204
    if not (on() or _admin()):
        return jsonify(ok=False), 404
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0]).strip()
    if oh.too_many("care:" + ip, limit=30, window=3600):
        return jsonify(ok=False, error="Слишком много попыток, попробуйте через час"), 429
    q = answers_ok(request.get_json(silent=True) or {})
    cat = oh.cached("care_cat", 120, catalog)
    main, eco = routine(q, cat), routine(q, cat, eco=True)
    return jsonify(ok=True, main=main, eco=eco, notes=notes(main), summary=summary(q, main), disclaimer=DISCLAIMER,
                   skin=SKINS[q["skin"]], concerns=[CONCERNS[c][0] for c in q["concerns"]])


@bp.get("/admin/api/care")
def admin_status():
    if not _admin():
        return jsonify(ok=False, error="Войдите заново"), 401
    with db() as d:
        n = (d.run("SELECT COUNT(*) FROM care_ai", one=True) or [0])[0]
    return jsonify(ok=True, prepared=n, products=len(catalog()), on=on(), ai=bool(inbox.OPENAI_KEY))


@bp.post("/admin/api/care/prepare")
def admin_prepare():
    if not _admin():
        return jsonify(ok=False, error="Войдите заново"), 401
    threading.Thread(target=prepare, kwargs={"limit": 600}, daemon=True, name="care-prep").start()
    return jsonify(ok=True)
