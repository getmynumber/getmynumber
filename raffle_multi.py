from flask import Flask, render_template_string, request, redirect, url_for, session, flash, abort
import os, random
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "devkey")

# ---- Database config (Postgres if DATABASE_URL set, else SQLite in ./instance) ----
DB_URL = os.getenv("DATABASE_URL")

if DB_URL:
    # Render/Heroku may give postgres://; SQLAlchemy expects postgresql://
    DB_URL = DB_URL.replace("postgres://", "postgresql://")
    app.config["SQLALCHEMY_DATABASE_URI"] = DB_URL
else:
    # Ensure instance/ exists and use a file we control
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    INSTANCE_PATH = os.path.join(BASE_DIR, "instance")
    os.makedirs(INSTANCE_PATH, exist_ok=True)  # <-- create folder if missing
    sqlite_path = os.path.join(INSTANCE_PATH, "raffle.db")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{sqlite_path}"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# ---- Models ----
class Charity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), unique=True, nullable=False)     # appears in URL
    name = db.Column(db.String(200), nullable=False)
    donation_url = db.Column(db.String(500), nullable=False)
    max_number = db.Column(db.Integer, nullable=False, default=500)   # 1..X per charity

class Entry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    charity_id = db.Column(db.Integer, db.ForeignKey("charity.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(40))
    number = db.Column(db.Integer, nullable=False)                    # unique per charity
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("charity_id", "number", name="uq_charity_number"),)

    charity = db.relationship("Charity", backref="entries")

# ---- Helpers ----
def get_charity_or_404(slug: str) -> Charity:
    c = Charity.query.filter_by(slug=slug.lower().strip()).first()
    if not c:
        abort(404)
    return c

def available_numbers(c: Charity):
    taken = {n for (n,) in db.session.query(Entry.number).filter(Entry.charity_id == c.id).all()}
    return [i for i in range(1, c.max_number + 1) if i not in taken]

def assign_number(c: Charity):
    avail = available_numbers(c)
    if not avail:
        return None
    return random.choice(avail)

# ---- Templates (inline for single-file simplicity) ----
LAYOUT = """
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title or "Get My Number" }}</title>
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;background:#0c0f14;color:#e8eef5}
  .wrap{max-width:900px;margin:0 auto;padding:24px}
  .card{background:#141a22;border:1px solid #1e2937;border-radius:16px;padding:24px;box-shadow:0 10px 30px rgba(0,0,0,.3)}
  a{color:#9fd0ff;text-decoration:none}
  input,button{font:inherit}
  input[type=text],input[type=email],input[type=tel],input[type=number],input[type=url],input[type=password]{
    width:100%;padding:12px;border-radius:10px;border:1px solid #2b3a4d;background:#0c121a;color:#e8eef5}
  button{background:#4f8cff;border:none;color:#fff;padding:12px 16px;border-radius:10px;cursor:pointer;font-weight:600}
  .muted{color:#9fb0c3}
  .pill{display:inline-block;border:1px solid #2b3a4d;border-radius:999px;padding:6px 10px;color:#cde3ff}
  table{width:100%;border-collapse:collapse;margin-top:12px}
  th,td{border-bottom:1px solid #1f2a3a;padding:8px;text-align:left}
  .grid{display:grid;gap:8px}
</style>
</head><body>
  <div class="wrap">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <div><a href="{{ url_for('home') }}" style="color:#e8eef5;text-decoration:none"><strong>Get My Number</strong></a></div>
      <div><a href="{{ url_for('admin_charities') }}">Add Charity</a></div>
    </div>
    <div class="card">
      {% with messages = get_flashed_messages() %}
        {% if messages %}{% for m in messages %}<div style="margin-bottom:8px;color:#ffd29f">{{ m }}</div>{% endfor %}{% endif %}
      {% endwith %}
      {{ body|safe }}
    </div>
  </div>
</body></html>
"""

def render(body, **ctx):
    return render_template_string(LAYOUT, body=body, **ctx)

# ---- Routes ----
@app.route("/")
def home():
    charities = Charity.query.order_by(Charity.name.asc()).all()
    body = """
    <h2>Pick a charity</h2>
    <p class="muted">Choose a raffle page:</p>
    <div class="grid">
      {% for c in charities %}
        <a class="pill" href="{{ url_for('charity_page', slug=c.slug) }}"><strong>{{ c.name }}</strong> — /{{ c.slug }}</a>
      {% else %}
        <p class="muted">No charities yet. Use <a href="{{ url_for('admin_charities') }}">Add Charity</a>.</p>
      {% endfor %}
    </div>
    """
    return render(body, charities=charities, title="Get My Number")

@app.route("/<slug>", methods=["GET", "POST"])
def charity_page(slug):
    charity = get_charity_or_404(slug)
    if request.method == "POST":
        # minimal validation
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
                    db.session.add(entry)
                    db.session.commit()
                    session["last_num"] = num
                    session["last_name"] = name
                    session["last_slug"] = charity.slug
                    return redirect(url_for("success", slug=charity.slug))
                except Exception:
                    db.session.rollback()
                    flash("That number was just taken—please try again.")
    remaining = len(available_numbers(charity))
    body = """
    <h2>{{ charity.name }} — Raffle</h2>
    <p class="muted">Numbers are unique between 1 and {{ charity.max_number }}. Remaining: <span class="pill">{{ remaining }}</span></p>
    <form method="post">
      <label>Name <input type="text" name="name" required></label>
      <label>Email <input type="email" name="email" required></label>
      <label>Phone <input type="tel" name="phone" placeholder="+44 7xxx xxxxxx"></label>
      <div style="margin-top:12px"><button type="submit">Get my number</button></div>
    </form>
    <p class="muted" style="margin-top:10px">After you get your number, we’ll send you to the donation page to complete the gift.</p>
    """
    return render(body, charity=charity, remaining=remaining, title=charity.name)

@app.route("/<slug>/success")
def success(slug):
    charity = get_charity_or_404(slug)
    if session.get("last_slug") != charity.slug or "last_num" not in session:
        return redirect(url_for("charity_page", slug=charity.slug))
    num = session.get("last_num")
    name = session.get("last_name", "Friend")
    body = """
    <h2>Thank you{{ ", %s" % name if name else "" }}!</h2>
    <p>Your raffle number for <strong>{{ charity.name }}</strong> is <strong>#{{ num }}</strong>.
       Please donate <strong>£{{ num }}</strong> to complete your entry.</p>
    <div class="grid">
      <a class="pill" href="{{ charity.donation_url }}" target="_blank" rel="noopener">Go to Donation Page</a>
      <button onclick="navigator.clipboard.writeText('{{ num }}').then(()=>alert('Amount copied!'))">Copy amount ({{ num }})</button>
    </div>
    <p class="muted">On the donation page, enter the amount shown above.</p>
    """
    return render(body, charity=charity, num=num, name=name, title=charity.name)

# ---- Simple “Add Charity” page (password from env) ----
@app.route("/admin/charities", methods=["GET","POST"])
def admin_charities():
    admin_pw = os.getenv("ADMIN_PASSWORD", "")
    ok = session.get("admin_ok", False)
    msg = None

    if request.method == "POST":
        if not ok:
            # First submit should carry the password
            if request.form.get("password","") == admin_pw and admin_pw:
                session["admin_ok"] = True
                ok = True
            else:
                msg = "Invalid password."
        elif ok:
            # Add a charity
            slug = request.form.get("slug","").strip().lower()
            name = request.form.get("name","").strip()
            url = request.form.get("donation_url","").strip()
            maxn = int(request.form.get("max_number","500") or 500)
            if not slug or not name or not url:
                msg = "All fields are required."
            elif Charity.query.filter_by(slug=slug).first():
                msg = "Slug already exists."
            else:
                c = Charity(slug=slug, name=name, donation_url=url, max_number=maxn)
                db.session.add(c)
                db.session.commit()
                msg = f"Saved. Public page: /{slug}"

    charities = Charity.query.order_by(Charity.name.asc()).all()
    remaining = {c.id: len(available_numbers(c)) for c in charities}
    body = """
    <h2>Manage Charities</h2>
    {% if msg %}<div style="margin:6px 0;color:#ffd29f">{{ msg }}</div>{% endif %}
    {% if not ok %}
      <form method="post">
        <label>Admin password <input type="password" name="password" required></label>
        <div style="margin-top:8px"><button>Enter</button></div>
      </form>
      <p class="muted">Set ADMIN_PASSWORD in your environment (Render → Environment).</p>
    {% else %}
      <form method="post" style="margin-bottom:12px">
        <label>Slug <input type="text" name="slug" placeholder="thekehilla" required></label>
        <label>Name <input type="text" name="name" placeholder="The Kehilla" required></label>
        <label>Donation URL <input type="url" name="donation_url" placeholder="https://www.charityextra.com/charity/kehilla" required></label>
        <label>Max number <input type="number" name="max_number" value="500" min="1"></label>
        <div style="margin-top:8px"><button>Add / Save</button></div>
      </form>
      <table>
        <thead><tr><th>Slug</th><th>Name</th><th>Max</th><th>Remaining</th><th>Open</th></tr></thead>
        <tbody>
          {% for c in charities %}
            <tr>
              <td>{{ c.slug }}</td>
              <td>{{ c.name }}</td>
              <td>{{ c.max_number }}</td>
              <td>{{ remaining[c.id] }}</td>
              <td><a class="pill" href="{{ url_for('charity_page', slug=c.slug) }}">Open</a></td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% endif %}
    """
    return render(body, ok=ok, msg=msg, charities=charities, remaining=remaining, title="Manage Charities")

# ---- Auto-init DB + seed The Kehilla on first boot ----
with app.app_context():
    db.create_all()
    if not Charity.query.filter_by(slug="thekehilla").first():
        db.session.add(Charity(
            slug="thekehilla",
            name="The Kehilla",
            donation_url="https://www.charityextra.com/charity/kehilla",
            max_number=500
        ))
        db.session.commit()
        print("Seeded default charity: /thekehilla")

# ---- Local dev runner ----
if __name__ == "__main__":
    app.run(debug=True)
