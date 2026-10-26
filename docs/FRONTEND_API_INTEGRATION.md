# AutoTerra Backend — документация для интеграции фронтенда

> Обновление pricing: [скидки текущего заказа и POST /api/orders/quote/](../ORDER_DISCOUNTS.md). Итоги и progress корзины берите с backend; effectiveDiscountPercent отражает фактическую скидку от прайса.

Дата сверки: **14 октября 2026**. Репозиторий: `sattarzhanovdev/autoterra-backend`, ветка `adilhan_new`, commit `4555a42823b092005a546c7c79e194146a3f83a0`.

Документ описывает код этого commit, а не подтверждённую конфигурацию production. Обновления сервера не выполнялись. URL production для клиента: `https://autoterra.shop/api/`; локально обычно `http://127.0.0.1:8000/api/` при `DEBUG=True`. Все примеры ID и данных ниже условные — подставляйте значения из ответов API.

## Содержание

1. [Общие правила и авторизация](#общие-правила-и-авторизация)
2. [Регистрация, вход, профиль и восстановление](#регистрация-вход-профиль-и-восстановление)
3. [Каталог, цены и настройки клиентов](#каталог-цены-и-настройки-клиентов)
4. [Заказы и оплата](#заказы-и-оплата)
5. [Покупки, колеровка и доставка](#покупки-колеровка-и-доставка)
6. [Дистрибьютор и импорт 1С](#дистрибьютор-и-импорт-1с)
7. [Менеджер, администратор и отчёты](#менеджер-администратор-и-отчёты)
8. [ИИ, карточки и документы знаний](#ии-карточки-и-документы-знаний)
9. [Рефералы, бонусы, обучение и уведомления](#рефералы-бонусы-обучение-и-уведомления)
10. [Серверные интеграции 1С и финансы](#серверные-интеграции-1с-и-финансы)
11. [Примеры TypeScript и curl](#примеры-typescript-и-curl)
12. [Полная карта маршрутов](#полная-карта-маршрутов)
13. [Структуры объектов](#структуры-объектов)
14. [Справочник каждого endpoint](#справочник-каждого-endpoint)
15. [Проверка интеграции](#проверка-интеграции)

## Общие правила и авторизация

### URL, HTTP и формат данных

- Все прикладные маршруты начинаются с `/api/`. Заканчивающий `/` обязателен в примерах: на редиректы для POST/файлов не рассчитывайте.
- Для JSON: `Content-Type: application/json`, `Accept: application/json`.
- Корневое тело JSON обычно **объект**. Только `integration/erp/stock-update/` и `integration/erp/catalog-sync/` принимают JSON-массив на верхнем уровне.
- Для файлов используйте `multipart/form-data`. При `FormData` браузер сам задаёт `Content-Type` с boundary — вручную его не устанавливайте.
- Обычная авторизация: `Authorization: Bearer <token>`. Cookies/session login не заменяют этот токен в прикладном API.
- API не использует DRF-обёртки. Ответы бывают `{results: [...]}`, `{order: ...}`, `{task: ...}`, одиночным объектом, бинарным файлом или текстом. Не приводите все ответы к одной форме.
- ID в большинстве прикладных DTO — **строки**. Финансовые отчёты и некоторые служебные ответы используют числовые ID. Сохраняйте тип поля конкретного ответа; в URL подставляйте числовой ID, не `ORD-00012`.
- Даты/время — ISO 8601; date-only — `YYYY-MM-DD`. Время `courierArriveUntil` — `HH:MM`. `dateJoined` эксперта — исключение: `DD.MM.YYYY`.
- Часовой пояс backend: `Asia/Bishkek`; хранение timezone-aware. Для отображения переводите дату в часовой пояс интерфейса.
- Цена в `Product`/`Order`/`Purchase` обычно JSON number; скидки, наценки, персональные цены и финансовая сверка часто decimal-string. Не объединяйте их без явного преобразования. `null`, пустая строка и `0` имеют разные значения.
- Остаток `Product.quantity` и количество заказа — целые числа. Материалы рецепта Color Lab могут иметь дробное количество.

### Bearer-токен и роли

Токен — непрозрачная случайная строка, **не JWT**. Его нельзя декодировать для определения роли или срока действия. В коде нет отдельного endpoint refresh/logout и автоматического TTL AuthToken; локальный выход очищает токен на фронтенде. Сброс пароля и удаление аккаунта отзывают токены. Проверку действительности выполняет сервер при каждом запросе.

| Роль в `/login/` и `/me/` | Внутренняя роль | Назначение |
|---|---|---|
| `autoservice` | `client` | Клиент, включая магазин категории `s` |
| `distributor` | `distributor` | Дистрибьютор |
| `courier` | `courier` | Курьер |
| `expert` | `ai_expert` | Эксперт ИИ/базы знаний |
| `manager` | `manager` | Менеджер закреплённых регионов |
| `admin` | `admin` / superuser | Глобальный администратор |

Клиентский бизнес-API требует `client.status=active`, активного пользователя и активного дистрибьютора. Полученный при регистрации/входе токен ещё не означает доступ к заказам. Состояния клиента: `new`, `under_review`, `active`, `blocked`, `archived`. Для заблокированного/ожидающего клиента доступ к `/me/` нужен для показа состояния аккаунта.

Матрица прав задаётся **конкретным endpoint**, а не только названием URL. Например, персональные цены меняют менеджер/admin; общую и брендовые скидки, статус и тип клиента — дистрибьютор/admin; менеджер имеет лишь чтение этих настроек. Курьер подтверждает наличные только в назначенной ему задаче, даже если глобальный администратор имеет доступ к просмотру.

### Пагинация

Для endpoints со стандартным списком:

```json
{
  "results": [],
  "pagination": {"page": 1, "pageSize": 20, "count": 0, "totalPages": 1, "hasNext": false, "hasPrevious": false},
  "count": 0
}
```

Query: `page` от 1; `page_size` (alias `pageSize`) default 20, максимум 100. Legacy `limit`/`offset` также поддерживаются; при переданном `offset` он определяет номер страницы. Это постраничная совместимость: `offset=25&limit=20` соответствует странице 2, а не произвольному срезу с элемента 25. Неверные/отрицательные размеры заменяются default; номер за пределами возвращает последнюю страницу. Останавливайте загрузку по `pagination.hasNext`, а не по тому, пустой ли массив. Смена фильтров сбрасывает страницу и ранее накопленные результаты.

Исключения: `/products/quote/` без пагинации; `order-config` возвращает справочники; unified-профиль ограничивает вложенные списки через legacy `limit`/`offset`; финансы используют собственные `page`/`pageSize=100`, а не объект `pagination`.

### Ошибки

| HTTP | Действие фронтенда |
|---|---|
| 400 | Показать ошибку данных/бизнес-правила; не повторять автоматически |
| 401 | Токен отсутствует/недействителен; очистить сессию и показать вход |
| 403 | Нет роли/прав или аккаунт ограничен; сохранить сессию, показать причину |
| 404 | Объект отсутствует либо скрыт областью доступа |
| 405 | Неверный метод; Django может вернуть не-JSON ответ и заголовок Allow |
| 409 | Конфликт состояния/дубль/незавершённый платёж; обновить объект |
| 500 | Ошибка сервера; сохранить понятное сообщение без показа HTML/stack trace |
| 502/503 | Зависимый сервис временно недоступен; дать повторную попытку |

Обычно тело `{ "detail": "..." }`, иногда дополнительно `code`, `status`, `errors`. Покупки используют также `{errors: {...}}` или `{error: "duplicate_detected", message: "..."}`. Для сообщения используйте `detail → message → errors → HTTP status`. `errors` бывает объектом валидации или массивом предупреждений. `200` импорта с непустым `errors` — частичный успех, а не повод отменить все результаты.

Пример ограничения аккаунта:

```json
{"detail":"Ваша регистрация получена. После проверки менеджером мы уведомим вас.","code":"account_not_active","status":"new"}
```

Ответ 404 из `get_object_or_404`, 405 от декоратора, ошибка reverse proxy и HTML от внешних сервисов могут не быть JSON. Проверяйте `Content-Type` перед декодированием.

### CORS и файлы

Разрешённые origin задаёт сервер через `CORS_ALLOWED_ORIGINS`. `DEBUG=True` разрешает все origin; production зависит от конфигурации. Поддержаны GET/POST/PUT/PATCH/DELETE/OPTIONS; `Authorization` и `X-Integration-Token` разрешены CORS middleware. Для отдельного фронтенд-домена проверьте preflight. Exposure `Content-Disposition` специально не настроен: cross-origin код не должен считать его всегда доступным; предусмотрите своё имя скачиваемого файла.

`media/…`, `/media/…` и абсолютные URL вложений нормализуйте относительно **origin backend**, а не текущего фронтенда или пути `/api/`. Фотографии товара (`images`) — прямые http(s)-URL внешних серверов; возможны недоступность, HTTP mixed content и ошибки загрузки. Показывайте placeholder.

## Регистрация, вход, профиль и восстановление

### Регистрация клиента

`POST /api/auth/register/` (alias `/api/register/`), без Bearer. Регистрация создаёт клиента и его первую торговую точку, не произвольную роль.

| Поле | Формат / обязательность |
|---|---|
| `username` | Обязательная строка: номер телефона/логин |
| `password` | Обязательная строка, минимум 8 символов |
| `inn` | 10 или 12 цифр; сервер убирает нецифровые символы |
| `region_id` | ID активного региона с активным дистрибьютором |
| `company_name`, `contact_name` | Непустые строки |
| `store_address` | Непустая строка: адрес точки |
| `email` | Обязательный корректный email |
| `category` | `a`/`b`/`c`/`s`, default `b` |
| `termsAccepted`, `personalDataConsent` | Именно JSON boolean `true` |
| `referralCode` | Необязательный код приглашения; alias `ref` |

```json
{"username":"+996555123456","password":"ExamplePass123","inn":"1234567890","region_id":"1","company_name":"Пример СТО","contact_name":"Иван","store_address":"Бишкек, ул. Примерная, 10","email":"client@example.com","category":"b","termsAccepted":true,"personalDataConsent":true}
```

201: `{status:"success", token, client:Client, requires_approval:true, pendingReferral:PendingClaim|null}`. Новая регистрация получает `new`, при таком же ИНН в другом регионе — `under_review`. Дубли телефона или ИНН в том же регионе: 409 (`phone_duplicate`, `inn_duplicate`, `integrity_error`). Не показывайте рабочий каталог, пока `/me/` не вернёт активный статус.

Категории: `a` дилерский салон; `b` автосервис с кузовным цехом; `c` гаражный сервис; `s` магазин. Категория **не определяет закупочную цену**.

### Вход и профиль

`POST /api/login/`: `{"phone":"+996555123456","password":"ExamplePass123"}`. В поле `phone` допустим текстовый username для служебных аккаунтов. 200: `{token, user:{id, phone, role, status, distributor?}}`. Для эксперта role=`expert`, для клиента=`autoservice`. 401 — неверные данные; 403 — профиль не создан/неизвестная роль.

`GET /api/me/` (alias `/api/auth/me/`) возвращает профиль непосредственно, **без обёртки user**. Клиент: `{id, phone, role, status, client, distributor}`. Дистрибьютор: `{id, phone, role, status, distributor}`. Курьер/менеджер/admin: `{id, phone, role, status}`. Эксперт дополнительно: `username`, `email`, `stats:{approvedCards,answeredTickets,rating}`, `specialty`, `expertId`, `region`, `dateJoined`.

### Восстановление и удаление

`POST /api/auth/password-reset/`: `phone` (alias `username`), `inn`, `new_password` (alias `password`), затем `code`. Уже **первый** запрос без code требует новый пароль длиной ≥8 и ИНН 10/12 цифр. Он возвращает `{status:"code_sent", detail}` и отправляет код на email аккаунта. Второй запрос с теми же полями и code возвращает `{status:"success",detail:"Пароль обновлен"}`. Код действует 15 минут. Восстановление реализовано для клиента/дистрибьютора по их ИНН; для других ролей нельзя обещать этот сценарий. После успеха все AuthToken удалены — нужен повторный вход.

`POST /api/auth/delete-account/` с Bearer и `{}` возвращает `{status:"success",detail:"Аккаунт удалён"}`. Это деактивация и обезличивание аккаунта с сохранением учётных документов, а не физическое удаление всех заказов. После ответа очистите токен и push-регистрацию на устройстве.

## Каталог, цены и настройки клиентов

`GET /api/dashboard/` — клиентская главная: `client`, `distributor`, `unreadCount`, `recentPurchases`, `activeColorRequests`, `referralSummary`, `bonusBalance`, `pendingReferral`.

`GET /api/products/`: пагинация Product и `categories`. Фильтры: `category` (точное значение), `brand` (точное), `search`, `inStock=true`. «Под заказ» (`onOrder`) входит в доступный ассортимент даже при нулевом остатке. Поиск общий для названия, SKU и синонимов через серверный search service.

`GET /api/products/quote/?ids=1,2,3`: актуальные цены и остатки, от 1 до 200 числовых ID длиной ≤18 цифр. Ответ `{results:Product[]}`. Недоступные/чужие ID не появляются в results; перед оформлением сверяйте наличие всех ожидаемых ID. Не подменяйте этим запросом подтверждение сервером заказа.

`GET /api/order-config/`: `client`, `distributor`, `stores`, `categories`, `brands`, `bonusBalance`. Всего каталога в ответе **нет**: товары подгружаются через `/products/`.

`GET /api/stores/` — точки текущего активного клиента. `POST /api/stores/` с `{name,address}` → 201 `{store:Store}`. `/api/stores/{store_id}/`: PUT/PATCH требуют **оба** `name` и `address`; DELETE деактивирует точку и возвращает `{ok:true}`. GET detail здесь не реализован.

### Серверная цена

Приоритет: активная персональная цена товара → брендовая скидка → общая персональная скидка клиента → ранговая скидка → базовая цена. Скидки не складываются с персональной ценой. Поле `priceSource`: `personal`, `brand_discount`, `personal_discount`, `rank`, `base`. Каталог клиента возвращает финальную `price`; `basePrice`, `discountPercent`, `hasDiscount`, `personalPrice`, `priceSource` дополняют её. Фронтенд не вычисляет цену заказа из базовой цены самостоятельно.

| Endpoint | Доступ / изменение |
|---|---|
| `/clients/{client_id}/settings/` GET/PATCH | Свой дистрибьютор/admin чтение и запись; региональный менеджер только чтение. PATCH: `status` и/или `category`; никаких иных ключей |
| `/clients/{client_id}/discount/` GET/PATCH | Та же матрица. PATCH строго `{personalDiscountPercent:"10.00"}` или `null`; 0–40%, до 2 знаков |
| `/clients/{client_id}/brand-discounts/` GET/PATCH/DELETE | Та же матрица. Query `search`, `discountsOnly=true`. PATCH `{discounts:[{brand:"HiQ",percent:"5.00"}]}`, 1–200 уникальных нормализованных брендов. DELETE `{brand:"HiQ"}` |
| `/clients/{client_id}/prices/` GET | Менеджер/admin доступны по региону; дистрибьютор читает своих клиентов. Query `overridesOnly` default `true`, `search` |
| `/clients/{client_id}/prices/{product_id}/` PUT/PATCH/DELETE | Запись только менеджер/admin. `{price:"500.00",isActive:true}`; положительная цена ≤9 999 999 999,99, 2 знака. Первый override → 201, обновление → 200. DELETE возвращает `status` и `item` с текущей ценой без override |
| `/clients/{client_id}/cash-permission/` GET/PATCH | Дистрибьютор/admin, без права менеджера. PATCH строго `{cashPaymentAllowed:true}` |
| `/me/markup/` GET/PATCH | Только активный клиент категории `s`. PATCH строго `{markupPercent:"30.00"}`, 0–9999.99%, ≤2 знака |

`null` общей скидки означает использовать следующие правила; `0` — явная нулевая скидка и остановка соответствующего fallback. DELETE брендовой скидки возвращает fallback. `canEdit` в ответах должен управлять доступностью UI. Наценка магазина информационная: `salePrice = finalPurchasePrice × (1 + markupPercent / 100)` для отображения; она не меняет `price`, заказ и сумму к оплате.

## Заказы и оплата

### Создание и изменение

Активный клиент: `GET /api/orders/`, `POST /api/orders/create/`. Создание:

```json
{"storeId":"1","deliveryMethod":"courier","paymentMethod":"online","comment":"Позвоните перед доставкой","items":[{"productId":"10","quantity":2}]}
```

`items` — непустой массив. `productId` числовой ID своего активного товара; `quantity` целое 1–1 000 000. Повторяющиеся productId суммируются, общий лимит на товар сохраняется. `storeId` необязателен; переданный ID должен быть активной точкой клиента. Способ доставки: `courier` (default) или `self_pickup`. Оплата: `online` (default) или `cash`. Cash требует разрешения клиента и courier. Не передавайте `price`/`totalAmount` как авторитетные значения — сервер берёт актуальную цену и резервирует товар.

201 `{order:Order}`. `outOfStock` нельзя заказать; для `onOrder` допускается количество сверх остатка. Ошибка товара/количества/суммы/остатка — 400; запрет cash — 403.

`GET /api/orders/{order_id}/`: владелец-клиент, свой дистрибьютор, менеджер региона или admin. Может вернуть 503 при невозможности сверить pending-платёж. Обновляйте карточку после возвращения из оплаты.

`POST /api/orders/{replace_order_id}/replace/` принимает **тот же полный** payload создания. Замена атомарна: новый заказ создаётся, старый отменяется и резервы возвращаются; ошибка нового заказа сохраняет старый. Не делайте на фронтенде отдельный cancel перед create. Pending-платёж → 409.

`POST /api/orders/{order_id}/cancel/` — владелец до оплаты/отправки в разрешённом состоянии. POST `{}`; успешный ответ `{status:"success",order:Order}`. Учитывайте оба поля ответа.

### Статусы и действия

| Статус | Смысл / дальнейшее действие |
|---|---|
| `new` | Ожидает оператора: confirm/adjust/reject; клиент может cancel |
| `adjusted` | Клиент должен принять корректировку или отменить |
| `confirmed` | Доступна онлайн-оплата; возможна отмена; разрешён выбор cash при условиях |
| `accepted` | Legacy: не новый основной сценарий |
| `paid` | Оплачен; оператор отправляет |
| `shipped` | Отправлен; доставка может завершить в fulfilled |
| `fulfilled`, `rejected`, `cancelled` | Завершённые состояния |

Оператор — свой дистрибьютор/admin:

- POST `confirm/`, body `{}` → `{order}`.
- POST `adjust/`: `{items:[{itemId:"25",quantity:1}],newItems:[{productId:"11",quantity:2}],reason:"Замена позиции"}`. Для существующей позиции `quantity=0` удаляет её. Ответ `{order}` со списком `adjustments`.
- POST `reject/`, `{reason:"Нет товара"}` → `{order}`.
- POST `ship/`, `{}` → `{order}`. Онлайн-заказ должен быть оплачен; наличный имеет отдельное исключение отправки после подтверждения.
- `/distributor/orders/{order_id}/status/` POST/PATCH — legacy изменение, принимает только `rejected` или `fulfilled` и проверяет переход. Не используйте его вместо confirm/ship/pay.

Клиент POST `accept-adjustment/`, `{}` переводит adjusted → confirmed. При pending/waiting_for_capture платеже изменения могут давать 409: дождитесь результата, а не повторяйте создание платежа/заказа.

### Онлайн-оплата

`POST /api/orders/{order_id}/pay/`, клиент-владелец, confirmed и online. Payload: `{}` либо `{useBonus:true}` (списать доступное) либо `{useBonus:"100.00"}`. 201:

```json
{"payment":{"id":"5","orderId":"12","provider":"yookassa","amount":900.0,"currency":"RUB","status":"pending","confirmationUrl":"https://example.com/payment","providerPaymentId":"provider-example","paidAt":null,"createdAt":"2026-10-14T10:00:00+06:00"},"bonusApplied":100.0}
```

Точные поля Payment указаны в разделе структур. Откройте `confirmationUrl`, затем перечитайте заказ. **Возврат на return URL не доказывает оплату**: сервер проверяет провайдера/webhook. Повтор pay использует pending-платёж; общую автоматическую повторную отправку всех POST не включайте. Если бонусы/скидка закрывают всю сумму, payment может сразу быть succeeded и confirmationUrl отсутствует — не открывайте пустую ссылку.

Webhooks `/payments/yookassa/webhook/` вызывает платёжная система, не мобильный клиент. `/payments/yookassa/return/` — пользовательская HTML-страница возврата, не JSON API проверки платежа.

### Наличные

`POST /orders/{order_id}/cash/`, body `{}`: клиент-владелец либо дистрибьютор/admin в области заказа. Условия: confirmed, courier, положительная сумма, разрешён cash у клиента, нет online-платежа/pending/succeeded и списанных бонусов. 200 `{order}`; конфликты → 409. Повтор уже выбранного cash идемпотентен.

Назначенный водитель: POST `/courier/tasks/{task_id}/collect-cash/`, `{}`. Это не произвольный ввод суммы: сервер берёт сумму заказа. Задача delivery должна быть in_progress, заказ shipped и cash. Ответ `{task}`. До подтверждения наличных `delivered` блокируется; повтор подтверждённого действия не удваивает учёт.

## Покупки, колеровка и доставка

### Покупки вне заказа

`GET /purchases/`: клиентский пагинированный список Purchase с `stats`. `POST /purchases/create/` принимает JSON либо multipart.

Обязательные поля: `document_number` (строка), `date` (ISO), `amount` (число/decimal-string). Необязательное `items`: массив `{sku,name,quantity,price}`; в multipart — JSON-строка. Для документа используйте `document`; также поддержаны общие поля attachments. 201 `{purchase:Purchase}`. Новая покупка создаётся в `new`, хотя модель имеет также default `pending_verification`: не предполагайте один стартовый статус во всех источниках. Дубликат номера+даты+суммы клиента → 400 `duplicate_detected`.

Дистрибьютор/admin: GET `/distributor/purchases/`, фильтры `status`, `to_verify=true`; PATCH/POST `/{purchase_id}/verify/` с `{status:"verified"}` или `{status:"rejected",rejection_reason:"Причина"}` → `{purchase}`.

### Общие вложения

Для purchase, ticket, Color Lab и proof принимаются поля `attachments`, `document`, `file`, `photo`, `proof`, `files`; допускается повторение одного поля. Файл ≤10 МиБ; расширения `.jpg`, `.jpeg`, `.png`, `.webp`, `.pdf`, `.mp4`, `.mov`. Проверяется конкретный MIME, отсутствующий/generic `application/octet-stream` допускается этим общим механизмом. Ошибки: `invalid_file_type`, `file_too_large`. Документы знаний используют **другие** правила, см. ниже.

### Color Lab

Клиент: GET `/color-requests/?active=true`; POST `/color-requests/create/` JSON/multipart. Рекомендуемый минимальный UI: `carBrand`, `colorCode`, `paintType`, `urgent`, `transferMethod`, контакты и адрес. `paintType`: `acrylic`, `baseClear` (default), `threeStage`. Не путать с клиентской категорией.

Поддержаны также `carModel`, `carYear` (≤4 символа), `vin`, `colorName`, `paintTypeNote` (≤255), `comment`, `pickupAddress`, `pickupTime`, `courierArriveUntil`, `contactPerson`, `contactPhone`. Aliases адреса/времени/контакта приведены в справочнике. `transferMethod`: `courier` или `self_delivery`. `pickupTime` ISO datetime; `courierArriveUntil` `HH:MM`. `urgent` — JSON bool, в multipart строковое значение, распознаваемое сервером. SLA создаётся 4 часа для срочной заявки, 24 часа для обычной.

201 `{request:ColorRequest}`. POST `/{request_id}/update/` разрешён владельцу только в created; POST `cancel/` — также только created, ответ `{status:"success"}`. Сервер не выполняет одинаково строгую проверку всех полей каждого legacy endpoint: фронтенд должен предлагать валидные перечисления и заполнять бизнес-обязательные поля формы, даже где код допускает пустую строку.

Дистрибьютор/admin: `/distributor/color-requests/` фильтры `status`, `active=true`; POST `/{request_id}/status/` с `status`, `recipe`, `comment`. Choices модели: `created`, `inProgress`, `ready`, `delivered`. Endpoint отмены дополнительно записывает `cancelled`, хотя это значение отсутствует в choices; UI должен уметь отображать отменённую заявку. В части старых фильтров ещё встречаются legacy-наименования: не создавайте новые UI-статусы по ним. Возврат готовой краски курьером не является автоматическим закрытием Color Lab; выдача оформляется отдельным статусом заявки.

### Доставка и сборка

Клиент GET `/courier-tasks/?active=true`; POST `/courier-tasks/create/` с `type`, `address`, `timeSlot`, `scheduledTime`, `contactName`, `contactPhone`, `carDescription`, `comment`. Типы: `delivery`, `pickup`, `color_lab_pickup`. 201 `{task}`. Клиентские `update/` и `cancel/` доступны только created и без связанного order; для связанной доставки нужно менять заказ. `proof/` — POST multipart для вложений **клиента**, не замена курьерскому `proof_photo`.

Дистрибьютор/admin: GET `/distributor/delivery-tasks/`; POST `/{task_id}/status/` с `status`, `courierId`, `reason`. Курьер: GET `/courier/tasks/` (свои; admin все), фильтр `status`; PATCH/POST `/{task_id}/status/` с `status`, **`courier_comment`** (snake_case), multipart **`proof_photo`**.

Последовательность order-delivery:

1. Назначить активного курьера: дистрибьютор через свой endpoint или admin POST `/courier/tasks/{task_id}/assign/` с `{courierId:"7"}`.
2. Водитель отмечает каждую позицию POST `/courier/tasks/{task_id}/items/{item_id}/pick/`, body `{}`. `item_id` — ID позиции OrderItem, не Product.
3. После `allItemsPicked=true` перевести assigned → in_progress. Для связанного заказа это также может выполнить paid/confirmed-cash → shipped.
4. Для cash подтвердить collect-cash.
5. Перевести in_progress → delivered. Связанный shipped-заказ становится fulfilled.

Статусы CourierTask: `created`, `assigned`, `in_progress`, `delivered`, `returned`, `cancelled`. Завершённую задачу нельзя произвольно вернуть назад. Нет прав на чужую задачу, нет сборки или не подтверждены наличные → 403/409/400 с причиной. Не перепрыгивайте шаги на клиенте.

## Дистрибьютор и импорт 1С

Главная: `/distributor/dashboard/` → `{metrics:{clients,purchasesToVerify,ordersToProcess,deliveriesToAssign,colorLabPending}}`. Клиенты `/distributor/clients/?search=...`; couriers `/distributor/couriers/`; заказы `/distributor/orders/` (search, status, client, region, distributor, dateFrom/dateTo); отчёт `/distributor/reports/`.

`/distributor/profile/` GET/PATCH: менять только `name`, `email`, `phone` (непустые строки). Телефон 7–15 цифр, допускаются `+ () -` и пробелы. ИНН и регионы через этот API не редактируются. Для admin нужен query `distributorId`; у дистрибьютора определяется его собственный профиль.

Ассортимент GET `/distributor/stock/`, фильтры `search`, `category`, `clientId` (для расчёта цены выбранного своего клиента). Ответ пагинированный Product и `categories`.

### Ручная и JSON-загрузка

POST `/distributor/stock/add/`: обязательный `sku`; `name`, `category`, `brand`, `price`, `quantity`, `status`, `volume`, `description`, `external_id`, `images`, `videoUrl`, `synonyms`, `isActive`. Дубликат SKU своего дистрибьютора → 400. Ответ **200**, `{status:"ok",product:Product}`.

POST `/distributor/stock/upload/`: `{items:[{sku,name,price,quantity,images,...}]}`. SKU обязателен; количество целое 0–1 000 000. Обновление по своему distributor+sku. `isActive` и `videoUrl` — camelCase, `external_id` — snake_case. Ошибка строки добавляется в `errors`, остальные продолжаются. Возвращает `status`, `processed`, `created`, `updated`, `errors`; HTTP 200 при хотя бы одной обработанной позиции, иначе 400.

**Особенность прав:** stock/add, stock/upload, stock/upload-file отклоняют admin с сообщением `Admin must specify distributorId`, но эти три обработчика не реализуют выбор distributorId. Для этого сценария используйте аккаунт дистрибьютора или серверную админку; добавление query/body ID само по себе не включает импорт от admin.

### Excel Wildberries / обычный / 1С

POST `/distributor/stock/upload-file/`, multipart поле `file`, `.xlsx`. Имя файла не определяет формат; шапка определяется по названиям колонок. Лист `Товары`/`Products`/`Товар` приоритетный, иначе active. WB/обычные файлы сохраняют существующую цену/остаток, если соответствующих колонок нет.

Для реального прайса 1С:

| Excel | Product / API |
|---|---|
| `Номенклатура` | `name` |
| `Код` | `sku`, основной ключ, сохраняются ведущие нули |
| `Артикул` | `wb_article` в модели; `wbArticle` в API |
| `Цена` | `price`, округление до 2 знаков |
| `Остаток` | `quantity`, целые доступные единицы |
| `Фото` | `images`, до 15 уникальных http(s)-ссылок |
| Верхний раздел | `group_name` / `groupName` |
| Ближайший раздел | `category` |

Шапка может быть разделена на несколько строк; папки определяются по Excel outline, даже если у папки заполнен код, цена или агрегатный остаток. Они не создаются как товары. `Источник фото` и `Кандидат фото` не заменяют `Фото`. Несколько ссылок: `;`, запятая или новая строка. Некорректные значения и превышение фото дают предупреждения.

Дробные остатки **округляются вниз** по принятому решению: `2.656 → 2`, `0.971 → 0`, с сообщением, номером Excel-строки и SKU. Тип Product.quantity не менялся. Цена/остаток/код с ошибкой, NaN/Infinity, отрицательные/превышающие диапазон значения — строка пропускается; остальные загружаются. Повторная загрузка обновляет по distributor+sku, не по дополнительному артикулу.

```json
{"status":"ok","created":0,"updated":10,"processed":10,"errors":["Строка 19 (00023104): дробный остаток 0.25 округлён вниз до 0 целых единиц."]}
```

Серверный обработчик Excel не задаёт свой отдельный максимальный размер файла; ограничения request/proxy зависят от окружения. Не переносите сюда 25 МиБ документов знаний. Для длинного импорта предусмотрите таймаут клиента и UI ожидания; повтор POST после сетевого таймаута сначала сверяйте с ассортиментом.

## Менеджер, администратор и отчёты

Менеджер получает доступ только к назначенным регионам; admin глобально. Не используйте название маршрута `/admin/` как доказательство admin-only: некоторые analytics/integration endpoints доступны региональному менеджеру.

- `/manager/dashboard/` и `/admin/analytics/`: показатели; query region/distributor, у admin-analytics также период `dateFrom`, `dateTo`.
- `/manager/clients/` GET: search, status, category, region, стандартная пагинация + `pendingCount`. POST: `inn`, `name`, `regionId`, `phone`, `email`, `category`, `city`, `contact`, `distributorId`; обязательны корректный ИНН, название и разрешённый активный регион. ID distributor должен соответствовать региону.
- `/manager/clients/{client_id}/unified/` GET: Client, stats и вложенные purchases/orders/colorRequests/tickets/referrals. Доступен также своему дистрибьютору. Для полноценных длинных списков используйте отдельные orders/purchases endpoints, не ограниченный unified.
- `/manager/clients/{client_id}/status/` POST/PUT/PATCH: строго `{status:"active"}`; фактическая запись доступна своему дистрибьютору/admin, менеджеру запрещена.
- `/manager/clients/{client_id}/remove/` DELETE/POST: `{confirmName:"Точное название"}`. DELETE физически удаляет клиента только без истории; при истории DELETE возвращает 409 (также для admin). POST архивирует только для admin; обычному менеджеру возвращает 403. Оперируйте `deleted`/`archived` из ответа.
- `/manager/clients/{client_id}/history/` GET/POST: история контактов; POST `result` обязательно, `type` default call (неизвестный тип заменяется call).
- `/manager/tasks/` GET/POST: список своих задач (admin все), POST `clientId`, `text` обязательны; `deadline` YYYY-MM-DD, `comment`. `/manager/tasks/{task_id}/` POST/PUT/PATCH: `status`, `comment`, `text`, `deadline`; обновление ограничено `manager=currentUser` даже для admin. Статусы `pending`, `completed`.
- `/admin/managers/`, `/admin/managers/{manager_id}/clients/` — admin-only списки. `/admin/manager-tasks/` GET/POST — задачи для конкретного managerId; detail только DELETE, ответ `{detail:"Удалено"}`.

Файловые отчёты: `/clients/export/?format=xlsx|docx|pdf|csv`, фильтры search/status/category/region/partnerStatus; доступ дистрибьютору/менеджеру/admin в своей области. `/reports/export/` — Excel отчёт для manager/admin. `/analytics/purchases/`: JSON с totals, bySku/byClient/byRegion/byDistributor/byMonth/filters; query `date_from`, `date_to`, `distributor`, `region`, `sku`, `status`; `export=xlsx|csv` возвращает файл. По умолчанию аналитика закупок считает принятые к учёту статусы из сервиса, а не все сырые строки.

## ИИ, карточки и документы знаний

### Чат

POST `/ai/chat/`, Bearer, `{message:"Как подготовить поверхность?"}`. Активный клиент либо другая авторизованная роль. message — строка до 4000 символов. Ответ:

```json
{"answer":"Ответ с указанием материалов [1].","sources":[{"type":"document","id":"8","chunkId":"42","title":"Технологическая инструкция","filename":"manual.pdf","page":2,"section":""}],"suggestEscalation":false}
```

Для карточки source: `type:"card"`, `id`, `title`, `page:null`, `section:""`, `fragment`; legacy `sourceId` появляется, если среди найденных материалов есть карточка. `sources` может быть пустым. Нельзя считать sourceId всегда присутствующим. Sources — найденный контекст, а не гарантия независимой проверки каждого утверждения.

Если контекста нет, модель не вызывается; ответ сообщает о недостатке данных. `suggestEscalation=true` помогает предложить обращение эксперту. При недоступном провайдере API может вернуть **200** с реальными выдержками; интерфейс должен показать answer, а не считать каждый такой ответ ошибкой HTTP. HF token/model/url/timeout — только серверные настройки; ключи не включаются во фронтенд. `HF_CHAT_TIMEOUT` default 45 секунд.

### Обращения и карточки

Клиент: `/tickets/` свои; эксперт/admin все. POST `/tickets/create/`: `question`, `category`, `risk` (`low`, `medium`, `high`, default low); JSON или multipart `photo`. 201 `{ticket}`. POST `/tickets/{ticket_id}/expert-answer/` — эксперт/admin: `answer`, `causes`, `status` default expertAnswered, `createKnowledgeCard`. Успех `{ticket}`. Завершённый ответ может автоматически создать draft-карточку; явное `createKnowledgeCard:false` отключает создание.

GET `/knowledge-cards/?search=...`: все авторизованные, клиент должен быть active; эксперт/admin видят черновики, остальные approved. POST create/update — эксперт/admin; `title`, `category`, `problem`, `causes`, `solution`, `status`, `skus`, `restrictions`. Создание возвращает **200** `{card}`, default draft. Не обещайте управление только собственными карточками: обработчики изменения не ограничивают автора, в отличие от документов.

### Документы

Управление: admin всеми, ai_expert своими. Клиент/менеджер/дистрибьютор/курьер не управляют оригиналами. Статусы: `processing` (включая очередь), `ready`, `failed`.

| Endpoint | Контракт |
|---|---|
| GET `/knowledge-documents/` | Стандартная пагинация KnowledgeDocument |
| POST `/knowledge-documents/upload/` | Multipart, ровно один `file`, optional `title`; 201 `{document}` |
| GET `/knowledge-documents/{document_id}/` | 200 `{document}` |
| DELETE тот же URL | 200 `{deleted:true}` |
| POST `/{document_id}/reprocess/` | Body `{}`; 202 `{document}`; уже processing → 409 |
| GET `/{document_id}/download/` | Авторизованный FileResponse, attachment, `Cache-Control: private, no-store` |

PDF/DOCX/TXT ≤25 МиБ. MIME должен соответствовать расширению: `application/pdf`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, `text/plain`; generic octet-stream для этой загрузки не подходит. Старый DOC не поддержан. Извлечение асинхронное, требует работающего server worker; успешный upload ещё не означает ready.

DTO: `id`, `title`, `originalName`, `fileType`, `size` (байты), `status`, `uploadedBy`, `createdAt`, `error`, `errorCode`. Нет публичного file URL или серверного storage path. Пустой/сканированный PDF → failed, errorCode=`ocr_required`; OCR пока нет. Возможны `empty_document`, `encrypted_pdf`, `too_complex`, `extraction_failed`, `processing_limit`; ошибки загрузки включают `invalid_format`, `invalid_pdf`, `invalid_docx`, `invalid_text`, `too_large`.

Для массовой загрузки выберите несколько файлов, отправляйте последовательными запросами или малой ограниченной группой; ошибка одного файла не останавливает остальные. Прогресс отправки и серверное processing различаются. Опрашивайте видимые pending-документы примерно каждые 5 секунд; останавливайте polling при выходе с экрана. Чужой ID эксперту возвращает 404. Содержимое ready-документов используется общей базой знаний чата — приватность оригинала не означает приватность найденных выдержек для клиентов.

## Рефералы, бонусы, обучение и уведомления

`GET /referrals/`: стандартный список Referral + `stats`, `referralCode`, `inviteLink`, `bonusTiers`, `activityMin`, `bonusBalance`. POST create: `inviteeName`, `inviteeInn`, optional `region`; нельзя пригласить себя, существующего участника или уже заявленный ИНН. `conditionMet`, `confirmation`, `bonusEarned` следует брать с сервера.

POST `/referrals/{referral_id}/confirm/` адресованному клиенту: `{confirmed:true}` или `{confirmed:false}`; уже принятое решение → 409. `pendingReferral` приходит также в регистрации/главной. Статусы confirmation: `auto`, `pending`, `confirmed`, `declined`. Старые gift-поля существуют для истории; отдельного согласования новых подарков этим API не добавляйте.

`GET /bonus-account/`: стандартный список движений и `balance`; amount со знаком. История включает начисления/списания/возвраты, поэтому не считайте баланс суммой лишь положительных записей.

`GET /learning-materials/`, query kind/category: опубликованные материалы для обычных ролей; эксперт/admin видят также draft/archived. `body`, `summary`, `videoUrl`, `fileUrl`, `durationMinutes`. Публичного endpoint создания обучающего материала здесь нет.

`GET /notifications/`: свои Notification + unreadCount. POST `/notifications/read/` `{ids:["1","2"]}` отметит эти свои уведомления; **без ids** — все. Ответ `{ok:true}`. POST `/notifications/token/`: `{token:"<FCM device token>",platform:"android"}` либо ios; `{token,remove:true}` удаляет привязку. Это FCM-token, не AuthToken.

POST `/notifications/send/`: служебные роли admin/manager/ai_expert/distributor, поля `userId`, `title`, `body`, optional `type`, `relatedLink`; scope получателя проверяется. `/debug/push/?send=1` — диагностика с возможной отправкой тестового push; не вызывайте её автоматически при открытии обычного интерфейса.

## Серверные интеграции 1С и финансы

### 1С/ERP

Эти маршруты предназначены для серверного обмена, не публичного browser-клиента. Авторизация **`X-Integration-Token`**, не пользовательский Bearer. Токен управляется через `/distributor/integration/`, `/distributor/integration/generate/` или manager/admin `/admin/integration/tokens/`, `/admin/integration/generate/{distributor_id}/`, `/admin/integration/logs/`. Генерация меняет активный token; учтите остановку старого обмена.

- POST `/integration/erp/stock-update/`: корневой массив `{sku,external_id,quantity,price}`, query `dry_run`; обновление существующих товаров, ответ status/updated/errors/dry_run/total_items.
- POST `/integration/erp/catalog-sync/`: корневой массив `{sku,external_id,name,category,brand,quantity,price}`, создание/обновление, ответ status/created/updated/errors. Не обещайте поддержку фотографий здесь: этого поля обработчик не читает.
- POST `/integration/erp/client-sync/`: **известный конфликт реализации**. Обработчик итерирует payload как массив `{inn,external_id,partner_status}`, но middleware для этого URL разрешает только JSON-объект. Массив получает 400; объект не соответствует циклу обработчика. Не интегрируйте этот обмен до отдельного исправления backend. Документация не исправляет endpoint.
- GET `/integration/erp/orders-export/`: `{orders:[...]}` заказов своего дистрибьютора с `external_id=null` (фильтра confirmed здесь нет); детали формата см. endpoint.
- `/integration/1c/exchange/`: текстовый CommerceML протокол с query mode/type/filename и X-Integration-Token (также читается token из query, но не помещайте секрет в публичные ссылки). `checkauth`, `init`, `file`, `import`. Это не JSON и не Excel upload.

### Внутренний учёт ЮKassa

Дистрибьютор свои операции, admin может query `distributor_id` или все. Менеджер не получает этот доступ лишь на основании своей роли.

GET `/finance/yookassa/`: query `date_from`, `date_to` (YYYY-MM-DD, включительно), `distributor_id`, `page`. Default период от начала текущего месяца до текущей даты backend. Ответ: `summary`, `operations`, `discrepancies`, `dateFrom`, `dateTo`, `distributorId`, `count`, `page`, `pageSize:100`, `distributors`. `operations` — одна страница, summary за весь период, discrepancies ограничены первыми 100. Здесь числовые ID и decimal-строки, нет стандартного pagination.

Поля `summary`: `paidOrders` (integer), `sales` (decimal-string), `commission` (decimal-string или null), `confirmedCommission` (decimal-string), `refunds` (decimal-string), `net` (decimal-string или null), `confirmedNet` (decimal-string), `unreconciledPayments` (integer), `discrepancies` (integer), `status` (`matched`, `discrepancies`, `empty`). `confirmedCommission`/`confirmedNet` — известная часть, а не всегда итог всей выборки.

POST `/finance/yookassa/import/`: multipart `file` CSV/XLSX ≤10 МиБ, optional `distributor_id`. 201 `{importId,imported,duplicates,skipped}`. Импорт реестра — внутреннее сопоставление, не команда списать деньги или повторно подтвердить заказ. 409 означает параллельный импорт.

GET `/finance/yookassa/export/`: те же date/distributor фильтры, XLSX со всей выбранной выборкой независимо от page. Строка операции содержит date, orderId, client, distributor, distributorId, paymentId, providerPaymentId, refundId, amount, fee, refund, net, status, registryAmount, kind. Проверяйте null комиссии и net при отсутствии подтверждённого реестра; не подставляйте ноль как доказанную комиссию.

## Примеры TypeScript и curl

Ниже базовый Fetch-клиент без внешнего SDK. Токен передаёт ваш auth-layer; решение о постоянном хранении и защите от XSS относится к приложению. Все вызовы строят адрес относительно `/api/`, не теряя префикс.

```typescript
const API_BASE_URL = "https://autoterra.shop/api/";

export class ApiError extends Error {
  constructor(public status: number, public payload: unknown, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

function errorMessage(value: unknown, status: number): string {
  if (value && typeof value === "object") {
    const body = value as Record<string, unknown>;
    if (typeof body.detail === "string") return body.detail;
    if (typeof body.message === "string") return body.message;
    if (body.errors) return JSON.stringify(body.errors);
  }
  return `Ошибка HTTP ${status}`;
}

export async function apiJson<T>(
  path: string,
  options: {method?: string; token?: string; body?: unknown; timeoutMs?: number} = {}
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? 30000);
  const headers = new Headers({Accept: "application/json"});
  if (options.token) headers.set("Authorization", `Bearer ${options.token}`);
  const form = options.body instanceof FormData;
  const body = options.body === undefined ? undefined : form
    ? options.body as FormData : JSON.stringify(options.body);
  if (body !== undefined && !form) headers.set("Content-Type", "application/json");
  try {
    const response = await fetch(new URL(path.replace(/^\/+/, ""), API_BASE_URL), {
      method: options.method ?? "GET", headers, body,
      signal: controller.signal, credentials: "omit"
    });
    const text = await response.text();
    let payload: unknown = text;
    if (response.headers.get("content-type")?.includes("application/json") && text) {
      try { payload = JSON.parse(text); } catch { payload = {detail: "Некорректный JSON ответа"}; }
    }
    if (!response.ok) throw new ApiError(response.status, payload, errorMessage(payload, response.status));
    if (typeof payload === "string") throw new Error("Ожидался JSON, получен другой формат ответа");
    return payload as T;
  } finally { clearTimeout(timer); }
}

export async function login(phone: string, password: string) {
  return apiJson<{token: string; user: {id: string; role: string; status: string}}>("login/", {
    method: "POST", body: {phone, password}
  });
}

export async function uploadKnowledgeDocument(token: string, file: File) {
  const data = new FormData();
  data.append("file", file, file.name);
  return apiJson<{document: {id: string; status: string; error: string}}>(
    "knowledge-documents/upload/", {method: "POST", token, body: data, timeoutMs: 120000}
  );
}

export async function downloadKnowledgeDocument(token: string, id: string): Promise<Blob> {
  const response = await fetch(new URL(`knowledge-documents/${encodeURIComponent(id)}/download/`, API_BASE_URL), {
    headers: {Authorization: `Bearer ${token}`}, credentials: "omit"
  });
  if (!response.ok) throw new Error(`Ошибка скачивания ${response.status}`);
  return response.blob();
}
```

Runtime-проверки полного DTO в примере не реализованы: generic `T` — TypeScript-подсказка, не валидация полученного JSON. Обрабатывайте ApiError 401 в auth-layer и 403 account_not_active отдельно. GET можно повторять ограниченно при сетевом сбое; POST create/pay/import не повторяйте автоматически общей retry-политикой. Для upload progress в браузере нужен XMLHttpRequest либо другая реализация, поддерживающая upload.onprogress; обычный Fetch не предоставляет точный процент загрузки.

```bash
curl -sS 'https://autoterra.shop/api/health/'

curl -sS -X POST 'https://autoterra.shop/api/login/' \
  -H 'Content-Type: application/json' \
  -d '{"phone":"YOUR_LOGIN","password":"YOUR_PASSWORD"}'

curl -sS 'https://autoterra.shop/api/products/?page=1&page_size=20' \
  -H 'Authorization: Bearer YOUR_TOKEN'

curl -sS -X POST 'https://autoterra.shop/api/distributor/stock/upload-file/' \
  -H 'Authorization: Bearer YOUR_DISTRIBUTOR_TOKEN' \
  -F 'file=@/path/to/price.xlsx'

curl -sS -X POST 'https://autoterra.shop/api/knowledge-documents/upload/' \
  -H 'Authorization: Bearer YOUR_EXPERT_TOKEN' \
  -F 'file=@/path/to/manual.pdf;type=application/pdf'

curl -sS -X POST 'https://autoterra.shop/api/ai/chat/' \
  -H 'Authorization: Bearer YOUR_TOKEN' -H 'Content-Type: application/json' \
  -d '{"message":"Как подготовить поверхность?"}'
```

Команды — примеры, не выполнявшиеся против production. Не подставляйте реальные токены в публикуемые инструкции/логи.

## Полная карта маршрутов
Всего **126 записей urlpatterns** (включая aliases и служебные URL). Пути ниже полные, с `/api/`. Для placeholder `{client_id}` передаётся целое число. `GET/POST*` означает отсутствие явного HTTP method gate в handler; ограничения отмечены в карточке.

| № | Метод | URL | Доступ |
|---|---|---|---|
| 1 | GET, PATCH, DELETE | `/api/clients/{client_id}/brand-discounts/` | distributor / admin; manager чтение |
| 2 | POST | `/api/orders/{replace_order_id}/replace/` | Активный клиент |
| 3 | GET | `/api/products/quote/` | Активный клиент |
| 4 | GET, PATCH | `/api/clients/{client_id}/settings/` | distributor / admin; manager чтение |
| 5 | GET, PATCH | `/api/distributor/profile/` | distributor / admin; область клиента/дистрибьютора |
| 6 | GET | `/api/finance/yookassa/` | Bearer, см. проверки handler |
| 7 | POST | `/api/finance/yookassa/import/` | Bearer, см. проверки handler |
| 8 | GET | `/api/finance/yookassa/export/` | Bearer, см. проверки handler |
| 9 | GET, PATCH | `/api/clients/{client_id}/cash-permission/` | distributor / admin; область клиента/дистрибьютора |
| 10 | POST | `/api/orders/{order_id}/cash/` | Владелец / свой distributor / admin |
| 11 | POST | `/api/courier/tasks/{task_id}/collect-cash/` | courier / admin; ограничения назначения |
| 12 | GET | `/api/docs/` | Без Bearer |
| 13 | GET | `/api/schema/` | Без Bearer |
| 14 | GET | `/api/health/` | Без Bearer |
| 15 | POST | `/api/register/` | Без Bearer |
| 16 | POST | `/api/auth/register/` | Без Bearer |
| 17 | POST | `/api/auth/password-reset/` | Без Bearer |
| 18 | POST | `/api/login/` | Без Bearer |
| 19 | GET | `/api/me/` | Bearer; клиентские ограничения описаны выше |
| 20 | GET | `/api/auth/me/` | Bearer; клиентские ограничения описаны выше |
| 21 | POST | `/api/auth/delete-account/` | Bearer, см. проверки handler |
| 22 | GET | `/api/dashboard/` | Активный клиент |
| 23 | GET, POST | `/api/stores/` | Активный клиент |
| 24 | DELETE, PATCH, PUT | `/api/stores/{store_id}/` | Активный клиент |
| 25 | GET, PATCH | `/api/me/markup/` | Активный клиент |
| 26 | GET | `/api/products/` | Активный клиент |
| 27 | GET | `/api/order-config/` | Активный клиент |
| 28 | GET | `/api/orders/` | Активный клиент |
| 29 | POST | `/api/orders/create/` | Активный клиент |
| 30 | GET/POST* | `/api/orders/{order_id}/` | Владелец / свой distributor / manager региона / admin |
| 31 | POST | `/api/orders/{order_id}/cancel/` | Активный клиент |
| 32 | POST | `/api/orders/{order_id}/confirm/` | distributor / admin; область клиента/дистрибьютора |
| 33 | POST | `/api/orders/{order_id}/adjust/` | distributor / admin; область клиента/дистрибьютора |
| 34 | POST | `/api/orders/{order_id}/reject/` | distributor / admin; область клиента/дистрибьютора |
| 35 | POST | `/api/orders/{order_id}/ship/` | distributor / admin; область клиента/дистрибьютора |
| 36 | POST | `/api/orders/{order_id}/accept-adjustment/` | Активный клиент |
| 37 | POST | `/api/orders/{order_id}/pay/` | Активный клиент |
| 38 | POST | `/api/payments/yookassa/webhook/` | Провайдер, без Bearer |
| 39 | GET | `/api/payments/yookassa/return/` | Без Bearer |
| 40 | GET | `/api/purchases/` | Активный клиент |
| 41 | POST | `/api/purchases/create/` | Активный клиент |
| 42 | GET | `/api/manager/dashboard/` | manager региона / admin |
| 43 | GET | `/api/manager/orders/` | manager региона / admin |
| 44 | GET, POST | `/api/manager/clients/` | manager региона / admin |
| 45 | GET | `/api/manager/clients/{client_id}/unified/` | manager региона / свой distributor / admin |
| 46 | GET | `/api/manager/clients/{client_id}/orders/` | Bearer, см. проверки handler |
| 47 | GET | `/api/manager/clients/{client_id}/purchases/` | Bearer, см. проверки handler |
| 48 | DELETE, POST | `/api/manager/clients/{client_id}/remove/` | manager региона / admin |
| 49 | POST, PUT, PATCH | `/api/manager/clients/{client_id}/status/` | distributor / admin; manager чтение |
| 50 | GET, POST | `/api/manager/clients/{client_id}/history/` | manager региона / admin |
| 51 | GET, POST | `/api/manager/tasks/` | manager региона / admin |
| 52 | POST, PUT, PATCH | `/api/manager/tasks/{task_id}/` | manager региона / admin |
| 53 | GET | `/api/reports/export/` | manager региона / admin |
| 54 | POST | `/api/integration/erp/stock-update/` | X-Integration-Token |
| 55 | POST | `/api/integration/erp/catalog-sync/` | X-Integration-Token |
| 56 | POST | `/api/integration/erp/client-sync/` | X-Integration-Token |
| 57 | GET | `/api/integration/erp/orders-export/` | X-Integration-Token |
| 58 | GET/POST* | `/api/integration/1c/exchange/` | X-Integration-Token |
| 59 | GET | `/api/admin/integration/tokens/` | manager региона / admin |
| 60 | POST | `/api/admin/integration/generate/{distributor_id}/` | manager региона / admin |
| 61 | GET | `/api/admin/integration/logs/` | manager региона / admin |
| 62 | GET | `/api/admin/analytics/` | manager региона / admin |
| 63 | GET | `/api/admin/managers/` | admin |
| 64 | GET, POST | `/api/admin/manager-tasks/` | admin |
| 65 | DELETE | `/api/admin/manager-tasks/{task_id}/` | admin |
| 66 | GET | `/api/admin/managers/{manager_id}/clients/` | admin |
| 67 | GET | `/api/distributor/couriers/` | distributor / admin; область клиента/дистрибьютора |
| 68 | GET | `/api/distributor/dashboard/` | distributor / admin; область клиента/дистрибьютора |
| 69 | GET | `/api/distributor/clients/` | distributor / admin; область клиента/дистрибьютора |
| 70 | GET | `/api/clients/export/` | distributor / manager / admin, в своей области |
| 71 | GET, PATCH | `/api/clients/{client_id}/discount/` | distributor / admin; manager чтение |
| 72 | GET | `/api/clients/{client_id}/prices/` | manager / admin; distributor чтение |
| 73 | PUT, PATCH, DELETE | `/api/clients/{client_id}/prices/{product_id}/` | manager / admin; distributor чтение |
| 74 | GET | `/api/distributor/purchases/` | distributor / admin; область клиента/дистрибьютора |
| 75 | PATCH, POST | `/api/distributor/purchases/{purchase_id}/verify/` | distributor / admin; область клиента/дистрибьютора |
| 76 | GET | `/api/distributor/orders/` | distributor / admin; область клиента/дистрибьютора |
| 77 | PATCH, POST | `/api/distributor/orders/{order_id}/status/` | distributor / admin; область клиента/дистрибьютора |
| 78 | GET | `/api/distributor/color-requests/` | distributor / admin; область клиента/дистрибьютора |
| 79 | POST | `/api/distributor/color-requests/{request_id}/status/` | distributor / admin; область клиента/дистрибьютора |
| 80 | GET | `/api/distributor/delivery-tasks/` | distributor / admin; область клиента/дистрибьютора |
| 81 | POST | `/api/distributor/delivery-tasks/{task_id}/status/` | distributor / admin; область клиента/дистрибьютора |
| 82 | GET | `/api/distributor/integration/` | distributor / admin; область клиента/дистрибьютора |
| 83 | POST | `/api/distributor/integration/generate/` | distributor / admin; область клиента/дистрибьютора |
| 84 | GET | `/api/distributor/stock/` | distributor / admin; область клиента/дистрибьютора |
| 85 | POST | `/api/distributor/stock/add/` | distributor / admin; область клиента/дистрибьютора |
| 86 | POST | `/api/distributor/stock/upload/` | distributor / admin; область клиента/дистрибьютора |
| 87 | POST | `/api/distributor/stock/upload-file/` | distributor / admin; область клиента/дистрибьютора |
| 88 | GET | `/api/distributor/reports/` | distributor / admin; область клиента/дистрибьютора |
| 89 | GET | `/api/color-requests/` | Активный клиент |
| 90 | POST | `/api/color-requests/create/` | Активный клиент |
| 91 | POST | `/api/color-requests/{request_id}/cancel/` | Активный клиент |
| 92 | POST | `/api/color-requests/{request_id}/update/` | Активный клиент |
| 93 | GET | `/api/courier-tasks/` | Активный клиент |
| 94 | POST | `/api/courier-tasks/create/` | Активный клиент |
| 95 | POST | `/api/courier-tasks/{task_id}/cancel/` | Активный клиент |
| 96 | POST | `/api/courier-tasks/{task_id}/update/` | Активный клиент |
| 97 | POST | `/api/courier-tasks/{task_id}/proof/` | Активный клиент |
| 98 | GET | `/api/courier/tasks/` | courier / admin; ограничения назначения |
| 99 | PATCH, POST | `/api/courier/tasks/{task_id}/status/` | courier / admin; ограничения назначения |
| 100 | POST | `/api/courier/tasks/{task_id}/items/{item_id}/pick/` | courier / admin; ограничения назначения |
| 101 | POST | `/api/courier/tasks/{task_id}/assign/` | admin |
| 102 | GET | `/api/referrals/` | Активный клиент |
| 103 | POST | `/api/referrals/create/` | Активный клиент |
| 104 | POST | `/api/referrals/{referral_id}/confirm/` | Активный клиент |
| 105 | GET | `/api/learning-materials/` | Bearer; клиентские ограничения описаны выше |
| 106 | GET | `/api/bonus-account/` | Активный клиент |
| 107 | GET | `/api/analytics/purchases/` | manager региона / admin |
| 108 | GET | `/api/tickets/` | Клиент свои; expert / admin все |
| 109 | POST | `/api/tickets/create/` | Активный клиент |
| 110 | POST | `/api/tickets/{ticket_id}/expert-answer/` | expert / admin |
| 111 | GET | `/api/knowledge-documents/` | admin / ai_expert (свои) |
| 112 | POST | `/api/knowledge-documents/upload/` | admin / ai_expert (свои) |
| 113 | GET, DELETE | `/api/knowledge-documents/{document_id}/` | admin / ai_expert (свои) |
| 114 | POST | `/api/knowledge-documents/{document_id}/reprocess/` | admin / ai_expert (свои) |
| 115 | GET | `/api/knowledge-documents/{document_id}/download/` | admin / ai_expert (свои) |
| 116 | GET | `/api/knowledge-cards/` | Bearer; клиентские ограничения описаны выше |
| 117 | POST | `/api/knowledge-cards/create/` | expert / admin |
| 118 | POST | `/api/knowledge-cards/{card_id}/update/` | expert / admin |
| 119 | GET | `/api/regions/` | Без Bearer |
| 120 | GET | `/api/distributors/` | Активный клиент |
| 121 | GET | `/api/debug/push/` | staff / admin / manager |
| 122 | GET | `/api/notifications/` | Bearer; клиентские ограничения описаны выше |
| 123 | POST | `/api/notifications/read/` | Bearer; клиентские ограничения описаны выше |
| 124 | POST | `/api/notifications/send/` | distributor / admin; область клиента/дистрибьютора |
| 125 | POST | `/api/notifications/token/` | Bearer; клиентские ограничения описаны выше |
| 126 | POST | `/api/ai/chat/` | Bearer; клиентские ограничения описаны выше |

## Структуры объектов
Таблицы перечисляют реальные JSON-ключи DTO. Колонка «Формирование» показывает точное выражение сериализации в backend: `str(...)` = строка; `float(...)` = number; `... if ... else None` = nullable; comprehension = массив. Это описание **ответа**, не список полей для записи. Вычисляемые поля менять через тело запроса нельзя. В expressions используются имена внутренних переменных только для однозначной сверки. Канонические имена полей для фронтенда — первый столбец.

### Client
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L857).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(client.id)` |
| `userId` | string | `str(client.user_id)` |
| `inn` | string | `client.inn` |
| `name` | string | `client.company_name` |
| `category` | string | `client.category` |
| `region` | string \| string | `client.region.name if client.region else ''` |
| `city` | string | `client.city` |
| `contact` | string | `client.contact_name` |
| `phone` | string | `client.phone` |
| `distributorId` | string | `str(client.distributor_id)` |
| `managerId` | string \| null | `str(client.manager_id) if client.manager_id else None` |
| `registrationSource` | string | `client.registration_source` |
| `email` | string | `client.user.email` |
| `address` | вычисляемое значение (см. выражение) | `'; '.join(client.stores.values_list('address', flat=True))` |
| `distributorName` | string \| string | `client.distributor.name if client.distributor else ''` |
| `status` | string | `client.status` |
| `markupPercent` | string \| null | `str(client.markup_percent) if client.category == 's' else None` |
| `cashPaymentAllowed` | boolean | `client.cash_payment_allowed` |
| `partnerStatus` | string | `client.partner_status` |
| `personalDiscountPercent` | string \| null | `str(client.personal_discount_percent) if client.personal_discount_percent is not None else None` |
| `referralCode` | string \| null | `client.referral_code` |
| `totalPurchases` | number | `float(Purchase.objects.filter(client=client, status='verified').aggregate(total=Sum('total_amount'))['total'] or 0)` |
| `createdAt` | string | `client.created_at.isoformat()` |

### Distributor
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L890).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(distributor.id)` |
| `userId` | string \| null | `str(distributor.user_id) if distributor.user_id else None` |
| `name` | string | `distributor.name` |
| `inn` | string | `distributor.inn` |
| `regions` | array | `distributor.regions` |
| `phone` | string | `distributor.phone` |
| `email` | string | `distributor.email` |
| `isActive` | boolean | `distributor.is_active` |

### Store
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L903).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(store.id)` |
| `name` | string | `store.name` |
| `address` | string | `store.address` |
| `isActive` | boolean | `store.is_active` |
| `createdAt` | string | `store.created_at.isoformat()` |

### Product
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L913).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(product.id)` |
| `distributorId` | string | `str(product.distributor_id)` |
| `sku` | string | `product.sku` |
| `wbArticle` | string \| null | `product.wb_article or None` |
| `groupName` | string \| null | `product.group_name or None` |
| `name` | string | `product.name` |
| `synonyms` | array | `product.synonyms` |
| `category` | string | `product.category` |
| `brand` | string | `product.brand` |
| `description` | string \| null | `product.description or None` |
| `color` | string \| null | `product.color or None` |
| `barcode` | string \| null | `product.barcode or None` |
| `images` | array | `product.images or []` |
| `videoUrl` | string \| null | `product.video_url or None` |
| `volume` | number | `float(product.volume)` |
| `weight` | number | `float(product.weight)` |
| `packageHeight` | number | `float(product.package_height)` |
| `packageLength` | number | `float(product.package_length)` |
| `packageWidth` | number | `float(product.package_width)` |
| `tnved` | string \| null | `product.tnved or None` |
| `vatRate` | string \| null | `product.vat_rate or None` |
| `price` | number | `float(details['price'])` |
| `quantity` | integer | `product.quantity` |
| `status` | string | `product.status` |
| `updatedAt` | string | `product.updated_at.isoformat()` |
| `basePrice` | number | `float(details['base_price'])` |
| `discountPercent` | number | `float(details['discount_percent'])` |
| `hasDiscount` | вычисляемое значение (см. выражение) | `details['has_discount']` |
| `personalPrice` | number \| null | `float(details['personal_price']) if details['personal_price'] is not None else None` |
| `priceSource` | вычисляемое значение (см. выражение) | `details['price_source']` |
| `markupPercent` | string | `str(client.markup_percent)` |
| `markupClientId` | string | `str(client.pk)` |

`basePrice`, `discountPercent`, `hasDiscount`, `personalPrice`, `priceSource` добавляются только при клиентском расчёте; `markupPercent` и `markupClientId` дополнительно только при category=s. Базовые distributor-ответы этих полей могут не иметь.

### Attachment
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L960).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(item.id)` |
| `url` | string \| null | `item.file.url if item.file else None` |
| `name` | string | `item.file.name.split('/')[-1] if item.file else ''` |
| `fileType` | string | `item.file_type` |
| `uploadedBy` | string \| null | `str(item.uploaded_by_id) if item.uploaded_by_id else None` |
| `uploadedAt` | string | `item.uploaded_at.isoformat()` |
| `description` | string \| null | `item.description or None` |

### OrderItem
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L972).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(item.id)` |
| `productId` | string \| null | `str(item.product_id) if item.product_id else None` |
| `sku` | string | `item.sku` |
| `name` | string | `item.name` |
| `category` | string | `item.category` |
| `quantity` | integer | `item.quantity` |
| `volume` | number | `float(item.volume)` |
| `price` | number | `float(item.price)` |
| `brand` | string | `item.brand` |
| `availableQuantity` | integer \| null | `available` |

### Order
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L997).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(order.id)` |
| `clientId` | string | `str(order.client_id)` |
| `clientName` | string | `order.client.company_name` |
| `clientInn` | string | `order.client.inn` |
| `clientRegion` | string | `order.client.region.name` |
| `storeAddress` | string \| string | `order.store.address if order.store else ''` |
| `distributorId` | string | `str(order.distributor_id)` |
| `storeId` | string \| null | `str(order.store_id) if order.store_id else None` |
| `storeName` | string \| string | `order.store.name if order.store else 'Не назначен'` |
| `documentNumber` | string | `f'ORD-{order.id:05d}'` |
| `date` | string | `order.created_at.isoformat()` |
| `totalAmount` | number | `float(order.total_amount)` |
| `status` | string | `order.status` |
| `deliveryMethod` | string | `order.delivery_method` |
| `paymentMethod` | string | `order.payment_method` |
| `cashCollected` | boolean | `order.payment_method == 'cash' and order.paid_at is not None` |
| `cashCollectedBy` | string \| null | `str(order.cash_collected_by_id) if order.cash_collected_by_id else None` |
| `canChooseCash` | boolean | `order.client.cash_payment_allowed and order.delivery_method == 'courier' and (order.status == 'confirmed') and (order.payment_method == 'online') and (order.total_amount > 0)` |
| `comment` | string | `order.comment` |
| `rejectionReason` | string \| null | `order.rejection_reason or None` |
| `courierId` | string \| null | `str(order.courier_id) if order.courier_id else None` |
| `courierName` | вычисляемое значение (см. выражение) | `_get_courier_name(order.courier)` |
| `estimatedDeliveryDate` | string \| null | `order.estimated_delivery_date.isoformat() if order.estimated_delivery_date else None` |
| `createdAt` | string | `order.created_at.isoformat()` |
| `confirmedAt` | string \| null | `order.confirmed_at.isoformat() if order.confirmed_at else None` |
| `paidAt` | string \| null | `order.paid_at.isoformat() if order.paid_at else None` |
| `shippedAt` | string \| null | `order.shipped_at.isoformat() if order.shipped_at else None` |
| `isPayable` | boolean | `order.status in Order.PAYABLE_STATUSES and order.payment_method == 'online'` |
| `bonusAvailable` | number | `float(bonus_balance(order.client))` |
| `bonusApplied` | number | `float(_bonus_applied(order))` |
| `adjustments` | array | `[_format_adjustment(a) for a in order.adjustments.all()]` |
| `pendingPayment` | PendingPayment \| null | `_format_pending_payment(order)` |
| `items` | OrderItem[] | `items` |

### OrderAdjustment
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1055).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(adj.id)` |
| `originalItems` | array | `adj.original_items` |
| `adjustedItems` | array | `adj.adjusted_items` |
| `reason` | string \| null | `adj.reason or None` |
| `createdAt` | string | `adj.created_at.isoformat()` |

### PendingPayment
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1065).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(payment.id)` |
| `status` | string | `payment.status` |
| `confirmationUrl` | string | `payment.confirmation_url` |

### PurchaseItem
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1076).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `sku` | string | `item.sku` |
| `name` | string | `item.name` |
| `category` | string | `item.category` |
| `quantity` | integer | `item.quantity` |
| `volume` | number | `float(item.volume)` |
| `price` | number | `float(item.price)` |
| `brand` | string | `item.brand` |

### Purchase
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1088).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(purchase.id)` |
| `clientId` | string | `str(purchase.client_id)` |
| `clientName` | string | `purchase.client.company_name` |
| `clientInn` | string | `purchase.client.inn` |
| `distributorId` | string | `str(purchase.distributor_id)` |
| `documentNumber` | string | `purchase.document_number` |
| `date` | string | `purchase.date.isoformat()` |
| `totalAmount` | number | `float(purchase.total_amount)` |
| `status` | string | `purchase.status` |
| `documentUrl` | string \| null | `purchase.document_file.url if purchase.document_file else purchase.document_url or None` |
| `documentHash` | string \| null | `purchase.document_hash or None` |
| `rejectionReason` | string \| null | `purchase.rejection_reason or None` |
| `createdAt` | string | `purchase.created_at.isoformat()` |
| `items` | array | `[_format_purchase_item(item) for item in purchase.items.all()]` |
| `attachments` | array | `[_format_attachment(item) for item in _attachments_for(purchase)]` |

### RecipeMaterial
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1108).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(item.id)` |
| `sku` | string | `item.sku` |
| `quantity` | number | `float(item.quantity)` |
| `unit` | string | `item.unit` |
| `comment` | string \| null | `item.comment or None` |
| `version` | integer | `item.version` |

### ColorRequest
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1119).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(item.id)` |
| `clientId` | string | `str(item.client_id)` |
| `clientName` | string | `item.client.company_name` |
| `carBrand` | string | `item.car_brand` |
| `carModel` | string | `item.car_model` |
| `carYear` | string \| null | `item.car_year or None` |
| `vin` | string | `item.vin` |
| `colorCode` | string | `item.color_code` |
| `colorName` | string | `item.color_name` |
| `paintType` | string | `item.paint_type` |
| `paintTypeLabel` | string | `item.get_paint_type_display()` |
| `paintTypeNote` | string \| null | `item.paint_type_note or None` |
| `urgent` | boolean | `item.urgent` |
| `comment` | string \| null | `item.comment or None` |
| `transferMethod` | string | `item.transfer_method` |
| `pickupAddress` | string \| null | `item.pickup_address or None` |
| `pickupTime` | string \| null | `item.pickup_time.isoformat() if item.pickup_time else None` |
| `courierArriveUntil` | string \| null | `item.courier_arrive_until.strftime('%H:%M') if item.courier_arrive_until else None` |
| `contactPerson` | string \| null | `item.contact_person or None` |
| `contactPhone` | string \| null | `item.contact_phone or None` |
| `slaDeadline` | string \| null | `item.sla_deadline.isoformat() if item.sla_deadline else None` |
| `isOverdue` | вычисляемое значение (см. выражение) | `is_overdue` |
| `assignedDistributorId` | string \| null | `str(item.assigned_distributor_id) if item.assigned_distributor_id else None` |
| `assignedStation` | string \| null | `item.assigned_station or None` |
| `status` | string | `item.status` |
| `statusHistory` | array | `item.status_history or []` |
| `recipe` | string \| null | `item.recipe or None` |
| `materials` | вычисляемое значение (см. выражение) | `materials` |
| `courierTasks` | вычисляемое значение (см. выражение) | `courier_tasks` |
| `createdAt` | string | `item.created_at.isoformat()` |
| `attachments` | array | `[_format_attachment(attachment) for attachment in _attachments_for(item)]` |

### CourierTask
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1175).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(item.id)` |
| `clientId` | string | `str(item.client_id)` |
| `clientName` | string | `item.client.company_name` |
| `clientInn` | string | `item.client.inn` |
| `orderId` | string \| null | `str(item.order_id) if item.order_id else None` |
| `orderNumber` | string \| null | `f'ORD-{item.order_id:05d}' if item.order_id else None` |
| `paymentMethod` | string \| string | `item.order.payment_method if item.order_id else 'online'` |
| `cashAmount` | number \| number | `float(item.order.total_amount) if item.order_id and item.order.payment_method == 'cash' else 0` |
| `cashCollected` | boolean | `bool(item.order_id and item.order.payment_method == 'cash' and item.order.paid_at)` |
| `orderItems` | object[] | `[{'id': str(order_item.pk), 'name': order_item.name, 'sku': order_item.sku, 'quantity': order_item.quantity, 'picked': order_item.pk in picked, 'pickedAt': picked[order_item.pk].picked_at.isoformat() if order_item.pk in picked else None, 'pickedBy': str(picked[order_item.pk].courier_id) if order_item.pk in picked else None} for order_item in order_items]` |
| `allItemsPicked` | boolean | `bool(order_items) and all((order_item.pk in picked for order_item in order_items))` |
| `colorRequestId` | string \| null | `str(item.color_request_id) if item.color_request_id else None` |
| `courierId` | string \| null | `str(item.courier_id) if item.courier_id else None` |
| `courierName` | string \| null | `item.courier.get_full_name() or item.courier.username if item.courier else None` |
| `courierPhone` | вычисляемое значение (см. выражение) | `_courier_phone(item.courier)` |
| `taskType` | string | `item.task_type` |
| `typeDisplay` | string | `item.get_task_type_display()` |
| `address` | string | `item.address` |
| `timeSlot` | string | `item.time_slot` |
| `contactName` | string \| null | `item.contact_name or None` |
| `contactPhone` | string \| null | `item.contact_phone or None` |
| `scheduledTime` | string \| null | `item.scheduled_time.isoformat() if item.scheduled_time else None` |
| `carDescription` | string \| null | `item.car_description or None` |
| `status` | string | `item.status` |
| `statusDisplay` | string | `item.get_status_display()` |
| `assignedCourierId` | string \| null | `str(item.courier_id) if item.courier_id else None` |
| `photoProof` | string \| null | `item.proof_photo.url if item.proof_photo else None` |
| `comment` | string | `item.comment` |
| `courierComment` | string | `item.courier_comment` |
| `statusHistory` | array | `item.status_history or []` |
| `createdAt` | string | `item.created_at.isoformat()` |
| `attachments` | array | `[_format_attachment(attachment) for attachment in _attachments_for(item)]` |

### ExpertTicket
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1224).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(item.id)` |
| `clientId` | string | `str(item.client_id)` |
| `clientName` | string | `item.client.company_name` |
| `question` | string | `item.question` |
| `category` | string | `item.category` |
| `risk` | string | `item.risk` |
| `status` | string | `item.status` |
| `aiDraftAnswer` | string \| null | `item.ai_draft_answer or None` |
| `aiAnswer` | string \| null | `item.ai_answer or None` |
| `expertAnswer` | string \| null | `item.expert_answer or None` |
| `linkedKnowledgeCardId` | string \| null | `str(item.linked_knowledge_card_id) if item.linked_knowledge_card_id else None` |
| `similarCases` | array | `item.similar_cases or []` |
| `createdAt` | string | `item.created_at.isoformat()` |
| `updatedAt` | string | `item.updated_at.isoformat()` |
| `attachments` | array | `[_format_attachment(a) for attachment in _attachments_for(item)]` |

### KnowledgeCard
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1244).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(item.id)` |
| `title` | string | `item.title or item.problem` |
| `category` | string | `item.category` |
| `problem` | string | `item.problem` |
| `causes` | string | `item.causes or ''` |
| `solution` | string | `item.solution` |
| `skus` | array | `item.skus or []` |
| `restrictions` | string \| null | `item.restrictions or None` |
| `status` | string | `item.status` |
| `isApproved` | boolean | `item.status == 'approved'` |
| `approvingExpert` | string \| string | `item.expert_author.username if item.expert_author else 'System'` |
| `revisionHistory` | array | `item.revision_history or []` |
| `createdAt` | string | `item.created_at.isoformat()` |
| `updatedAt` | string | `item.updated_at.isoformat()` |

### Notification
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1263).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(item.id)` |
| `title` | string | `item.title` |
| `body` | string | `item.body` |
| `type` | string | `item.type` |
| `relatedLink` | string | `item.related_link` |
| `isRead` | boolean | `item.is_read` |
| `createdAt` | string | `item.created_at.isoformat()` |

### Referral
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1275).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(item.id)` |
| `inviterId` | string | `str(item.inviter_id)` |
| `inviteeInn` | string | `item.invitee_inn` |
| `inviteeName` | string | `item.invitee_name` |
| `region` | string | `item.region` |
| `isRegistered` | boolean | `item.is_registered` |
| `hasPurchase` | boolean | `item.has_purchase` |
| `purchaseAmount` | number | `float(item.purchase_amount)` |
| `conditionMet` | boolean | `item.condition_met` |
| `gift` | string \| null | `item.gift or None` |
| `giftStatus` | string | `item.gift_status` |
| `giftComment` | string \| null | `item.gift_comment or None` |
| `confirmation` | string | `item.confirmation` |
| `bonusRate` | number | `float(referral_bonus.rate_for(item.purchase_amount))` |
| `bonusEarned` | number | `float(referral_bonus.accrued_for(item))` |
| `createdAt` | string | `item.created_at.isoformat()` |

### PendingClaim
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1302).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(referral.id)` |
| `inviterName` | string | `referral.inviter.company_name` |
| `inviterCity` | string | `referral.inviter.city or ''` |

### Payment
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L2477).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(payment.id)` |
| `orderId` | string | `str(payment.order_id)` |
| `provider` | string | `payment.provider` |
| `providerPaymentId` | string | `payment.provider_payment_id` |
| `amount` | number | `float(payment.amount)` |
| `currency` | string | `payment.currency` |
| `status` | string | `payment.status` |
| `confirmationUrl` | string \| null | `payment.confirmation_url or None` |
| `createdAt` | string | `payment.created_at.isoformat()` |
| `paidAt` | string \| null | `payment.paid_at.isoformat() if payment.paid_at else None` |

### ManagerTask
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3404).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(task.id)` |
| `clientId` | string \| null | `str(task.client_id) if task.client_id else None` |
| `clientName` | string \| string | `task.client.company_name if task.client_id else ''` |
| `managerId` | string | `str(task.manager_id)` |
| `managerName` | string | `task.manager.get_full_name() or task.manager.username` |
| `text` | string | `task.text` |
| `deadline` | string \| null | `task.deadline.isoformat() if task.deadline else None` |
| `status` | string | `task.status` |
| `comment` | string | `task.comment` |
| `createdAt` | string | `task.created_at.isoformat()` |

### ContactHistory
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3419).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(entry.id)` |
| `clientId` | string | `str(entry.client_id)` |
| `type` | string | `entry.contact_type` |
| `typeDisplay` | string | `entry.get_contact_type_display()` |
| `result` | string | `entry.result` |
| `date` | string | `entry.date.isoformat()` |
| `authorId` | string | `str(entry.manager_id)` |
| `authorName` | string | `entry.manager.get_full_name() or entry.manager.username` |

### LearningMaterial
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5016).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(item.id)` |
| `title` | string | `item.title` |
| `kind` | string | `item.kind` |
| `kindLabel` | string | `item.get_kind_display()` |
| `category` | string | `item.category` |
| `summary` | string | `item.summary` |
| `body` | string | `item.body` |
| `videoUrl` | string \| null | `item.video_url or None` |
| `fileUrl` | string \| null | `item.file_url or None` |
| `durationMinutes` | integer \| null | `item.duration_minutes` |
| `status` | string | `item.status` |
| `createdAt` | string | `item.created_at.isoformat()` |

### ClientPrice
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L6208).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `productId` | string | `str(product.pk)` |
| `sku` | string | `product.sku` |
| `name` | string | `product.name` |
| `productActive` | boolean | `product.is_active` |
| `basePrice` | string | `str(details['base_price'])` |
| `rankPrice` | string | `str(apply_discount(product.price, discount_percent(client, product)))` |
| `price` | string | `str(details['price'])` |
| `priceSource` | вычисляемое значение (см. выражение) | `details['price_source']` |
| `discountPercent` | string | `str(details['discount_percent'])` |
| `overrideId` | string \| null | `str(override.pk) if override else None` |
| `personalPrice` | string \| null | `str(override.price) if override else None` |
| `isActive` | boolean \| boolean | `override.is_active if override else False` |
| `updatedAt` | string \| null | `override.updated_at.isoformat() if override else None` |

### KnowledgeDocument
Источник: [сериализация](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/knowledge_views.py#L25).

| JSON-поле | Тип / форма | Формирование / условие |
|---|---|---|
| `id` | string | `str(doc.pk)` |
| `title` | string | `doc.title` |
| `originalName` | string | `doc.original_name` |
| `fileType` | string | `doc.file_type` |
| `size` | integer | `doc.size` |
| `status` | string | `doc.status` |
| `uploadedBy` | string \| null | `str(doc.uploaded_by_id) if doc.uploaded_by_id else None` |
| `createdAt` | string | `doc.created_at.isoformat()` |
| `error` | string | `doc.error` |
| `errorCode` | string | `doc.error_code` |

### Перечисления модели
Choices описывают канонические значения. Наличие choices в модели не означает, что каждый legacy handler проверяет их одинаково строго. UI должен предлагать эти значения; права и допустимый переход дополнительно проверяются endpoint.

**ClientProfile.CATEGORY_CHOICES**

| Значение | Подпись |
|---|---|
| `a` | A · Дилерский салон |
| `b` | B · Автосервис с кузовным цехом |
| `c` | C · Гаражный сервис |
| `s` | Магазин |

**ClientProfile.STATUS_CHOICES**

| Значение | Подпись |
|---|---|
| `new` | Новый |
| `under_review` | На проверке |
| `active` | Активный |
| `blocked` | Заблокирован |
| `archived` | Архивный |

**ClientProfile.SOURCE_CHOICES**

| Значение | Подпись |
|---|---|
| `app` | Мобильное приложение |
| `web` | Веб-сайт |
| `manager` | Менеджер |

**Order.STATUS_CHOICES**

| Значение | Подпись |
|---|---|
| `new` | Новый |
| `confirmed` | Подтверждён |
| `adjusted` | Скорректирован |
| `accepted` | Принят |
| `rejected` | Отклонён |
| `paid` | Оплачен |
| `shipped` | Отправлен |
| `fulfilled` | Выполнен |
| `cancelled` | Отменён |

**Order.STATUS_TRANSITIONS**

| Текущий статус | Следующие статусы модели |
|---|---|
| `new` | adjusted, cancelled, confirmed, rejected |
| `confirmed` | cancelled, paid |
| `adjusted` | cancelled, confirmed |
| `accepted` | cancelled, confirmed, rejected |
| `rejected` | Нет |
| `paid` | shipped |
| `shipped` | fulfilled |
| `fulfilled` | Нет |
| `cancelled` | Нет |

**Order.DELIVERY_CHOICES**

| Значение | Подпись |
|---|---|
| `courier` | Курьерская доставка |
| `self_pickup` | Самовывоз |

**Payment.STATUS_CHOICES**

| Значение | Подпись |
|---|---|
| `pending` | Ожидает оплаты |
| `waiting_for_capture` | Ожидает подтверждения |
| `succeeded` | Оплачен |
| `canceled` | Отменён |

**Purchase.STATUS_CHOICES**

| Значение | Подпись |
|---|---|
| `new` | Новая |
| `pending` | На проверке |
| `pending_verification` | Ожидает подтверждения |
| `under_review` | Ручная проверка |
| `duplicate_review` | Проверка дубля |
| `verified` | Подтверждена |
| `rejected` | Отклонена |

**ColorRequest.STATUS_CHOICES**

| Значение | Подпись |
|---|---|
| `created` | Создана |
| `inProgress` | В работе |
| `ready` | Готова |
| `delivered` | Выдана |

**ColorRequest.TRANSFER_CHOICES**

| Значение | Подпись |
|---|---|
| `courier` | Курьер |
| `self_delivery` | Сам привезу |

**ColorRequest.PAINT_TYPE_CHOICES**

| Значение | Подпись |
|---|---|
| `acrylic` | Акрил |
| `baseClear` | База + лак |
| `threeStage` | Трёхстадийная |

**CourierTask.TYPE_CHOICES**

| Значение | Подпись |
|---|---|
| `delivery` | Доставка |
| `pickup` | Забор лючка |
| `color_lab_pickup` | Забор для Color Lab |

**CourierTask.STATUS_CHOICES**

| Значение | Подпись |
|---|---|
| `created` | Создан |
| `assigned` | Назначен |
| `in_progress` | В пути |
| `delivered` | Доставлено |
| `returned` | Возвращено |
| `cancelled` | Отменено |

**ExpertTicket.STATUS_CHOICES**

| Значение | Подпись |
|---|---|
| `open` | Открыт |
| `aiAnswered` | Ответ AI |
| `escalated` | Передан эксперту |
| `expertAnswered` | Ответ эксперта |
| `closed` | Закрыт |

**ExpertTicket.RISK_CHOICES**

| Значение | Подпись |
|---|---|
| `low` | Низкий |
| `medium` | Средний |
| `high` | Высокий |

**Notification.TYPE_CHOICES**

| Значение | Подпись |
|---|---|
| `info` | Информация |
| `action_required` | Требуется действие |
| `recommendation` | Рекомендация |
| `order` | Заказ |
| `color` | Колеровка |
| `delivery` | Доставка |
| `referral` | Реферал |
| `ai` | AI |
| `system` | Система |

**KnowledgeCard.STATUS_CHOICES**

| Значение | Подпись |
|---|---|
| `draft` | Черновик |
| `approved` | Одобрено |
| `rejected` | Отклонено |
| `archived` | Архив |

**LearningMaterial.KIND_CHOICES**

| Значение | Подпись |
|---|---|
| `lesson` | Урок |
| `checklist` | Чек-лист |
| `video` | Видео |
| `manual` | Инструкция |
| `webinar` | Запись вебинара |

**LearningMaterial.STATUS_CHOICES**

| Значение | Подпись |
|---|---|
| `draft` | Черновик |
| `published` | Опубликован |
| `archived` | Архив |

**ManagerTask.STATUS_CHOICES**

| Значение | Подпись |
|---|---|
| `pending` | Ожидает |
| `completed` | Выполнено |

## Справочник каждого endpoint
Входные поля здесь собраны из обработчика и его проверенных общих сценариев. Default — значение при отсутствии ключа в конкретной ветке чтения, **не гарантия необязательности**: обязательность может проверяться далее или в serializer. Для критичных форм используйте требования основных разделов. Ответы приведены как **схемы**, не готовый JSON: слова `string`, `integer`, имена DTO и `значение сервера` обозначают тип/вычисляемое значение; `|` обозначает альтернативу. Общие 401/403/ошибки состояния из permission helpers применяются дополнительно к таблице локальных ответов.

### 1. `/api/clients/{client_id}/brand-discounts/`
**Методы:** GET, PATCH, DELETE. **Доступ:** distributor / admin; manager чтение. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L6435).

| Где | Поле | Default / правило |
|---|---|---|
| query | `search` | `''` |
| query | `discountsOnly` | `None` |
| JSON/form | `discounts` | `Без get-default; обязательность см. проверки` |
| JSON/form | `brand` | `Без get-default; обязательность см. проверки` |
| элемент массива | `percent` | `Без get-default; обязательность см. проверки` |
| элемент массива | `brand` | `Без get-default; обязательность см. проверки` |
| JSON | `discounts` | `массив {brand,percent}, 1–200` |
| JSON | `brand` | `для DELETE` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: значение сервера[], pagination: Pagination, count: integer, canEdit: значение сервера} |
| 200 | {status: "ok"} |
| 200 | {status: "ok", updated: integer} |
| 400 | {detail: "Передайте brand"} |
| 400 | {detail: "Укажите корректный бренд"} |
| 400 | {detail: "Передайте discounts: от 1 до 200 скидок"} |
| 400 | {detail: "Укажите brand и percent для каждой скидки"} |
| 400 | {detail: "Скидка: от 0 до 40%, до двух знаков после запятой"} |
| 400 | {detail: "Бренд не найден в ассортименте дистрибьютора клиента"} |
| 400 | {detail: "Бренд повторяется в запросе"} |

### 2. `/api/orders/{replace_order_id}/replace/`
**Методы:** POST. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1958).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `storeId` | `None` |
| JSON/form | `paymentMethod` | `'online'` |
| JSON/form | `items` | `None` |
| JSON/form | `deliveryMethod` | `'courier'` |
| JSON/form | `comment` | `None` |
| элемент массива | `quantity` | `Без get-default; обязательность см. проверки` |
| элемент массива | `productId` | `Без get-default; обязательность см. проверки` |

| HTTP | Форма / причина ответа |
|---|---|
| 201 | {order: Order} |
| 400 | {detail: "Передайте JSON-объект"} |
| 400 | {detail: "Комментарий должен быть текстом"} |
| 400 | {detail: "Добавьте товары в заказ"} |
| 400 | {detail: "Некорректный способ доставки"} |
| 400 | {detail: "Некорректный способ оплаты"} |
| 400 | {detail: "Наличные доступны при курьерской доставке"} |
| 400 | {detail: "Указанный магазин не найден"} |
| 400 | {detail: "Количество должно быть положительным целым числом"} |
| 403 | {detail: "Доступ клиента ограничен", code: "account_not_active", status: значение сервера} |
| 403 | {detail: "Оплата наличными не разрешена для этого клиента"} |
| 400 | {detail: "Товар недоступен у вашего дистрибьютора"} |
| 400 | {detail: "Некорректная сумма заказа"} |
| 404 | {detail: "Заказ не найден"} |
| 409 | {detail: "Дождитесь завершения платежа"} |
| 400 | {detail: "Нельзя изменить заказ на текущем этапе"} |
| 400 | {detail: string} |

### 3. `/api/products/quote/`
**Методы:** GET. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1860).

