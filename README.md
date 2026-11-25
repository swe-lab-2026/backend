# BiletFlow — архитектура backend

## 1. Назначение документа

Этот документ описывает backend-архитектуру учебного проекта BiletFlow — платформы для создания мероприятий, продажи и выдачи билетов, управления заказами и проверки QR-кодов на входе.

Зафиксированный стек проекта:

- **Backend:** Python + FastAPI;
- **Web frontend:** Next.js;
- **Mobile:** React Native;
- **Database:** PostgreSQL;
- **API:** REST/JSON;
- **Документация API:** OpenAPI, автоматически формируемая FastAPI.

На этапе Academic MVP используется **модульный монолит**. Все backend-модули работают в одном FastAPI-приложении и одной PostgreSQL-базе, но код разделён по бизнес-областям. Для учебной команды это проще микросервисов, а при росте проекта отдельные модули можно вынести в самостоятельные сервисы.

---

## 2. Общая схема системы

```mermaid
flowchart TD
    WEB[Next.js Web App]
    MOBILE[React Native App]
    API[FastAPI REST API]
    DB[(PostgreSQL)]
    EMAIL[Email provider]
    PAY[Payment provider or simulator]
    FILES[File storage]

    WEB -->|HTTPS + JSON| API
    MOBILE -->|HTTPS + JSON| API
    API -->|SQL transactions| DB
    API -->|Verification and tickets| EMAIL
    API -->|Payment request and webhook| PAY
    API -->|Event images| FILES
```

FastAPI является единственной точкой доступа к данным. Next.js и React Native не подключаются к PostgreSQL напрямую.

---

## 3. Архитектурный стиль

### 3.1. Модульный монолит

Каждый бизнес-модуль содержит собственные API-маршруты, схемы запросов и ответов, бизнес-логику и операции с базой данных. При этом все модули запускаются как одно приложение.

Основные преимущества для BiletFlow:

- единый репозиторий и простой локальный запуск;
- общая транзакция PostgreSQL для заказа, оплаты и выдачи билетов;
- меньше инфраструктуры и DevOps-работы;
- понятное распределение задач между участниками команды;
- возможность позже выделить Payments, Notifications или Check-in в отдельный сервис.

### 3.2. Слои backend

Внутри каждого модуля используются четыре логических слоя:

| Слой | Ответственность |
|---|---|
| API | HTTP-маршруты, параметры, DTO, коды ответа |
| Service | Бизнес-правила и сценарии использования |
| Repository | Запросы и транзакции PostgreSQL |
| Model | Таблицы, сущности и ограничения данных |

Контроллер API не должен самостоятельно выполнять сложные SQL-запросы или рассчитывать остатки билетов. Он проверяет входные данные, вызывает service и преобразует результат в HTTP-ответ.

```mermaid
flowchart LR
    ROUTER[API Router] --> SERVICE[Service]
    SERVICE --> REPO[Repository]
    REPO --> DB[(PostgreSQL)]
```

---

## 4. Структура backend-проекта

```text
backend/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── api/
│   │   ├── dependencies.py
│   │   └── v1/
│   │       └── router.py
│   ├── core/
│   │   ├── exceptions.py
│   │   ├── security.py
│   │   ├── permissions.py
│   │   └── logging.py
│   ├── modules/
│   │   ├── auth/
│   │   │   ├── router.py
│   │   │   ├── schemas.py
│   │   │   ├── service.py
│   │   │   ├── repository.py
│   │   │   └── models.py
│   │   ├── users/
│   │   ├── organizers/
│   │   ├── events/
│   │   ├── ticket_types/
│   │   ├── campaigns/
│   │   ├── inventory/
│   │   ├── orders/
│   │   ├── payments/
│   │   ├── tickets/
│   │   ├── checkin/
│   │   ├── notifications/
│   │   ├── support/
│   │   └── audit/
│   └── integrations/
│       ├── email.py
│       ├── payments.py
│       └── file_storage.py
├── migrations/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
├── .env.example
├── Dockerfile
└── requirements.txt
```

Внутри модуля разрешены зависимости на общий `core`, соединение с базой и публичные сервисы других модулей. Прямое изменение таблиц другого модуля следует выполнять только через его service либо явно согласованный repository-сценарий.

---

## 5. Backend-модули

### 5.1. Auth

Отвечает за:

- регистрацию по email и паролю;
- подтверждение email;
- вход и выход;
- access- и refresh-токены;
- обновление access-токена;
- восстановление пароля;
- завершение всех активных сессий;
- ограничение числа неудачных попыток входа.

### 5.2. Users

Отвечает за профиль пользователя, контактные данные, язык интерфейса, статус аккаунта и глобальную роль.

### 5.3. Organizers

Создаёт профиль организатора, хранит реквизиты и статус проверки. Один пользователь может быть обычным покупателем и одновременно организатором.

### 5.4. Events

Управляет созданием, редактированием, публикацией и отменой мероприятий, их описанием, местом, временем, изображениями и персоналом.

### 5.5. Ticket Types и Inventory

Управляет категориями билетов, ценой, лимитами и остатком. Остаток не хранится отдельным изменяемым числом:

```text
available = capacity - sold - active_holds
```

Резерв билетов создаётся на ограниченное время во время checkout. После оплаты резерв превращается в проданные билеты; после истечения времени — освобождается.

### 5.6. Orders

Создаёт заказ и позиции билетов, связывает покупателя с участником и контролирует срок checkout. При создании заказа количество билетов резервируется транзакционно на 10 минут; после истечения срока заказ становится `EXPIRED`, а резерв освобождается. Бесплатная регистрация сразу создаёт подтверждённый заказ со статусом `PAID`, суммой `0.00 KZT` и билетами — платёжные данные не нужны. Организатор видит только регистрации из подтверждённых заказов через `GET /api/v1/events/{event_id}/attendees`.

Основные статусы:

```text
PENDING -> PAID
PENDING -> EXPIRED
PENDING -> CANCELLED
PAID -> PARTIALLY_REFUNDED
PAID/PARTIALLY_REFUNDED -> REFUNDED
```

### 5.7. Payments

Содержит единый интерфейс для платёжного провайдера. Для лабораторной работы может использоваться симулятор оплаты, но доменная логика должна выглядеть так же, как при реальной интеграции.

Критичные требования:

- повторный webhook не должен повторно выдавать билеты;
- каждому платёжному запросу назначается idempotency key;
- подтверждение оплаты выполняется одной транзакцией;
- финансовые записи не удаляются физически.

### 5.8. Tickets

После успешной оплаты создаёт по одной записи `ticket` для каждого `order_item`. QR-код содержит случайный непрозрачный токен. В базе хранится только его криптографический хеш.

### 5.9. Check-in

Проверяет QR-код, принадлежность билета мероприятию и его статус. Двойной проход предотвращается блокировкой строки билета внутри транзакции.

### 5.10. Campaigns

Управляет промокодами, реферальными ссылками, периодом действия, лимитом использований и применимостью к типам билетов.

