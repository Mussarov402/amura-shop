# API рабочего места менеджера WB/Ozon

Модуль «Рабочее место менеджера» в системе AMURA (по умолчанию выключен). Код — `agent.py`.

Ключи маркетплейсов остаются только на Render. Менеджер работает через сервер системы AMURA:
- **читает** данные кабинетов;
- **присылает заявки** на действия.

Сервер выполняет заявку своим ключом только после «Да» владельца в разделе «Заказы → На согласование».

## Доступ

- Адрес: `https://admin.amura.kz/agent/api/...`
- Заголовок: `Authorization: Bearer <MP_AGENT_TOKEN>`.
  - Токен — переменная на Render, его задаёт владелец.
  - Токен отдельный от входа владельца и действует только на `/agent/api/*`.
- Не больше 60 запросов в минуту, иначе ответ `429`.
- Ответы:
  - модуль выключен — `404`;
  - токен неверный — `401`;
  - ошибка площадки — `502` с текстом ошибки.
- Каждый вызов пишется в журнал, владелец видит его на экране «На согласование».

## Справка

`GET /agent/api/help` — магазины (`ozon`, `ozon2`, `wb`, `wb2`…), список чтений и действий с параметрами, текущие правила самостоятельности.

## Чтение

`GET /agent/api/read/<магазин>/<что>?p=<JSON параметров>`

Параметры можно передать и обычной строкой запроса: `?isAnswered=false&take=50&skip=0`. Если передан `p`, его значения важнее.

Проходят только разрешённые параметры. Ответ — `{"ok": true, "shop": "...", "data": <ответ площадки как есть>}`.

### WB

Документация сверена 28.10.2026.

| что | метод площадки | параметры |
|---|---|---|
| `orders` | GET statistics `/api/v1/supplier/orders` | `dateFrom`, `flag` |
| `sales` | GET statistics `/api/v1/supplier/sales` | `dateFrom`, `flag` |
| `fbs_new` | GET marketplace `/api/v3/orders/new` | — |
| `warehouses` | GET marketplace `/api/v3/warehouses` | — |
| `stocks` | POST marketplace `/api/v3/stocks/{warehouseId}` | `warehouseId`, `chrtIds` |
| `cards` | POST content `/content/v2/get/cards/list` | `settings` |
| `charcs` | GET content `/content/v2/object/charcs/{subjectId}` | `subjectId`, `locale` |
| `cards_trash` | POST content `/content/v2/get/cards/trash` | `settings` |
| `card_errors` | POST content `/content/v2/cards/error/list` | `cursor`, `order` |
| `prices` | GET discounts-prices `/api/v2/list/goods/filter` | `limit`, `offset`, `filterNmID` |
| `feedbacks` | GET feedbacks `/api/v1/feedbacks` | `isAnswered`, `nmId`, `take`, `skip`, `order`, `dateFrom`, `dateTo` |
| `questions` | GET feedbacks `/api/v1/questions` | те же |
| `unseen` | GET feedbacks `/api/v1/new-feedbacks-questions` | — |
| `adverts_count` | GET advert `/adv/v1/promotion/count` | — |
| `adverts` | GET advert `/api/advert/v2/adverts` | `ids`, `statuses`, `payment_type` |
| `advert_stats` | GET advert `/adv/v3/fullstats` | `ids`, `beginDate`, `endDate` (≤31 дня) |
| `advert_spend` | GET advert `/adv/v1/upd` | `from`, `to` |
| `advert_budgets` | POST advert `/api/advert/v2/budget` | `ids` |
| `daily_limits` | GET advert `/api/advert/v0/daily-limits` | `advertIds` |
| `bid_recommendations` | GET advert `/api/advert/v0/bids/recommendations` | `nmId`, `advertId` |
| `balance` | GET advert `/adv/v1/balance` | — |
| `search_report` | POST seller-analytics `/api/v2/search-report/report` | тело как в документации |
| `search_texts` | POST seller-analytics `/api/v2/search-report/product/search-texts` | тело |
| `search_orders` | POST seller-analytics `/api/v2/search-report/product/orders` | тело |
| `funnel` | POST seller-analytics `/api/analytics/v3/sales-funnel/products` | тело |
| `realization` | POST finance `/api/finance/v1/sales-reports/detailed` | `dateFrom`, `dateTo`, `limit`, `rrdId`, `period`, `fields` |
| `promotions` | GET dp-calendar `/api/v1/calendar/promotions` | `startDateTime`, `endDateTime`, `allPromo`, `limit`, `offset` |
| `promotion_goods` | GET dp-calendar `/api/v1/calendar/promotions/nomenclatures` | `promotionID`, `inAction`, `limit`, `offset` |

### Ozon

Seller API. Тело запроса передаётся как есть.