| Где | Поле | Default / правило |
|---|---|---|
| query | `ids` | `''` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: Product[]} |
| 400 | {detail: "Укажите от 1 до 200 ID товаров"} |

### 4. `/api/clients/{client_id}/settings/`
**Методы:** GET, PATCH. **Доступ:** distributor / admin; manager чтение. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L6135).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `category` | `Без get-default; обязательность см. проверки` |
| JSON/form | `status` | `Без get-default; обязательность см. проверки` |
| JSON | `status` | `enum` |
| JSON | `category` | `a/b/c/s` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {client: Client, canEdit: значение сервера, statuses: {value: значение сервера, label: значение сервера}[], categories: {value: значение сервера, label: значение сервера}[]} |
| 400 | {detail: "Передайте status и/или category"} |
| 400 | {detail: string} |

### 5. `/api/distributor/profile/`
**Методы:** GET, PATCH. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L6168).

| Где | Поле | Default / правило |
|---|---|---|
| query | `distributorId` | `''` |
| JSON | `name` | `непустая строка` |
| JSON | `email` | `валидный email` |
| JSON | `phone` | `7–15 цифр` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {distributor: Distributor} |
| 404 | {detail: "Укажите дистрибьютора"} |
| 400 | {detail: "Укажите ID дистрибьютора"} |
| 400 | {detail: "Можно изменить только название, email и телефон. ИНН и регионы меняет администратор в панели управления."} |
| 404 | {detail: "Дистрибьютор не найден"} |
| 400 | {detail: string} |
| 400 | {detail: "Введите телефон: от 7 до 15 цифр"} |
| 400 | {detail: значение сервера} |

