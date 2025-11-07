# raffle_multi.py — multi-charity raffle (single file, polished UI, embedded logo for /thekehilla)
# ----------------------------------------------------------------------
# Features:
# - Public per-charity raffle pages with progress bar & polished design
# - Admin (env-guarded): add/edit charities, entries log (filters), CSV export, bulk mark-paid/unpaid/delete, partner user mgmt
# - Partner (per charity): login, entries list, add/edit/delete, bulk actions (restricted to own charity)
# - DB: SQLite (./instance/raffle.db) by default or Postgres via DATABASE_URL
# - Light auto-migration for Entry.paid / Entry.paid_at columns
# - Embedded logo ONLY on /thekehilla via KEHILLA_LOGO_DATA_URI (replace with your PNG/JPG base64 when ready)

from flask import (
    Flask, render_template_string, request, redirect,
    url_for, session, flash, abort, send_file
)
import os, random, csv, io
from datetime import datetime, timedelta
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint, inspect, text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash

# ====== CONFIG ================================================================

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "devkey")
app.permanent_session_lifetime = timedelta(minutes=30)

DB_URL = os.getenv("DATABASE_URL")
if DB_URL:
    DB_URL = DB_URL.replace("postgres://", "postgresql://")
    app.config["SQLALCHEMY_DATABASE_URI"] = DB_URL
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    INSTANCE = os.path.join(BASE_DIR, "instance")
    os.makedirs(INSTANCE, exist_ok=True)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(INSTANCE,'raffle.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# Embedded logo only for /thekehilla.
# Replace this with your actual bitmap data URI when ready, e.g.:
# KEHILLA_LOGO_DATA_URI = "data:image/png;base64,AAAA...."
KEHILLA_LOGO_DATA_URI = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAALQAAAA8CAIAAABATAfQAAASAElEQVR42uYVwY7aMBCFv1WgQqv5S2oQm2k3Yg2qEoZKk8o0tJ8fA2y5Xz4b2H2M8S8V0Qm9oLk0e1Q6Q3mA3v1gWl/0wF4y/7xM1tXqv1A0d3WlqH5w1k5s4y7mG3w5g2L+FQ9/0Xk2m3J9y0mQx1m5o1xXWm8g6Lz2gqf3C9m4g2zJ6h4oQ8C7D8A9aH8b1t6+2k3H3G3pD8lqJt9J3m9Ztq9nJ8WcKpKJ8uHqVgqgVQdJ1S6qk9bP/4v7b1r2qvZ5u1cOQ4Q+0fW2On8wBzvQh7G9hXG2f8oBqv5z4z3rQk3J8+ZrX4eCq9b1e1m1kz0gE8f4F1iGd7qj4i5p8Hq6m8lJQ9v2v1k3t2H4oWw3O1r0b2M3s3o6zYzMq8k6rZ3vU4wJ0c7Tt2G2I6N0rW8eUuT5x1bQ3H8k4YcQwF3S2j6v7bHk2yA7h3dVq0a2m4JrU7Qm9vE6b7k8GQm9V3p1J9JkO8S5dYq8d0m5r8d7w6o2Qk3m6i7u0q2V8k6b5yYl4mVq4tG2oV9qf4yGq8b3u7mH9Qk9wF6H8oH0UOq3pM3g8mTz2Rr8Ww5Rr8oKk7cU+fKJrX2Qf9ZgB5x3l4r5F6mJb9mUo8iQm8Q5n+z7rGk4Rt9m5iWk4pJ5v1oU7P5K6wZq5t8oX0m8f7P6m7k7a1o8U8Sg3gk1"

# ====== MODELS ================================================================

class Charity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), unique=True, nullable=False)        # URL slug
    name = db.Column(db.String(200), nullable=False)
    donation_url = db.Column(db.String(500), nullable=False)
    max_number = db.Column(db.Integer, nullable=False, default=500)     # 1..max

