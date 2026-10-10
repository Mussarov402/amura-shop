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
        "sources": {"own": "Наша база (копия МойСклад, обновляется каждые 10 минут)"},
    },
    "warehouse": {
        "title": "Склад",
        "about": "Остатки по складам: итоги по каждому складу и поиск товара с остатком и резервом на каждом складе. Только просмотр.",
        "views": ["wh"],
        "sources": {"own": "Наша база (копия МойСклад, обновляется каждые 10 минут)"},
    },
}


def _key(mid, suffix=""):
    return f"mod_{mid}{suffix}"


def enabled(mid):
    if mid not in REGISTRY:
        return False
    try:
        with inbox.db() as d:
            return inbox.get_setting(d, _key(mid), "0") == "1"
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
        return {mid: True for mid in REGISTRY if inbox.get_setting(d, _key(mid), "0") == "1"}


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