### 6. `/api/finance/yookassa/`
**Методы:** GET. **Доступ:** Bearer, см. проверки handler. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/finance_views.py#L50).

Нестандартная пагинация: operations, page/pageSize=100; summary за весь период, discrepancies максимум 100.

| Где | Поле | Default / правило |
|---|---|---|
| query | `page` | `1` |
| query | `date_from` | `необязательно` |
| query | `date_to` | `необязательно` |
| query | `distributor_id` | `необязательно` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | Вычисляемый JSON-объект; поля приведены в соответствующем сценарии и по ссылке на контракт |
| 400 | {detail: string} |

### 7. `/api/finance/yookassa/import/`
**Методы:** POST. **Доступ:** Bearer, см. проверки handler. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/finance_views.py#L75).

| Где | Поле | Default / правило |
|---|---|---|
| файл | `file` | `None` |
| multipart form | `distributor_id` | `необязательно` |

| HTTP | Форма / причина ответа |
|---|---|
| 201 | Вычисляемый JSON-объект; поля приведены в соответствующем сценарии и по ссылке на контракт |
| 400 | {detail: "Загрузите file: CSV или XLSX"} |
| 400 | {detail: "Размер файла не должен превышать 10 МБ"} |
| 400 | {detail: string} |
| 400 | {detail: "Поврежденный файл реестра"} |
| 409 | {detail: "Реестр импортируется параллельно. Повторите загрузку."} |