class Entry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    charity_id = db.Column(db.Integer, db.ForeignKey("charity.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(40))
    number = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    paid = db.Column(db.Boolean, nullable=False, default=False)
    paid_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (UniqueConstraint("charity_id", "number", name="uq_charity_number"),)
    charity = db.relationship("Charity", backref="entries")

class CharityUser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    charity_id = db.Column(db.Integer, db.ForeignKey("charity.id"), nullable=False, index=True)
    username = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("charity_id", "username", name="uq_charityuser_char_user"),)
    charity = db.relationship("Charity", backref="users")

    def set_password(self, pw: str): self.password_hash = generate_password_hash(pw)
    def check_password(self, pw: str) -> bool: return check_password_hash(self.password_hash, pw)

# ====== LAYOUT / RENDER =======================================================

LAYOUT = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title or "Get My Number" }}</title>
<meta name="color-scheme" content="dark light">
<style>
  :root{
    --bg:#0b0f14; --bg-soft:#0f1520; --card:#121924; --card-2:#0f1722;
    --text:#e9f0f7; --muted:#a8b6c7; --brand:#5aa8ff; --brand-2:#7cd2ff;
    --ok:#3ad19f; --warn:#ffd29f; --danger:#ff7a7a; --border:#223146;
    --shadow:0 10px 30px rgba(0,0,0,.35); --radius:16px;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:
    radial-gradient(1200px 800px at 10% -10%, #122034, transparent),
    radial-gradient(900px 700px at 110% 10%, #101a28, transparent), var(--bg);
    color:var(--text)}
  a{color:var(--brand);text-decoration:none}
  .wrap{max-width:1100px;margin:0 auto;padding:28px}
  .nav{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
  .logo{display:flex;gap:10px;align-items:center;color:var(--text);text-decoration:none}
  .logo-badge{width:36px;height:36px;border-radius:10px;display:grid;place-items:center;
              background:linear-gradient(135deg,var(--brand),var(--brand-2));color:#05111e;font-weight:900}
  .nav-links a{color:var(--muted);padding:8px 10px;border:1px solid transparent;border-radius:999px}
  .nav-links a:hover{border-color:var(--border);color:var(--text)}
  .card{background:linear-gradient(180deg,var(--card),var(--card-2));border:1px solid var(--border);border-radius:var(--radius);padding:24px;box-shadow:var(--shadow)}
  .hero h1{margin:0 0 6px 0;font-size:28px}
  .hero p{margin:0;color:var(--muted)}
  .stack{display:flex;gap:10px;flex-wrap:wrap}
  .pill,button.btn{display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border-radius:999px;border:1px solid var(--border);color:var(--text);background:transparent;cursor:pointer}
  button.btn{background:linear-gradient(135deg,var(--brand),var(--brand-2));border:none;color:#05111e;font-weight:700}
  button.btn.secondary{background:transparent;border:1px solid var(--border);color:var(--text)}
  button.btn:disabled{opacity:.6;cursor:not-allowed}
  .grid{display:grid;gap:12px}
  @media(min-width:720px){ .grid-2{grid-template-columns:1fr 1fr} }
  input[type=text],input[type=email],input[type=tel],input[type=number],input[type=url],input[type=password]{
    width:100%;padding:13px 14px;border-radius:12px;border:1px solid var(--border);
    background:#0c141f;color:var(--text);outline:none
  }
  label{display:grid;gap:6px;margin-bottom:10px;color:var(--muted);font-size:14px}
  .muted{color:var(--muted)} .sep{height:10px}
  table{width:100%;border-collapse:collapse;margin-top:12px;border:1px solid var(--border);border-radius:12px;overflow:hidden}
  thead th{background:#0f1722;color:var(--muted);font-weight:600}
  th,td{padding:10px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}
  tr:hover td{background:#0e1520}
  .badge{display:inline-block;padding:4px 8px;border-radius:10px;border:1px solid var(--border);color:#cfe6ff}
  .badge.ok{background:rgba(58,209,159,.12);border-color:#214e3f;color:#7ef2c8}
  .badge.warn{background:rgba(255,210,159,.12);border-color:#5c4930;color:#ffd7aa}
  .progress{height:10px;background:#0a131d;border:1px solid var(--border);border-radius:999px;overflow:hidden}
  .progress > i{display:block;height:100%;background:linear-gradient(90deg,var(--brand),var(--brand-2))}
  .footer{color:var(--muted);text-align:center;margin-top:16px;font-size:13px}
  .row{display:flex;gap:10px;flex-wrap:wrap}
</style>
<script>
  document.addEventListener('DOMContentLoaded', ()=>{
    document.querySelectorAll('form[data-safe-submit]').forEach(f=>{
      f.addEventListener('submit', ()=>{
        const btn = f.querySelector('button[type="submit"]');
        if(btn){ btn.disabled = true; btn.textContent = 'Working…'; }
      });
    });
  });
</script>
</head>
<body>
  <div class="wrap">
    <nav class="nav">
      <a class="logo" href="{{ url_for('home') }}">
        <span class="logo-badge">#</span>
        <strong>Get My Number</strong>
      </a>
      <div class="nav-links">
        <a href="{{ url_for('admin_charities') }}">Admin</a>
        <a href="{{ url_for('partner_login') }}">Partner</a>
      </div>
    </nav>

    <section class="card">
      {% with messages = get_flashed_messages() %}
        {% if messages %}
          <div class="stack" style="margin-bottom:10px">
            {% for m in messages %}<span class="badge warn">{{ m }}</span>{% endfor %}
          </div>
        {% endif %}
      {% endwith %}
      {{ body|safe }}
    </section>

    <p class="footer">© {{ datetime.utcnow().year }} Get My Number • Secure raffle donations</p>
  </div>
</body></html>
"""

def render(body, **ctx):
    inner = render_template_string(body, request=request, datetime=datetime, **ctx)
    return render_template_string(LAYOUT, body=inner, request=request, datetime=datetime, **ctx)

# ====== HELPERS ===============================================================

def get_charity_or_404(slug: str) -> Charity:
    c = Charity.query.filter_by(slug=slug.lower().strip()).first()
    if not c: abort(404)
    return c

def available_numbers(c: Charity):
    taken = {n for (n,) in db.session.query(Entry.number).filter(Entry.charity_id == c.id).all()}
    return [i for i in range(1, c.max_number + 1) if i not in taken]

def assign_number(c: Charity):
    avail = available_numbers(c)
    return random.choice(avail) if avail else None

def partner_guard(slug):
    if not session.get("partner_ok"): return None
    if session.get("partner_slug") != slug: return None
    c = Charity.query.filter_by(slug=slug).first()
    if not c or session.get("partner_charity_id") != c.id: return None
    return c

# ====== PUBLIC ================================================================

@app.route("/")
def home():
    charities = Charity.query.order_by(Charity.name.asc()).all()
    body = """
    <h2>Pick a charity</h2>
    <p class="muted">Choose a raffle page:</p>
    <div>
      {% for c in charities %}
        <a class="pill" href="{{ url_for('charity_page', slug=c.slug) }}"><strong>{{ c.name }}</strong></a>
      {% else %}
        <p class="muted">No charities yet. Use <a href="{{ url_for('admin_charities') }}">Admin</a> to add one.</p>
      {% endfor %}
    </div>
    """
    return render(body, charities=charities, title="Get My Number")

@app.route("/<slug>", methods=["GET","POST"])
def charity_page(slug):
    charity = get_charity_or_404(slug)

    if request.method == "POST":
        name = request.form.get("name","").strip()
        email = request.form.get("email","").strip()
        phone = request.form.get("phone","").strip()
        if not name or not email:
            flash("Name and Email are required.")
        else:
            num = assign_number(charity)
            if not num:
                flash("Sorry, all numbers are taken for this charity.")
            else:
                try:
                    entry = Entry(charity_id=charity.id, name=name, email=email, phone=phone, number=num)
                    db.session.add(entry); db.session.commit()
                    session["last_num"] = num; session["last_name"] = name; session["last_slug"] = charity.slug
                    return redirect(url_for("success", slug=charity.slug))
                except IntegrityError:
                    db.session.rollback()
                    flash("That number was just taken—please try again.")

    total = charity.max_number
    remaining = len(available_numbers(charity))
    taken = total - remaining
    pct = int((taken / total) * 100) if total else 0

    # Only show embedded logo on /thekehilla
    kehilla_logo = KEHILLA_LOGO_DATA_URI if charity.slug == "thekehilla" else None

    body = """
    <div class="hero">
      <h1>{{ charity.name }}</h1>
      {% if kehilla_logo %}
        <img src="{{ kehilla_logo }}" alt="{{ charity.name }} logo" style="max-width:180px;margin:12px 0;border-radius:12px;">
      {% endif %}
      <p>Numbers are unique between 1 and {{ total }}.</p>
      <div class="sep"></div>
      <div class="grid grid-2">
        <div>
          <div class="row">
            <span class="badge ok">Available: {{ remaining }}</span>
            <span class="badge">Taken: {{ taken }}</span>
          </div>
          <div class="sep"></div>
          <div class="progress" aria-label="progress"><i style="width: {{ pct }}%"></i></div>
          <p class="muted" style="margin-top:6px">{{ pct }}% filled</p>
        </div>
        <form method="post" data-safe-submit>
          <label>Full name <input type="text" name="name" required placeholder="e.g. Sarah Cohen"></label>
          <label>Email <input type="email" name="email" required placeholder="name@example.com"></label>
          <label>Phone (optional) <input type="tel" name="phone" placeholder="+44 7xxx xxxxxx"></label>
          <div class="row" style="margin-top:8px">
            <button class="btn" type="submit">Get my number</button>
            <a class="pill" href="{{ charity.donation_url }}" target="_blank" rel="noopener">Donation page</a>
          </div>
        </form>
      </div>
      <div class="sep"></div>
      <p class="muted">You’ll get a random number still available. Your donation equals your number.</p>
    </div>
    """
    return render(body, charity=charity, total=total, remaining=remaining, taken=taken, pct=pct,
                  title=charity.name, kehilla_logo=kehilla_logo)

@app.route("/<slug>/success")
def success(slug):
    charity = get_charity_or_404(slug)
    if session.get("last_slug") != charity.slug or "last_num" not in session:
        return redirect(url_for("charity_page", slug=charity.slug))
    num = session.get("last_num"); name = session.get("last_name", "Friend")

    body = """
    <div class="hero">
      <h1>Thank you{{ ", %s" % name if name else "" }} 🎉</h1>
      <p>Your raffle number for <strong>{{ charity.name }}</strong> is:</p>
      <h2 style="margin:10px 0;"><span class="badge" style="font-size:22px">#{{ num }}</span></h2>
      <p class="muted">Please donate <strong>£{{ num }}</strong> to complete your entry.</p>
      <div class="row" style="margin-top:12px">
        <a class="btn" href="{{ charity.donation_url }}" target="_blank" rel="noopener">Go to donation page</a>
        <button class="pill" onclick="navigator.clipboard.writeText('{{ num }}').then(()=>alert('Amount copied to clipboard'))">Copy amount ({{ num }})</button>
      </div>
    </div>
    """
    return render(body, charity=charity, num=num, name=name, title=charity.name)

# ====== ADMIN (env guarded) ===================================================

@app.route("/admin/charities", methods=["GET","POST"])
def admin_charities():
    admin_user = os.getenv("ADMIN_USERNAME", "admin")
    admin_pw   = os.getenv("ADMIN_PASSWORD", "")
    ok = session.get("admin_ok", False)
    last_login = session.get("admin_login_time")
    msg = None

    if last_login:
        try:
            if datetime.utcnow() - datetime.fromisoformat(last_login) > app.permanent_session_lifetime:
                session.clear(); ok = False; flash("Session expired. Please log in again.")
        except Exception:
            session.clear(); ok = False

    if request.method == "POST":
        if not ok:
            if (request.form.get("username") == admin_user and
                request.form.get("password") == admin_pw and admin_pw):
                session.permanent = True
                session["admin_ok"] = True
                session["admin_login_time"] = datetime.utcnow().isoformat()
                ok = True; flash("Logged in successfully.")
            else:
                msg = "Invalid username or password."
        else:
            slug = request.form.get("slug","").strip().lower()
            name = request.form.get("name","").strip()
            url  = request.form.get("donation_url","").strip()
            try: maxn = int(request.form.get("max_number","500") or 500)
            except ValueError: maxn = 500
            if not slug or not name or not url:
                msg = "All fields are required."
            elif Charity.query.filter_by(slug=slug).first():
                msg = "Slug already exists."
            else:
                c = Charity(slug=slug, name=name, donation_url=url, max_number=maxn)
                db.session.add(c); db.session.commit()
                msg = f"Saved. Public page: /{slug}"

    charities = Charity.query.order_by(Charity.name.asc()).all()
    remaining = {c.id: len(available_numbers(c)) for c in charities}

    body = """
    <h2>Manage Charities</h2>
    {% if msg %}<div style="margin:6px 0;color:#ffd29f">{{ msg }}</div>{% endif %}
    {% if not ok %}
      <form method="post">
        <label>Username <input type="text" name="username" required></label>
        <label>Password <input type="password" name="password" required></label>
        <div style="margin-top:8px"><button class="btn">Enter</button></div>
      </form>
    {% else %}
      <div class="row" style="margin-bottom:10px">
        <a class="pill" href="{{ url_for('admin_logout') }}">Log out</a>
      </div>
      <form method="post" style="margin-bottom:12px">
        <label>Slug <input type="text" name="slug" placeholder="thekehilla" required></label>
        <label>Name <input type="text" name="name" placeholder="The Kehilla" required></label>
        <label>Donation URL <input type="url" name="donation_url" placeholder="https://www.charityextra.com/charity/kehilla" required></label>
        <label>Max number <input type="number" name="max_number" value="500" min="1"></label>
        <div style="margin-top:8px"><button class="btn">Add / Save</button></div>
      </form>
      <table>
        <thead><tr><th>Slug</th><th>Name</th><th>Max</th><th>Remaining</th><th>Actions</th></tr></thead>
        <tbody>
          {% for c in charities %}
            <tr>
              <td>{{ c.slug }}</td>
              <td>{{ c.name }}</td>
              <td>{{ c.max_number }}</td>
              <td>{{ remaining[c.id] }}</td>
              <td>
                <a class="pill" href="{{ url_for('charity_page', slug=c.slug) }}">Open</a>
                <a class="pill" href="{{ url_for('edit_charity', slug=c.slug) }}">Edit</a>
                <a class="pill" href="{{ url_for('admin_charity_entries', slug=c.slug) }}">Entries</a>
                <a class="pill" href="{{ url_for('admin_charity_users', slug=c.slug) }}">Users</a>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% endif %}
    """
    return render(body, ok=ok, msg=msg, charities=charities, remaining=remaining, title="Manage Charities")

@app.route("/admin/logout")
def admin_logout():
    session.clear(); flash("Logged out.")
    return redirect(url_for("admin_charities"))

@app.route("/admin/charity/<slug>", methods=["GET","POST"])
def edit_charity(slug):
    if not session.get("admin_ok"): return redirect(url_for("admin_charities"))
    charity = Charity.query.filter_by(slug=slug).first_or_404()
    msg = None
    if request.method == "POST":
        charity.name = request.form.get("name", charity.name).strip()
        charity.donation_url = request.form.get("donation_url", charity.donation_url).strip()
        try: charity.max_number = int(request.form.get("max_number", charity.max_number))
        except ValueError: msg = "Invalid number format."
        else:
            db.session.commit(); msg = "Charity updated successfully."
    body = """
    <h2>Edit Charity</h2>
    {% if msg %}<div style="margin:6px 0;color:#ffd29f">{{ msg }}</div>{% endif %}
    <form method="post" data-safe-submit>
      <label>Name <input type="text" name="name" value="{{ charity.name }}" required></label>
      <label>Donation URL <input type="url" name="donation_url" value="{{ charity.donation_url }}" required></label>
      <label>Max number <input type="number" name="max_number" value="{{ charity.max_number }}" min="1"></label>
      <div style="margin-top:8px"><button class="btn">Save Changes</button></div>
    </form>
    <p><a class="pill" href="{{ url_for('admin_charities') }}">← Back to Manage Charities</a></p>
    """
    return render(body, charity=charity, msg=msg, title=f"Edit {charity.name}")

# ====== ADMIN: ENTRIES / CSV / BULK ==========================================

@app.route("/admin/charity/<slug>/entries")
def admin_charity_entries(slug):
    if not session.get("admin_ok"): return redirect(url_for("admin_charities"))
    charity = Charity.query.filter_by(slug=slug).first_or_404()
    flt = request.args.get("filter")
    q = Entry.query.filter_by(charity_id=charity.id)
    if flt == "paid": q = q.filter(Entry.paid.is_(True))
    elif flt == "unpaid": q = q.filter(Entry.paid.is_(False))
    entries = q.order_by(Entry.id.desc()).all()
    body = """
    <h2>Entries — {{ charity.name }}</h2>
    <p class="muted">Total: {{ entries|length }}</p>

    <p>
      <a class="pill" href="{{ url_for('admin_charity_entries', slug=charity.slug) }}">All</a>
      <a class="pill" href="{{ url_for('admin_charity_entries', slug=charity.slug, filter='unpaid') }}">Unpaid</a>
      <a class="pill" href="{{ url_for('admin_charity_entries', slug=charity.slug, filter='paid') }}">Paid</a>
      <a class="pill" href="{{ url_for('admin_charity_entries_csv', slug=charity.slug) }}">Download CSV</a>
      <a class="pill" href="{{ url_for('admin_charities') }}">← Back</a>
    </p>

    <form method="post" action="{{ url_for('admin_bulk_entries', slug=charity.slug) }}">
      <div class="row" style="margin:8px 0">
        <button class="pill" type="submit" name="action" value="mark_paid">Mark paid</button>
        <button class="pill" type="submit" name="action" value="mark_unpaid">Unmark</button>
        <button class="pill" type="submit" name="action" value="delete" onclick="return confirm('Delete selected entries?')">Delete</button>
      </div>

      <table>
        <thead>
          <tr>
            <th><input type="checkbox" onclick="for(const cb of document.querySelectorAll('.rowcb')) cb.checked=this.checked"></th>
            <th>ID</th><th>Name</th><th>Email</th><th>Phone</th>
            <th>Number</th><th>Created</th><th>Paid</th><th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {% for e in entries %}
            <tr>
              <td><input class="rowcb" type="checkbox" name="ids" value="{{ e.id }}"></td>
              <td>{{ e.id }}</td>
              <td>{{ e.name }}</td>
              <td>{{ e.email }}</td>
              <td>{{ e.phone }}</td>
              <td><strong>#{{ e.number }}</strong></td>
              <td>{{ e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else "" }}</td>
              <td>
                {{ "Yes" if e.paid else "No" }}
                {% if e.paid_at %}<span class="muted">({{ e.paid_at.strftime("%Y-%m-%d %H:%M") }})</span>{% endif %}
              </td>
              <td>
                <form method="post" action="{{ url_for('toggle_paid', entry_id=e.id, next=request.full_path) }}" style="display:inline">
                  <button class="pill" type="submit">{{ "Unmark" if e.paid else "Mark paid" }}</button>
                </form>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </form>
    """
    return render(body, charity=charity, entries=entries, title=f"Entries – {charity.name}")

@app.route("/admin/charity/<slug>/entries.csv")
def admin_charity_entries_csv(slug):
    if not session.get("admin_ok"): return redirect(url_for("admin_charities"))
    charity = Charity.query.filter_by(slug=slug).first_or_404()
    entries = Entry.query.filter_by(charity_id=charity.id).order_by(Entry.id.asc()).all()
    output = io.StringIO(); w = csv.writer(output)
    w.writerow(["id","name","email","phone","number","created_at","paid","paid_at","charity_slug","charity_name"])
    for e in entries:
        w.writerow([
            e.id, e.name, e.email, e.phone, e.number,
            e.created_at.isoformat() if e.created_at else "",
            1 if e.paid else 0,
            e.paid_at.isoformat() if e.paid_at else "",
            charity.slug, charity.name
        ])
    data = output.getvalue().encode("utf-8")
    return send_file(io.BytesIO(data), mimetype="text/csv", as_attachment=True, download_name=f"{slug}_entries.csv")

@app.route("/admin/entry/<int:entry_id>/toggle-paid", methods=["POST"])
def toggle_paid(entry_id):
    if not (session.get("admin_ok") or session.get("partner_ok")):
        return redirect(url_for("admin_charities"))
    e = Entry.query.get_or_404(entry_id)
    e.paid = not e.paid
    e.paid_at = datetime.utcnow() if e.paid else None
    db.session.commit()
    next_url = request.args.get("next") or url_for("admin_charity_entries", slug=e.charity.slug)
    return redirect(next_url)

@app.route("/admin/charity/<slug>/entries/bulk", methods=["POST"])
def admin_bulk_entries(slug):
    if not session.get("admin_ok"): return redirect(url_for("admin_charities"))
    charity = Charity.query.filter_by(slug=slug).first_or_404()
    ids = request.form.getlist("ids"); action = request.form.get("action")
    if not ids or not action: return redirect(url_for("admin_charity_entries", slug=slug))
    q = Entry.query.filter(Entry.charity_id == charity.id, Entry.id.in_(ids))
    now = datetime.utcnow()
    if action == "mark_paid":
        for e in q.all(): e.paid = True; e.paid_at = now
        db.session.commit()
    elif action == "mark_unpaid":
        for e in q.all(): e.paid = False; e.paid_at = None
        db.session.commit()
    elif action == "delete":
        q.delete(synchronize_session=False); db.session.commit()
    return redirect(url_for("admin_charity_entries", slug=slug))

# ====== ADMIN: PARTNER USERS ==================================================

@app.route("/admin/charity/<slug>/users", methods=["GET","POST"])
def admin_charity_users(slug):
    if not session.get("admin_ok"): return redirect(url_for("admin_charities"))
    charity = Charity.query.filter_by(slug=slug).first_or_404()
    msg = None
    if request.method == "POST":
        uname = request.form.get("username","").strip().lower()
        pw = request.form.get("password","").strip()
        if not uname or not pw:
            msg = "Username and password required."
        elif CharityUser.query.filter_by(charity_id=charity.id, username=uname).first():
            msg = "Username already exists for this charity."
        else:
            u = CharityUser(charity_id=charity.id, username=uname)
            u.set_password(pw); db.session.add(u); db.session.commit()
            msg = "User created."
    users = CharityUser.query.filter_by(charity_id=charity.id).order_by(CharityUser.username.asc()).all()
    body = """
    <h2>Users — {{ charity.name }}</h2>
    {% if msg %}<div style="margin:6px 0;color:#ffd29f">{{ msg }}</div>{% endif %}
    <form method="post" style="margin-bottom:12px" data-safe-submit>
      <label>Username <input type="text" name="username" required></label>
      <label>Password <input type="password" name="password" required></label>
      <div style="margin-top:8px"><button class="btn">Create User</button></div>
    </form>
    <table>
      <thead><tr><th>Username</th><th>Actions</th></tr></thead>
      <tbody>
        {% for u in users %}
          <tr>
            <td>{{ u.username }}</td>
            <td>
              <form method="post" action="{{ url_for('admin_delete_user', slug=charity.slug, uid=u.id) }}" style="display:inline" onsubmit="return confirm('Delete user {{ u.username }}?')">
                <button class="pill" type="submit">Delete</button>
              </form>
            </td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
    <p><a class="pill" href="{{ url_for('admin_charities') }}">← Back</a></p>
    """
    return render(body, charity=charity, users=users, msg=msg, title=f"Users – {charity.name}")

@app.route("/admin/charity/<slug>/users/<int:uid>/delete", methods=["POST"])
def admin_delete_user(slug, uid):
    if not session.get("admin_ok"): return redirect(url_for("admin_charities"))
    charity = Charity.query.filter_by(slug=slug).first_or_404()
    u = CharityUser.query.get_or_404(uid)
    if u.charity_id != charity.id: abort(403)
    db.session.delete(u); db.session.commit()
    return redirect(url_for("admin_charity_users", slug=slug))

# ====== PARTNER AREA ==========================================================

@app.route("/partner/login", methods=["GET","POST"])
def partner_login():
    msg = None
    if request.method == "POST":
        slug = request.form.get("slug","").strip().lower()
        username = request.form.get("username","").strip().lower()
        password = request.form.get("password","").strip()
        charity = Charity.query.filter_by(slug=slug).first()
        if not charity:
            msg = "Unknown charity slug."
        else:
            u = CharityUser.query.filter_by(charity_id=charity.id, username=username).first()
            if u and u.check_password(password):
                session.clear(); session.permanent = True
                session["partner_ok"] = True
                session["partner_slug"] = slug
                session["partner_charity_id"] = charity.id
                session["partner_username"] = username
                return redirect(url_for("partner_entries", slug=slug))
            else:
                msg = "Invalid username or password."
    body = """
    <h2>Partner Login</h2>
    {% if msg %}<div style="margin:6px 0;color:#ffd29f">{{ msg }}</div>{% endif %}
    <form method="post" data-safe-submit>
      <label>Charity slug <input type="text" name="slug" placeholder="thekehilla" required></label>
      <label>Username <input type="text" name="username" required></label>
      <label>Password <input type="password" name="password" required></label>
      <div style="margin-top:8px"><button class="btn">Login</button></div>
    </form>
    <p class="muted"><a href="{{ url_for('home') }}">Back to home</a></p>
    """
    return render(body, msg=msg, title="Partner Login")

@app.route("/partner/logout")
def partner_logout():
    session.pop("partner_ok", None)
    session.pop("partner_slug", None)
    session.pop("partner_charity_id", None)
    session.pop("partner_username", None)
    flash("Logged out.")
    return redirect(url_for("partner_login"))

@app.route("/partner/<slug>/entries")
def partner_entries(slug):
    charity = partner_guard(slug)
    if not charity: return redirect(url_for("partner_login"))
    flt = request.args.get("filter")
    q = Entry.query.filter_by(charity_id=charity.id)
    if flt == "paid": q = q.filter(Entry.paid.is_(True))
    elif flt == "unpaid": q = q.filter(Entry.paid.is_(False))
    entries = q.order_by(Entry.id.desc()).all()
    body = """
    <h2>Entries — {{ charity.name }}</h2>
    <p>
      <a class="pill" href="{{ url_for('partner_new_entry', slug=charity.slug) }}">Add Entry</a>
      <a class="pill" href="{{ url_for('partner_entries', slug=charity.slug) }}">All</a>
      <a class="pill" href="{{ url_for('partner_entries', slug=charity.slug, filter='unpaid') }}">Unpaid</a>
      <a class="pill" href="{{ url_for('partner_entries', slug=charity.slug, filter='paid') }}">Paid</a>
      <a class="pill" href="{{ url_for('partner_logout') }}">Log out</a>
    </p>

    <form method="post" action="{{ url_for('partner_bulk_entries', slug=charity.slug) }}">
      <div class="row" style="margin:8px 0">
        <button class="pill" type="submit" name="action" value="mark_paid">Mark paid</button>
        <button class="pill" type="submit" name="action" value="mark_unpaid">Unmark</button>
        <button class="pill" type="submit" name="action" value="delete" onclick="return confirm('Delete selected entries?')">Delete</button>
      </div>

      <table>
        <thead><tr>
          <th><input type="checkbox" onclick="for(const cb of document.querySelectorAll('.rowcb')) cb.checked=this.checked"></th>
          <th>ID</th><th>Name</th><th>Email</th><th>Phone</th><th>No.</th><th>Created</th><th>Paid</th><th>Actions</th>
        </tr></thead>
        <tbody>
        {% for e in entries %}
          <tr>
            <td><input class="rowcb" type="checkbox" name="ids" value="{{ e.id }}"></td>
            <td>{{ e.id }}</td>
            <td>{{ e.name }}</td>
            <td>{{ e.email }}</td>
            <td>{{ e.phone }}</td>
            <td><strong>#{{ e.number }}</strong></td>
            <td>{{ e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else "" }}</td>
            <td>{{ "Yes" if e.paid else "No" }}</td>
            <td>
              <form method="post" action="{{ url_for('toggle_paid', entry_id=e.id, next=request.full_path) }}" style="display:inline">
                <button class="pill" type="submit">{{ "Unmark" if e.paid else "Mark paid" }}</button>
              </form>
              <a class="pill" href="{{ url_for('partner_edit_entry', slug=charity.slug, entry_id=e.id) }}">Edit</a>
              <form method="post" action="{{ url_for('partner_delete_entry', slug=charity.slug, entry_id=e.id) }}" style="display:inline" onsubmit="return confirm('Delete this entry?')">
                <button class="pill" type="submit">Delete</button>
              </form>
            </td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </form>
    """
    return render(body, charity=charity, entries=entries, title=f"{charity.name} – Entries")

@app.route("/partner/<slug>/entries/new", methods=["GET","POST"])
def partner_new_entry(slug):
    charity = partner_guard(slug)
    if not charity: return redirect(url_for("partner_login"))
    msg = None
    if request.method == "POST":
        name = request.form.get("name","").strip()
        email = request.form.get("email","").strip()
        phone = request.form.get("phone","").strip()
        number_raw = request.form.get("number","").strip()
        if not name or not email:
            msg = "Name and Email required."
        else:
            if number_raw:
                try:
                    num = int(number_raw)
                    if num < 1 or num > charity.max_number: msg = f"Number must be between 1 and {charity.max_number}."
                except ValueError:
                    msg = "Number must be an integer."; num = None
            else:
                taken = {n for (n,) in db.session.query(Entry.number).filter(Entry.charity_id==charity.id).all()}
                avail = [i for i in range(1, charity.max_number+1) if i not in taken]
                num = random.choice(avail) if avail else None
                if not num: msg = "No numbers available."
            if not msg and num is not None:
                try:
                    e = Entry(charity_id=charity.id, name=name, email=email, phone=phone, number=num)
                    db.session.add(e); db.session.commit()
                    return redirect(url_for("partner_entries", slug=charity.slug))
                except IntegrityError:
                    db.session.rollback(); msg = "That number is already taken."
    body = """
    <h2>Add Entry — {{ charity.name }}</h2>
    {% if msg %}<div style="margin:6px 0;color:#ffd29f">{{ msg }}</div>{% endif %}
    <form method="post" data-safe-submit>
      <label>Name <input type="text" name="name" required></label>
      <label>Email <input type="email" name="email" required></label>
      <label>Phone <input type="tel" name="phone"></label>
      <label>Number (leave blank to auto-assign) <input type="number" name="number" min="1" max="{{ charity.max_number }}"></label>
      <div style="margin-top:8px"><button class="btn">Save</button> <a class="pill" href="{{ url_for('partner_entries', slug=charity.slug) }}">Cancel</a></div>
    </form>
    """
    return render(body, charity=charity, msg=msg, title=f"Add Entry – {charity.name}")

@app.route("/partner/<slug>/entry/<int:entry_id>/edit", methods=["GET","POST"])
def partner_edit_entry(slug, entry_id):
    charity = partner_guard(slug)
    if not charity: return redirect(url_for("partner_login"))
    e = Entry.query.get_or_404(entry_id)
    if e.charity_id != charity.id: abort(403)
    msg = None
    if request.method == "POST":
        e.name = request.form.get("name", e.name).strip()
        e.email = request.form.get("email", e.email).strip()
        e.phone = request.form.get("phone", e.phone).strip()
        number_raw = request.form.get("number","").strip()
        if number_raw:
            try:
                newnum = int(number_raw)
                if newnum < 1 or newnum > charity.max_number:
                    msg = f"Number must be between 1 and {charity.max_number}."
                else:
                    e.number = newnum
            except ValueError:
                msg = "Number must be an integer."
        try:
            if not msg:
                db.session.commit()
                return redirect(url_for("partner_entries", slug=charity.slug))
        except IntegrityError:
            db.session.rollback(); msg = "That number is already taken."
    body = """
    <h2>Edit Entry — {{ charity.name }}</h2>
    {% if msg %}<div style="margin:6px 0;color:#ffd29f">{{ msg }}</div>{% endif %}
    <form method="post" data-safe-submit>
      <label>Name <input type="text" name="name" value="{{ e.name }}" required></label>
      <label>Email <input type="email" name="email" value="{{ e.email }}" required></label>
      <label>Phone <input type="tel" name="phone" value="{{ e.phone or '' }}"></label>
      <label>Number <input type="number" name="number" value="{{ e.number }}" min="1" max="{{ charity.max_number }}"></label>
      <div style="margin-top:8px"><button class="btn">Save</button> <a class="pill" href="{{ url_for('partner_entries', slug=charity.slug) }}">Cancel</a></div>
    </form>
    """
    return render(body, charity=charity, e=e, msg=msg, title=f"Edit Entry – {charity.name}")

@app.route("/partner/<slug>/entry/<int:entry_id>/delete", methods=["POST"])
def partner_delete_entry(slug, entry_id):
    charity = partner_guard(slug)
    if not charity: return redirect(url_for("partner_login"))
    e = Entry.query.get_or_404(entry_id)
    if e.charity_id != charity.id: abort(403)
    db.session.delete(e); db.session.commit()
    return redirect(url_for("partner_entries", slug=charity.slug))

@app.route("/partner/<slug>/entries/bulk", methods=["POST"])
def partner_bulk_entries(slug):
    charity = partner_guard(slug)
    if not charity: return redirect(url_for("partner_login"))
    ids = request.form.getlist("ids"); action = request.form.get("action")
    if not ids or not action: return redirect(url_for("partner_entries", slug=slug))
    q = Entry.query.filter(Entry.charity_id == charity.id, Entry.id.in_(ids))
    now = datetime.utcnow()
    if action == "mark_paid":
        for e in q.all(): e.paid = True; e.paid_at = now
        db.session.commit()
    elif action == "mark_unpaid":
        for e in q.all(): e.paid = False; e.paid_at = None
        db.session.commit()
    elif action == "delete":
        q.delete(synchronize_session=False); db.session.commit()
    return redirect(url_for("partner_entries", slug=slug))

# ====== (Optional) MANUAL MIGRATION ==========================================

@app.route("/admin/migrate")
def admin_migrate():
    if not session.get("admin_ok"): return redirect(url_for("admin_charities"))
    try:
        with db.engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE entry ADD COLUMN paid BOOLEAN DEFAULT 0")
    except Exception as e:
        print("paid column:", e)
    try:
        with db.engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE entry ADD COLUMN paid_at DATETIME")
    except Exception as e:
        print("paid_at column:", e)
    return "Migration attempted. Go back to Entries and refresh."

# ====== DB INIT / SEED ========================================================

with app.app_context():
    db.create_all()
    try:
        insp = inspect(db.engine)
        cols = {c['name'] for c in insp.get_columns('entry')}
        with db.engine.begin() as conn:
            if 'paid' not in cols:
                conn.execute(text("ALTER TABLE entry ADD COLUMN paid BOOLEAN DEFAULT 0"))
            if 'paid_at' not in cols:
                conn.execute(text("ALTER TABLE entry ADD COLUMN paid_at DATETIME"))
    except Exception as e:
        print("Auto-migration check failed:", e)

    # Seed default charity for convenience
    if not Charity.query.filter_by(slug="thekehilla").first():
        db.session.add(Charity(
            slug="thekehilla",
            name="The Kehilla",
            donation_url="https://www.charityextra.com/charity/kehilla",
            max_number=500
        ))
        db.session.commit()
        print("Seeded default charity: /thekehilla")

    thek = Charity.query.filter_by(slug="thekehilla").first()
    if thek and not CharityUser.query.filter_by(charity_id=thek.id, username="kehilla").first():
        u = CharityUser(charity_id=thek.id, username="kehilla")
        u.set_password("change_me_now")
        db.session.add(u); db.session.commit()
        print("Seeded charity user: username=kehilla / password=change_me_now")

# ====== LOCAL RUNNER ==========================================================

if __name__ == "__main__":
    app.run(debug=True)

