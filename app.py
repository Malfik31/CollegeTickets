"""EventPass — события колледжа и электронные билеты (Flask + SQLAlchemy + JS)."""
import csv, io, os, re, secrets, threading
from datetime import datetime
from functools import wraps

import segno
from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from flask_sqlalchemy import SQLAlchemy
from markupsafe import Markup
from sqlalchemy import CheckConstraint, Engine, UniqueConstraint, event as sa_event, func
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY", "dev-secret-change-me"),
    SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", "sqlite:///eventpass.db"),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    CSRF_ENABLED=os.getenv("NO_CSRF") != "1",
)
UPLOAD_DIR = os.path.join(app.static_folder, "uploads")
db = SQLAlchemy(app)
reg_lock = threading.Lock()  # + SELECT ... FOR UPDATE (на PostgreSQL/MySQL)
PER_PAGE = 9


@sa_event.listens_for(Engine, "connect")
def _sqlite_fk(conn, _):
    if conn.__class__.__module__.startswith("sqlite3"):
        conn.execute("PRAGMA foreign_keys=ON")


# ───────────────────────── Модели ─────────────────────────
class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), unique=True, nullable=False)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="participant")  # participant|organizer|admin
    created_at = db.Column(db.DateTime, default=datetime.now)


class Event(db.Model):
    __table_args__ = (CheckConstraint("capacity > 0"), CheckConstraint("end_at > start_at"),
                      CheckConstraint("registration_deadline <= start_at"))
    id = db.Column(db.Integer, primary_key=True)
    organizer_id = db.Column(db.ForeignKey("user.id"), nullable=False, index=True)
    category_id = db.Column(db.ForeignKey("category.id"), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=False)
    location = db.Column(db.String(120), nullable=False)
    cover = db.Column(db.String(120))
    start_at = db.Column(db.DateTime, nullable=False, index=True)
    end_at = db.Column(db.DateTime, nullable=False)
    registration_deadline = db.Column(db.DateTime, nullable=False)
    capacity = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="draft", index=True)  # draft|published|cancelled
    organizer = db.relationship("User")
    category = db.relationship("Category")

    @property
    def shown_status(self):  # «завершено» вычисляется по времени
        return "finished" if self.status == "published" and self.end_at < datetime.now() else self.status

    @property
    def active_count(self):
        return Ticket.query.filter_by(event_id=self.id, status="active").count()

    @property
    def free(self):
        return self.capacity - self.active_count


class Ticket(db.Model):
    __table_args__ = (UniqueConstraint("event_id", "user_id"),)
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.ForeignKey("event.id"), nullable=False, index=True)
    user_id = db.Column(db.ForeignKey("user.id"), nullable=False, index=True)
    token = db.Column(db.String(64), unique=True, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="active")  # active|cancelled
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)
    checked_in_at = db.Column(db.DateTime)
    event = db.relationship("Event")
    user = db.relationship("User")

    @property
    def shown_status(self):
        return "cancelled" if self.status == "cancelled" else ("used" if self.checked_in_at else "active")


class CheckInLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.ForeignKey("ticket.id"), nullable=False, index=True)
    operator_id = db.Column(db.ForeignKey("user.id"), nullable=False, index=True)
    action = db.Column(db.String(20), nullable=False)  # check_in|duplicate
    created_at = db.Column(db.DateTime, default=datetime.now)


def new_token():
    return secrets.token_urlsafe(24)


# ───────────────────────── Безопасность и утилиты ─────────────────────────
@app.before_request
def load_user():
    g.user = db.session.get(User, session["uid"]) if "uid" in session else None
    if request.method == "POST" and app.config["CSRF_ENABLED"]:
        sent = request.form.get("csrf") or request.headers.get("X-CSRF")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "x")):
            abort(400, "Сессия устарела, обновите страницу.")


@app.template_filter("merge")
def merge(d, extra):
    return {**d, **extra}


@app.context_processor
def inject():
    def csrf():
        session.setdefault("csrf", secrets.token_hex(16))
        return session["csrf"]
    return dict(csrf=csrf, user=g.user,
                csrf_field=lambda: Markup(f'<input type="hidden" name="csrf" value="{csrf()}">'),
                now=datetime.now(), fmt=lambda d: d.strftime("%d.%m.%Y %H:%M") if d else "—",
                ST={"draft": "Черновик", "published": "Опубликовано", "finished": "Завершено",
                    "cancelled": "Отменено", "active": "Действителен", "used": "Использован"})