### 8. `/api/finance/yookassa/export/`
**Методы:** GET. **Доступ:** Bearer, см. проверки handler. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/finance_views.py#L96).

Бинарный XLSX, полный период. JSON только при ошибке.

| Где | Поле | Default / правило |
|---|---|---|
| query | `date_from` | `необязательно` |
| query | `date_to` | `необязательно` |
| query | `distributor_id` | `необязательно` |

| HTTP | Форма / причина ответа |
|---|---|
| 400 | {detail: string} |

### 9. `/api/clients/{client_id}/cash-permission/`
**Методы:** GET, PATCH. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L6336).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `cashPaymentAllowed` | `Без get-default; обязательность см. проверки` |
| JSON | `cashPaymentAllowed` | `boolean` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {cashPaymentAllowed: значение сервера, canEdit: true} |
| 404 | {detail: "Клиент не найден"} |
| 400 | {detail: "Передайте cashPaymentAllowed: true или false"} |

### 10. `/api/orders/{order_id}/cash/`
**Методы:** POST. **Доступ:** Владелец / свой distributor / admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L6360).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {order: Order} |
| 401 | {detail: "Unauthorized"} |
| 404 | {detail: "Заказ не найден"} |
| 403 | {detail: "Оплата наличными не разрешена для этого клиента"} |
| 409 | {detail: "Наличные доступны для подтверждённого заказа с курьерской доставкой"} |
| 409 | {detail: "По заказу уже начата онлайн-оплата. Завершите или отмените платёж перед выбором наличных."} |