Реализовано в SCRUM-15 — см. раздел [Promo-code campaigns and campaign QR codes](#promo-code-campaigns-and-campaign-qr-codes-scrum-15).

### 5.11. Notifications

Отвечает за подтверждение email, восстановление пароля, отправку билета и уведомления об изменениях мероприятия.

- В development/test используется log-only `DevEmailProvider`; реальная отправка не выполняется.
- В production обязателен `EMAIL_PROVIDER=brevo`. Backend не запустится с dev-провайдером, без `BREVO_API_KEY` или с невалидным sender email.
- Brevo вызывается напрямую через `httpx` (`POST /v3/smtp/email`), без отдельной очереди. HTTP-клиент создаётся один раз в lifespan.
- Подтверждение заказа хранится в outbox `order_notifications` с уникальностью `(order_id, kind)`, поэтому повторный confirm не создаёт дубль письма и не выдаёт новые билеты.
- Временные ошибки Brevo (429, 5xx, timeout) повторяются с exponential backoff и учётом `Retry-After`; постоянные ошибки (400/401/403, невалидный адрес) помечаются `permanent:` и больше не повторяются.
- API-ключи, коды подтверждения, reset-токены и содержимое писем не логируются.

### 5.12. Support и Audit

Support хранит обращения пользователей. Audit фиксирует важные действия: публикацию события, изменение цены, возврат, check-in, изменение ролей и блокировку пользователя.

---

## 6. API

Все маршруты имеют префикс:

```text
/api/v1
```

Пример группировки endpoints:

| Модуль | Основные endpoints |
|---|---|
| Auth | `/auth/register`, `/auth/verify-email`, `/auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/resend-verification`, `/auth/logout-all`, `/auth/request-password-reset`, `/auth/reset-password` |
| Users | `/users/me` |
| Organizers | `/organizers/me`, `/organizers/me/payout-account` |
| Events | `/events/assigned`, `/events/{event_id}/admins`, `/events/{event_id}/admins/{user_id}`, `/events/{event_id}/attendees` |
| Ticket types | `/events/{event_id}/ticket-types` |
| Campaigns | `/events/{event_id}/campaigns`, `/events/{event_id}/campaigns/analytics`, `/campaigns/validate`, `/campaigns/qr/{campaign_token}` |
| Orders | `/orders`, `/orders/{order_id}` |
| Payments | `/payments`, `/payments/webhook` |
| Tickets | `/tickets`, `/tickets/{public_id}` |
| Check-in | `/events/{event_id}/check-in` |
| Support | `/support/cases`, `/support/cases/{case_id}/messages` |

### 6.1. Формат успешного ответа

```json
{
  "data": {
    "id": "uuid",
    "status": "PAID"
  }
}
```

### 6.2. Формат ошибки

```json
{
  "error": {
    "code": "TICKET_SOLD_OUT",
    "message": "Selected ticket type is sold out",
    "details": {}
  }
}
```

Для клиента важен стабильный `error.code`; текст `message` может переводиться или меняться.

---

## 7. Authentication и authorization

### 7.1. Выбранный вариант для MVP

Основной способ входа:

> **Email + password, с обязательным подтверждением email одноразовым кодом.**

OAuth2, SMS-вход и корпоративный SSO не входят в первый MVP. Их можно добавить позже, не меняя основную модель пользователей.

### 7.2. Регистрация

```mermaid
sequenceDiagram
    participant C as Next.js or React Native
    participant A as FastAPI
    participant D as PostgreSQL
    participant E as Email

    C->>A: POST /auth/register
    A->>D: Create unverified user
    A->>D: Store hash of verification code
    A->>E: Send 6-digit code
    C->>A: POST /auth/verify-email
    A->>D: Validate code and expiration
    A->>D: Set email_verified_at
    A-->>C: Account verified
```

Пароль хешируется стойким алгоритмом. Исходный пароль и исходный verification code в базе не сохраняются.

### 7.3. Вход

1. Клиент отправляет email и пароль на `/auth/login`.
2. Backend находит пользователя и проверяет хеш пароля.
3. Backend проверяет статус пользователя и подтверждение email.
4. Backend создаёт серверную refresh-session.
5. Клиент получает короткоживущий access token и refresh token.

Рекомендуемые сроки:

- access token — **15 минут**;
- refresh token — **30 дней**;
- email verification code — **10 минут**;
- password reset code — **10 минут**.

### 7.4. Хранение токенов на клиентах

Для Next.js:

- refresh token — `HttpOnly`, `Secure`, `SameSite=Lax` cookie;
- access token — короткоживущий, хранится в памяти приложения;
- не хранить токены в `localStorage`.

Для React Native:

- refresh token — в защищённом системном хранилище устройства;
- access token — в памяти приложения;
- запросы передают access token в `Authorization: Bearer <token>`.

В PostgreSQL хранится хеш refresh token, а не исходный token. При refresh старый refresh token заменяется новым. Повторное использование старого токена завершает всю связанную сессию.

### 7.5. Разница между authentication и authorization

- **Authentication** отвечает на вопрос: «Кто пользователь?»
- **Authorization** отвечает на вопрос: «Что этому пользователю разрешено?»

В BiletFlow authorization строится на ролях и владении ресурсом:

| Роль/условие | Разрешения |
|---|---|
| Guest | Смотреть опубликованные публичные события |
| User | Покупать билеты, видеть свои заказы и билеты |
| Event owner | Полностью управлять собственным событием |
| Event admin | Управлять конкретным событием в пределах выданных прав |
| Check-in staff | Только проверять билеты назначенного события |
| Platform admin | Управлять платформой, пользователями и спорными операциями |

Проверки выполняются на backend. Скрытая кнопка на frontend не является защитой.

Пример логики:

```python
if current_user.global_role == "PLATFORM_ADMIN":
    allow()
elif event.owner_user_id == current_user.id:
    allow()
elif has_event_permission(current_user.id, event.id, "EDIT_EVENT"):
    allow()
else:
    deny(status_code=403)
```

### 7.6. Дополнительные auth-таблицы

К основной SQL-схеме необходимо добавить:

| Таблица | Назначение |
|---|---|
| `refresh_sessions` | Активные устройства, хеш refresh token, срок действия, отзыв |
| `email_verification_tokens` | Хеш кода подтверждения email, срок и число попыток |
| `password_reset_tokens` | Хеш кода сброса пароля, срок и факт использования |

---

## 8. Ключевые сценарии

### 8.1. Покупка билета

```mermaid
sequenceDiagram
    participant C as Client
    participant A as FastAPI
    participant D as PostgreSQL
    participant P as Payment

    C->>A: Create order
    A->>D: Lock inventory and create hold
    A-->>C: Order PENDING
    C->>A: Start payment
    A->>P: Create payment
    P-->>A: Payment webhook
    A->>D: Mark payment and order PAID
    A->>D: Consume hold and create tickets
    A-->>P: 200 OK
```

Операции после подтверждения оплаты выполняются в одной транзакции. Повторная обработка webhook должна возвращать успешный ответ, но не создавать новые билеты.

### 8.2. Check-in

1. Мобильное приложение сканирует QR-код.
2. Отправляет токен и `event_id` в FastAPI.
3. Backend хеширует полученный токен и ищет билет.
4. PostgreSQL блокирует строку билета через `SELECT ... FOR UPDATE`.
5. Backend проверяет событие и статус `VALID`.
6. Статус меняется на `CHECKED_IN`, создаётся `checkin_record`.
7. Повторное сканирование возвращает конфликт и время первого прохода.

---

## 9. Работа с PostgreSQL

PostgreSQL является единственным источником истины для пользователей, заказов, остатков, платежей и билетов.

Обязательные правила:

- UUID для бизнес-сущностей;
- `TIMESTAMPTZ` для дат и времени;
- денежные значения — `NUMERIC(12,2)`, не `float`;
- миграции базы хранятся в Git;
- финансовые, билетные и audit-записи не удаляются физически;
- создание заказа, списание inventory и check-in используют транзакции;
- внешние ключи и уникальные ограничения остаются в базе, а не только в Python;
- запросы каталога, заказов и check-in должны использовать индексы из SQL-схемы.

### 9.1. Защита от overselling

Во время создания резерва backend:

1. начинает транзакцию;
2. блокирует нужный `ticket_types`;
3. считает оплаченные билеты и активные неистёкшие holds;
4. сравнивает результат с capacity;
5. создаёт hold либо возвращает `TICKET_SOLD_OUT`;
6. завершает транзакцию.

Обычная проверка остатка без блокировки недостаточна: два параллельных запроса могут одновременно увидеть последний билет.

---

## 10. Security

- Только HTTPS вне локальной разработки.
- Пароли хешируются и никогда не логируются.
- Секреты находятся в environment variables, а `.env` не добавляется в Git.
- Для login, verification и reset password вводится ограничение попыток.
- Все входные данные валидируются FastAPI-схемами.
- CORS разрешает только адреса Next.js и мобильного клиента в нужной среде.
- Webhook оплаты проверяется по подписи провайдера.
- Идентификатор объекта из URL всегда проверяется вместе с правами пользователя.
- Для чувствительных операций создаётся audit log.
- QR-токен должен быть случайным и непредсказуемым; в базе хранится только хеш.

---

## 11. Конфигурация

Минимальный `.env.example`:

```dotenv
APP_ENV=development
APP_NAME=BiletFlow API
API_PREFIX=/api/v1
DATABASE_URL=postgresql://user:password@localhost:5432/biletflow
JWT_ACCESS_SECRET=change-me
JWT_REFRESH_SECRET=change-me
ACCESS_TOKEN_TTL_MINUTES=15
REFRESH_TOKEN_TTL_DAYS=30
EMAIL_CODE_TTL_MINUTES=10
FRONTEND_WEB_URL=http://localhost:3000
CORS_ORIGINS=http://localhost:3000
EMAIL_FROM=BiletFlow <no-reply@biletflow.local>

# dev для локальной разработки; в production обязателен brevo.
EMAIL_PROVIDER=dev
BREVO_API_KEY=
BREVO_SENDER_EMAIL=no-reply@your-verified-domain.com
BREVO_SENDER_NAME=BiletFlow
BREVO_TIMEOUT_SECONDS=10
NOTIFICATION_RETRY_BASE_SECONDS=30
NOTIFICATION_RETRY_MAX_SECONDS=3600
PAYMENT_MODE=simulated
```

Для Brevo sender email должен быть подтверждён в аккаунте Brevo (Sender
Authentication). `BREVO_SENDER_EMAIL`/`BREVO_SENDER_NAME` имеют приоритет; если
не заданы, используется адрес из `EMAIL_FROM`. Автоматические тесты всегда
используют `DevEmailProvider` и не делают сетевых вызовов.

Реальные значения секретов не должны находиться в репозитории.

---

## 12. Тестирование

### Unit tests

- расчёт цены и скидки;
- допустимые переходы статусов;
- проверка permissions;
- создание и проверка токенов;
- правила возврата.

### Integration tests

- операции repository с тестовой PostgreSQL;
- транзакционное резервирование билетов;
- повторная обработка payment webhook;
- блокировка двойного check-in;
- отзыв refresh-session.

### E2E tests

- регистрация → подтверждение email → login;
- создание события → публикация;
- покупка → оплата → получение QR-билета;
- сканирование QR → повторное сканирование;
- отмена/возврат.

---

## 13. Логирование и health checks

Логи должны содержать request ID, endpoint, HTTP status, duration и user ID, если пользователь авторизован. Пароли, access/refresh tokens, QR-токены и полные платёжные данные логировать запрещено.

Endpoints состояния:

```text
GET /health/live   — процесс FastAPI запущен
GET /health/ready  — FastAPI может подключиться к PostgreSQL
```

---

## 14. Порядок реализации

1. Создать FastAPI skeleton, конфигурацию и соединение с PostgreSQL.
2. Подключить миграции и базовые модели пользователей.
3. Реализовать email/password auth и refresh sessions.
4. Реализовать роли, ownership и permissions.
5. Реализовать события и типы билетов.
6. Реализовать inventory holds и заказы.
7. Подключить симулятор оплаты и идемпотентную обработку результата.
8. Генерировать билеты и QR-токены.
9. Реализовать check-in.
10. Добавить campaigns, notifications, support и audit.
11. Покрыть критичные сценарии интеграционными и E2E-тестами.

---

## 15. Принятые решения для Academic MVP

| Вопрос | Решение |
|---|---|
| Backend | FastAPI |
| Web | Next.js |
| Mobile | React Native |
| Database | PostgreSQL |
| Архитектура | Модульный монолит |
| API | REST `/api/v1` |
| Login | Email + password |
| Подтверждение | Одноразовый код на email |
| Сессия | Access token + rotating refresh token |
| Permissions | Global role + event ownership + event permissions |
| Payment | Симулятор за адаптером для MVP |
| Ticket | Одна запись на одного участника, QR с непрозрачным токеном |
| SMS | Не входит в MVP |
| OAuth2 login | После MVP при необходимости |
| SSO | Не требуется для текущего типа продукта |

---

## 16. Профиль организатора и Event Admin

### 16.1. Профиль организатора

Все endpoints требуют подтверждённый email (`Depends(require_verified_user)`) и работают только с профилем текущего JWT-пользователя. `user_id`, `verification_status`, `created_at` и `updated_at` нельзя задать через body.

```http
POST   /api/v1/organizers/me                  # создать профиль (201)
GET    /api/v1/organizers/me                  # свой профиль (404 ORGANIZER_PROFILE_NOT_FOUND, если нет)
PATCH  /api/v1/organizers/me                  # частичное обновление
PUT    /api/v1/organizers/me/payout-account   # демонстрационный payout-аккаунт
```

Пример создания профиля:

```json
{
  "display_name": "BiletFlow Events",
  "legal_name": "BiletFlow Events LLP",
  "contact_email": "organizer@example.com",
  "contact_phone": "+77001234567",
  "terms_accepted": true
}
```

- Один пользователь — один `OrganizerProfile`; повторное создание → `409 ORGANIZER_PROFILE_ALREADY_EXISTS`.
- `verification_status` начинается с `NOT_SUBMITTED` и меняется только бэкендом.
- Payout — демонстрационная информация: `status` всегда `PENDING` при создании, `external_account_ref` маскируется в ответе (`****-001`). Номера карт/CVV не принимаются и не сохраняются.
- Если `is_default=true`, предыдущий default-аккаунт этого организатора автоматически снимается (одна транзакция).

### 16.2. Event Admin

Event Admin — **event-scoped** роль (`event_staff.role = EVENT_ADMIN`), а не глобальная. При назначении `users.global_role` не меняется. Назначения не кладутся в JWT: права проверяются запросом к PostgreSQL на каждый вызов, поэтому удаление назначения действует немедленно.

```http
GET    /api/v1/events/assigned                        # события, где current_user имеет доступ к check-in: владелец или EVENT_ADMIN/CHECK_IN_STAFF (пагинация limit/offset)
GET    /api/v1/events/{event_id}/admins               # список Event Admin события
POST   /api/v1/events/{event_id}/admins               # назначить (body: {"email": "..."})
DELETE /api/v1/events/{event_id}/admins/{user_id}     # удалить назначение (204)
```

Пример назначения:

```json
{
  "email": "event-admin@example.com"
}
```

Правила назначения:

- назначать/удалять может только владелец события или `PLATFORM_ADMIN`;
- целевой пользователь должен существовать, быть `ACTIVE` и иметь подтверждённый email;
- нельзя назначить владельца администратором собственного события (`409 EVENT_OWNER_CANNOT_BE_ADMIN`);
- повторное назначение `(event_id, user_id)` → `409 EVENT_ADMIN_ALREADY_ASSIGNED`;
- Event Admin получает только `{"view_event": true, "check_in_tickets": true}` — он не может менять владельца, управлять персоналом или видеть другие события.

Event Admin видит только события, где есть запись `event_staff(event_id, user_id)`. Фильтрация выполняется SQL-join-ом `events ⋈ event_staff`, а не в Python.

### 16.3. PLATFORM_ADMIN vs EVENT_ADMIN

| | PLATFORM_ADMIN | EVENT_ADMIN |
|---|---|---|
| Область | Платформа целиком | Одно конкретное событие |
| Хранение | `users.global_role` | строка в `event_staff` |
| Назначение | вручную на бэкенде | владелец события или `PLATFORM_ADMIN` |
| Видит события | любые | только назначенные |
| Управляет персоналом события | да | нет |
| Check-in события | да | да (пока назначение активно) |

### 16.4. Event-scoped authorization

- `require_event_owner(event_id)` — владелец события или `PLATFORM_ADMIN` (управление персоналом).
- `require_event_staff_role(*roles)` — только персонал события с указанной ролью.
- `require_event_checkin_access(event_id)` — владелец, `PLATFORM_ADMIN` или активный `EVENT_ADMIN`/`CHECK_IN_STAFF` события. Предназначена для будущего check-in API.

Отказы фиксируются в `audit_logs` как `event.staff_access_denied`.

### 16.5. Audit

| Действие | `action` |
|---|---|
| Создание профиля | `organizer.profile_created` |
| Обновление профиля | `organizer.profile_updated` |
| Изменение payout-аккаунта | `organizer.payout_account_updated` |
| Назначение Event Admin | `event.admin_assigned` |
| Удаление Event Admin | `event.admin_removed` |
| Отказ в event-доступе | `event.staff_access_denied` |

В audit не попадают password hashes, JWT, refresh tokens и полные payout references.

### 16.6. Локальный запуск тестов

```bash
cp .env.example .env            # заполнить JWT_ACCESS_SECRET / JWT_REFRESH_SECRET
pip install -e ".[dev]"         # pytest, aiosqlite, ruff, mypy
make lint                       # ruff check app tests
make typecheck                  # mypy
make test                       # pytest
make migrate                    # применить SQL-миграции к DATABASE_URL
```

SQL-миграции (`sql/migrate_*.sql`) выполняются версионированно: каждая — ровно один раз, применённые версии хранятся в таблице `schema_migrations`. Базы, созданные до введения трекинга, определяются по существующим таблицам и получают базовые версии без повторного выполнения.

Тесты по умолчанию используют sqlite in-memory (тестовый fallback проекта). Для прогона на отдельной тестовой PostgreSQL (включая тест параллельного резервирования и сценарии миграций):

```bash
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/test_db make test
```

CI (`.github/workflows/ci.yml`) выполняет lint, typecheck, тесты на sqlite и полный сьют на PostgreSQL 17.



## Paid ticket sales (demo payment simulation)

Paid sales are activated **per event**. The event owner can inspect the checklist,
accept the terms, simulate the activation fee, and activate sales only after the
organizer profile is verified and an ACTIVE payout account exists.

- `GET /api/v1/events/{event_id}/paid-sales/activation`
- `POST /api/v1/events/{event_id}/paid-sales/terms` with `{"accepted": true}`
- `POST /api/v1/events/{event_id}/paid-sales/activation-fee` (internal demo only)
- `POST /api/v1/events/{event_id}/paid-sales/activate`

The activation-fee endpoint records a **simulation**, not a real charge. The
demo activation fee is 1,000 KZT.

Paid order creation is rejected until that event is activated. The backend
calculates a 3% processing fee (rounded to two decimal places) and stores the
result in the order. The current demo checkout supports KZT ticket types.

1. Create a pending order with `POST /api/v1/orders`.
2. Start the internal simulation with
   `POST /api/v1/orders/{order_id}/payments/simulate`.
3. Resolve the demo payment with
   `POST /api/v1/orders/payments/{payment_id}/simulate-result` and
   `{"outcome": "success"}`, `{"outcome": "failed"}`, or
   `{"outcome": "abandoned"}`.

Every simulated payment response is marked `is_simulated: true` and carries a
demo-only notice. No real payment provider is contacted. A successful result
confirms the order and issues tickets; failed/abandoned results cancel the
pending order and release its inventory holds. The unique simulated-payment
index and row locking make repeated starts/callbacks idempotent. The legacy
`/confirm` and `/checkout` endpoints no longer allow clients to self-confirm
paid orders; use the payment confirmation flow instead.

Apply the new `migrate_006_paid_sales.sql` migration before using these
endpoints (it is included in the default startup migration list).


## Promo-code campaigns and campaign QR codes (SCRUM-15)

A campaign belongs to one event and carries a promo code, a discount, an
optional validity window, an optional redemption limit and an optional list of
ticket types. Organizer endpoints require the event owner (or a
`PLATFORM_ADMIN`) through the shared `require_event_owner` dependency.

### Endpoints

| Method | Path | Access | Purpose |
|---|---|---|---|
| `POST` | `/api/v1/events/{event_id}/campaigns` | owner | Create a campaign |
| `GET` | `/api/v1/events/{event_id}/campaigns` | owner | List campaigns |
| `GET` | `/api/v1/events/{event_id}/campaigns/{campaign_id}` | owner | Campaign details |
| `PATCH` | `/api/v1/events/{event_id}/campaigns/{campaign_id}` | owner | Update / disable (`{"is_active": false}`) |
| `GET` | `/api/v1/events/{event_id}/campaigns/{campaign_id}/analytics` | owner | Performance of one campaign |
| `GET` | `/api/v1/events/{event_id}/campaigns/analytics` | owner | Performance of all event campaigns |
| `GET` | `/api/v1/events/{event_id}/campaigns/{campaign_id}/qr.png` | owner | PNG of the campaign QR link |
| `POST` | `/api/v1/campaigns/validate` | signed-in user | Validate a code and preview prices (never consumes) |
| `GET` | `/api/v1/campaigns/qr/{campaign_token}` | public | Resolve a scanned campaign QR (never consumes) |
| `POST` | `/api/v1/orders` | signed-in user | Checkout; accepts optional `promo_code` **or** `campaign_token` |

Create request:

```json
{
  "name": "Spring promo",
  "code": "SPRING10",
  "discount_type": "PERCENT",
  "discount_value": "10",
  "starts_at": "2026-12-01T00:00:00+05:00",
  "expires_at": "2026-12-31T23:59:59+05:00",
  "redemption_limit": 100,
  "ticket_type_ids": ["<ticket-type-uuid>"],
  "is_active": true
}
```

Validation: `PERCENT` is `0 < v <= 100`; `FIXED` is a positive KZT amount with
at most 2 decimals; `expires_at > starts_at`; timestamps need a timezone
offset (stored in UTC); `redemption_limit >= 1` when given; ticket types must
belong to the campaign's event; codes are 3–40 chars `[A-Za-z0-9_-]`, stored
upper-case and unique per event (case-insensitive). An empty `ticket_type_ids`
means the campaign applies to all ticket types. The discount cannot change once
the code has been used (`409 CAMPAIGN_IN_USE`), and the limit cannot drop below
current usage (`409 REDEMPTION_LIMIT_BELOW_USAGE`). Campaigns are never
deleted; disable them so attribution stays intact.

Checkout with a promotion:

```json
POST /api/v1/orders
{"event_id": "...", "items": [{"ticket_type_id": "...", "quantity": 2}], "promo_code": "spring10"}
```

Response fields added to every order: `campaign_id` and `promo_code` (the
historical code snapshot); `subtotal_amount`, `discount_amount`, `fee_amount`
and `total_amount` already existed.

### Promo errors

All use the standard error envelope (`{"error": {"code", "message", "details"}}`).

| HTTP | Code | Meaning |
|---|---|---|
| 404 | `PROMO_NOT_FOUND` | No such code for this event / unknown token |
| 409 | `PROMO_DISABLED` | Campaign is disabled |
| 409 | `PROMO_NOT_STARTED` | Before `starts_at` |
| 409 | `PROMO_EXPIRED` | At/after `expires_at` |
| 409 | `PROMO_LIMIT_REACHED` | Redemption limit exhausted |
| 409 | `PROMO_NOT_APPLICABLE` | No eligible ticket types in the order, eligible items are free, token from another event, or `FIXED` on a non-KZT order |
| 409 | `PROMO_ALREADY_RESERVED` | The same customer already has a pending checkout with this code; `details.order_id` points to it |
| 404 | `CAMPAIGN_NOT_FOUND` | QR token unknown, malformed, or its event is not published |

### Discount calculation (server-side only)

The backend recalculates every amount at checkout. Monetary fields sent by a
client are ignored. All maths uses `Decimal` and `NUMERIC(12,2)`, never floats.

- **Eligibility:** only units of eligible ticket types are discounted. Other
  units in the same order pay full price (mixed orders are allowed).
- **PERCENT:** each eligible unit gets `round_half_up(unit_price × pct / 100, 0.01)`.
  The order discount is the sum of the unit discounts.
- **FIXED (KZT):** one amount per order, capped at the eligible subtotal. It is
  split across eligible units in proportion to price, in whole tiyn. Leftover
  tiyn go to the first units in ticket-type-id order, so the split is
  deterministic. Totals can never go negative.
- **Fee:** the 3% processing fee is charged on the discounted amount.
  `total = subtotal − discount + fee` (enforced by the `orders_total_formula`
  CHECK).
- **No stacking:** one promotion per order. Sending both `promo_code` and
  `campaign_token` returns `422`.
- A discount that brings the payable amount to `0.00` confirms the order
  immediately (no payment step). Events with priced tickets still need paid
  sales to be activated.

Example: 2 × 25.00 KZT with 10% → subtotal 50.00, discount 5.00, fee 1.35,
total 46.35.

### Redemption lifecycle and concurrency

`promo_redemptions.status` (one row per order) moves through:

```text
checkout (paid)  -> RESERVED --payment succeeded--> REDEEMED
checkout (free)  -> REDEEMED
RESERVED --payment failed/abandoned, reservation expired, order cancelled--> RELEASED
REDEEMED --order cancelled/refunded--------------------------------------> RELEASED
```

- `campaigns.redemption_count` = RESERVED + REDEEMED. Checkout takes capacity
  with a conditional `UPDATE … SET redemption_count = redemption_count + 1
  WHERE redemption_count < max_redemptions`. Under concurrency PostgreSQL
  re-checks that predicate on the latest row version, and the CHECK constraint
  `campaigns_redemption_count_valid` keeps the count at or below the limit.
  Checkouts for one event also serialize on the event row lock.
- Releases are guarded by the status predicate, so retries and concurrent
  cleanup never release twice or push the counter negative.
- A pending paid checkout holds capacity until its reservation TTL
  (`ORDER_RESERVATION_TTL_MINUTES`). A failed, abandoned or expired checkout
  returns that capacity. Previewing (`/campaigns/validate`) and QR resolution
  are read-only.
- A partial unique index allows one RESERVED redemption per customer per
  campaign. A repeated checkout gets `PROMO_ALREADY_RESERVED` and cannot drain
  the limit.
- A reservation is honoured at payment time even if the campaign has expired
  or been disabled since checkout (the price was locked when the order was
  created).

### Campaign QR codes (separate from admission QR)

- A campaign QR is a **promotional link, not a ticket**. It encodes
  `{PUBLIC_WEB_BASE_URL}/campaigns/{campaign_token}`. The token is a random
  UUIDv4 (122 bits from the OS CSPRNG): opaque, not sequential, and it carries
  no user data or secrets.
- Admission QR codes are HMAC-signed `v1.<ticket public_id>.<signature>`
  payloads. The check-in and validate endpoints explicitly reject campaign
  tokens, bare or as a URL, with `400 CAMPAIGN_QR_NOT_ADMISSION`. Any other
  URL or random string gets `401 INVALID_TICKET_QR`. Ticket payloads do not
  resolve as campaigns.
- `PUBLIC_WEB_BASE_URL` has no default, so no production domain is invented.
  While it is empty, responses include `campaign_token` and a relative
  `qr_path`, `qr_url` is `null`, and `qr.png` returns
  `409 CAMPAIGN_LINK_NOT_CONFIGURED`. Production requires an `https://` origin.

**Frontend integration contract:**

1. The web app serves the route `/campaigns/:token`.
2. That page calls `GET /api/v1/campaigns/qr/{token}`. The response has the
   event summary, the promotion (name, discount type and value, window,
   eligible ticket types) and `checkout: {event_id, campaign_token}`. It
   exposes no code, counts or buyer data.
3. Optionally preview prices with `POST /api/v1/campaigns/validate` using
   `campaign_token`.
4. Check out with `POST /api/v1/orders`, passing `campaign_token`. The server
   validates the promotion again and prices the order itself.

### Analytics definitions

`GET …/campaigns/{campaign_id}/analytics` is computed from the database at
request time:

| Field | Definition |
|---|---|
| `redemption_count` | Capacity in use now (RESERVED + REDEEMED) |
| `reserved_redemptions` | Pending paid checkouts |
| `successful_redemptions` | REDEEMED (paid/free-confirmed, not reversed) |
| `released_redemptions` | Failed, expired, cancelled or refunded usages |
| `discount_total` | Sum of discounts on REDEEMED rows |
| `attributed_order_count` / `attributed_revenue` | **Net**: orders currently `PAID`/`CONFIRMED`, sum of `total_amount` actually charged |
| `gross_attributed_order_count` / `gross_attributed_revenue` | Every order that was ever paid/confirmed (`paid_at` set), including ones later reversed |
| `reversed_order_count` / `reversed_revenue` | Paid orders later cancelled or refunded (gross − net) |

Refunds and cancellations lower net revenue and successful redemptions. Gross
figures keep the historical sale. Pending, expired and failed checkouts never
count as sales. Revenue includes the processing fee because it is the amount
the customer paid.

### Migration

`sql/migrate_007_campaigns.sql` (in the default `SCHEMA_FILES` list, applied
on startup or with `make migrate`) adds:
- `campaigns.redemption_count` and its CHECK constraint
- `promo_redemptions.status`, `user_id`, `released_at` and `updated_at`
- `orders.promo_code_snapshot`
- indexes for the reservation uniqueness, campaign/status lookups and orders
  by campaign

All changes are additive with defaults. Existing rows are backfilled and
nothing is dropped.


## Cancellations, refunds, notifications and audit log (SCRUM-14)

All refunds are **simulated** (`is_simulated: true`): no payment provider is
contacted and no real money moves. Only full refunds are supported.

### Endpoints

| Method | Path | Access | Purpose |
|---|---|---|---|
| `GET` | `/api/v1/events/{event_id}/refund-policy` | public (visible events) | Read the refund policy |
| `PUT` | `/api/v1/events/{event_id}/refund-policy` | owner | Set `refunds_enabled`, `refund_deadline_hours`, `policy_text` |
| `POST` | `/api/v1/orders/{order_id}/cancel` | buyer | Cancel a pending or free order, or refund a paid one (returns `OrderResponse`) |
| `POST` | `/api/v1/orders/{order_id}/refund` | buyer | Explicit refund of a paid order (returns `RefundResultResponse`) |
| `GET` | `/api/v1/orders/{order_id}/refunds` | buyer, owner, admin | Refund records for the order |
| `GET` | `/api/v1/orders/{order_id}/refund-eligibility` | buyer | Read-only preview: cancellable or refundable, reason, amount, deadline |
| `POST` | `/api/v1/events/{event_id}/cancel` | owner | Cancel the event (optional body `{"reason": "..."}`); safe to repeat |
| `GET` | `/api/v1/events/{event_id}/cancellation` | owner | Cancellation progress and totals |
| `GET` | `/api/v1/events/{event_id}/audit-log?limit=&offset=&action=` | owner | Read-only audit trail for the event |

`PLATFORM_ADMIN` may act wherever the owner or buyer can.

```json
PUT /api/v1/events/{event_id}/refund-policy
{"refunds_enabled": true, "refund_deadline_hours": 24, "policy_text": "Full refund until 24h before start"}

POST /api/v1/orders/{order_id}/refund  ->  200
{"order_id": "...", "order_status": "REFUNDED", "replayed": false,
 "refund": {"id": "...", "amount": "23.18", "currency": "KZT", "status": "SUCCEEDED",
            "kind": "CUSTOMER_REQUEST", "is_simulated": true, "...": "..."},
 "notice": "DEMO ONLY — refunds are simulated; no real money is moved."}
```

### Refund eligibility rules (enforced server-side)

**Customer requests** (`/cancel`, `/refund`) are allowed only for the **buyer**
(or a platform admin) and only when:

- the order is confirmed;
- no ticket has been checked in (`409 TICKET_ALREADY_CHECKED_IN`);
- the event has not started (`409 EVENT_ALREADY_STARTED`).

A **paid** order must also meet the event policy:

- `refunds_enabled` is true (`409 REFUNDS_DISABLED`);
- the request is before `starts_at − refund_deadline_hours` (`409 REFUND_DEADLINE_PASSED`).

Rejected requests are recorded in the audit log as `refund.denied`.

- New and existing events default to **refunds disabled**.
- A free registration can be cancelled until the event starts. It never creates
  a monetary refund, and asking it for a refund returns `409 ORDER_NOT_PAID`.
- A pending, unpaid checkout can always be cancelled. Its hold and promo
  reservation are released and the pending simulated payment is marked
  `CANCELLED`.
- **The event owner may cancel or refund individual orders** of their own event
  through `/orders/{id}/cancel` and `/refund` (SRS 4.9). A paid order gets a full
  refund (`kind = ORGANIZER_REQUEST`) regardless of `refunds_enabled`, the
  deadline or the start time; a free registration is cancelled without a
  refund; checked-in orders are refused. Other organizers get `403`.

**Organizer event cancellation** follows its own policy. Every paid order gets
a full refund regardless of `refunds_enabled` or the deadline. Free
registrations are cancelled without a refund, and pending checkouts are
cancelled. Refund records carry `kind`: `CUSTOMER_REQUEST`,
`EVENT_CANCELLATION` or `ORGANIZER_REQUEST`.

### Lifecycle transitions

```text
Paid refund:      Order PAID -> REFUNDED | Payment SUCCEEDED -> REFUNDED
                  Refund PENDING -> SUCCEEDED (simulated, same transaction)
                  Tickets VALID -> REFUNDED
Free cancel:      Order PAID/CONFIRMED -> CANCELLED | Tickets VALID -> CANCELLED
Pending cancel:   Order PENDING -> CANCELLED | Payment PENDING -> CANCELLED | holds RELEASED
Event cancelled:  Event -> CANCELLED (cancelled_at, cancellation_reason), then each order as above
```

Original financial records stay unchanged, and refunds are separate rows:

- payment `amount` and `paid_at`;
- order `subtotal_amount`, `discount_amount`, `total_amount` and `paid_at`.

The refund amount is the payment actually charged, so a promo order refunds
its discounted total including the fee. Every transition also releases the
promo redemption (SCRUM-15) and frees inventory.

Refunded and cancelled tickets fail both `/tickets/validate`
(`valid: false, reason: TICKET_REFUNDED | TICKET_CANCELLED`) and `/check-in`
(`409 TICKET_NOT_VALID`). This is the existing admission check; no second
validation path was added.

### Idempotency and concurrency

- Every refund or cancellation locks the **order row, then its payment**. The
  payment simulation uses the same order (changed from payment-then-order), so
  the flows serialize without deadlocks.
- A refund is only created from `PAID` + `SUCCEEDED`. A partial unique index
  `uq_refunds_active_per_payment` allows **one non-failed refund per payment**
  at the database level.
- Repeating `/refund` or `/cancel` returns `200` with the stored result and
  `replayed: true` (for `/refund`). Nothing is processed again and no second
  email is queued. Concurrent requests serialize on the row lock: one does the
  work and the rest replay.
- No network call happens while business rows are locked. Emails are queued in
  the outbox and sent after commit.

### Event cancellation processing

1. The event row is locked, set to `CANCELLED` (with an audit entry) and
   committed, so new checkouts and payments are rejected (`EVENT_CANCELLED`).
2. Active orders are closed in committed batches of
   `EVENT_CANCELLATION_BATCH_SIZE`, using `FOR UPDATE SKIP LOCKED`. Progress is
   the orders themselves: an order leaves PENDING/PAID/CONFIRMED exactly once.
3. If the process dies mid-way, calling `POST /events/{id}/cancel` again, or
   the background sweep (every `RESERVATION_CLEANUP_INTERVAL_SECONDS`),
   resumes with the remaining orders. Refunds and notifications are never
   duplicated.
4. `GET /events/{id}/cancellation` reports `orders_remaining`,
   `orders_refunded`, `orders_cancelled`, `event_cancellation_refund_total` and
   `complete`.

Campaign attribution (`orders.campaign_id`, `promo_code_snapshot`) is kept, and
campaign analytics count these orders as reversed.

### Notifications

Notifications use the existing transactional outbox (`order_notifications`)
and the existing provider (Brevo in production, the log-only dev provider
otherwise). Rows are written in the **same transaction** as the business
change and delivered after commit; the retry worker uses exponential backoff
and treats permanent errors as final. Uniqueness on
`(order_id, kind, dedupe_key)` makes enqueueing idempotent.

| Kind | Trigger | Dedupe | Context in the email |
|---|---|---|---|
| `PAYMENT_FAILED` | simulated payment `failed` (not `abandoned`) | per payment | event, order #, amount, status |
| `EVENT_UPDATED` | owner changes title, start/end time, timezone, venue name/address or city on a published event | per update × order | event, order #, changed fields, timestamp |
| `EVENT_CANCELLED` | event cancellation (confirmed orders) | per order | event, order #, reason, refund amount (if any), timestamp |
| `REFUND_COMPLETED` | customer refund | per order | event, order #, refund amount, timestamp, demo notice |

Event-cancellation refunds are announced inside the `EVENT_CANCELLED` email,
so buyers get one email, not two. Emails contain no payment details, tokens or
other customers' data, and all user text is HTML-escaped.

### Audit log

`audit_logs` gives each entry these fields:

- `id`, `timestamp` (`created_at`)
- `actor_id` (`actor_user_id`), `actor_type` (`CUSTOMER`, `ORGANIZER`,
  `PLATFORM_ADMIN`, `USER` or `SYSTEM`)
- `action`, `entity_type`, `entity_id`, `event_id`
- `metadata`, `before`, `after`
- `request_id` (the `X-Request-ID` correlation id)

Entries are written in the business transaction, so they commit or roll back
together with it.

Recorded actions include `event.updated`, `event.cancelled`,
`event.refund_policy_updated`, `order.cancelled`, `order.refunded`,
`refund.requested`, `refund.completed`, `refund.denied`,
`payment.state_changed` and `campaign.created/updated`.

Keys that look like credentials (`password`, `token`, `secret`, `api_key`,
`card`, …) are stripped, and no emails or payment secrets are written.

**Append-only through ordinary interfaces:**

- **API:** only `GET` exists; `POST`, `PUT`, `PATCH` and `DELETE` return
  `405`. Owners see only their own events.
- **ORM:** a `before_update`/`before_delete` listener raises.
- **PostgreSQL:** the trigger `trg_audit_logs_append_only` rejects `UPDATE`,
  `DELETE` and `TRUNCATE`. The one exception is the FK cascade that nulls
  `actor_user_id`/`event_id` when a user or draft event is deleted.

This is **not cryptographic immutability**: a database superuser or the table
owner can disable the trigger. For stronger guarantees, run the API with a
role that does not own the table.

### Migration

`sql/migrate_008_refunds_audit.sql` is in the default `SCHEMA_FILES` and is
applied on startup or with `make migrate`. It adds:

- event policy and cancellation columns;
- refund `kind`, `currency`, `is_simulated`, `completed_at` and the unique
  refund index;
- outbox `dedupe_key` and `payload` (replacing the `(order_id, kind)` unique
  constraint with `(order_id, kind, dedupe_key)`);
- audit `actor_type`, `metadata`, `request_id`, an event index and the
  append-only trigger.

All changes are additive with defaults. Existing audit rows with an actor are
backfilled to `actor_type = 'USER'`.

### Testing simulated refunds manually

1. Create and publish an event with a paid KZT ticket type and activate paid
   sales (see *Paid ticket sales* above).
2. `PUT /events/{id}/refund-policy` with `{"refunds_enabled": true}`.
3. Buy: `POST /orders`, then `POST /orders/{id}/payments/simulate`, then
   `POST /orders/payments/{payment_id}/simulate-result` with `{"outcome": "success"}`.
4. `GET /orders/{id}/refund-eligibility`, then `POST /orders/{id}/refund`.
   Repeat the call and expect `replayed: true`.
5. Scan the old ticket QR at `/events/{id}/check-in` and expect `409 TICKET_NOT_VALID`.
6. `GET /events/{id}/audit-log` to see the trail. With `EMAIL_PROVIDER=dev`,
   emails appear in the API logs.
7. For event cancellation: `POST /events/{id}/cancel` with a reason, then
   `GET /events/{id}/cancellation`.

Automated: `make test` (SQLite) or, for the locking, concurrency and trigger
tests, `TEST_DATABASE_URL=postgresql+asyncpg://… make test`.

## Support cases and asynchronous messages (SCRUM-16)

REST + polling (no WebSockets). Lifecycle history is written to the existing
append-only `audit_logs`; messages live in `support_messages`; notifications
are in-app rows in the existing `notifications` table.

### Endpoints

| Method | Path | Who | Purpose |
|---|---|---|---|
| `POST` | `/api/v1/support/cases` | any active user | Open a case (`subject`, `category`, `message`, optional `ticket_id` / `order_id` / `event_id`, optional `case_type`) |
| `GET` | `/api/v1/support/cases` | any active user | Accessible cases only; `scope=mine\|event\|all` (all: Platform Admins), filters `status` (repeatable), `category`, `case_type`, `event_id`, `escalated`, `assigned_to_me`, `role=requester\|handler`, `q`; `sort`, `limit`, `offset` |
| `GET` | `/api/v1/support/cases/{id}` | participant | Details + caller's `access` (`relation`, `can_manage`, `can_escalate`) |
| `GET` | `/api/v1/support/cases/{id}/messages` | participant | Oldest first; `after=<message_id>` or `after_sequence` (polling), `before_sequence` (history), `since` (timestamp), `limit` ≤ 100 |
| `POST` | `/api/v1/support/cases/{id}/messages` | participant | Send a message (1–5000 chars, not blank) |
| `PATCH` | `/api/v1/support/cases/{id}` | participant (see rules) | `{"status"?, "assignee_user_id"?, "note"?}`; omitted fields unchanged, `null` assignee unassigns; all-or-nothing |
| `PATCH` | `/api/v1/support/cases/{id}/status` | participant (see rules) | `{"status", "note"?}` |
| `PATCH` | `/api/v1/support/cases/{id}/assignee` | handler | `{"assignee_user_id": uuid \| null}` |
| `GET` | `/api/v1/support/cases/{id}/assignees` (alias `/assignable-users`) | handler | Users the case may be assigned to (`user_id`, `name`, `role`) |
| `POST` | `/api/v1/support/cases/{id}/escalate` | requester, owner, support staff | Escalate an attendee case to Platform Admins (`{"reason"}`) |
| `GET` | `/api/v1/support/cases/{id}/activity` | participant | Chronological lifecycle history (actor, timestamp, from/to) |
| `GET` | `/api/v1/users/me/notifications` | self | In-app notifications (`unread_only`, paging, `unread_count`) |
| `POST` | `/api/v1/users/me/notifications/{id}/read` | self | Mark one read (idempotent) |

### Routing and context

* `ATTENDEE_ORGANIZER`: a customer and the organizer staff of one event. Needs an
  event/order/ticket the customer owns (an event-only case needs an order for
  the event or a publicly visible event).
* `PLATFORM`: an organizer (about their own event, or without context) or any
  user with an account/technical issue, and Platform Admins.
* Case responses carry `requester`/`assignee` (`user_id`, `display_name`,
  `name`), flat `event_id`/`order_id`/`ticket_id`/`ticket_public_id`, and nested
  `event` (`id`, `title`), `order` (`id`, `order_number`) and `ticket` (`id`,
  `public_id`, `ticket_type_name`). Messages carry `sender` (`user_id`,
  `display_name`, `name`, `role`).
* `ticket_id` accepts the ticket id or its `public_id` and resolves its order
  and event; `order_id` resolves its event. Extra
  identifiers must agree (`422 SUPPORT_CONTEXT_MISMATCH`). Unknown and
  not-yours references both return `404 SUPPORT_CONTEXT_NOT_FOUND`.
* Categories: `TICKET_DELIVERY`, `PAYMENT`, `REFUND`, `SEATING`,
  `EVENT_INFORMATION`, `CHECK_IN`, `ACCOUNT`, `TECHNICAL`.

### Authorization matrix

| Caller | Attendee case (own event context) | Platform case |
|---|---|---|
| Requester | full participant (resolve / reopen, message, escalate) | full participant |
| Event owner | handler | — (only if requester) |
| `EVENT_ADMIN` / `ORGANIZER_STAFF` of that event | handler | no access |
| `CHECK_IN_STAFF` | no access | no access |
| Platform Admin | handler **only after escalation** | handler |
| Anyone else | `403 SUPPORT_CASE_ACCESS_DENIED`, no metadata | same |

Access is recomputed from PostgreSQL on every request (removing an EventStaff
row revokes it immediately); the assignee field never grants access by
itself. Listings put the same rule in the SQL `WHERE`, so inaccessible cases
are neither returned nor counted.

### Lifecycle

`OPEN → IN_PROGRESS | WAITING_FOR_CUSTOMER | RESOLVED`,
`IN_PROGRESS → WAITING_FOR_CUSTOMER | RESOLVED`,
`WAITING_FOR_CUSTOMER → IN_PROGRESS | RESOLVED`,
`RESOLVED → OPEN | IN_PROGRESS` (reopen). Handlers may make any listed
transition; the requester may resolve or reopen (`RESOLVED → OPEN`). A
customer reply on `WAITING_FOR_CUSTOMER` moves the case to `IN_PROGRESS`.
Messages cannot be posted on a `RESOLVED` case (reopen first).

Assignees: event owner or support staff of the event (plus Platform Admins once
escalated) for attendee cases; Platform Admins for platform cases. Active
accounts only.

### Ordering and polling

Each message gets a per-case, gap-free `sequence` under the case row lock, so
ordering is deterministic even with identical timestamps. Poll with
`after_sequence=<next_cursor>`; with no new messages `next_cursor` stays the
same.

### Notifications

In-app (`channel = IN_APP`) for: case created (handlers), new message (the
other side: assignee if any, otherwise all handlers; or the requester),
assigned/reassigned (new and previous assignee), significant status changes
(`WAITING_FOR_CUSTOMER`, `RESOLVED`, reopen), escalation (Platform Admins plus
the other side). Recipients are intersected with the users currently
authorized for the case; the actor and blocked accounts are skipped and
duplicates collapse.

## Administration, analytics and event history (SCRUM-17)

### Platform Admin endpoints (all require `PLATFORM_ADMIN`)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/admin/users` | Search `q` (email, name, id), `status`, `role`, `is_organizer`, `sort`, paging |
| `GET` | `/api/v1/admin/users/{id}` | Inspect one user (no password hash or tokens) |
| `POST` | `/api/v1/admin/users/{id}/suspend` · `/unsuspend` | `{"reason"}`; suspend = status `BLOCKED` + refresh sessions revoked |
| `GET` | `/api/v1/admin/events` · `/{id}` | Search `q`, `status`, `suspended`, `owner_user_id`, `starts_from/to`, `sort` |
| `POST` | `/api/v1/admin/events/{id}/suspend` · `/unsuspend` | Suspend a suspicious event |
| `POST` | `/api/v1/admin/events/{id}/paid-sales/stop` · `/resume` | Stop paid checkout only (free registration continues) |
| `GET` | `/api/v1/admin/orders` | Search `q` (order number, buyer email, id), `status`, `event_id`, `buyer_user_id`, dates, `sort` |
| `GET` | `/api/v1/admin/payments` | Search `q` (provider ref, order number, id), `status`, `provider`, `order_id`, `event_id`, dates |
| `GET` | `/api/v1/admin/activations` | Paid-sales activation records (`NOT_ACTIVATED` / `ACTIVE` / `SUSPENDED`) |
| `GET` | `/api/v1/admin/refunds` | Refunds by `status`, `kind`, `event_id`, dates |
| `GET` | `/api/v1/admin/failures` | Failed payments, failed refunds and failed outbox emails (`kind`) |
| `GET` · `POST` | `/api/v1/admin/disputes` | List / record a dispute against a captured payment |
| `PATCH` | `/api/v1/admin/disputes/{id}` | `OPEN → UNDER_REVIEW → RESOLVED \| REJECTED` |
| `GET` | `/api/v1/admin/campaigns` | All campaigns (`q`: name, code, event title) with `effective_status`, `redemption_count` |
| `GET` | `/api/v1/admin/export/{users\|events\|orders\|payments}.csv` | CSV export (≤ 10,000 rows, same `q`, formula-safe cells, audited) |
| `POST` | `/api/v1/admin/organizers/{user_id}/verification` | `{"status": "VERIFIED\|REJECTED\|PENDING", "reason"?}` |
| `POST` | `/api/v1/admin/payout-accounts/{id}/status` | `{"status": "ACTIVE\|BLOCKED\|PENDING", "reason"?}` |
| `GET` | `/api/v1/admin/support/escalated` | Escalated attendee support cases |
| `GET` | `/api/v1/admin/actions` | Administrative actions from the audit log |

Every administrative action writes an `admin.*` audit entry
(`actor_type = PLATFORM_ADMIN`) with the target, the admin, the reason
(`metadata.reason`), the timestamp and `before`/`after` state.

### Suspension enforcement

* **User**: `users.status = BLOCKED`. `get_current_user` re-reads the user on
  every request, so access tokens issued earlier are rejected with
  `403 USER_BLOCKED`; refresh sessions are revoked; login is refused.
* **Event** (`events.suspended_at`, orthogonal to `status`): `POST /orders`, the
  legacy `/purchase`, holds, promo quotes, starting a payment and completing a
  pending payment all refuse (`409 EVENT_SUSPENDED`). A pending payment that
  completes after suspension becomes `FAILED` (`failure_code EVENT_SUSPENDED`);
  its order is cancelled and holds and promo capacity are released. Publish and
  duplicate are blocked, and the event is hidden from the catalog, public detail
  and campaign QR resolution. Existing paid orders, valid tickets, payments and
  refunds are untouched and keep their lifecycle (refunds still work).
* **Paid sales stop** (`event_sales_activations.sales_suspended_at`): paid
  checkout and payment completion fail with `PAID_SALES_SUSPENDED`; re-running
  the activation checklist cannot lift it; free registration continues.

### Disputes

No payment provider exists, so `payment_disputes` is a minimal bookkeeping
entity recorded by Platform Admins against captured payments (one open dispute
per payment). It never changes payments, orders or refunds; money only moves
through the refund flow.

### Organizer analytics

* `GET /api/v1/events/{event_id}/analytics`: event owner, Platform Admin, or
  event staff with `permissions.view_analytics = true`. Default Event Admin
  permissions do not include analytics.
* `GET /api/v1/organizers/me/analytics`: aggregated over events the caller owns
  or has `view_analytics` on (relationship based; a global role does not widen
  it). Optional `event_id`, `ticket_type_id`, `start`, `end`.

Filters: `start` inclusive, `end` exclusive, UTC when no offset; `end <= start`
gives `422 INVALID_DATE_RANGE`. Every reference is authorized before
aggregation: a foreign `event_id` gives 403, a foreign/unknown `ticket_type_id`
gives 404. All metrics are SQL aggregates over authoritative rows (no locks, a
fixed number of queries). The response includes `definitions`.

| Metric | Formula and time basis |
|---|---|
| `capacity.total_capacity` | Σ min(event capacity, Σ ticket type capacity); snapshot |
| `capacity.sold_tickets` | order items of `PAID`/`CONFIRMED` orders; snapshot |
| `capacity.reserved_tickets` | active, unexpired holds; snapshot |
| `capacity.remaining_tickets` | capacity − sold − reserved (≥ 0); snapshot |
| `attendance.checked_in` | tickets in `CHECKED_IN` (reversal returns to `VALID`; repeated scans are rejected); snapshot |
| `attendance.eligible_tickets` | `VALID` + `CHECKED_IN` (cancelled/refunded excluded) |
| `attendance.attendance_rate` | checked_in / eligible_tickets |
| `attendance.period_*` | check-in/reversal records by `created_at`; admitted = distinct `CHECKED_IN` tickets by `checked_in_at` |
| `sales[].tickets_sold`, `orders` | items/orders with `paid_at` in range (payment confirmation; pending/failed never have `paid_at`) |
| `sales[].gross_ticket_sales` | Σ `unit_price` |
| `sales[].discounts` | Σ item `discount_amount` (already inside revenue; never subtracted twice) |
| `sales[].ticket_revenue` | Σ `final_price` |
| `sales[].fees` | Σ `fee_amount` (null with a ticket type filter) |
| `sales[].gross_revenue` | Σ `total_amount` charged = ticket_revenue + fees |
| `sales[].refunds` | Σ `SUCCEEDED` refund amounts by `completed_at` |
| `sales[].net_revenue` | gross_revenue − refunds |
| `campaigns[]` | redemption counts (snapshot) and attributed orders/tickets/discounts/revenue by `paid_at` |

Money is grouped per currency and never combined.

`sales_over_time` (added after PR 19, additive) lists one entry per day and
currency for the same period as `sales`: `{date, currency, orders, tickets,
gross}`, where `gross` is list prices (no fees, like `gross_ticket_sales`). Days
are bucketed by `orders.paid_at` in the event timezone (`scope.timezone`; UTC
for an aggregate over several events), so a chart needs one request instead of
one per bar.

**Processing fee**: `gross_ticket_sales`, `discounts`, `ticket_revenue` and
`sales_over_time.gross` exclude the 3% fee; `fees` is the fee; `gross_revenue`,
`refunds` and `net_revenue` include it (refunds return the full amount charged).

### Event history and timeline

* `GET /api/v1/organizers/me/events`: events the caller owns or is
  `EVENT_ADMIN`/`ORGANIZER_STAFF` of. `category` (repeatable): `CANCELLED`
  (status), `COMPLETED` (status `COMPLETED` or ended), `ACTIVE` (started, not
  ended), `UPCOMING` (not started). Suspension is reported as `is_suspended` and
  never changes the category. Also `status`, `q`, `suspended`,
  `starts_from/to`, `sort`, paging.
* `GET /api/v1/events/{event_id}/audit-log`: also accepts `from`/`to`
  (inclusive dates, event timezone) and `action_prefix`; entries add
  `actor_name` and `entity_label` (e.g. `Order #1042`, a ticket type name).
* `GET /api/v1/events/{event_id}/timeline` (owner or Platform Admin): the
  event's entries in the append-only audit log, newest first (`order=asc`
  available). `action` filters are repeatable (exact, or a prefix ending in `.`
  or `.*`); `entity_type`, `start`/`end` and paging are also available. Each
  entry has actor (id, type, name), timestamp, action, entity type/id/label and a
  redacted description (no raw payloads). Access-denied probes are excluded,
  and nothing is synthesized for actions that were never recorded.

