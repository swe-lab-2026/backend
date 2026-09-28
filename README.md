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

Создаёт заказ, рассчитывает стоимость, применяет промокод, связывает покупателя с участниками и контролирует переходы статуса заказа.

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

### 5.11. Notifications

Отвечает за подтверждение email, восстановление пароля, отправку билета и уведомления об изменениях мероприятия. На первом этапе отправка может выполняться сразу после успешной операции.

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
| Auth | `/auth/register`, `/auth/verify-email`, `/auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/resend-verification`, `/auth/logout-all` |
| Users | `/users/me` |
| Organizers | `/organizers/me`, `/organizers/me/payout-account` |
| Events | `/events/assigned`, `/events/{event_id}/admins`, `/events/{event_id}/admins/{user_id}` |
| Ticket types | `/events/{event_id}/ticket-types` |
| Campaigns | `/events/{event_id}/campaigns`, `/campaigns/validate` |
| Orders | `/orders`, `/orders/{order_id}`, `/orders/{order_id}/checkout` |
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
EMAIL_FROM=no-reply@biletflow.local
PAYMENT_MODE=simulated
```

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
GET    /api/v1/events/assigned                        # события, где current_user = EVENT_ADMIN (пагинация limit/offset)
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
```

Тесты по умолчанию используют sqlite in-memory (тестовый fallback проекта). Для прогона на отдельной тестовой PostgreSQL:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/test_db pytest
```