def wants_json():
    return request.is_json or request.accept_mimetypes.best == "application/json"


def fail(msg, code, back=None):
    if wants_json():
        return jsonify(error=msg), code
    flash(msg, "error")
    return redirect(back or request.referrer or url_for("events"))


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not g.user:
            if wants_json():
                return jsonify(error="Требуется вход"), 401
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w


def role_required(*roles):
    def deco(f):
        @wraps(f)
        @login_required
        def w(*a, **k):
            if g.user.role not in roles:
                abort(403)
            return f(*a, **k)
        return w
    return deco


def own_event(eid):
    """Событие, которым управляет текущий организатор (иначе 404/403 — на сервере)."""
    ev = db.get_or_404(Event, eid)
    if g.user.role != "organizer" or ev.organizer_id != g.user.id:
        abort(403)
    return ev


@app.errorhandler(HTTPException)
def http_error(e):
    msgs = {400: "Некорректный запрос", 403: "Доступ запрещён", 404: "Страница не найдена",
            413: "Файл слишком большой (максимум 2 МБ)"}
    text = e.description if e.code == 400 and e.description else msgs.get(e.code, e.name)
    if request.path.endswith("/check-in") and request.method == "POST":
        return jsonify(error=text), e.code
    return render_template("error.html", code=e.code, text=text), e.code


def parse_dt(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        return None


# ───────────────────────── Аутентификация ─────────────────────────
@app.route("/register", methods=["GET", "POST"])
def signup():
    errors, f = {}, request.form
    if request.method == "POST":
        name, email, pw = f.get("name", "").strip(), f.get("email", "").strip().lower(), f.get("password", "")
        if len(name) < 2: errors["name"] = "Введите имя (минимум 2 символа)"
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email): errors["email"] = "Некорректный email"
        elif User.query.filter_by(email=email).first(): errors["email"] = "Этот email уже зарегистрирован"
        if len(pw) < 6: errors["password"] = "Пароль — минимум 6 символов"
        if not errors:
            u = User(name=name, email=email, password_hash=generate_password_hash(pw))
            db.session.add(u); db.session.commit()
            session.clear(); session["uid"] = u.id
            return redirect(url_for("events"))
    return render_template("signup.html", errors=errors, f=f)