### Migration

`sql/migrate_009_support_admin_analytics.sql` (in the default `SCHEMA_FILES`):
support routing/escalation/resolution columns, the `WAITING_FOR_CUSTOMER` enum
value, the message `sequence`/`sender_role` columns (backfilled by
`created_at, id`), legacy category normalization (originals kept in
`audit_logs`), user/event/paid-sales suspension columns, the
`payment_disputes` table, and read-path indexes. Additive; nothing is dropped.
Apply with `make migrate` (or on startup).

## Frontend request (SCRUM-21/23/24/25)

* **Organizer cancel/refund**: `POST /orders/{id}/cancel` and `/refund` accept
  the event owner (and Platform Admins) as well as the buyer. Owner refunds are
  full refunds (`kind = ORGANIZER_REQUEST`) that bypass the customer policy,
  deadline and start-time check; checked-in orders are still refused. Free
  registrations are cancelled without a refund. Other organizers get 403.
* **Demo self-service** (`DEMO_SIMULATIONS_ENABLED`, default `true`; DEMO ONLY,
  audited): `POST /organizers/me/verification/simulate` (profile → VERIFIED),
  `POST /organizers/me/payout-account/simulate-approval` (account → ACTIVE),
  `GET /organizers/me/payout-account` (default or newest, reference masked).
  With the flag off, Platform Admins use the admin routes above.
