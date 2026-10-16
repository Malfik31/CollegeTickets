# EventPass — события колледжа и электронные билеты

Flask + SQLAlchemy (SQLite) + JavaScript (fetch для контроля входа). Семестровый проект, вариант 04.

## Запуск (чистая БД)
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python seed.py          # создаёт схему и демо-данные (7 событий, 21 билет, 3 организатора/админ, 8 студентов)
python app.py           # http://127.0.0.1:5000
pytest                  # тесты
```
Переменные: `SECRET_KEY`, `DATABASE_URL` (по умолчанию `sqlite:///eventpass.db`; для PostgreSQL работает блокировка `SELECT … FOR UPDATE`).

## Демо-аккаунты (пароль `password123`)
| Роль | Email |
|---|---|
| Администратор | admin@example.com |
| Организатор | org1@example.com, org2@example.com |
| Участник | user1@example.com … user8@example.com |

Назначение организаторов — страница «Пользователи» (только администратор).

## Структура БД (ER)
```
User(id, name, email*, password_hash, role, created_at)
Category(id, name*)
Event(id, organizer_id→User, category_id→Category, title, description, location, cover,
      start_at, end_at, registration_deadline, capacity, status)   CHECK capacity>0, end>start, deadline<=start
Ticket(id, event_id→Event, user_id→User, token*, status, created_at, checked_in_at)   UNIQUE(event_id,user_id)
CheckInLog(id, ticket_id→Ticket, operator_id→User, action, created_at)
```
Индексы: все FK, `Event.start_at`, `Event.status`, `Ticket.created_at`, `User.email`. Каскадного удаления нет — история сохраняется (события и билеты не удаляются, а переходят в статусы).

## Статусы и правила
* Событие: `draft → published → cancelled`; «завершено» вычисляется по `end_at`. Прочие переходы → 409.
* Билет: `active` / `cancelled`; «использован» = `checked_in_at` не пусто. Отменённый билет восстанавливается повторной записью при наличии мест.
* Вместимость: проверка внутри блокировки (`threading.Lock` + `FOR UPDATE`), UNIQUE(event_id,user_id) в БД.
* Токен — `secrets.token_urlsafe(24)`; QR содержит только токен.
* Проверку входа делает только организатор этого события (иначе 403). Повторный проход → 409 «уже использован» + время первого входа.
* Права проверяются на сервере; CSRF-токен на всех POST; CSV защищён от формул.

## Маршруты
`GET /events`, `POST /events/{id}/register`, `GET /tickets/{id}`, `POST /tickets/{id}/cancel`, `POST /events/{id}/check-in`, `GET /events/{id}/attendees`, `GET /events/{id}/export`; также `/register`, `/login`, `/profile`, `/my/tickets`, `/my/events`, `/events/new`, `/events/{id}/edit`, `/events/{id}/status`, `/admin/users`.
С заголовком `Accept: application/json` маршруты возвращают JSON и коды 400/401/403/404/409.

## Проверка критериев приёмки
1. Событие на 2 места: тест `test_capacity_and_release`; вручную — «Лекция: основы SQL» (2 места).
2. Отметка входа и повтор — `test_checkin_twice_and_foreign_organizer`.
3. Чужой организатор: войти как org2 и открыть `/events/1/attendees` → 403.
4. Счётчики панели и CSV считаются одним запросом `ticket_query`.