### 11. `/api/courier/tasks/{task_id}/collect-cash/`
**Методы:** POST. **Доступ:** courier / admin; ограничения назначения. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L6396).

Только назначенный пользователь с внутренней ролью courier, не admin от имени другого водителя. Денежная сумма берётся с сервера.

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {task: CourierTask} |
| 404 | {detail: "Задача не найдена"} |
| 403 | {detail: "Наличные подтверждает назначенный водитель"} |
| 409 | {detail: "Это не доставка заказа"} |
| 409 | {detail: "В заказе не выбрана оплата наличными"} |
| 409 | {detail: "Получение наличных доступно только во время доставки"} |
| 409 | {detail: "По заказу уже зарегистрирован платёж"} |

### 12. `/api/docs/`
**Методы:** GET. **Доступ:** Без Bearer. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/schema.py#L375).

HTML Swagger UI. Встроенная OpenAPI схема неполная; этот документ охватывает фактические маршруты.

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

### 13. `/api/schema/`
**Методы:** GET. **Доступ:** Без Bearer. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/schema.py#L371).

JSON OpenAPI 3.0.3; схема содержит устаревшие/неполные определения, включая bearerFormat=JWT. Генерировать весь SDK из неё без дополнений нельзя.

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | Вычисляемый JSON-объект; поля приведены в соответствующем сценарии и по ссылке на контракт |

### 14. `/api/health/`
**Методы:** GET. **Доступ:** Без Bearer. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1328).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {status: "ok", service: "autoterra-api"} |

### 15. `/api/register/`
**Методы:** POST. **Доступ:** Без Bearer. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1334).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `username` | `None` |
| JSON/form | `inn` | `None` |
| JSON/form | `region_id` | `None` |
| JSON/form | `referralCode` | `None` |
| JSON/form | `ref` | `None` |
| JSON | `username` | `обязательно` |
| JSON | `password` | `≥8` |
| JSON | `inn` | `10/12 цифр` |
| JSON | `region_id` | `обязательно` |
| JSON | `company_name` | `обязательно` |
| JSON | `contact_name` | `обязательно` |
| JSON | `store_address` | `обязательно` |
| JSON | `email` | `обязательно` |
| JSON | `termsAccepted` | `true` |
| JSON | `personalDataConsent` | `true` |
| JSON | `category` | `'b'` |

| HTTP | Форма / причина ответа |
|---|---|
| 201 | {status: "success", token: значение сервера, client: Client, requires_approval: boolean, pendingReferral: PendingClaim \| null} |
| 400 | {detail: значение сервера, errors: значение сервера} |
| 409 | {detail: string, code: "phone_duplicate"} |
| 409 | {detail: string, code: "inn_duplicate"} |
| 409 | {detail: "Ошибка уникальности данных: возможно, ИНН или телефон были зарегистрированы только что другим пользователем.", code: "integrity_error"} |
| 500 | {detail: "Не удалось завершить регистрацию. Попробуйте позднее или обратитесь в поддержку.", code: "server_error"} |

### 16. `/api/auth/register/`
**Методы:** POST. **Доступ:** Без Bearer. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1334).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `username` | `None` |
| JSON/form | `inn` | `None` |
| JSON/form | `region_id` | `None` |
| JSON/form | `referralCode` | `None` |
| JSON/form | `ref` | `None` |
| JSON | `username` | `обязательно` |
| JSON | `password` | `≥8` |
| JSON | `inn` | `10/12 цифр` |
| JSON | `region_id` | `обязательно` |
| JSON | `company_name` | `обязательно` |
| JSON | `contact_name` | `обязательно` |
| JSON | `store_address` | `обязательно` |
| JSON | `email` | `обязательно` |
| JSON | `termsAccepted` | `true` |
| JSON | `personalDataConsent` | `true` |
| JSON | `category` | `'b'` |

| HTTP | Форма / причина ответа |
|---|---|
| 201 | {status: "success", token: значение сервера, client: Client, requires_approval: boolean, pendingReferral: PendingClaim \| null} |
| 400 | {detail: значение сервера, errors: значение сервера} |
| 409 | {detail: string, code: "phone_duplicate"} |
| 409 | {detail: string, code: "inn_duplicate"} |
| 409 | {detail: "Ошибка уникальности данных: возможно, ИНН или телефон были зарегистрированы только что другим пользователем.", code: "integrity_error"} |
| 500 | {detail: "Не удалось завершить регистрацию. Попробуйте позднее или обратитесь в поддержку.", code: "server_error"} |

### 17. `/api/auth/password-reset/`
**Методы:** POST. **Доступ:** Без Bearer. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1514).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `inn` | `None` |
| JSON/form | `new_password` | `None` |
| JSON/form | `password` | `None` |
| JSON/form | `phone` | `None` |
| JSON/form | `username` | `None` |
| JSON/form | `code` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {status: "success", detail: "Пароль обновлен"} |
| 400 | {detail: "Некорректные данные восстановления"} |
| 400 | {detail: "Введите телефон или логин"} |
| 400 | {detail: "Пароль должен быть не менее 8 символов"} |
| 400 | {detail: "ИНН должен состоять из 10 или 12 цифр"} |
| 404 | {detail: "Аккаунт с такими данными не найден"} |
| 200 | {status: "code_sent", detail: "Введите код из письма на email вашего аккаунта."} |
| 400 | {detail: "Код недействителен или истёк. Запросите новый."} |
| 400 | {detail: "Для восстановления пароля обратитесь к менеджеру: email не указан."} |
| 503 | {detail: "Не удалось отправить код. Попробуйте позднее."} |