* **Event page**: `GET /events/{id}/ticket-types` items have `available`
  (capacity − sold − active holds, capped by remaining event capacity; public).
  `GET /events/{id}` has `paid_sales_active` (activated, not stopped, event not
  suspended).
* **Attendees**: `POST /orders` accepts optional
  `attendees: [{ticket_type_id, full_name, email}]`, exactly one per ticket
  (422 otherwise). Omitted: every ticket is issued to the buyer, as before.
* **Campaign analytics** responses include `tickets_sold` (tickets of
  attributed orders that are currently PAID/CONFIRMED).
* **COMPLETED**: the background loop moves PUBLISHED events whose `ends_at` has
  passed to `COMPLETED` (audited as `SYSTEM`, `event.completed`).
* **Support** (all additive to PR 19): `ticket_public_id` and nested `event`,
  `order`, `ticket` objects on cases; `name` next to `display_name` on people;
  `scope=mine|event|all`; `?after=<message_id>`; combined
  `PATCH /support/cases/{id}`; `/assignees`. Categories unchanged
  (`EVENT_INFORMATION`, ...).
* **First Platform Admin**: register and verify an account through the API,
  then on the server run `make admin EMAIL=you@example.com`
  (`python -m app.db.bootstrap_admin --email ...`; in Docker:
  `docker compose exec api python -m app.db.bootstrap_admin --email ...`).
  It promotes the existing account (idempotent, audited as SYSTEM); there is
  no HTTP route for this by design.