| что | метод |
|---|---|
| `postings` | `/v4/posting/fbs/list` |
| `stocks` | `/v4/product/info/stocks` |
| `products` | `/v3/product/list` |
| `product_info` | `/v3/product/info/list` |
| `prices` | `/v5/product/info/prices` |
| `reviews` | `/v1/review/list` |
| `review_info` | `/v1/review/info` |
| `review_comments` | `/v1/review/comment/list` |
| `analytics` | `/v1/analytics/data` |
| `queries` | `/v1/analytics/product-queries` |
| `transactions` | `/v3/finance/transaction/list` |
| `actions` | GET `/v1/actions` |
| `category_tree` | `/v1/description-category/tree` |
| `category_attributes` | `/v1/description-category/attribute` |
| `attribute_values` | `/v1/description-category/attribute/values` |
| `attribute_search` | `/v1/description-category/attribute/values/search` |
| `import_info` | `/v1/product/import/info` |
| `pictures_info` | `/v2/product/pictures/info` |
| `action_candidates` | `/v1/actions/candidates` |

Пока не подключено: вопросы Ozon (метода нет в проверенной документации) и Ozon Performance (реклама Ozon; расходы видны в калькуляторе прибыли).

### Калькулятор прибыли

`GET /agent/api/calc?q=<код, название или штрихкод>` — то же, что вкладка «Калькулятор» в системе AMURA.

## Заявки на действия

```
POST /agent/api/requests
{"kind": "...", "shop": "wb", "params": {...}, "comment": "зачем"}
```

Ответ: `{"ok": true, "id": 12, "status": "new", "auto": false}`. Владелец получает сообщение в Telegram.

`GET /agent/api/requests[?status=new|done|error|rejected]` — заявки и их результат (`result` — ответ площадки).

