import os
import random
import csv
import io
import datetime

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, session, send_file
)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import FlaskForm
from wtforms import StringField, EmailField, TelField, IntegerField, SubmitField, PasswordField
from wtforms.validators import DataRequired, Email, Length, Regexp, Optional, NumberRange
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from email_validator import validate_email, EmailNotValidError
from jinja2 import DictLoader
from dotenv import load_dotenv

# Load .env if present
load_dotenv()

# ------------------ Flask app & DB ------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "devkey")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///raffle.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["20/minute"],
    storage_uri="memory://",   # explicitly choose memory store
)


# ------------------ Templates (inline) ------------------
TEMPLATES = {
"base.html": r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Kehilla Raffle</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;background:#0c0f14;color:#e8eef5}
    .wrap{max-width:720px;margin:0 auto;padding:24px}
    .card{background:#141a22;border:1px solid #1e2937;border-radius:16px;padding:24px;box-shadow:0 10px 30px rgba(0,0,0,.3)}
    label{display:block;margin:12px 0 6px}
    input[type=text],input[type=email],input[type=tel],input[type=number]{width:100%;padding:12px;border-radius:10px;border:1px solid #2b3a4d;background:#0c121a;color:#e8eef5}
    button, .btn{background:#4f8cff;border:none;color:#fff;padding:12px 16px;border-radius:10px;cursor:pointer;font-weight:600}
    .muted{color:#9fb0c3}
    .row{display:flex;gap:12px;align-items:center}
    .row > *{flex:1}
    .topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
    a{color:#9fd0ff;text-decoration:none}
    .notice{margin:8px 0;color:#ffd29f}
    .grid{display:grid;gap:8px}
    table{width:100%;border-collapse:collapse}
    th,td{border-bottom:1px solid #1f2a3a;padding:8px;text-align:left}
    .pill{display:inline-block;border:1px solid #2b3a4d;border-radius:999px;padding:6px 10px;color:#cde3ff}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div><strong>Kehilla Raffle</strong></div>
      <div><a href="{{ url_for('admin_login') }}">Admin</a></div>
    </div>
    <div class="card">
      {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
          {% for cat, msg in messages %}
            <div class="notice">{{ msg }}</div>
          {% endfor %}
        {% endif %}
      {% endwith %}
      {% block content %}{% endblock %}
    </div>
  </div>
</body>
</html>
""",
"index.html": r"""
{% extends "base.html" %}
{% block content %}
<h2>Join the Raffle</h2>
<p class="muted">Numbers are unique between 1 and {{ max_number }}. Remaining: <span class="pill">{{ remaining }}</span></p>

<form method="post">
  {{ form.hidden_tag() }}
  <label>{{ form.name.label }} {{ form.name() }}</label>
  <label>{{ form.email.label }} {{ form.email() }}</label>
  <label>{{ form.phone.label }} {{ form.phone(placeholder="+44...") }}</label>

  <div style="margin-top:16px">
    {{ form.submit(class_="btn") }}
  </div>
</form>

<p class="muted" style="margin-top:12px">
  After you get your number, we’ll send you to the donation page to complete the gift.
</p>
{% endblock %}
""",
"success.html": r"""
{% extends "base.html" %}
{% block content %}
<h2>Thank you{{ ", %s" % name if name else "" }}!</h2>
<p>Your raffle number is <strong>#{{ number }}</strong>. Please donate <strong>£{{ number }}</strong> to complete your entry.</p>

<div class="grid">
  <a class="btn" href="{{ donate_url }}" target="_blank" rel="noopener">Go to Donation Page</a>
  <button id="copyBtn">Copy amount ({{ number }})</button>
</div>
<p class="muted">On the donation page, enter the amount shown above.</p>

<script>
document.getElementById("copyBtn").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(String({{ number }})); alert("Amount copied!"); }
  catch(e){ alert("Copy failed. Amount: {{ number }}"); }
});
</script>
{% endblock %}
""",
"admin_login.html": r"""
{% extends "base.html" %}
{% block content %}
<h2>Admin Sign-in</h2>
<form method="post">
  {{ form.hidden_tag() }}
  <label>{{ form.username.label }} {{ form.username() }}</label>
  <label>{{ form.password.label }} {{ form.password(type="password") }}</label>
  {{ form.submit(class_="btn") }}
</form>
{% endblock %}
""",
"admin_dashboard.html": r"""
{% extends "base.html" %}
{% block content %}
<h2>Admin Dashboard</h2>
<p>Remaining: <span class="pill">{{ remaining }}</span> / {{ max_number }}</p>

<form method="post" style="margin:12px 0;">
  {{ form.hidden_tag() }}
  <div class="row">
    <label>{{ form.max_number.label }} {{ form.max_number(min=1) }}</label>
    {{ form.submit(class_="btn") }}
  </div>
</form>

<p>
  <a class="btn" href="{{ url_for('admin_export') }}">Download CSV</a>
  <a class="btn" href="{{ url_for('admin_logout') }}">Logout</a>
</p>

<table>
  <thead><tr><th>ID</th><th>Name</th><th>Email</th><th>Phone</th><th>Number</th><th>When</th></tr></thead>
  <tbody>
    {% for e in entries %}
      <tr>
        <td>{{ e.id }}</td>
        <td>{{ e.name }}</td>
        <td>{{ e.email }}</td>
        <td>{{ e.phone }}</td>
        <td><strong>#{{ e.number }}</strong></td>
        <td>{{ e.created_at.strftime("%Y-%m-%d %H:%M") }}</td>
      </tr>
    {% endfor %}
  </tbody>
</table>
{% endblock %}
""",
}
app.jinja_loader = DictLoader(TEMPLATES)

# ------------------ Models ------------------
class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    max_number = db.Column(db.Integer, default=100)  # X (1..X)

class Entry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(40), nullable=True)
    number = db.Column(db.Integer, unique=True, nullable=False)  # unique ensures no duplicates
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

# ------------------ Admin auth (single user via env) ------------------
class Admin(UserMixin):
    id = "admin"

login_manager = LoginManager(app)
login_manager.login_view = "admin_login"

@login_manager.user_loader
def load_user(user_id):
    if user_id == "admin":
        return Admin()
    return None

# ------------------ Forms ------------------
class PublicForm(FlaskForm):
    name  = StringField("Name", validators=[DataRequired(), Length(max=120)])
    email = EmailField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    phone = TelField(
        "Phone",
        validators=[
            Optional(),
            Length(max=40),
            # Allow digits, +, (, ), spaces, dot, and dash. Dash is escaped or placed at end.
            Regexp(r"^[0-9+()\-.\s]*$", message="Phone format invalid"),
        ],
    )
    submit = SubmitField("Get my number")

class AdminLoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Sign in")

class AdminSettingsForm(FlaskForm):
    max_number = IntegerField("Total raffle numbers (X)", validators=[DataRequired(), NumberRange(min=1, max=100000)])
    submit = SubmitField("Save")

# ------------------ Helpers ------------------
def get_settings():
    s = Settings.query.first()
    if not s:
        s = Settings(max_number=100)
        db.session.add(s)
        db.session.commit()
    return s

def available_numbers():
    s = get_settings()
    taken = {n for (n,) in db.session.query(Entry.number).all()}
    return [i for i in range(1, s.max_number + 1) if i not in taken]

def assign_number():
    avail = available_numbers()
    if not avail:
        return None
    return random.choice(avail)

# ------------------ Routes ------------------
@app.route("/", methods=["GET", "POST"])
@limiter.limit("5/minute")
def index():
    form = PublicForm()
    s = get_settings()

    if form.validate_on_submit():
        # Normalize/validate email
        try:
            v = validate_email(form.email.data, check_deliverability=False)
            email = v.normalized
        except EmailNotValidError:
            flash("Please enter a valid email address.", "danger")
            return render_template("index.html", form=form, max_number=s.max_number, remaining=len(available_numbers()))

        num = assign_number()  # ALWAYS random; users cannot pick
        if not num:
            flash("Sorry, all numbers are taken.", "warning")
            return render_template("index.html", form=form, max_number=s.max_number, remaining=0)

        try:
            entry = Entry(
                name=form.name.data.strip(),
                email=email,
                phone=(form.phone.data or "").strip(),
                number=num,
            )
            db.session.add(entry)
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash("Number was just taken—please try again.", "warning")
            return render_template("index.html", form=form, max_number=s.max_number, remaining=len(available_numbers()))

        session["last_number"] = num
        session["last_name"] = entry.name
        return redirect(url_for("success"))

    return render_template("index.html", form=form, max_number=s.max_number, remaining=len(available_numbers()))

@app.route("/success")
def success():
    num = session.get("last_number")
    name = session.get("last_name", "Friend")
    if not num:
        return redirect(url_for("index"))
    kehilla_url = "https://www.charityextra.com/charity/kehilla"
    return render_template("success.html", number=num, name=name, donate_url=kehilla_url)

# ------------------ Admin ------------------
from flask_login import current_user

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    form = AdminLoginForm()
    if form.validate_on_submit():
        if (form.username.data == os.getenv("ADMIN_USERNAME", "admin")
            and form.password.data == os.getenv("ADMIN_PASSWORD", "")):
            login_user(Admin())
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials.", "danger")
    return render_template("admin_login.html", form=form)

@app.route("/admin/logout")
@login_required
def admin_logout():
    logout_user()
    return redirect(url_for("index"))

@app.route("/admin", methods=["GET", "POST"])
@login_required
def admin_dashboard():
    s = get_settings()
    form = AdminSettingsForm(max_number=s.max_number)
    if form.validate_on_submit():
        s.max_number = form.max_number.data
        db.session.commit()
        flash("Settings updated.", "success")
        return redirect(url_for("admin_dashboard"))

    entries = Entry.query.order_by(Entry.created_at.desc()).all()
    return render_template("admin_dashboard.html",
                           form=form,
                           entries=entries,
                           remaining=len(available_numbers()),
                           max_number=s.max_number)

@app.route("/admin/export.csv")
@login_required
def admin_export():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id","name","email","phone","number","created_at"])
    for e in Entry.query.order_by(Entry.id.asc()).all():
        writer.writerow([e.id, e.name, e.email, e.phone, e.number, e.created_at.isoformat()])
    output.seek(0)
    return send_file(io.BytesIO(output.getvalue().encode("utf-8")),
                     mimetype="text/csv", as_attachment=True,
                     download_name="kehilla_raffle_entries.csv")

# ------------------ CLI helper ------------------
@app.cli.command("init-db")
def init_db():
    db.create_all()
    get_settings()
    print("Database initialised.")

# ------------------ Main ------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        get_settings()
    app.run(debug=True)