* **Migration**: `sql/migrate_010_organizer_refunds.sql` widens
  `refunds_kind_valid` with `ORGANIZER_REQUEST`. `migrate_009` is unchanged
  from PR 19 (already applied in deployments).

## Reported events, platform settings, image upload, calendar, health (SRS 4.12, 4.2, 8)

### Reported events

* `POST /events/{event_id}/reports` (verified user): `{reason, details?}` with
  `reason` one of `FRAUD`, `MISLEADING`, `INAPPROPRIATE`, `PROHIBITED`, `SPAM`,
  `OTHER` (`OTHER` requires `details`). Only publicly visible events (404
  otherwise); owners cannot report their own event (409
  `CANNOT_REPORT_OWN_EVENT`); one OPEN/UNDER_REVIEW report per user and event
  (409 `REPORT_ALREADY_OPEN`). The owner is never told who reported.
* `GET /users/me/reports`: the caller's reports.
* `GET /admin/reports` (`status`, `reason`, `event_id`, paging),
  `GET /admin/reports/{id}`: include the event, reporter, whether the event is
  suspended and `event_active_reports` (open reports on the same event).
* `PATCH /admin/reports/{id}` `{status, resolution_note?, suspend_event?}`:
  OPEN ↔ UNDER_REVIEW → RESOLVED | DISMISSED (terminal). `suspend_event: true`
  is allowed only with `RESOLVED` and uses `resolution_note` as the suspension
  reason (same effect and audit as `POST /admin/events/{id}/suspend`). The
  reporter gets an `EVENT_REPORT_REVIEWED` in-app notification.

