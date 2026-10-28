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
| `wb_bid` | WB | `advert_id`, `nm_id`, `bid` (₸), `placement` (`combined` / `search` / `recommendations`) |
| `wb_campaign` | WB | `advert_id`, `action` (`start` / `pause` / `stop`) |
| `wb_budget_deposit` | WB | `advert_id`, `sum` (₸), `source` (0 — счёт, 1 — баланс, 3 — бонусы). **Всегда через «Да».** |
| `wb_promotion` | WB | `promotionID`, `nmIDs` (список) |
| `ozon_review_answer` | Ozon | `id`, `text` |
| `ozon_price` | Ozon | `offer_id`, `price`, `old_price` |
| `ozon_promotion` | Ozon | `action_id`, `products` (список `{product_id, action_price, stock}`) |

## Правила самостоятельности

Решение владельца 28.10.2026: правила включены по умолчанию. Заявка по правилу выполняется сразу, ответ — `"auto": true`.

- **Ответы на отзывы 4–5★** (WB и Ozon). Оценку сервер проверяет сам: WB — `/api/v1/feedback`, Ozon — `/v1/review/info`. Отзывы 1–3★ — всегда через «Да».
- **Ответы на вопросы WB.**
- **Ставки WB в пределах дневного бюджета магазина.** Бюджет владелец задаёт по каждому магазину; пока он 0, ставки идут через «Да». Ставка выполняется сразу, только если у кампании включён дневной лимит и он не выше бюджета.

Всегда через «Да»:
- пополнение бюджета;
- запуск и остановка кампаний;
- цены и скидки;
- акции;
- тексты карточек.

Кнопка владельца «Стоп» отключает всё автоматическое: каждая заявка ждёт «Да».

Раз в день, в 21:00 по Алматы, владелец получает в Telegram сводку: что сделано без «Да» за сутки.
