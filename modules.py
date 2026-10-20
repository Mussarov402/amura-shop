"""Подключаемые модули панели (см. docs/MIGRATION.md, «Архитектура модулей»).
Состояние — в таблице setting: mod_<id> = «1»/«0» (по умолчанию выключен), mod_<id>_src = источник данных.
Выключенный модуль не виден в меню, его API отвечает 404 и он ни на что не влияет."""
import inbox

# id -> название, описание, разделы панели (views), источники данных (первый — по умолчанию)
REGISTRY = {
    "finance": {
        "title": "Финансы",
        "about": "Деньги на счетах и в кассах, поступления и выплаты по дням, взаиморасчёты с контрагентами. Только просмотр.",
        "views": ["money"],
        "default": True,      # решение владельца 10.10.2026: готовые разделы включены сразу; выключатель в «Модулях» — для отката
        "sources": {"own": "Наша база (копия МойСклад, обновляется каждые 10 минут)"},
    },
    "cards": {
        "title": "Карточки товаров",
        "about": "Карточка товара по нажатию в «Товарах» и «Складе»: фото (загружаются в МойСклад и попадают на сайт), одно видео, штрихкоды, цены, остатки по складам.",
        "views": [],
        "default": True,
        "sources": {"own": "Наша база (копия МойСклад) + фото из МойСклад"},
    },
    "sources": {
        "title": "Источники",
        "about": "Откуда пришли покупатели: ссылки с меткой для авторов, блогеров и рекламы, промокоды, переходы, заказы, выручка и окупаемость по каждому источнику.",
        "views": ["sources"],
        "sources": {"own": "Наша база (переходы и заказы с сайта) + статусы заказов из копии МойСклад"},
    },
    "wishes": {
        "title": "Пожелания клиентов",
        "about": "Анкета на сайте (/#opros): какие бренды и товары хотят видеть покупатели, что улучшить, оценка. Кнопка «В пожелания» в чатах. Топ запрошенных брендов и наличие в каталоге.",
        "views": ["wishes"],
        "sources": {"own": "Наша база (ответы анкеты и сообщения из чатов) + каталог сайта"},
    },
    "cardpay": {
        "title": "Оплата картой (Freedom Pay)",
        "about": "Кнопка «Оплатить картой» на розничном сайте после оформления заказа: Visa/Mastercard, Apple/Google Pay. Оплата отмечается в заказе и в МойСклад, вам приходит «выбейте чек в кассе Kaspi». Работает, только когда ключи Freedom Pay вписаны на Render.",
        "views": [],
        "sources": {"own": "Freedom Pay (интернет-эквайринг)"},
    },
    "kaspipay": {
        "title": "Оплата Kaspi (ApiPay)",
        "about": "Вариант «Kaspi» при оформлении на розничном сайте: счёт приходит покупателю в приложение Kaspi на номер из заказа. Оплачен — заказ сразу «Подтвержден», платёж в МойСклад на счёт Kaspi, вам сообщение. Работает, только когда ключ ApiPay вписан на Render.",
        "views": [],
        "sources": {"own": "ApiPay.kz (счета Kaspi Pay по номеру телефона)"},
    },
    "analytics": {
        "title": "Аналитика карточек",
        "about": "Воронка розничного сайта, как у WB: переходы в карточку → в корзину → заказали, по каждому товару и по источникам. Обезличенно, без cookies.",
        "views": ["analytics"],
        "default": True,      # 17.10.2026: просил владелец; выключатель — для отката
        "sources": {"own": "Наша база (обезличенные события розничного сайта) + заказы с сайта"},
    },
    "partners": {
        "title": "Заявки на сотрудничество",
        "about": "Раздел «Сотрудничество» на сайтах: «Стать оптовиком» (amura.kz) и «Стать продавцом на AMURA». Заявки — в «Клиенты → Заявки», владельцу приходит уведомление.",
        "views": ["partners"],
        "default": True,      # 17.10.2026: просил владелец
        "sources": {"own": "Наша база (заявки с сайтов)"},
    },
    "warehouse": {
        "title": "Склад",
        "about": "Остатки по складам: итоги по каждому складу и поиск товара с остатком и резервом на каждом складе. Только просмотр.",
        "views": ["wh"],
        "default": True,
        "sources": {"own": "Наша база (копия МойСклад, обновляется каждые 10 минут)"},
    },
}


def _key(mid, suffix=""):
    return f"mod_{mid}{suffix}"


def _dflt(mid):
    """Состояние, пока владелец не переключал модуль сам: «1» для готовых разделов (REGISTRY[...]["default"])."""
    return "1" if REGISTRY[mid].get("default") else "0"


def enabled(mid):
    if mid not in REGISTRY:
        return False
    try:
        with inbox.db() as d:
            return inbox.get_setting(d, _key(mid), _dflt(mid)) == "1"
    except Exception:
        return False


def source(mid):
    m = REGISTRY[mid]
    with inbox.db() as d:
        v = inbox.get_setting(d, _key(mid, "_src"), "")
    return v if v in m["sources"] else next(iter(m["sources"]))


def on_map():
    """Какие модули включены: {id: True}. Для меню панели (ничего секретного)."""
    with inbox.db() as d:
        return {mid: True for mid in REGISTRY if inbox.get_setting(d, _key(mid), _dflt(mid)) == "1"}


def listing():
    on = on_map()
    return [{"id": mid, "title": m["title"], "about": m["about"], "on": bool(on.get(mid)),
             "source": source(mid), "sources": [{"id": k, "title": v} for k, v in m["sources"].items()]}
            for mid, m in REGISTRY.items()]


def update(mid, on=None, src=None):
    if mid not in REGISTRY:
        raise KeyError(mid)
    with inbox.db() as d:
        if on is not None:
            inbox.set_setting(d, _key(mid), "1" if on else "0")
        if src is not None and src in REGISTRY[mid]["sources"]:
            inbox.set_setting(d, _key(mid, "_src"), src)


def simplify_1017():
    """Решение владельца 17.10.2026: учёт — в МойСклад, система AMURA — простой кабинет продавца.
    Один раз выключаем разделы-дубли МойСклад («Финансы» с «Деньгами»/«Отчётами», «Склад»), если владелец их сам не переключал.
    Включить обратно — «Настройки → Модули». Вызывается только на сервере (app.py), в тестах — нет."""
    try:
        with inbox.db() as d:
            if inbox.get_setting(d, "mod_simplify_1017", "") == "1":
                return
            for mid in ("finance", "warehouse"):
                if inbox.get_setting(d, _key(mid), "") == "":
                    inbox.set_setting(d, _key(mid), "0")
            inbox.set_setting(d, "mod_simplify_1017", "1")
    except Exception as e:
        print("Модули: упрощение меню не применено:", e, flush=True)