### 18. `/api/login/`
**Методы:** POST. **Доступ:** Без Bearer. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1454).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `password` | `''` |
| JSON/form | `phone` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 403 | {detail: "Неизвестная роль"} |
| 400 | {detail: "Некорректный логин или пароль"} |
| 200 | {token: значение сервера, user: {id: string, phone: значение сервера, role: "distributor", status: "active", distributor: Distributor}} |
| 200 | {token: значение сервера, user: {id: string, phone: значение сервера, role: "courier", status: "active"}} |
| 200 | {token: значение сервера, user: {id: string, phone: значение сервера, role: "expert", status: "active"}} |
| 200 | {token: значение сервера, user: {id: string, phone: значение сервера, role: значение сервера, status: "active"}} |
| 401 | {detail: "Неверный логин или пароль"} |
| 200 | {token: значение сервера, user: {id: string, phone: значение сервера, role: "autoservice", status: значение сервера}} |
| 403 | {detail: "Профиль дистрибьютора не создан"} |
| 403 | {detail: "Профиль клиента не создан"} |

### 19. `/api/me/`
**Методы:** GET. **Доступ:** Bearer; клиентские ограничения описаны выше. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1576).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 403 | {detail: "Профиль не найден"} |
| 401 | {detail: "Unauthorized"} |
| 200 | {id: string, phone: значение сервера, role: "distributor", status: "active", distributor: Distributor} |
| 200 | {id: string, phone: значение сервера, role: "courier", status: "active"} |
| 200 | {id: string, username: значение сервера, email: значение сервера, phone: значение сервера, role: "expert", status: "active", stats: {approvedCards: значение сервера, answeredTickets: значение сервера, rating: number}, specialty: значение сервера \| "Эксперт AutoTerra", expertId: string, region: значение сервера \| "Все регионы", dateJoined: string} |
| 200 | {id: string, phone: значение сервера, role: значение сервера, status: "active"} |
| 200 | {id: string, phone: значение сервера, role: "autoservice", status: значение сервера, client: Client, distributor: Distributor} |
| 403 | {detail: "Профиль дистрибьютора не создан"} |
| 403 | {detail: "Профиль клиента не создан"} |

### 20. `/api/auth/me/`
**Методы:** GET. **Доступ:** Bearer; клиентские ограничения описаны выше. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1576).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 403 | {detail: "Профиль не найден"} |
| 401 | {detail: "Unauthorized"} |
| 200 | {id: string, phone: значение сервера, role: "distributor", status: "active", distributor: Distributor} |
| 200 | {id: string, phone: значение сервера, role: "courier", status: "active"} |
| 200 | {id: string, username: значение сервера, email: значение сервера, phone: значение сервера, role: "expert", status: "active", stats: {approvedCards: значение сервера, answeredTickets: значение сервера, rating: number}, specialty: значение сервера \| "Эксперт AutoTerra", expertId: string, region: значение сервера \| "Все регионы", dateJoined: string} |
| 200 | {id: string, phone: значение сервера, role: значение сервера, status: "active"} |
| 200 | {id: string, phone: значение сервера, role: "autoservice", status: значение сервера, client: Client, distributor: Distributor} |
| 403 | {detail: "Профиль дистрибьютора не создан"} |
| 403 | {detail: "Профиль клиента не создан"} |

### 21. `/api/auth/delete-account/`
**Методы:** POST. **Доступ:** Bearer, см. проверки handler. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1654).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {status: "success", detail: "Аккаунт удалён"} |
| 401 | {detail: "Unauthorized"} |

### 22. `/api/dashboard/`
**Методы:** GET. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1714).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {client: Client, distributor: Distributor, unreadCount: integer, recentPurchases: Purchase[], activeColorRequests: ColorRequest[], referralSummary: {invitedCount: integer, buyersCount: integer, giftCount: integer}, bonusBalance: number, pendingReferral: PendingClaim \| null} |

### 23. `/api/stores/`
**Методы:** GET, POST. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1747).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `name` | `None` |
| JSON/form | `address` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 405 | {detail: "Method not allowed"} |
| 200 | {results: Store[], pagination: Pagination, count: integer} |
| 201 | {store: Store} |
| 400 | {detail: "Название обязательно"} |
| 400 | {detail: "Адрес обязателен"} |
| 400 | {detail: "Неверный JSON"} |

### 24. `/api/stores/{store_id}/`
**Методы:** DELETE, PATCH, PUT. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1774).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `name` | `None` |
| JSON/form | `address` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 405 | {detail: "Method not allowed"} |
| 200 | {store: Store} |
| 200 | {ok: true} |
| 400 | {detail: "Название обязательно"} |
| 400 | {detail: "Адрес обязателен"} |
| 400 | {detail: "Неверный JSON"} |

### 25. `/api/me/markup/`
**Методы:** GET, PATCH. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1806).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `markupPercent` | `Без get-default; обязательность см. проверки` |
| JSON | `markupPercent` | `decimal-string` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {clientId: string, markupPercent: string} |
| 403 | {detail: "Наценка доступна только клиенту-магазину"} |
| 400 | {detail: "Передайте только markupPercent"} |
| 400 | {detail: "Наценка должна быть от 0 до 9999.99%"} |

### 26. `/api/products/`
**Методы:** GET. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1825).

| Где | Поле | Default / правило |
|---|---|---|
| query | `inStock` | `None` |
| query | `category` | `None` |
| query | `search` | `None` |
| query | `brand` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: Product[], pagination: Pagination, count: integer, categories: значение сервера} |

### 27. `/api/order-config/`
**Методы:** GET. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1873).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {client: Client, distributor: Distributor, stores: Store[], categories: значение сервера, brands: значение сервера, bonusBalance: number} |

### 28. `/api/orders/`
**Методы:** GET. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1901).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: Order[], pagination: Pagination, count: integer} |

### 29. `/api/orders/create/`
**Методы:** POST. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1958).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `storeId` | `None` |
| JSON/form | `paymentMethod` | `'online'` |
| JSON/form | `items` | `None` |
| JSON/form | `deliveryMethod` | `'courier'` |
| JSON/form | `comment` | `None` |
| элемент массива | `quantity` | `Без get-default; обязательность см. проверки` |
| элемент массива | `productId` | `Без get-default; обязательность см. проверки` |

| HTTP | Форма / причина ответа |
|---|---|
| 201 | {order: Order} |
| 400 | {detail: "Передайте JSON-объект"} |
| 400 | {detail: "Комментарий должен быть текстом"} |
| 400 | {detail: "Добавьте товары в заказ"} |
| 400 | {detail: "Некорректный способ доставки"} |
| 400 | {detail: "Некорректный способ оплаты"} |
| 400 | {detail: "Наличные доступны при курьерской доставке"} |
| 400 | {detail: "Указанный магазин не найден"} |
| 400 | {detail: "Количество должно быть положительным целым числом"} |
| 403 | {detail: "Доступ клиента ограничен", code: "account_not_active", status: значение сервера} |
| 403 | {detail: "Оплата наличными не разрешена для этого клиента"} |
| 400 | {detail: "Товар недоступен у вашего дистрибьютора"} |
| 400 | {detail: "Некорректная сумма заказа"} |
| 404 | {detail: "Заказ не найден"} |
| 409 | {detail: "Дождитесь завершения платежа"} |
| 400 | {detail: "Нельзя изменить заказ на текущем этапе"} |
| 400 | {detail: string} |

### 30. `/api/orders/{order_id}/`
**Методы:** GET/POST*. **Доступ:** Владелец / свой distributor / manager региона / admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L1909).

Документируется использование GET. Код не ограничивает method декоратором; для фронтенда не использовать POST как альтернативу.

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {order: Order} |
| 401 | {detail: "Unauthorized"} |
| 404 | {detail: "Заказ не найден"} |
| 403 | {detail: "Нет доступа к заказу"} |
| 503 | {detail: "Не удалось проверить оплату в ЮKassa. Обновите заказ позднее."} |

### 31. `/api/orders/{order_id}/cancel/`
**Методы:** POST. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L2072).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {status: "success", order: Order} |
| 404 | {detail: "Заказ не найден"} |
| 409 | {detail: "Дождитесь завершения или отмены платежа в ЮKassa, затем обновите заказ."} |
| 400 | {detail: "Нельзя отменить заказ на текущем этапе"} |

### 32. `/api/orders/{order_id}/confirm/`
**Методы:** POST. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L2177).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {order: Order} |
| 404 | {detail: "Заказ не найден"} |
| 409 | {detail: "Дождитесь завершения или отмены платежа в ЮKassa, затем обновите заказ."} |
| 400 | {detail: string} |
| 400 | {detail: "Изменения должен принять клиент"} |

### 33. `/api/orders/{order_id}/adjust/`
**Методы:** POST. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L2210).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `items` | `None` |
| JSON/form | `newItems` | `None` |
| JSON/form | `reason` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {order: Order} |
| 404 | {detail: "Заказ не найден"} |
| 409 | {detail: "Дождитесь завершения или отмены платежа в ЮKassa, затем обновите заказ."} |
| 400 | {detail: string} |
| 400 | {detail: "Некорректные данные корректировки"} |
| 400 | {detail: "Передайте позиции для корректировки"} |
| 400 | {detail: "Позиция не принадлежит заказу"} |
| 400 | {detail: "После корректировки в заказе не осталось позиций. Отклоните заказ."} |
| 400 | {detail: "Некорректная сумма заказа"} |
| 400 | {detail: "Некорректные данные позиции"} |
| 400 | {detail: "Некорректные данные новой позиции"} |
| 400 | {detail: "Слишком большое количество"} |
| 400 | {detail: "Товар недоступен у этого дистрибьютора"} |

### 34. `/api/orders/{order_id}/reject/`
**Методы:** POST. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L2390).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `reason` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {order: Order} |
| 404 | {detail: "Заказ не найден"} |
| 409 | {detail: "Дождитесь завершения или отмены платежа в ЮKassa, затем обновите заказ."} |
| 400 | {detail: string} |

### 35. `/api/orders/{order_id}/ship/`
**Методы:** POST. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L2450).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {order: Order} |
| 404 | {detail: "Заказ не найден"} |
| 409 | {detail: "Дождитесь завершения или отмены платежа в ЮKassa, затем обновите заказ."} |
| 400 | {detail: string} |

### 36. `/api/orders/{order_id}/accept-adjustment/`
**Методы:** POST. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L2423).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {order: Order} |
| 404 | {detail: "Заказ не найден"} |
| 409 | {detail: "Дождитесь завершения или отмены платежа в ЮKassa, затем обновите заказ."} |
| 400 | {detail: "Заказ не в статусе корректировки"} |

### 37. `/api/orders/{order_id}/pay/`
**Методы:** POST. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L2665).

| Где | Поле | Default / правило |
|---|---|---|
| JSON | `useBonus` | `true / неотрицательная сумма / 0` |

| HTTP | Форма / причина ответа |
|---|---|
| 201 | {payment: Payment, bonusApplied: number} |
| 400 | {detail: "Некорректный запрос"} |
| 400 | {detail: string} |
| 503 | {detail: "ЮKassa пока не подтвердила создание платежа. Повторите попытку: повторного списания не будет."} |
| 503 | {detail: "Оплата временно недоступна"} |
| 502 | {detail: "Не удалось выполнить запрос к ЮKassa. Попробуйте позднее."} |
| 409 | {detail: "Платёж требует проверки оператором в ЮKassa. Повторное списание заблокировано."} |

Дополнительные проверки выполняет общий helper (оплата/состояние доставки/тип покрытия/вложения/serializer): 400/403/404/409/503 могут возвращаться также от него. См. соответствующий сценарий выше.

### 38. `/api/payments/yookassa/webhook/`
**Методы:** POST. **Доступ:** Провайдер, без Bearer. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L2711).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `event` | `None` |
| JSON/form | `object` | `Без get-default; обязательность см. проверки` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {status: "ok"} |
| 400 | {detail: "Invalid notification"} |
| 200 | {status: "ignored"} |
| 400 | {detail: "Invalid payment id"} |
| 503 | {detail: "Payment not registered yet"} |
| 503 | {detail: "Verification unavailable"} |

### 39. `/api/payments/yookassa/return/`
**Методы:** GET. **Доступ:** Без Bearer. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L2736).

HTML, не подтверждение успешной оплаты.

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

### 40. `/api/purchases/`
**Методы:** GET. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L2745).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: Purchase[], pagination: Pagination, count: integer, stats: значение сервера} |

### 41. `/api/purchases/create/`
**Методы:** POST. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L2768).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `items` | `None` |
| элемент массива | `sku` | `None` |
| элемент массива | `name` | `None` |
| элемент массива | `price` | `None` |
| элемент массива | `quantity` | `None` |
| JSON/form | `document_number` | `обязательно` |
| JSON/form | `date` | `обязательно` |
| JSON/form | `amount` | `обязательно` |
| файл | `document` | `необязательно` |

| HTTP | Форма / причина ответа |
|---|---|
| 201 | {purchase: Purchase} |
| 400 | {errors: значение сервера} |
| 400 | {error: "duplicate_detected", message: string} |

Дополнительные проверки выполняет общий helper (оплата/состояние доставки/тип покрытия/вложения/serializer): 400/403/404/409/503 могут возвращаться также от него. См. соответствующий сценарий выше.

### 42. `/api/manager/dashboard/`
**Методы:** GET. **Доступ:** manager региона / admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3433).

| Где | Поле | Default / правило |
|---|---|---|
| query | `region` | `None` |
| query | `distributor` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {totalClients: integer, newRegistrations: integer, activeOrders: integer, totalOrders: integer, fulfilledOrders: integer, totalTurnover: значение сервера, regionalStats: {region: значение сервера, clients: integer, orders: integer}[]} |

### 43. `/api/manager/orders/`
**Методы:** GET. **Доступ:** manager региона / admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3479).

| Где | Поле | Default / правило |
|---|---|---|
| query | `status` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: Order[], pagination: Pagination, count: integer} |

### 44. `/api/manager/clients/`
**Методы:** GET, POST. **Доступ:** manager региона / admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5441).

| Где | Поле | Default / правило |
|---|---|---|
| query | `status` | `None` |
| query | `category` | `None` |
| query | `region` | `None` |
| JSON/form | `regionId` | `None` |
| JSON/form | `distributorId` | `None` |
| JSON/form | `inn` | `''` |
| JSON/form | `name` | `''` |
| JSON/form | `phone` | `''` |
| JSON/form | `email` | `''` |
| JSON/form | `category` | `'b'` |
| JSON/form | `city` | `''` |
| query | `search` | `None` |
| JSON/form | `contact` | `''` |

| HTTP | Форма / причина ответа |
|---|---|
| 405 | {detail: "Method not allowed"} |
| 200 | {results: ManagerClient[], pagination: Pagination, count: integer, pendingCount: значение сервера} |
| 201 | {client: ManagerClient} |
| 400 | {detail: "ИНН должен содержать 10 или 12 цифр"} |
| 400 | {detail: "Название обязательно"} |
| 400 | {detail: "Регион обязателен"} |
| 403 | {detail: "Доступ к региону запрещён"} |
| 409 | {detail: string} |
| 400 | {detail: "Дистрибьютор не соответствует выбранному региону"} |
| 400 | {detail: "Invalid JSON"} |
| 404 | {detail: "Активный регион с дистрибьютором не найден"} |
| 400 | {detail: string} |

### 45. `/api/manager/clients/{client_id}/unified/`
**Методы:** GET. **Доступ:** manager региона / свой distributor / admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5570).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {client: значение сервера, stats: {orderCount: integer, paidOrderCount: integer, orderTurnover: number, purchaseTurnover: number, averageOrder: number \| 0, lastOrderAt: string \| null}, purchases: значение сервера, orders: значение сервера, colorRequests: значение сервера, tickets: значение сервера, referrals: значение сервера} |

### 46. `/api/manager/clients/{client_id}/orders/`
**Методы:** GET. **Доступ:** Bearer, см. проверки handler. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5631).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: Order[], pagination: Pagination, count: integer} |

### 47. `/api/manager/clients/{client_id}/purchases/`
**Методы:** GET. **Доступ:** Bearer, см. проверки handler. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5639).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: Purchase[], pagination: Pagination, count: integer} |

### 48. `/api/manager/clients/{client_id}/remove/`
**Методы:** DELETE, POST. **Доступ:** manager региона / admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5667).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `confirmName` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {archived: true} |
| 404 | {detail: "Клиент не найден"} |
| 400 | {detail: "Подтвердите точное название клиента"} |
| 200 | {deleted: true} |
| 403 | {detail: "Архивировать клиента может дистрибьютор или администратор"} |
| 409 | {detail: "У клиента есть история. Архивируйте учётную запись.", canArchive: boolean} |

### 49. `/api/manager/clients/{client_id}/status/`
**Методы:** POST, PUT, PATCH. **Доступ:** distributor / admin; manager чтение. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5695).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `status` | `Без get-default; обязательность см. проверки` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {client: ManagerClient} |
| 400 | {detail: "Недопустимый статус"} |

### 50. `/api/manager/clients/{client_id}/history/`
**Методы:** GET, POST. **Доступ:** manager региона / admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5711).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `type` | `'call'` |
| JSON/form | `result` | `''` |

| HTTP | Форма / причина ответа |
|---|---|
| 405 | {detail: "Method not allowed"} |
| 403 | {detail: "Доступ запрещён"} |
| 200 | {results: ContactHistory[], pagination: Pagination, count: integer} |
| 201 | {entry: ContactHistory} |
| 400 | {detail: "Результат обязателен"} |
| 400 | {detail: "Invalid JSON"} |

### 51. `/api/manager/tasks/`
**Методы:** GET, POST. **Доступ:** manager региона / admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5754).

| Где | Поле | Default / правило |
|---|---|---|
| query | `status` | `None` |
| JSON/form | `clientId` | `None` |
| JSON/form | `deadline` | `None` |
| JSON/form | `text` | `''` |
| JSON/form | `comment` | `''` |

| HTTP | Форма / причина ответа |
|---|---|
| 405 | {detail: "Method not allowed"} |
| 200 | {results: ManagerTask[], pagination: Pagination, count: integer} |
| 201 | {task: ManagerTask} |
| 400 | {detail: "Текст задачи обязателен"} |
| 404 | {detail: "Клиент не найден в доступных регионах"} |
| 400 | {detail: "Некорректный статус задачи"} |
| 400 | {detail: "Invalid JSON"} |

### 52. `/api/manager/tasks/{task_id}/`
**Методы:** POST, PUT, PATCH. **Доступ:** manager региона / admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5815).

Ищется только задача manager=currentUser: глобальный доступ к списку не даёт редактировать любую задачу.

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `status` | `Без get-default; обязательность см. проверки` |
| JSON/form | `comment` | `Без get-default; обязательность см. проверки` |
| JSON/form | `text` | `Без get-default; обязательность см. проверки` |
| JSON/form | `deadline` | `Без get-default; обязательность см. проверки` |
| JSON | `status` | `необязательно` |
| JSON | `comment` | `необязательно` |
| JSON | `text` | `необязательно` |
| JSON | `deadline` | `необязательно` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {task: ManagerTask} |
| 400 | {detail: "Invalid JSON"} |

### 53. `/api/reports/export/`
**Методы:** GET. **Доступ:** manager региона / admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4314).

Бинарный Excel, не JSON.

| Где | Поле | Default / правило |
|---|---|---|
| query | `region` | `None` |

### 54. `/api/integration/erp/stock-update/`
**Методы:** POST. **Доступ:** X-Integration-Token. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3494).

| Где | Поле | Default / правило |
|---|---|---|
| query | `dry_run` | `None` |
| элемент массива | `sku` | `None` |
| элемент массива | `external_id` | `None` |
| элемент массива | `quantity` | `None` |
| элемент массива | `price` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 401 | {detail: "Token is missing"} |
| 401 | {detail: "Invalid or inactive token"} |
| 400 | {detail: "Expected a JSON array"} |
| 200 | {status: значение сервера, updated: значение сервера, errors: значение сервера, dry_run: значение сервера, total_items: integer} |
| 500 | {detail: "Internal server error"} |

