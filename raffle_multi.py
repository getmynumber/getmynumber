from flask import Flask, render_template_string, request, redirect, url_for, session, flash, abort
import os, random
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint, inspect, text
from datetime import datetime, timedelta
import csv, io
from flask import send_file

app = Flask(__name__)
# expire admin sessions after 30 minutes
app.permanent_session_lifetime = timedelta(minutes=30)
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
    paid = db.Column(db.Boolean, nullable=False, default=False)
    paid_at = db.Column(db.DateTime, nullable=True)	

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
    # First render the inner body template with context...
    inner = render_template_string(body, **ctx)
    # ...then render the outer layout and inject the result.
    return render_template_string(LAYOUT, body=inner, **ctx)

# ---- Routes ----
@app.route("/")
def home():
    charities = Charity.query.order_by(Charity.name.asc()).all()
    body = """
    <h2>Pick a charity</h2>
    <p class="muted">Choose a raffle page:</p>
    <div class="grid">
      {% for c in charities %}
        <a class="pill" href="{{ url_for('charity_page', slug=c.slug) }}"><strong>{{ c.name }}</strong></a>
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

@app.route("/admin/charity/<slug>/entries")
def admin_charity_entries(slug):
    # require login
    if not session.get("admin_ok"):
        return redirect(url_for("admin_charities"))

    flt = request.args.get("filter")
    q = Entry.query.filter_by(charity_id=charity.id)
    if flt == "paid":
    	q = q.filter(Entry.paid.is_(True))
    elif flt == "unpaid":
    	q = q.filter(Entry.paid.is_(False))
    entries = q.order_by(Entry.id.desc()).all()


    body = """
    <h2>Entries — {{ charity.name }}</h2>
    <p class="muted">Total: {{ entries|length }}</p>
    <p><a class="pill" href="{{ url_for('admin_charity_entries_csv', slug=charity.slug) }}">Download CSV</a>
       <a class="pill" href="{{ url_for('admin_charities') }}">← Back</a></p>
    <p>
  	<a class="pill" href="{{ url_for('admin_charity_entries', slug=charity.slug) }}">All</a>
  	<a class="pill" href="{{ url_for('admin_charity_entries', slug=charity.slug, filter='unpaid') }}">Unpaid</a>
  	<a class="pill" href="{{ url_for('admin_charity_entries', slug=charity.slug, filter='paid') }}">Paid</a>
  	<a class="pill" href="{{ url_for('admin_charity_entries_csv', slug=charity.slug) }}">Download CSV</a>
  	<a class="pill" href="{{ url_for('admin_charities') }}">← Back</a>
    </p>
    <table>
  <thead>
    <tr>
      <th>ID</th><th>Name</th><th>Email</th><th>Phone</th><th>Number</th>
      <th>Created</th><th>Paid</th><th>Actions</th>
    </tr>
  </thead>
  <tbody>
    {% for e in entries %}
      <tr>
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
    """
    return render(body, charity=charity, entries=entries, title=f"Entries – {charity.name}")


@app.route("/admin/charity/<slug>/entries.csv")
def admin_charity_entries_csv(slug):
    # require login
    if not session.get("admin_ok"):
        return redirect(url_for("admin_charities"))

    charity = Charity.query.filter_by(slug=slug).first_or_404()
    entries = (Entry.query
               .filter_by(charity_id=charity.id)
               .order_by(Entry.id.asc())
               .all())

    output = io.StringIO()
    w = csv.writer(output)
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
    return send_file(
        io.BytesIO(data),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"{charity.slug}_entries.csv"
    )

# ---- Simple “Add Charity” page (password from env) ----
@app.route("/admin/charities", methods=["GET","POST"])
def admin_charities():
    admin_user = os.getenv("ADMIN_USERNAME", "admin")
    admin_pw   = os.getenv("ADMIN_PASSWORD", "")
    ok = session.get("admin_ok", False)
    last_login = session.get("admin_login_time")
    msg = None

    # Enforce timeout (30 mins)
    if last_login:
        try:
            if datetime.utcnow() - datetime.fromisoformat(last_login) > app.permanent_session_lifetime:
                session.clear()
                ok = False
                flash("Session expired. Please log in again.")
        except Exception:
            session.clear()
            ok = False

    if request.method == "POST":
        if not ok:
            # LOGIN SUBMIT
            if (request.form.get("username") == admin_user and
                request.form.get("password") == admin_pw and admin_pw):
                session.permanent = True
                session["admin_ok"] = True
                session["admin_login_time"] = datetime.utcnow().isoformat()
                ok = True
                flash("Logged in successfully.")
            else:
                msg = "Invalid username or password."
        else:
            # ADD / SAVE CHARITY SUBMIT
            slug = request.form.get("slug","").strip().lower()
            name = request.form.get("name","").strip()
            url  = request.form.get("donation_url","").strip()
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

    # HTML
    body = """
    <h2>Manage Charities</h2>
    {% if msg %}<div style="margin:6px 0;color:#ffd29f">{{ msg }}</div>{% endif %}

    {% if not ok %}
      <!-- LOGIN FORM (this is Step 3) -->
      <form method="post">
        <label>Username <input type="text" name="username" required></label>
        <label>Password <input type="password" name="password" required></label>
        <div style="margin-top:8px"><button>Enter</button></div>
      </form>

    {% else %}
      <div style="margin-bottom:10px">
        <a class="pill" href="{{ url_for('admin_logout') }}">Log out</a>
      </div>

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
              <td>
 		<a class="pill" href="{{ url_for('charity_page', slug=c.slug) }}">Open</a>
  		<a class="pill" href="{{ url_for('edit_charity', slug=c.slug) }}">Edit</a>
  		<a class="pill" href="{{ url_for('admin_charity_entries', slug=c.slug) }}">Entries</a>
	      </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% endif %}
    """
    return render(body, ok=ok, msg=msg, charities=charities, remaining=remaining, title="Manage Charities")

@app.route("/admin/charity/<slug>", methods=["GET", "POST"])
def edit_charity(slug):
    # Require login
    if not session.get("admin_ok"):
        return redirect(url_for("admin_charities"))

    charity = Charity.query.filter_by(slug=slug).first_or_404()
    msg = None

    if request.method == "POST":
        charity.name = request.form.get("name", charity.name).strip()
        charity.donation_url = request.form.get("donation_url", charity.donation_url).strip()
        try:
            charity.max_number = int(request.form.get("max_number", charity.max_number))
        except ValueError:
            msg = "Invalid number format."
        else:
            db.session.commit()
            msg = "Charity updated successfully."

    body = """
    <h2>Edit Charity</h2>
    {% if msg %}<div style="margin:6px 0;color:#ffd29f">{{ msg }}</div>{% endif %}
    <form method="post">
      <label>Name <input type="text" name="name" value="{{ charity.name }}" required></label>
      <label>Donation URL <input type="url" name="donation_url" value="{{ charity.donation_url }}" required></label>
      <label>Max number <input type="number" name="max_number" value="{{ charity.max_number }}" min="1"></label>
      <div style="margin-top:8px"><button>Save Changes</button></div>
    </form>
    <p><a href="{{ url_for('admin_charities') }}" class="pill">← Back to Manage Charities</a></p>
    """
    return render(body, charity=charity, msg=msg, title=f"Edit {charity.name}")

@app.route("/admin/entry/<int:entry_id>/toggle-paid", methods=["POST"])
def toggle_paid(entry_id):
    if not session.get("admin_ok"):
        return redirect(url_for("admin_charities"))
    e = Entry.query.get_or_404(entry_id)
    e.paid = not e.paid
    e.paid_at = datetime.utcnow() if e.paid else None
    db.session.commit()
    next_url = request.args.get("next") or url_for("admin_charity_entries", slug=e.charity.slug)
    return redirect(next_url)

@app.route("/admin/migrate")
def admin_migrate():
    if not session.get("admin_ok"):
        return redirect(url_for("admin_charities"))
    try:
        with db.engine.begin() as conn:
            # add columns if they don't exist (SQLite ignores DEFAULT for existing rows)
            conn.exec_driver_sql("ALTER TABLE entry ADD COLUMN paid BOOLEAN DEFAULT 0")
    except Exception as e:
        print("paid column:", e)
    try:
        with db.engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE entry ADD COLUMN paid_at DATETIME")
    except Exception as e:
        print("paid_at column:", e)
    return "Migration attempted. Go back to Entries and refresh."

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

# --- lightweight auto-migration for new columns in existing SQLite DBs ---
with app.app_context():
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


# ---- Local dev runner ----
if __name__ == "__main__":
    app.run(debug=True)

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for("admin_charities"))

