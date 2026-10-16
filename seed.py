"""Создаёт схему БД и демо-данные: python seed.py"""
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash
from app import app, db, init_db, User, Event, Ticket, Category, new_token

with app.app_context():
    db.drop_all(); init_db()
    mk = lambda n, e, r: User(name=n, email=e, role=r, password_hash=generate_password_hash("password123"))
    admin = mk("Администратор", "admin@example.com", "admin")
    orgs = [mk("Организатор Анна", "org1@example.com", "organizer"), mk("Организатор Борис", "org2@example.com", "organizer")]
    users = [mk(f"Студент {i}", f"user{i}@example.com", "participant") for i in range(1, 9)]
    db.session.add_all([admin, *orgs, *users]); db.session.commit()
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    cat = {c.name: c.id for c in Category.query}
    d = lambda days, h=0: now + timedelta(days=days, hours=h)
    specs = [  # title, category, organizer, start, cap, status
        ("Мастер-класс по Flask", "Мастер-класс", 0, d(3), 5, "published"),
        ("Весенний концерт колледжа", "Концерт", 1, d(7), 50, "published"),
        ("Хакатон «Код за ночь»", "Хакатон", 0, d(10), 10, "published"),
        ("Лекция: основы SQL", "Лекция", 1, d(5), 2, "published"),
        ("Отменённый квиз", "Мастер-класс", 0, d(4), 20, "cancelled"),
        ("Прошедшая лекция по Git", "Лекция", 1, d(-3), 20, "published"),
        ("Черновик: день открытых дверей", "Концерт", 0, d(20), 100, "draft"),
    ]
    evs = []
    for t, c, o, s, cap, st in specs:
        e = Event(title=t, category_id=cat[c], organizer_id=orgs[o].id, start_at=s, end_at=s + timedelta(hours=3),
                  registration_deadline=s - timedelta(hours=1), capacity=cap, status=st,
                  description=f"Подробная программа события «{t}». Вход по электронному билету.",
                  location="Актовый зал" if o else "Аудитория 204")
        evs.append(e)
    db.session.add_all(evs); db.session.commit()
    # (событие, студент, статус, посетил)
    plan = [(0, i, "active", False) for i in range(5)] + [(0, 5, "cancelled", False)] + \
           [(1, i, "active", False) for i in range(4)] + [(2, i, "active", False) for i in range(3)] + \
           [(3, 0, "active", False), (3, 1, "active", False), (3, 2, "cancelled", False)] + \
           [(5, i, "active", True) for i in range(4)] + [(5, 4, "cancelled", False)]
    for ei, ui, st, vis in plan:
        db.session.add(Ticket(event_id=evs[ei].id, user_id=users[ui].id, token=new_token(), status=st,
                              created_at=now - timedelta(days=5), checked_in_at=(evs[ei].start_at + timedelta(minutes=10)) if vis else None))
    db.session.commit()
    print(f"Готово: событий {len(evs)}, билетов {len(plan)}")
    print("Аккаунты (пароль password123): admin@example.com, org1@example.com, org2@example.com, user1@example.com … user8@example.com")
