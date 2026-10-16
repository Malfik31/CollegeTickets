import os, tempfile
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["NO_CSRF"] = "1"
from datetime import datetime, timedelta
import pytest
from werkzeug.security import generate_password_hash
from app import app, db, init_db, User, Event, Ticket, Category

J = {"Accept": "application/json"}


@pytest.fixture()
def ctx():
    with app.app_context():
        db.drop_all(); init_db()
        for n, r in [("o1", "organizer"), ("o2", "organizer"), ("u1", "participant"), ("u2", "participant"), ("u3", "participant")]:
            db.session.add(User(name=n, email=f"{n}@x.io", role=r, password_hash=generate_password_hash("secret1")))
        db.session.commit()
        s = datetime.now() + timedelta(days=2)
        db.session.add(Event(title="E", description="d" * 20, location="L", category_id=1, organizer_id=1, start_at=s,
                             end_at=s + timedelta(hours=2), registration_deadline=s - timedelta(hours=1), capacity=2, status="published"))
        db.session.commit()
        yield


def client(email=None):
    c = app.test_client()
    if email: c.post("/login", data={"email": f"{email}@x.io", "password": "secret1"})
    return c


def test_capacity_and_release(ctx):
    a, b, c = client("u1"), client("u2"), client("u3")
    assert a.post("/events/1/register", headers=J).status_code == 201
    assert b.post("/events/1/register", headers=J).status_code == 201
    assert c.post("/events/1/register", headers=J).status_code == 409
    assert a.post("/events/1/register", headers=J).status_code == 409  # дубль
    assert a.post("/tickets/1/cancel", headers=J).status_code == 200
    assert c.post("/events/1/register", headers=J).status_code == 201   # место освободилось
    assert a.post("/events/1/register", headers=J).status_code == 409   # восстановить нельзя — мест нет


def test_checkin_twice_and_foreign_organizer(ctx):
    client("u1").post("/events/1/register", headers=J)
    tok = Ticket.query.first().token
    o1, o2 = client("o1"), client("o2")
    assert o2.post("/events/1/check-in", json={"token": tok}).status_code == 403
    assert o2.get("/events/1/attendees").status_code == 403
    assert o1.post("/events/1/check-in", json={"token": tok}).status_code == 200
    r = o1.post("/events/1/check-in", json={"token": tok})
    assert r.status_code == 409 and "Уже использован" in r.json["error"]
    assert o1.get("/events/1/attendees").status_code == 200
    assert Ticket.query.filter(Ticket.checked_in_at.isnot(None)).count() == 1


def test_rules(ctx):
    assert client().post("/events/1/register", headers=J).status_code == 401
    assert client("u1").get("/events/new").status_code == 403
    client("o1").post("/events/1/status", data={"action": "cancel"})
    assert client("u1").post("/events/1/register", headers=J).status_code == 409
    assert client("o1").post("/events/1/status", data={"action": "publish"}, headers=J).status_code == 409