### Platform settings

* `GET /admin/settings`, `PATCH /admin/settings`
  `{activation_fee_amount?, processing_fee_percent?, reason?}` (fee ≥ 0,
  percent 0–20). Audited as `admin.platform_settings_updated` (visible in
  `GET /admin/actions`). Defaults are the old hard-coded values: 1000.00 KZT
  and 3%.
* New orders and promo quotes use the current percent; existing orders keep
  their stored `fee_amount`. An unpaid activation fee follows the current
  setting; a paid one is never changed.

### Event image upload

* `POST /events/{event_id}/images/upload` (owner or Platform Admin), multipart
  field `file`: JPEG, PNG or WebP, at most `MEDIA_MAX_UPLOAD_BYTES` (5 MB).
  The image is decoded and re-encoded (metadata such as GPS is removed); other
  files get 415, larger ones 413. The response `object_key` is the image URL:
  put it in `images` on `PATCH /events/{id}` to attach it (same contract as
  before, external URLs still work).
* `GET /media/{key}` serves stored files (public, immutable cache headers).
  Files live under `MEDIA_ROOT` (`media/` locally, the `media_data` volume in
  Docker). The URL is `{PUBLIC_BASE_URL}/api/v1/media/...`; with
  `PUBLIC_BASE_URL` empty it is relative, which works through the web app's
  `/api/v1` proxy but not for the mobile app.

### Calendar export

* `GET /events/{event_id}/calendar.ics`: RFC 5545 file, same visibility as
  `GET /events/{id}`. `URL` points to `{PUBLIC_WEB_BASE_URL}/catalog/{id}` when
  that is set; cancelled events have `STATUS:CANCELLED`.

### Health checks

* `GET /health/live`: the process is up.
* `GET /health/ready`: `SELECT 1` on PostgreSQL and `PING` on Redis (when
  configured); 503 with `checks` when one fails. Used by the Docker healthcheck.

### Migration

`sql/migrate_011_reports_settings.sql` adds `event_reports` and
`platform_settings` (seeded with id 1). Additive; apply with `make migrate` or
on startup. New dependencies: `python-multipart`, `pillow` (already installed
through `qrcode[pil]`).