| kind | магазин | params |
|---|---|---|
| `wb_feedback_answer` | WB | `id`, `text` (2–5000 символов) |
| `wb_question_answer` | WB | `id`, `text` |
| `wb_price` | WB | `nmID`, `price` (₸) и/или `discount` (%) |
| `wb_card_text` | WB | `nmID`, `title` и/или `description` (остальное в карточке не меняется) |
| `wb_card_update` | WB | `nmID` и хотя бы одно из полей: `characteristics` (список `{id, value}`, `id` из справочника `charcs` предмета), `dimensions` (`{length, width, height}` — целые, см; `weightBrutto` — кг), `title` (≤60 символов), `description`, `documents` — список `[{type, number, startDate, endDate}]` (`type`: 1 сертификат, 2 декларация, 3 СГР…; даты `ГГГГ-ММ-ДД`; бессрочный — `isEndless: true` без `endDate`; `id` — только чтобы изменить уже прикреплённый документ). Переданные документы **заменяют все** документы карточки; если `documents` не передан, текущие документы карточки отправляются как есть (с `id`), WB их не теряет. `excludeDocuments` через заявку не ставится. Цену, бренд, vendorCode, штрихкоды и фото менять нельзя — заявка получит `400`. Значения проверяются по справочнику: `charcType` 4 — одно число, 1 — список строк не длиннее `maxCount`, 0 — характеристика не используется. Остальные поля карточки не меняются. Прежняя версия изменённых полей сохраняется в `result.before`. |
| `wb_card_trash` | WB | `nmIDs` (список). Убирает карточки в корзину WB — это не удаление. Только через «Да». Вернуть — кнопка владельца «Вернуть как было» (`/content/v2/cards/recover`). WB сам удаляет карточки, пролежавшие в корзине больше 30 дней, если на них нет остатков. |
| `wb_card_create` | WB | `subjectID`, `vendorCode` (≤72), `title` (≤60), `sizes` — `[{skus: [EAN], techSize?, wbSize?}]`. Необязательные: `description`, `brand`, `dimensions` (как в `wb_card_update`), `characteristics` (`{id, value}` по справочнику `charcs`), `photos` (до 30 ссылок `https://` прямо на файл). **Цену передавать нельзя** — заявка получит `400`; цену ставит `wb_price` через «Да». EAN проверяется по контрольной цифре. Метод WB — `/content/v2/cards/upload` (создание асинхронное, синхронизация до 30 минут). Фото — `/content/v3/media/save`: сервер ждёт появления карточки до ~20 с; не появилась — в `result.photos` пояснение, фото пришлите заявкой `wb_card_media`, когда будет nmID (чтение `cards` или `card_errors`). Выполняется сразу по правилу «Карточки». |
| `wb_card_media` | WB | `nmID`, `photos` (1–30 ссылок `https://`). Метод WB заменяет **все** фото карточки — присылайте полный набор. Прежние ссылки сохраняются в `result.before`, у владельца есть «Вернуть как было». Правило «Карточки». |
| `wb_card_barcodes` | WB | `items` — `[{nmID, ean, chrtID?}]`, до 100 карточек в одной заявке. Дописывает заводской EAN в `sizes[].skus` (`/content/v2/cards/update`; WB разрешает баркоды только добавлять). **Удалить баркод через это действие нельзя**, баркоды WB 2040… остаются. `chrtID` нужен, только если у карточки несколько размеров. Сервер отказывает (`400`), если EAN неверный, уже есть у этой карточки или стоит на другой карточке кабинета. Владелец видит таблицу «nmID — название WB — EAN — товар МойСклад (код, название)» и жмёт одно «Да». **Всегда через «Да».** После выполнения пары «WB ↔ МойСклад» пересчитываются, остатки по этим товарам уходят со следующей отправкой. |
| `mp_noshow` | WB или Ozon | `id` — nmID (WB) или offer_id (Ozon), `hide` (`true` — не выставлять, по умолчанию; `false` — снова выставлять). Остаток товара в этом магазине уходит 0 со следующей сверкой. **Всегда через «Да».** Отдельно и автоматически: карточки WB с `needKiz: true` получают остаток 0, пока владелец не включил «Маркировка (КИЗ) подключена» на экране «Ozon и WB». |
| `wb_bid` | WB | `advert_id`, `nm_id`, `bid` (₸), `placement` (`combined` / `search` / `recommendations`) |
| `wb_campaign` | WB | `advert_id`, `action` (`start` / `pause` / `stop`) |
| `wb_budget_deposit` | WB | `advert_id`, `sum` (₸), `source` (0 — счёт, 1 — баланс, 3 — бонусы). **Всегда через «Да».** |
| `wb_promotion` | WB | `promotionID`, `nmIDs` (список). Сервер берёт плановую цену акции (`planPrice` из `promotions/nomenclatures`). Если она ниже «Мин. цены в акциях», заявка получит `400`. |
| `ozon_review_answer` | Ozon | `id`, `text` |
| `ozon_price` | Ozon | `offer_id` и хотя бы одно из полей: `price`, `min_price`, `auto_action_enabled`, `auto_add_to_ozon_actions_list_enabled`. Необязательные: `old_price`, `currency_code`. `min_price` — ниже этой цены Ozon не опустит цену в автоакциях; задаётся в ₸, сервер пересчитывает её, как `price`, и включает `min_price_for_auto_actions_enabled`. Флаги автоакций — `ENABLED` / `DISABLED` / `UNKNOWN`. Поле `auto_add_to_ozon_actions_list_enabled` в доступной нам SDK не описано — сервер передаёт его Ozon как есть. Заявка без `price` или с `min_price` / флагами — всегда через «Да». |
| `ozon_card_create` | Ozon | `offer_id`, `name`, `description_category_id`, `type_id`, `price`, `depth`, `width`, `height` (мм), `weight` (г), `attributes` (список `{id, values: [{value} \| {dictionary_value_id}]}`). Необязательные: `images`, `primary_image` (только `https://`), `barcode`, `old_price`, `vat`, `complex_attributes`. **Всегда через «Да»**: Ozon требует цену в том же запросе (`/v3/product/import`). Ответ — `task_id`, статус — чтение `import_info`. |
| `ozon_card_update` | Ozon | `offer_id`, `attributes` — `[{id, complex_id?, values: [{value} \| {dictionary_value_id}]}]`, `id` из `category_attributes`. Пример — rich-контент: `{id: 11254, values: [{value: "<JSON rich-контента строкой>"}]}`. Метод — `/v1/product/attributes/update`. Меняются только переданные характеристики. Отдельного поля для названия нет: название — тоже характеристика, её `id` берите из `category_attributes`. Выполняется сразу по правилу «Карточки». Прежние значения сохраняются в `result.before`, у владельца есть «Вернуть как было»; характеристика, которой раньше не было, при откате очищается (`values: []`). Ответ — `task_id`, статус — чтение `import_info`. |
| `ozon_pictures` | Ozon | `product_id`, `images` (1–30 ссылок `https://`), `color_image`, `images360`. Выполняется сразу по правилу «Карточки». |
| `ozon_promotion` | Ozon | `action_id`, `products` (список `{product_id, action_price, stock}`). `action_price` — в валюте кабинета. Если цена в ₸ ниже «Мин. цены в акциях», заявка получит `400`. |
| `ozon_promotion_exit` | Ozon | `action_id`, `product_ids` — выйти из акции (`/v1/actions/products/deactivate`). Через «Да». Такие заявки создаёт и сторож акций. |
| `price_floor` | WB или Ozon | `id` (nmID WB или offer_id Ozon), `floor` (₸; 0 — снять) — «Мин. цена в акциях». **Всегда через «Да».** Владелец задаёт её и сам на экране «На согласование». |