### 55. `/api/integration/erp/catalog-sync/`
**Методы:** POST. **Доступ:** X-Integration-Token. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3603).

| Где | Поле | Default / правило |
|---|---|---|
| элемент массива | `external_id` | `None` |
| элемент массива | `sku` | `None` |
| элемент массива | `name` | `'Новый товар'` |
| элемент массива | `category` | `'Без категории'` |
| элемент массива | `brand` | `'AutoTerra'` |
| элемент массива | `quantity` | `0` |
| элемент массива | `price` | `0` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {status: "success" \| "partial", created: значение сервера, updated: значение сервера, errors: значение сервера} |
| 401 | {detail: "Token is missing"} |
| 401 | {detail: "Invalid token"} |
| 400 | {detail: "Expected a JSON array"} |

### 56. `/api/integration/erp/client-sync/`
**Методы:** POST. **Доступ:** X-Integration-Token. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3679).

НЕ ИНТЕГРИРОВАТЬ без исправления: цикл обработчика ожидает массив, middleware application/json для URL разрешает лишь объект.

| Где | Поле | Default / правило |
|---|---|---|
| элемент массива | `inn` | `None` |
| элемент массива | `external_id` | `None` |
| элемент массива | `partner_status` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {updated: значение сервера, errors: значение сервера} |
| 401 | {detail: "Token is missing"} |
| 401 | {detail: "Invalid token"} |

### 57. `/api/integration/erp/orders-export/`
**Методы:** GET. **Доступ:** X-Integration-Token. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3725).

Не только confirmed: выгружаются все свои заказы с external_id IS NULL; экспорт сам не устанавливает этот ID.

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {orders: значение сервера} |
| 401 | {detail: "Token is missing"} |
| 401 | {detail: "Invalid token"} |

### 58. `/api/integration/1c/exchange/`
**Методы:** GET/POST*. **Доступ:** X-Integration-Token. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3760).

Текстовый протокол. mode=checkauth → success/имя cookie/token; init → zip=no и file_limit=10000000; file принимает raw body, import запускает разбор import/offers. Ответ failure может иметь HTTP 200, проверяйте текст.

| Где | Поле | Default / правило |
|---|---|---|
| query | `mode` | `None` |
| query | `type` | `None` |
| query | `token` | `None` |
| query | `filename` | `None` |

### 59. `/api/admin/integration/tokens/`
**Методы:** GET. **Доступ:** manager региона / admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3920).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: _format_token_row[], pagination: Pagination, count: integer} |

### 60. `/api/admin/integration/generate/{distributor_id}/`
**Методы:** POST. **Доступ:** manager региона / admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3949).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 404 | {detail: "Дистрибьютор не найден"} |
| 200 | {token: значение сервера} |
| 500 | {detail: string} |

### 61. `/api/admin/integration/logs/`
**Методы:** GET. **Доступ:** manager региона / admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3977).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: {id: string, distributorName: значение сервера, type: значение сервера, status: значение сервера, details: значение сервера, createdAt: string}[], pagination: Pagination, count: integer} |

### 62. `/api/admin/analytics/`
**Методы:** GET. **Доступ:** manager региона / admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3999).

| Где | Поле | Default / правило |
|---|---|---|
| query | `region` | `None` |
| query | `distributor` | `None` |
| элемент массива | `distributor__name` | `Без get-default; обязательность см. проверки` |
| элемент массива | `turnover` | `Без get-default; обязательность см. проверки` |
| query | `dateFrom` | `YYYY-MM-DD` |
| query | `dateTo` | `YYYY-MM-DD` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {totalClients: значение сервера, monthlyTurnover: number, turnover: number, pendingRegistrations: integer, activeClients: integer, paidOrders: integer, pendingPurchases: integer, colorLab: integer, deliveries: integer, newOrders: значение сервера, openTickets: значение сервера, syncSuccessRate: number, turnoverChart: значение сервера, categoriesChart: значение сервера, recentActions: значение сервера, system: значение сервера \| null} |
| 400 | {detail: "Некорректный период"} |

### 63. `/api/admin/managers/`
**Методы:** GET. **Доступ:** admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5934).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: {id: string, username: значение сервера, name: значение сервера \| значение сервера, regions: значение сервера[]}[], pagination: Pagination, count: integer} |
| 403 | {detail: "Нет доступа"} |
| 405 | {detail: "Method not allowed"} |

### 64. `/api/admin/manager-tasks/`
**Методы:** GET, POST. **Доступ:** admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5963).

| Где | Поле | Default / правило |
|---|---|---|
| query | `managerId` | `None` |
| query | `status` | `None` |
| JSON/form | `managerId` | `None` |
| JSON/form | `clientId` | `None` |
| JSON/form | `deadline` | `None` |
| JSON/form | `text` | `''` |
| JSON/form | `comment` | `''` |

| HTTP | Форма / причина ответа |
|---|---|
| 405 | {detail: "Method not allowed"} |
| 403 | {detail: "Нет доступа"} |
| 200 | {results: ManagerTask[], pagination: Pagination, count: integer} |
| 201 | {task: ManagerTask} |
| 400 | {detail: "Текст задачи обязателен"} |
| 400 | {detail: "Менеджер обязателен"} |
| 400 | {detail: "Invalid JSON"} |
| 404 | {detail: "Менеджер не найден"} |

### 65. `/api/admin/manager-tasks/{task_id}/`
**Методы:** DELETE. **Доступ:** admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L6026).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 405 | {detail: "Method not allowed"} |
| 403 | {detail: "Нет доступа"} |
| 200 | {detail: "Удалено"} |

### 66. `/api/admin/managers/{manager_id}/clients/`
**Методы:** GET. **Доступ:** admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L6042).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: {id: string, name: значение сервера}[], pagination: Pagination, count: integer} |
| 403 | {detail: "Нет доступа"} |
| 405 | {detail: "Method not allowed"} |

### 67. `/api/distributor/couriers/`
**Методы:** GET. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4168).

Возвращает всех активных пользователей role=courier, а не отдельное распределение по дистрибьюторам.

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: _format_courier[], pagination: Pagination, count: integer} |

### 68. `/api/distributor/dashboard/`
**Методы:** GET. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4187).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {metrics: {clients: integer, purchasesToVerify: integer, ordersToProcess: integer, deliveriesToAssign: integer, colorLabPending: integer}} |

### 69. `/api/distributor/clients/`
**Методы:** GET. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4265).

| Где | Поле | Default / правило |
|---|---|---|
| query | `search` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: Client[], pagination: Pagination, count: integer} |

### 70. `/api/clients/export/`
**Методы:** GET. **Доступ:** distributor / manager / admin, в своей области. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3330).

Бинарный файл xlsx/docx/pdf/csv; формат задаётся query format.

| Где | Поле | Default / правило |
|---|---|---|
| query | `search` | `None` |
| query | `category` | `None` |
| query | `format` | `None` |
| query | `status` | `необязательно` |
| query | `region` | `необязательно` |
| query | `partnerStatus` | `необязательно` |

| HTTP | Форма / причина ответа |
|---|---|
| 401 | {detail: "Unauthorized"} |
| 400 | {detail: string} |
| 503 | {detail: string} |

### 71. `/api/clients/{client_id}/discount/`
**Методы:** GET, PATCH. **Доступ:** distributor / admin; manager чтение. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L6290).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `personalDiscountPercent` | `Без get-default; обязательность см. проверки` |
| JSON | `personalDiscountPercent` | `decimal-string или null` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {clientId: string, personalDiscountPercent: string \| null, canEdit: значение сервера} |
| 400 | {detail: "Передайте personalDiscountPercent"} |
| 400 | {detail: "Скидка должна быть от 0 до 40%, не более двух знаков после запятой"} |

### 72. `/api/clients/{client_id}/prices/`
**Методы:** GET. **Доступ:** manager / admin; distributor чтение. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L6226).

| Где | Поле | Default / правило |
|---|---|---|
| query | `overridesOnly` | `'true'` |
| query | `search` | `''` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: ClientPrice[], pagination: значение сервера, count: значение сервера, canEdit: значение сервера} |

### 73. `/api/clients/{client_id}/prices/{product_id}/`
**Методы:** PUT, PATCH, DELETE. **Доступ:** manager / admin; distributor чтение. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L6250).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `price` | `existing.price if existing else None` |
| JSON/form | `isActive` | `existing.is_active if existing else True` |

| HTTP | Форма / причина ответа |
|---|---|
| 201 if created else 200 | {item: ClientPrice} |
| 404 | {detail: "Товар не найден у дистрибьютора клиента"} |
| 200 | {status: "ok", item: ClientPrice} |
| 400 | {detail: "Передайте price и/или isActive"} |
| 400 | {detail: "Цена должна быть больше нуля, до 9 999 999 999,99 ₽, не более двух знаков после запятой"} |
| 400 | {detail: "isActive должен быть true или false"} |

### 74. `/api/distributor/purchases/`
**Методы:** GET. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4277).

| Где | Поле | Default / правило |
|---|---|---|
| query | `status` | `None` |
| query | `to_verify` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: Purchase[], pagination: Pagination, count: integer} |

### 75. `/api/distributor/purchases/{purchase_id}/verify/`
**Методы:** PATCH, POST. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4358).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `status` | `None` |
| JSON/form | `rejection_reason` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {purchase: Purchase} |
| 404 | {detail: "Покупка не найдена"} |
| 400 | {detail: "Некорректный статус"} |

### 76. `/api/distributor/orders/`
**Методы:** GET. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4390).

| Где | Поле | Default / правило |
|---|---|---|
| query | `search` | `None` |
| query | `status` | `необязательно` |
| query | `client` | `числовой ID, необязательно` |
| query | `region` | `числовой ID, необязательно` |
| query | `distributor` | `числовой ID, необязательно` |
| query | `dateFrom` | `YYYY-MM-DD` |
| query | `dateTo` | `YYYY-MM-DD` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: Order[], pagination: Pagination, count: integer} |
| 400 | {detail: "Некорректный фильтр"} |
| 400 | {detail: "Некорректная дата"} |

### 77. `/api/distributor/orders/{order_id}/status/`
**Методы:** PATCH, POST. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4500).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `status` | `None` |
| JSON/form | `rejection_reason` | `None` |
| JSON/form | `courier_id` | `None` |
| JSON/form | `estimated_delivery_date` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {order: Order} |
| 404 | {detail: "Заказ не найден"} |
| 409 | {detail: "Дождитесь завершения или отмены платежа в ЮKassa, затем обновите заказ."} |
| 400 | {detail: "Некорректный статус"} |
| 400 | {detail: "Недопустимый переход статуса заказа"} |

### 78. `/api/distributor/color-requests/`
**Методы:** GET. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4212).

| Где | Поле | Default / правило |
|---|---|---|
| query | `status` | `None` |
| query | `active` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: ColorRequest[], pagination: Pagination, count: integer} |

### 79. `/api/distributor/color-requests/{request_id}/status/`
**Методы:** POST. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4233).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `status` | `None` |
| JSON/form | `recipe` | `None` |
| JSON/form | `comment` | `'Статус обновлен дистрибьютором'` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {request: ColorRequest} |
| 404 | {detail: "Заявка не найдена"} |
| 403 | {detail: "Access denied"} |

### 80. `/api/distributor/delivery-tasks/`
**Методы:** GET. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4421).

| Где | Поле | Default / правило |
|---|---|---|
| query | `status` | `None` |
| query | `active` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: CourierTask[], pagination: Pagination, count: integer} |

### 81. `/api/distributor/delivery-tasks/{task_id}/status/`
**Методы:** POST. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4440).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `status` | `None` |
| JSON/form | `courierId` | `None` |
| JSON/form | `reason` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {task: CourierTask} |
| 404 | {detail: "Заявка не найдена"} |
| 400 | {detail: "Курьер не найден"} |

Дополнительные проверки выполняет общий helper (оплата/состояние доставки/тип покрытия/вложения/serializer): 400/403/404/409/503 могут возвращаться также от него. См. соответствующий сценарий выше.

### 82. `/api/distributor/integration/`
**Методы:** GET. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3873).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {token: значение сервера \| null, createdAt: string \| null, logs: {id: значение сервера, type: значение сервера, status: значение сервера, details: значение сервера, createdAt: string}[]} |
| 403 | {detail: "Используйте /api/admin/integration/tokens/ для администраторов"} |

### 83. `/api/distributor/integration/generate/`
**Методы:** POST. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3900).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {token: значение сервера} |
| 403 | {detail: "Используйте /api/admin/integration/generate/ для администраторов"} |

### 84. `/api/distributor/stock/`
**Методы:** GET. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4559).

| Где | Поле | Default / правило |
|---|---|---|
| query | `clientId` | `None` |
| query | `search` | `None` |
| query | `category` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: Product[], pagination: Pagination, count: integer, categories: значение сервера} |
| 404 | {detail: "Клиент не найден"} |

### 85. `/api/distributor/stock/add/`
**Методы:** POST. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4582).

Успех HTTP 200. Admin отклоняется. Не следует использовать для повторного импорта того же SKU.

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `sku` | `None` |
| JSON/form | `quantity` | `0` |
| JSON/form | `status` | `'inStock' if quantity > 5 else 'low' if quantity else 'outOfStock'` |
| JSON/form | `price` | `0` |
| JSON/form | `volume` | `0` |
| JSON/form | `synonyms` | `None` |
| JSON/form | `name` | `'Новый товар'` |
| JSON/form | `category` | `'Общее'` |
| JSON/form | `brand` | `'AutoTerra'` |
| JSON/form | `description` | `''` |
| JSON/form | `external_id` | `None` |
| JSON/form | `images` | `None` |
| JSON/form | `videoUrl` | `None` |
| JSON/form | `isActive` | `True` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {status: "ok", product: Product} |
| 400 | {detail: "Admin must specify distributorId"} |
| 400 | {detail: "SKU обязателен"} |
| 400 | {detail: string} |
| 400 | {detail: "Некорректная цена или объём"} |
| 400 | {detail: "Некорректный статус товара"} |
| 400 | {detail: "Некорректный остаток"} |

### 86. `/api/distributor/stock/upload/`
**Методы:** POST. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4641).

Корневой JSON объект с items. Поля изменяются только если присутствуют в строке; ошибка строки не отменяет остальные. Admin отклоняется.

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `items` | `[]` |
| элемент массива | `sku` | `None` |
| элемент массива | `quantity` | `Без get-default; обязательность см. проверки` |
| элемент массива | `status` | `Без get-default; обязательность см. проверки` |
| элемент массива | `videoUrl` | `Без get-default; обязательность см. проверки` |
| элемент массива | `price` | `Без get-default; обязательность см. проверки` |
| элемент массива | `synonyms` | `Без get-default; обязательность см. проверки` |
| элемент массива | `images` | `Без get-default; обязательность см. проверки` |
| элемент массива | `isActive` | `Без get-default; обязательность см. проверки` |
| элемент items | `name` | `при отсутствии поле не меняется` |
| элемент items | `category` | `при отсутствии поле не меняется` |
| элемент items | `brand` | `при отсутствии поле не меняется` |
| элемент items | `description` | `при отсутствии поле не меняется` |
| элемент items | `external_id` | `при отсутствии поле не меняется` |
| элемент items | `price` | `при отсутствии поле не меняется` |
| элемент items | `quantity` | `при отсутствии поле не меняется` |
| элемент items | `status` | `при отсутствии поле не меняется` |
| элемент items | `synonyms` | `при отсутствии поле не меняется` |
| элемент items | `images` | `при отсутствии поле не меняется` |
| элемент items | `videoUrl` | `при отсутствии поле не меняется` |
| элемент items | `isActive` | `при отсутствии поле не меняется` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 if created + updated else 400 | {status: "ok", processed: значение сервера, created: значение сервера, updated: значение сервера, errors: значение сервера} |
| 400 | {detail: "Admin must specify distributorId"} |
| 400 | {detail: "Передайте список товаров"} |

### 87. `/api/distributor/stock/upload-file/`
**Методы:** POST. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4697).

Excel WB/plain/1C определяется автоматически. Admin отклоняется даже при добавленном distributorId. errors при HTTP 200 — предупреждения/пропущенные строки.

| Где | Поле | Default / правило |
|---|---|---|
| файл | `file` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {status: "ok", created: значение сервера, updated: значение сервера, processed: значение сервера, errors: значение сервера} |
| 400 | {detail: "Admin must specify distributorId"} |
| 400 | {detail: "Файл не передан (поле 'file')"} |
| 400 | {detail: значение сервера \| "В файле не найдено товаров.", errors: значение сервера} |

### 88. `/api/distributor/reports/`
**Методы:** GET. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4738).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {clients: integer, orders: integer, newOrders: integer, paidOrders: integer, turnover: number, products: integer, stockQuantity: значение сервера \| 0, pendingPurchases: integer} |

### 89. `/api/color-requests/`
**Методы:** GET. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L2842).

| Где | Поле | Default / правило |
|---|---|---|
| query | `active` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: ColorRequest[], pagination: Pagination, count: integer} |

### 90. `/api/color-requests/create/`
**Методы:** POST. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L2940).

JSON или multipart; общие файлы attachments/photo/... . aliases address/pickupDate/scheduledTime/contactName распознаются. Предлагайте только канонические значения paintType/transferMethod.

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `paintType` | `None` |
| JSON/form | `urgent` | `None` |
| JSON/form | `courierArriveUntil` | `None` |
| JSON/form | `pickupTime` | `None` |
| JSON/form | `pickupDate` | `None` |
| JSON/form | `scheduledTime` | `None` |
| JSON/form | `carBrand` | `None` |
| JSON/form | `carModel` | `None` |
| JSON/form | `vin` | `None` |
| JSON/form | `colorCode` | `None` |
| JSON/form | `colorName` | `None` |
| JSON/form | `comment` | `None` |
| JSON/form | `transferMethod` | `None` |
| JSON/form | `pickupAddress` | `None` |
| JSON/form | `address` | `None` |
| JSON/form | `contactPerson` | `None` |
| JSON/form | `contactName` | `None` |
| JSON/form | `contactPhone` | `None` |
| JSON/form | `carYear` | `None` |
| JSON/form | `paintTypeNote` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 201 | {request: ColorRequest} |
| 400 | {detail: string} |

Дополнительные проверки выполняет общий helper (оплата/состояние доставки/тип покрытия/вложения/serializer): 400/403/404/409/503 могут возвращаться также от него. См. соответствующий сценарий выше.

### 91. `/api/color-requests/{request_id}/cancel/`
**Методы:** POST. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L2854).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {status: "success"} |
| 404 | {detail: "Заявка не найдена"} |
| 400 | {detail: "Нельзя отменить заявку, которая уже в работе"} |

### 92. `/api/color-requests/{request_id}/update/`
**Методы:** POST. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L2874).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `carBrand` | `color_request.car_brand` |
| JSON/form | `carModel` | `color_request.car_model` |
| JSON/form | `vin` | `color_request.vin` |
| JSON/form | `colorCode` | `color_request.color_code` |
| JSON/form | `colorName` | `color_request.color_name` |
| JSON/form | `pickupAddress` | `color_request.pickup_address` |
| JSON/form | `contactPerson` | `color_request.contact_person` |
| JSON/form | `contactPhone` | `color_request.contact_phone` |
| JSON/form | `comment` | `color_request.comment` |
| JSON/form | `transferMethod` | `color_request.transfer_method` |
| JSON/form | `paintType` | `None` |
| JSON/form | `urgent` | `None` |
| JSON/form | `pickupTime` | `None` |
| JSON/form | `courierArriveUntil` | `None` |
| JSON/form | `carYear` | `color_request.car_year` |
| JSON/form | `paintTypeNote` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {request: ColorRequest} |
| 404 | {detail: "Заявка не найдена"} |
| 400 | {detail: "Нельзя изменить заявку, которая уже в работе"} |