@app.route("/login", methods=["GET", "POST"])
def login():
    error, f = None, request.form
    if request.method == "POST":
        u = User.query.filter_by(email=f.get("email", "").strip().lower()).first()
        if u and check_password_hash(u.password_hash, f.get("password", "")):
            session.clear(); session["uid"] = u.id
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("events"))
        error = "Неверный email или пароль"
    return render_template("login.html", error=error, f=f)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("events"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    errors, f = {}, request.form
    if request.method == "POST":
        name, pw = f.get("name", "").strip(), f.get("password", "")
        if len(name) < 2: errors["name"] = "Введите имя (минимум 2 символа)"
        if pw and len(pw) < 6: errors["password"] = "Пароль — минимум 6 символов"
        if not errors:
            g.user.name = name
            if pw: g.user.password_hash = generate_password_hash(pw)
            db.session.commit(); flash("Профиль сохранён", "ok")
            return redirect(url_for("profile"))
    return render_template("profile.html", errors=errors, f=f if request.method == "POST" else {"name": g.user.name})


@app.route("/admin/users")
@role_required("admin")
def admin_users():
    page = User.query.order_by(User.id).paginate(page=request.args.get("page", 1, int), per_page=20, error_out=False)
    return render_template("admin_users.html", page=page)


@app.post("/admin/users/<int:uid>/role")
@role_required("admin")
def set_role(uid):
    u, role = db.get_or_404(User, uid), request.form.get("role")
    if u.role == "admin" or role not in ("participant", "organizer"):
        return fail("Нельзя изменить эту роль", 400)
    u.role = role; db.session.commit()
    flash(f"Роль пользователя {u.name} изменена", "ok")
    return redirect(url_for("admin_users"))


# ───────────────────────── Афиша ─────────────────────────
@app.route("/")
@app.route("/events")
def events():
    q = Event.query.filter(Event.status == "published", Event.end_at >= datetime.now())
    cat, day = request.args.get("category", type=int), request.args.get("date", "")
    if cat: q = q.filter_by(category_id=cat)
    if day:
        try:
            d = datetime.strptime(day, "%Y-%m-%d")
            q = q.filter(Event.start_at >= d, Event.start_at < d.replace(hour=23, minute=59, second=59))
        except ValueError:
            flash("Некорректная дата фильтра", "error")
    page = q.order_by(Event.start_at).paginate(page=request.args.get("page", 1, int), per_page=PER_PAGE, error_out=False)
    return render_template("events.html", page=page, cats=Category.query.order_by(Category.name).all(), cat=cat, day=day)


@app.route("/events/<int:eid>")
def event_page(eid):
    ev = db.get_or_404(Event, eid)
    is_owner = g.user and g.user.id == ev.organizer_id
    if ev.status == "draft" and not is_owner:
        abort(404)
    mine = Ticket.query.filter_by(event_id=eid, user_id=g.user.id).first() if g.user else None
    return render_template("event.html", ev=ev, mine=mine, is_owner=is_owner, free=ev.free)


# ───────────────────────── Регистрация и билеты ─────────────────────────
@app.post("/events/<int:eid>/register")
@login_required
def register(eid):
    back = url_for("event_page", eid=eid)
    with reg_lock:
        ev = db.session.get(Event, eid, with_for_update=True)
        if not ev or ev.status == "draft":
            return fail("Событие не найдено", 404, url_for("events"))
        now = datetime.now()
        if ev.status == "cancelled": return fail("Событие отменено — регистрация закрыта", 409, back)
        if ev.shown_status == "finished" or now >= ev.start_at: return fail("Событие уже началось или завершилось", 409, back)
        if now > ev.registration_deadline: return fail("Срок регистрации истёк", 409, back)
        t = Ticket.query.filter_by(event_id=eid, user_id=g.user.id).first()
        if t and t.status == "active": return fail("Вы уже зарегистрированы на это событие", 409, back)
        if ev.active_count >= ev.capacity: return fail("Свободных мест не осталось", 409, back)
        if t:  # восстановление отменённой регистрации
            t.status, t.checked_in_at = "active", None
        else:
            t = Ticket(event_id=eid, user_id=g.user.id, token=new_token()); db.session.add(t)
        db.session.commit()
    if wants_json(): return jsonify(ok=True, ticket_id=t.id), 201
    flash("Вы зарегистрированы! Ваш билет ниже.", "ok")
    return redirect(url_for("ticket_page", tid=t.id))


def my_ticket_or_403(tid):
    t = db.get_or_404(Ticket, tid)
    if not g.user or t.user_id != g.user.id:
        abort(403)
    return t


@app.get("/tickets/<int:tid>")
@login_required
def ticket_page(tid):
    return render_template("ticket.html", t=my_ticket_or_403(tid))


@app.get("/tickets/<int:tid>/qr.svg")
@login_required
def ticket_qr(tid):  # в QR — только токен, без персональных данных
    t = my_ticket_or_403(tid)
    buf = io.BytesIO(); segno.make(t.token, error="m").save(buf, kind="svg", scale=6, border=2)
    return Response(buf.getvalue(), mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.post("/tickets/<int:tid>/cancel")
@login_required
def ticket_cancel(tid):
    t = my_ticket_or_403(tid)
    back = url_for("ticket_page", tid=tid)
    if t.status == "cancelled": return fail("Регистрация уже отменена", 409, back)
    if t.checked_in_at: return fail("Билет уже использован — отмена невозможна", 409, back)
    if datetime.now() >= t.event.start_at: return fail("Отмена возможна только до начала события", 409, back)
    t.status = "cancelled"; db.session.commit()
    if wants_json(): return jsonify(ok=True)
    flash("Регистрация отменена, место освобождено", "ok")
    return redirect(url_for("my_tickets"))


@app.get("/my/tickets")
@login_required
def my_tickets():
    q = Ticket.query.filter_by(user_id=g.user.id).join(Event).order_by(Event.start_at.desc())
    page = q.paginate(page=request.args.get("page", 1, int), per_page=PER_PAGE, error_out=False)
    return render_template("my_tickets.html", page=page)


# ───────────────────────── Кабинет организатора ─────────────────────────
@app.get("/my/events")
@role_required("organizer")
def my_events():
    q = Event.query.filter_by(organizer_id=g.user.id).order_by(Event.start_at.desc())
    page = q.paginate(page=request.args.get("page", 1, int), per_page=PER_PAGE, error_out=False)
    return render_template("my_events.html", page=page)


def save_cover(file):
    if not file or not file.filename: return None, None
    ext = file.filename.rsplit(".", 1)[-1].lower()
    head = file.stream.read(12); file.stream.seek(0)
    ok = (head[:3] == b"\xff\xd8\xff" and ext in ("jpg", "jpeg")) or (head[:8] == b"\x89PNG\r\n\x1a\n" and ext == "png") \
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP" and ext == "webp")
    if not ok: return None, "Допустимы изображения JPG, PNG или WEBP"
    name = f"{secrets.token_hex(8)}.{ext}"
    file.save(os.path.join(UPLOAD_DIR, name))
    return name, None


def validate_event(f, ev=None):
    e, d = {}, {}
    d["title"], d["description"], d["location"] = (f.get(k, "").strip() for k in ("title", "description", "location"))
    if not 3 <= len(d["title"]) <= 120: e["title"] = "Название: от 3 до 120 символов"
    if len(d["description"]) < 10: e["description"] = "Описание — минимум 10 символов"
    if not d["location"]: e["location"] = "Укажите место"
    d["category_id"] = f.get("category_id", type=int)
    if not d["category_id"] or not db.session.get(Category, d["category_id"]): e["category_id"] = "Выберите категорию"
    for k in ("start_at", "end_at", "registration_deadline"):
        d[k] = parse_dt(f.get(k))
        if not d[k]: e[k] = "Укажите дату и время"
    if not e.keys() & {"start_at", "end_at", "registration_deadline"}:
        if d["end_at"] <= d["start_at"]: e["end_at"] = "Окончание должно быть позже начала"
        if d["registration_deadline"] > d["start_at"]: e["registration_deadline"] = "Срок регистрации не позже начала"
        if not ev and d["start_at"] <= datetime.now(): e["start_at"] = "Начало должно быть в будущем"
    d["capacity"] = f.get("capacity", type=int)
    if not d["capacity"] or d["capacity"] < 1 or d["capacity"] > 10000: e["capacity"] = "Вместимость: целое число от 1 до 10000"
    elif ev and d["capacity"] < ev.active_count:
        e["capacity"] = f"Нельзя уменьшить ниже числа активных билетов ({ev.active_count})"
    return d, e


@app.route("/events/new", methods=["GET", "POST"])
@role_required("organizer")
def event_new():
    return event_form(None)


@app.route("/events/<int:eid>/edit", methods=["GET", "POST"])
@login_required
def event_edit(eid):
    ev = own_event(eid)
    if ev.status == "cancelled" or ev.shown_status == "finished":
        flash("Отменённое или завершённое событие редактировать нельзя", "error")
        return redirect(url_for("my_events"))
    return event_form(ev)


def event_form(ev):
    errors, f = {}, request.form
    if request.method == "POST":
        d, errors = validate_event(f, ev)
        cover, err = save_cover(request.files.get("cover"))
        if err: errors["cover"] = err
        if not errors:
            if not ev:
                ev = Event(organizer_id=g.user.id); db.session.add(ev)
            for k, v in d.items(): setattr(ev, k, v)
            if cover: ev.cover = cover
            db.session.commit(); flash("Событие сохранено", "ok")
            return redirect(url_for("my_events"))
    elif ev:
        f = {k: getattr(ev, k) for k in ("title", "description", "location", "category_id", "capacity")}
        for k in ("start_at", "end_at", "registration_deadline"): f[k] = getattr(ev, k).strftime("%Y-%m-%dT%H:%M")
    return render_template("event_form.html", ev=ev, errors=errors, f=f, cats=Category.query.order_by(Category.name).all())


@app.post("/events/<int:eid>/status")
@login_required
def event_status(eid):
    ev, action = own_event(eid), request.form.get("action")
    back = url_for("my_events")
    if action == "publish" and ev.status == "draft":
        if ev.start_at <= datetime.now(): return fail("Нельзя опубликовать событие, которое уже началось", 409, back)
        ev.status = "published"
    elif action == "cancel" and ev.shown_status == "published":
        ev.status = "cancelled"
    else:
        return fail("Недопустимый переход статуса", 409, back)
    db.session.commit(); flash("Статус обновлён", "ok")
    return redirect(back)


def ticket_query(ev, q=""):
    qs = Ticket.query.filter_by(event_id=ev.id, status="active").join(User)
    if q: qs = qs.filter(db.or_(User.name.ilike(f"%{q}%"), User.email.ilike(f"%{q}%")))
    return qs


@app.get("/events/<int:eid>/attendees")
@login_required
def attendees(eid):
    ev, q = own_event(eid), request.args.get("q", "").strip()
    page = ticket_query(ev, q).order_by(Ticket.created_at).paginate(page=request.args.get("page", 1, int), per_page=20, error_out=False)
    base = Ticket.query.filter_by(event_id=eid, status="active")
    stats = dict(reg=base.count(), visited=base.filter(Ticket.checked_in_at.isnot(None)).count(),
                 cancelled=Ticket.query.filter_by(event_id=eid, status="cancelled").count())
    return render_template("attendees.html", ev=ev, page=page, q=q, stats=stats)


def safe_cell(v):  # защита от CSV-инъекций
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in "=+-@\t\r" else v


@app.get("/events/<int:eid>/export")
@login_required
def export(eid):
    ev = own_event(eid)
    out = io.StringIO(); w = csv.writer(out, delimiter=";")
    w.writerow(["Имя", "Email", "Дата регистрации", "Посетил", "Время входа"])
    for t in ticket_query(ev).order_by(Ticket.created_at):
        w.writerow([safe_cell(t.user.name), safe_cell(t.user.email), t.created_at.strftime("%d.%m.%Y %H:%M"),
                    "да" if t.checked_in_at else "нет", t.checked_in_at.strftime("%d.%m.%Y %H:%M") if t.checked_in_at else ""])
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="event_{eid}_attendees.csv"'})


@app.route("/events/<int:eid>/check-in", methods=["GET", "POST"])
@login_required
def check_in(eid):
    ev = own_event(eid)
    if request.method == "GET":
        return render_template("checkin.html", ev=ev)
    raw = ((request.get_json(silent=True) or {}).get("token") or request.form.get("token") or "").strip()
    token = raw.rstrip("/").rsplit("/", 1)[-1]  # допускаем и ссылку, и токен
    if not token: return jsonify(error="Введите токен билета"), 400
    if ev.status != "published": return jsonify(error="Событие отменено или не опубликовано — вход закрыт"), 409
    t = Ticket.query.filter_by(token=token).first()
    if not t or t.event_id != ev.id: return jsonify(error="Билет не найден для этого события"), 404
    if t.status == "cancelled": return jsonify(error="Регистрация по этому билету отменена"), 409
    stat = lambda: dict(reg=ticket_query(ev).count(), visited=ticket_query(ev).filter(Ticket.checked_in_at.isnot(None)).count())
    with reg_lock:
        db.session.refresh(t)
        if t.checked_in_at:
            db.session.add(CheckInLog(ticket_id=t.id, operator_id=g.user.id, action="duplicate")); db.session.commit()
            return jsonify(error=f"Уже использован. Первый вход: {t.checked_in_at:%d.%m.%Y %H:%M:%S}",
                           first=t.checked_in_at.isoformat(), stats=stat()), 409
        t.checked_in_at = datetime.now()
        db.session.add(CheckInLog(ticket_id=t.id, operator_id=g.user.id, action="check_in")); db.session.commit()
    return jsonify(ok=True, name=t.user.name, stats=stat())


def init_db():
    db.create_all()
    for n in ("Мастер-класс", "Концерт", "Хакатон", "Лекция"):
        if not Category.query.filter_by(name=n).first(): db.session.add(Category(name=n))
    db.session.commit()


if __name__ == "__main__":
    with app.app_context(): init_db()
    app.run(debug=True)