## Мин. цена в акциях

Слово владельца 30.10.2026: в акциях участвовать можно, но не ниже определённой цены.

- **Пол цены** хранится в системе AMURA, по товару и магазину, в ₸. Ставит владелец или заявка `price_floor` через «Да». Прочитать: `GET /agent/api/floor` → `floors` и `watch` (последняя проверка: `found`, `errors`).
- **Заявки в акции** `wb_promotion` / `ozon_promotion` с ценой ниже пола отклоняются (`400`). Повторно цена проверяется в момент «Да».
- **Сторож акций**: раз в сутки (9:00 по Алматы) или кнопкой владельца читает:
  - WB — `/api/v1/calendar/promotions` и `promotions/nomenclatures` (`inAction=true`);
  - Ozon — `/v1/actions` и `/v1/actions/products`.

  Если товар в акции дешевле пола:
  - владельцу — Telegram и push;
  - по Ozon — заявка `ozon_promotion_exit` (одна на акцию, пока открыта);
  - по WB — тревога «выйдите в кабинете WB»: API выхода из акции у WB нет.
- **Автоакции WB** сторож не видит: метод `nomenclatures` для них неприменим (документация WB).

## Валюта Ozon

Все цены в заявках Ozon (`ozon_card_create`, `ozon_price`) — в тенге, как во всей системе AMURA. Сервер сам узнаёт валюту кабинета по `currency_code` в `/v5/product/info/prices`; если товаров ещё нет — берёт `OZON_CURRENCY` на Render. Если кабинет не в тенге, сервер пересчитывает цену по курсу покупки МиГ (рубли Ozon продаём обменнику — слово владельца 31.10.2026) (запасной — НБ РК), как калькулятор. В `result` заявки записываются обе суммы — `price_kzt` и `price` с `currency` — и курс (`note`).

Можно сразу прислать цену в валюте кабинета — тогда передайте `currency_code` (например `RUB`), и пересчёта не будет.

## Правила самостоятельности

Решение владельца 28.10.2026: правила включены по умолчанию. Заявка по правилу выполняется сразу, ответ — `"auto": true`.

- **Ответы на отзывы 4–5★** (WB и Ozon). Оценку сервер проверяет сам: WB — `/api/v1/feedback`, Ozon — `/v1/review/info`. Отзывы 1–3★ — всегда через «Да».
- **Ответы на вопросы WB.**
- **Карточки WB** (`wb_card_update`, слово владельца 29.10.2026; `wb_card_create`, `wb_card_media`, `ozon_card_update` — 30.10.2026). У изменений и фото у владельца есть кнопка «Вернуть как было»: прежняя версия отправляется обратно.
- **Ставки WB в пределах дневного бюджета магазина.** Бюджет владелец задаёт по каждому магазину; пока он 0, ставки идут через «Да». Ставка выполняется сразу, только если у кампании включён дневной лимит и он не выше бюджета.

Всегда через «Да»:
- пополнение бюджета;
- запуск и остановка кампаний;
- цены и скидки;
- акции;
- тексты карточек;
- штрихкоды карточек WB (`wb_card_barcodes`);
- «не выставлять» товар (`mp_noshow`);
- мин. цена в акциях (`price_floor`), выход из акции Ozon (`ozon_promotion_exit`), `ozon_price` с `min_price` или флагами автоакций.

Кнопка владельца «Стоп» отключает всё автоматическое: каждая заявка ждёт «Да».

Раз в день, в 21:00 по Алматы, владелец получает в Telegram сводку: что сделано без «Да» за сутки.

## Цены в коридоре

Правило владельца, по умолчанию выключено. Включает его владелец переключателем на экране «На согласование».

Заявка `wb_price` или `ozon_price` выполняется сразу, если выполнены все условия:
- правило включено и нет «Стоп»;
- новая цена для покупателя внутри коридора;
- шаг от текущей цены не больше 5 %;
- цену этого товара не меняли последние сутки;
- у WB не меняется скидка.

Иначе заявка идёт на «Да», а в описании заявки написано почему.

**Границы коридора:**
- **нижняя** — минимальная цена калькулятора для маржи 20 % на живых данных: ставки кабинета, себестоимость, ДРР. Для товара моложе 30 дней с первой продажи — 15 %; оба процента — настройки владельца. Если в расчёте есть строки «нет данных», граница неполная, и без «Да» цена не меняется;
- **верхняя** — базовая цена +15 %. Базовую задаёт владелец; если не задал — берётся цена на момент первого расчёта.

Каждый день в 9:00 сервер пересчитывает коридоры. Если нижняя граница стала выше текущей цены, он создаёт заявку на повышение и отправляет её владельцу на «Да» — сам цену не поднимает.