Дополнительные проверки выполняет общий helper (оплата/состояние доставки/тип покрытия/вложения/serializer): 400/403/404/409/503 могут возвращаться также от него. См. соответствующий сценарий выше.

### 93. `/api/courier-tasks/`
**Методы:** GET. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L2992).

| Где | Поле | Default / правило |
|---|---|---|
| query | `active` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: CourierTask[], pagination: Pagination, count: integer} |

### 94. `/api/courier-tasks/create/`
**Методы:** POST. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3005).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `type` | `'delivery'` |
| JSON/form | `address` | `client.city` |
| JSON/form | `timeSlot` | `'10:00 - 18:00'` |
| JSON/form | `contactName` | `client.contact_name` |
| JSON/form | `contactPhone` | `client.phone` |
| JSON/form | `carDescription` | `''` |
| JSON/form | `comment` | `''` |
| JSON/form | `scheduledTime` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 201 | {task: CourierTask} |

### 95. `/api/courier-tasks/{task_id}/cancel/`
**Методы:** POST. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3035).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {status: "success"} |
| 404 | {detail: "Заявка не найдена"} |
| 409 | {detail: "Изменяйте доставку через связанный заказ"} |
| 400 | {detail: "Нельзя отменить заявку, которая уже в работе"} |

### 96. `/api/courier-tasks/{task_id}/update/`
**Методы:** POST. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3058).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `type` | `task.task_type` |
| JSON/form | `address` | `task.address` |
| JSON/form | `contactName` | `task.contact_name` |
| JSON/form | `contactPhone` | `task.contact_phone` |
| JSON/form | `timeSlot` | `task.time_slot` |
| JSON/form | `comment` | `task.comment` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {task: CourierTask} |
| 404 | {detail: "Заявка не найдена"} |
| 409 | {detail: "Изменяйте доставку через связанный заказ"} |
| 400 | {detail: "Нельзя изменить заявку, которая уже в работе"} |

### 97. `/api/courier-tasks/{task_id}/proof/`
**Методы:** POST. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3086).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {task: CourierTask} |
| 404 | {detail: "Заявка не найдена"} |
| 400 | {detail: "Добавьте файл подтверждения"} |

Дополнительные проверки выполняет общий helper (оплата/состояние доставки/тип покрытия/вложения/serializer): 400/403/404/409/503 могут возвращаться также от него. См. соответствующий сценарий выше.

### 98. `/api/courier/tasks/`
**Методы:** GET. **Доступ:** courier / admin; ограничения назначения. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3103).

| Где | Поле | Default / правило |
|---|---|---|
| query | `status` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: CourierTask[], pagination: Pagination, count: integer} |

### 99. `/api/courier/tasks/{task_id}/status/`
**Методы:** PATCH, POST. **Доступ:** courier / admin; ограничения назначения. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3166).

proof_photo — отдельный multipart файл, courier_comment — snake_case. Общие ограничения сборки/состояния/наличных описаны в сценарии доставки.

| Где | Поле | Default / правило |
|---|---|---|
| multipart form | `status` | `None` |
| multipart form | `courier_comment` | `None` |
| JSON/form | `status` | `None` |
| JSON/form | `courier_comment` | `None` |
| файл | `proof_photo` | `Без get-default; обязательность см. проверки` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {task: CourierTask} |
| 404 | {detail: "Задача не найдена"} |
| 403 | {detail: "Нет прав для редактирования этой задачи"} |

Дополнительные проверки выполняет общий helper (оплата/состояние доставки/тип покрытия/вложения/serializer): 400/403/404/409/503 могут возвращаться также от него. См. соответствующий сценарий выше.

### 100. `/api/courier/tasks/{task_id}/items/{item_id}/pick/`
**Методы:** POST. **Доступ:** courier / admin; ограничения назначения. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3146).

Только назначенный курьер. item_id относится к OrderItem. assigned, delivery и связанный order обязательны.

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {task: CourierTask} |
| 404 | {detail: "Задача не найдена"} |
| 403 | {detail: "Нет доступа к задаче"} |
| 409 | {detail: "Сборка этой доставки недоступна"} |
| 404 | {detail: "Позиция заказа не найдена"} |

### 101. `/api/courier/tasks/{task_id}/assign/`
**Методы:** POST. **Доступ:** admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3219).

Только admin. Для собственного назначения дистрибьютором используйте distributor/delivery-tasks/.../status/.

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `courierId` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {task: CourierTask} |
| 403 | {detail: "Только администратор может назначать курьеров"} |
| 400 | {detail: "Курьер не найден"} |
| 404 | {detail: "Заявка не найдена"} |

### 102. `/api/referrals/`
**Методы:** GET. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4759).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: Referral[], pagination: Pagination, count: integer, stats: значение сервера, referralCode: значение сервера, inviteLink: string, bonusTiers: {from: number, rate: number}[], activityMin: number, bonusBalance: number} |

### 103. `/api/referrals/create/`
**Методы:** POST. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4804).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `region` | `None` |
| JSON/form | `inviteeInn` | `None` |
| JSON/form | `inviteeName` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 201 | {referral: Referral} |
| 400 | {detail: "Укажите название СТО"} |
| 400 | {detail: "ИНН — 10 цифр для организации, 12 для ИП"} |
| 400 | {detail: "Нельзя пригласить самого себя"} |
| 409 | {detail: "Это СТО уже есть в вашем списке приглашений"} |
| 409 | {detail: "Это СТО уже заявлено другим участником программы"} |
| 409 | {detail: "СТО с таким ИНН уже зарегистрировано в AutoTerra — заявку можно подать только на нового участника"} |

### 104. `/api/referrals/{referral_id}/confirm/`
**Методы:** POST. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4870).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `confirmed` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {referral: Referral} |
| 404 | {detail: "Приглашение не найдено"} |
| 403 | {detail: "Это приглашение адресовано не вам"} |
| 409 | {detail: "Решение уже принято", confirmation: значение сервера} |

### 105. `/api/learning-materials/`
**Методы:** GET. **Доступ:** Bearer; клиентские ограничения описаны выше. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5034).

| Где | Поле | Default / правило |
|---|---|---|
| query | `kind` | `None` |
| query | `category` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: LearningMaterial[], pagination: Pagination, count: integer} |
| 401 | {detail: "Unauthorized"} |

### 106. `/api/bonus-account/`
**Методы:** GET. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4991).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: _format[], pagination: Pagination, count: integer, balance: number} |

### 107. `/api/analytics/purchases/`
**Методы:** GET. **Доступ:** manager региона / admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L4904).

Без export — JSON с агрегатами; с export=xlsx|csv — бинарный файл.

| Где | Поле | Default / правило |
|---|---|---|
| query | `export` | `None` |
| JSON/form | `totals` | `Без get-default; обязательность см. проверки` |
| JSON/form | `bySku` | `Без get-default; обязательность см. проверки` |
| JSON/form | `byClient` | `Без get-default; обязательность см. проверки` |
| JSON/form | `byRegion` | `Без get-default; обязательность см. проверки` |
| JSON/form | `byDistributor` | `Без get-default; обязательность см. проверки` |
| JSON/form | `byMonth` | `Без get-default; обязательность см. проверки` |
| query | `date_from` | `необязательно` |
| query | `date_to` | `необязательно` |
| query | `distributor` | `необязательно` |
| query | `region` | `необязательно` |
| query | `sku` | `необязательно` |
| query | `status` | `необязательно` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {totals: {count: значение сервера, amount: number, average: number, clients: значение сервера, quantity: значение сервера}, bySku: {sku: значение сервера, name: значение сервера, quantity: значение сервера, amount: number, documents: значение сервера}[], byClient: {name: значение сервера, inn: значение сервера, region: значение сервера \| "", documents: значение сервера, amount: number}[], byRegion: {region: значение сервера \| "", clients: значение сервера, documents: значение сервера, amount: number}[], byDistributor: {distributor: значение сервера \| "", documents: значение сервера, amount: number}[], byMonth: {month: string \| "", documents: значение сервера, amount: number}[], filters: значение сервера} |
| 503 | {detail: string} |

### 108. `/api/tickets/`
**Методы:** GET. **Доступ:** Клиент свои; expert / admin все. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5066).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: ExpertTicket[], pagination: Pagination, count: integer} |

### 109. `/api/tickets/create/`
**Методы:** POST. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5084).

JSON или multipart с общими вложениями. Клиент должен быть active.

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `risk` | `'low'` |
| JSON/form | `question` | `None` |
| JSON/form | `category` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 201 | {ticket: ExpertTicket} |

Дополнительные проверки выполняет общий helper (оплата/состояние доставки/тип покрытия/вложения/serializer): 400/403/404/409/503 могут возвращаться также от него. См. соответствующий сценарий выше.

### 110. `/api/tickets/{ticket_id}/expert-answer/`
**Методы:** POST. **Доступ:** expert / admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5115).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `status` | `'expertAnswered'` |
| JSON/form | `createKnowledgeCard` | `None` |
| JSON/form | `answer` | `None` |
| JSON/form | `causes` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {ticket: ExpertTicket} |
| 404 | {detail: "Тикет не найден"} |

### 111. `/api/knowledge-documents/`
**Методы:** GET. **Доступ:** admin / ai_expert (свои). [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/knowledge_views.py#L33).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: KnowledgeDocument[], pagination: Pagination, count: integer} |

### 112. `/api/knowledge-documents/upload/`
**Методы:** POST. **Доступ:** admin / ai_expert (свои). [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/knowledge_views.py#L42).

Ровно один file, PDF/DOCX/TXT ≤25 МиБ, MIME соответствует расширению; обработка позже worker.

| Где | Поле | Default / правило |
|---|---|---|
| файл | `file` | `None` |
| multipart form | `title` | `''` |

| HTTP | Форма / причина ответа |
|---|---|
| 201 | {document: KnowledgeDocument} |
| 400 | {detail: "Размер файла превышает 25 МБ.", code: "too_large"} |
| 400 | {detail: "Отправьте один файл в поле file."} |
| 400 | {detail: string, code: значение сервера} |

### 113. `/api/knowledge-documents/{document_id}/`
**Методы:** GET, DELETE. **Доступ:** admin / ai_expert (свои). [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/knowledge_views.py#L63).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {document: KnowledgeDocument} |
| 404 | {detail: "Документ не найден"} |
| 200 | {deleted: true} |

### 114. `/api/knowledge-documents/{document_id}/reprocess/`
**Методы:** POST. **Доступ:** admin / ai_expert (свои). [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/knowledge_views.py#L79).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 202 | {document: KnowledgeDocument} |
| 404 | {detail: "Документ не найден"} |
| 409 | {detail: "Документ уже обрабатывается"} |

### 115. `/api/knowledge-documents/{document_id}/download/`
**Методы:** GET. **Доступ:** admin / ai_expert (свои). [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/knowledge_views.py#L97).

FileResponse attachment; Cache-Control private, no-store; чужой документ эксперту 404.

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 404 | {detail: "Документ не найден"} |
| 404 | {detail: "Файл недоступен"} |

### 116. `/api/knowledge-cards/`
**Методы:** GET. **Доступ:** Bearer; клиентские ограничения описаны выше. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5166).

Эксперт/admin видят все статусы; прочие — approved. Клиент обязан быть active.

| Где | Поле | Default / правило |
|---|---|---|
| query | `search` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: KnowledgeCard[], pagination: Pagination, count: integer} |
| 401 | {detail: "Unauthorized"} |

### 117. `/api/knowledge-cards/create/`
**Методы:** POST. **Доступ:** expert / admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5197).

Успех HTTP 200, default draft. Не 201.

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `title` | `''` |
| JSON/form | `category` | `''` |
| JSON/form | `problem` | `''` |
| JSON/form | `causes` | `''` |
| JSON/form | `solution` | `''` |
| JSON/form | `status` | `'draft'` |
| JSON/form | `skus` | `[]` |
| JSON/form | `restrictions` | `''` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {card: KnowledgeCard} |

### 118. `/api/knowledge-cards/{card_id}/update/`
**Методы:** POST. **Доступ:** expert / admin. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5219).

Автор не ограничивается: любой expert/admin может изменить найденную карточку.

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `status` | `None` |
| JSON | `title` | `сохраняется текущее значение при отсутствии` |
| JSON | `category` | `сохраняется текущее значение при отсутствии` |
| JSON | `problem` | `сохраняется текущее значение при отсутствии` |
| JSON | `causes` | `сохраняется текущее значение при отсутствии` |
| JSON | `solution` | `сохраняется текущее значение при отсутствии` |
| JSON | `status` | `сохраняется текущее значение при отсутствии` |
| JSON | `skus` | `сохраняется текущее значение при отсутствии` |
| JSON | `restrictions` | `сохраняется текущее значение при отсутствии` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {card: KnowledgeCard} |
| 404 | {detail: "Карточка не найдена"} |

### 119. `/api/regions/`
**Методы:** GET. **Доступ:** Без Bearer. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5387).

Без Bearer доступен список активных регионов; у авторизованного manager фильтр своих регионов.

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: {id: string, code: значение сервера, name: значение сервера, active: значение сервера}[], pagination: Pagination, count: integer} |

### 120. `/api/distributors/`
**Методы:** GET. **Доступ:** Активный клиент. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L3250).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: {id: string, name: значение сервера}[], pagination: Pagination, count: integer} |
| 401 | {detail: "Unauthorized"} |

### 121. `/api/debug/push/`
**Методы:** GET. **Доступ:** staff / admin / manager. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5327).

send=1 может отправить тестовый push; только диагностика для служебного пользователя.

| Где | Поле | Default / правило |
|---|---|---|
| query | `send` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | Вычисляемый JSON-объект; поля приведены в соответствующем сценарии и по ссылке на контракт |
| 401 | {detail: "Unauthorized"} |
| 403 | {detail: "Forbidden"} |

### 122. `/api/notifications/`
**Методы:** GET. **Доступ:** Bearer; клиентские ограничения описаны выше. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5410).

Нет собственных полей тела/query, кроме path ID, стандартной пагинации для списков и параметров, указанных в примечании.

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {results: Notification[], pagination: Pagination, count: integer, unreadCount: integer} |
| 401 | {detail: "Unauthorized"} |

### 123. `/api/notifications/read/`
**Методы:** POST. **Доступ:** Bearer; клиентские ограничения описаны выше. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5426).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `ids` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {ok: true} |
| 401 | {detail: "Unauthorized"} |
| 400 | {detail: "Некорректный список уведомлений"} |

### 124. `/api/notifications/send/`
**Методы:** POST. **Доступ:** distributor / admin; область клиента/дистрибьютора. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5278).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `userId` | `None` |
| JSON/form | `title` | `None` |
| JSON/form | `body` | `None` |
| JSON/form | `type` | `'info'` |
| JSON/form | `relatedLink` | `''` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {status: "ok", push: значение сервера} |
| 403 | {detail: "Forbidden"} |
| 400 | {detail: "Missing fields"} |
| 404 | {detail: "User not found"} |
| 403 | {detail: "Клиент вне вашей зоны ответственности"} |
| 403 | {detail: "У клиента нет экспертного обращения"} |
| 403 | {detail: "Клиент другого дистрибьютора"} |

### 125. `/api/notifications/token/`
**Методы:** POST. **Доступ:** Bearer; клиентские ограничения описаны выше. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5249).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `platform` | `'android'` |
| JSON/form | `remove` | `None` |
| JSON/form | `token` | `None` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {status: "created" \| "updated"} |
| 401 | {detail: "Unauthorized"} |
| 400 | {detail: "token is required"} |
| 400 | {detail: "platform must be 'android' or 'ios'"} |
| 200 | {status: "removed"} |

### 126. `/api/ai/chat/`
**Методы:** POST. **Доступ:** Bearer; клиентские ограничения описаны выше. [Исходный контракт](https://github.com/sattarzhanovdev/autoterra-backend/blob/4555a42823b092005a546c7c79e194146a3f83a0/api/views.py#L5895).

| Где | Поле | Default / правило |
|---|---|---|
| JSON/form | `message` | `''` |
| элемент массива | `source` | `Без get-default; обязательность см. проверки` |

| HTTP | Форма / причина ответа |
|---|---|
| 200 | {answer: значение сервера, sources: значение сервера, suggestEscalation: значение сервера \| boolean} |
| 401 | {detail: "Unauthorized"} |
| 400 | {detail: "message должен быть строкой."} |
| 200 | {answer: "Пожалуйста, введите ваш вопрос.", suggestEscalation: false, sources: []} |
| 400 | {detail: "Вопрос слишком длинный (максимум 4000 символов)."} |

## Проверка интеграции

### Чек-лист фронтенда

- Регистрация содержит email, store_address и оба boolean-согласия; new/under_review показывают ожидание проверки.
- Роли autoservice/expert нормализованы; токен не декодируется как JWT.
- 401 и 403 account_not_active обрабатываются по-разному; не-JSON ошибки не ломают парсер.
- Пагинация заканчивается по hasNext; quote/finance/unified используют свои формы.
- Cart обновляет цены через quote, а создание заказа отдаёт вычисление цены серверу.
- Замена заказа вызывает replace, не cancel + create.
- Оплата доступна confirmed; возврат из браузера проверяется GET order, не считается подтверждением платежа.
- Cash включается только после permission; водитель завершает сборку и подтверждает наличные перед delivered.
- Фото из images отображаются с fallback; отсутствующий photos URL не генерируется из SKU.
- Excel импорт показывает created/updated/processed и warnings отдельно, включая floor дробных остатков.
- Knowledge upload использует правильный MIME и один файл на запрос; polling не продолжается после выхода с экрана.
- Приватные документы скачиваются с Bearer через Blob/FileResponse, не публичный media URL.
- Финансовые null-комиссии не превращаются в подтверждённый 0.
- CORS/preflight, HTTPS и ограничения proxy проверены на фактическом окружении перед релизом.

### Результаты локальной проверки

- 126/126 маршрутов имеют отдельные карточки, методы и ссылки на контракт указанного commit.
- 7 JSON-примеров синтаксически корректны.
- TypeScript Fetch-клиент прошёл `tsc --noEmit --strict --target ES2022 --lib ES2022,DOM`.
- 12 документированных сценариев реально выполнены на временной SQLite-базе: health, regions, регистрация, login, me, products, quote, создание/отмена заказа, покупка, AI и запрет доступа к knowledge-документам клиенту.
- 95 связанных тестов backend прошли (2 пропуска для условий СУБД); `manage.py check` не нашёл ошибок.
- Проверочные запросы к production не выполнялись. Только файл документации; код backend не менялся.

### Область сверки

Документ сверён с urlpatterns, permission helpers, serializers, formatter-функциями, состояниями модели и существующими тестами указанного commit. Полная карта автоматически проверяется на наличие всех 126 зарегистрированных API URL, включая aliases. JSON-примеры проходят синтаксическую проверку. Ключевые payload/response-формы проверяются локально на временной базе; production не используется. Это версия контракта кода: ручной Swagger в /api/schema/ не является исчерпывающей заменой этому файлу.

### Известные особенности, которые нельзя скрывать от интегратора

1. Swagger неполный и ошибочно обозначает Bearer как JWT.
2. erp/client-sync имеет конфликт массива и middleware; рабочего JSON-сценария для него здесь не заявлено.
3. Stock write endpoints не реализуют выбор distributorId для admin, несмотря на текст ошибки.
4. Многие legacy handlers не валидируют все choices/типы так же строго, как новые операции. Документ не обещает универсальный валидатор.
5. Сторонние фотографии могут быть HTTP или недоступными; model images хранит URL, а не файлы на backend.
6. Нет общего logout/refresh endpoint и единой схемы ошибок/пагинации для абсолютно всех маршрутов.
7. API не имеет здесь WebSocket/SSE/streaming endpoint чата. Ответ AI — обычный JSON; processing документа опрашивается отдельно.

При обновлении backend документ следует пересверить с новым commit. Не используйте examples ID, пароли и token placeholders как реальные учётные данные.
