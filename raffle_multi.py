
# ===== THEME (discreet palette) ==============================================
SITE_NAME = "GetMyNumber"
THEME = {
    "brand_hex": "#0f172a",
    "accent_hex": "#0ea5a4",
    "accent_soft": "#99f6e4",
    "bg_hex": "#f8fafc",
    "text_hex": "#0b1324",
}
import os
THEME["brand_hex"] = os.getenv("THEME_BRAND_HEX", THEME["brand_hex"])
THEME["accent_hex"] = os.getenv("THEME_ACCENT_HEX", THEME["accent_hex"])
MAIN_LOGO_DATA_URI = os.getenv("MAIN_LOGO_DATA_URI")


# ===== DISCREET LOGO (inline SVG) ============================================
def render_logo_svg(title="GetMyNumber"):
    return """
<svg xmlns="http://www.w3.org/2000/svg" aria-label="{title}" viewBox="0 0 180 40" class="h-7 w-auto">
  <defs>
    <style>
      .gm-stroke{stroke:var(--brand);stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round}
      .gm-fill{fill:var(--brand)}
    </style>
  </defs>
  <path class="gm-stroke" d="M10 6h120c3 0 6 3 6 6v0a6 6 0 0 0 0 12v0c0 3-3 6-6 6H10c-3 0-6-3-6-6v0a6 6 0 0 0 0-12v0c0-3 3-6 6-6z"/>
  <path class="gm-stroke" d="M34 16c0-3 2-5 5-5 2 0 3 1 4 2 1-1 2-2 4-2 3 0 5 2 5 5 0 6-9 10-9 10s-9-4-9-10z"/>
  <path class="gm-stroke" d="M43 14c-2 0-3 1-3 3 0 3 3 3 6 3m-6 2h7"/>
  <text x="70" y="26" class="gm-fill" style="font:600 18px system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial">GetMyNumber</text>
</svg>
""".format(title=title)


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



BASE_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{{ title or SITE_NAME }}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    :root{
      --brand: {{ theme.brand_hex }};
      --accent: {{ theme.accent_hex }};
      --accent-soft: {{ theme.accent_soft }};
      --text: {{ theme.text_hex }};
      --bg: {{ theme.bg_hex }};
    }
    .btn { @apply inline-flex items-center justify-center rounded-xl px-4 py-2 text-white bg-[var(--accent)] hover:opacity-90 transition; }
    .btn-ghost { @apply inline-flex items-center justify-center rounded-xl px-4 py-2 text-[var(--brand)] bg-white border border-gray-200 hover:bg-gray-50; }
    .card { @apply bg-white rounded-2xl shadow-sm border border-gray-100; }
    .muted { color: #667085; }
  </style>
</head>
<body class="min-h-screen bg-[var(--bg)] text-[var(--text)]">
  <header class="border-b border-gray-200 bg-white">
    <div class="max-w-6xl mx-auto px-4 py-3 flex items-center gap-3">
      <a href="/" class="flex items-center gap-3">
        {% if main_logo_data_uri %}
          <img src="{{ main_logo_data_uri }}" alt="{{ SITE_NAME }} logo" class="h-7 w-auto">
        {% else %}
          {{ logo|safe }}
        {% endif %}
      </a>
      <nav class="ml-auto flex items-center gap-3 text-sm">
        <a class="btn-ghost" href="/charities">Choose Charity</a>
        <a class="btn-ghost" href="/partner/login">Partner</a>
        <a class="btn-ghost" href="/admin">Admin</a>
      </nav>
    </div>
  </header>

  <main class="max-w-6xl mx-auto px-4 py-8">
    {{ body|safe }}
  </main>

  <footer class="border-t border-gray-200">
    <div class="max-w-6xl mx-auto px-4 py-8 text-xs muted">
      © {{ now.year }} {{ SITE_NAME }} · <a class="underline" href="/how-it-works">How it works</a>
    </div>
  </footer>
</body>
</html>
"""
def render_page(title, body_html, charity=None):
    logo = render_logo_svg(SITE_NAME)
    return render_template_string(
        BASE_HTML,
        title=title,
        body=body_html,
        logo=logo,
        theme=THEME,
        SITE_NAME=SITE_NAME,
        now=datetime.utcnow(),
        main_logo_data_uri=MAIN_LOGO_DATA_URI
    )



@app.route("/", methods=["GET"])
def home():
    body = render_template_string("""
      <section class="grid md:grid-cols-2 gap-8 items-center">
        <div>
          <h1 class="text-3xl md:text-4xl font-semibold leading-tight mb-3 text-[var(--brand)]">
            Raffle for good — simple, transparent, fair.
          </h1>
          <p class="text-[15px] muted mb-6">
            Get a random number. Donate the same amount. Every entry supports your chosen charity.
          </p>
          <div class="flex gap-3">
            <a class="btn" href="/charities">Choose Charity</a>
            <a class="btn-ghost" href="/how-it-works">How it works</a>
          </div>
          <p class="text-xs mt-3 muted">Gift Aid support coming soon • Secure payments</p>
        </div>
        <div class="card p-6">
          <div class="text-sm muted mb-2">Live example</div>
          <div class="flex items-center gap-3">
            <div class="rounded-xl px-4 py-3 bg-[var(--accent-soft)] text-[var(--brand)] font-semibold"># 27</div>
            <div class="text-sm">Your donation would be <b>£27</b></div>
          </div>
          <div class="mt-4"><a class="btn" href="/charities">Get My Number</a></div>
        </div>
      </section>
      <section class="grid sm:grid-cols-3 gap-4 mt-10">
        <div class="card p-5">
          <div class="font-semibold mb-1">1) Get a number</div>
          <p class="text-sm muted">We generate a fair, random number.</p>
        </div>
        <div class="card p-5">
          <div class="font-semibold mb-1">2) Donate that amount</div>
          <p class="text-sm muted">Pay securely with card or the charity’s page.</p>
        </div>
        <div class="card p-5">
          <div class="font-semibold mb-1">3) Support the cause</div>
          <p class="text-sm muted">Your entry helps the charity raise more.</p>
        </div>
      </section>
    """)
    return render_page("Home", body)


@app.route("/charities", methods=["GET"])
def charities():
    rows = Charity.query.order_by(Charity.name.asc()).all()
    body = render_template_string("""
      <h2 class="text-2xl font-semibold mb-5">Choose a charity</h2>
      <div class="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {% for c in rows %}
        <a class="card p-5 hover:shadow-md transition border-gray-100" href="/{{ c.slug }}">
          <div class="flex items-center gap-3">
            {% if c.logo_data_uri %}
              <img src="{{ c.logo_data_uri }}" class="h-8 w-auto" alt="{{ c.name }} logo">
            {% endif %}
            <div class="font-semibold">{{ c.name }}</div>
          </div>
          {% if c.description %}
            <p class="text-sm muted mt-2 line-clamp-2">{{ c.description }}</p>
          {% endif %}
        </a>
        {% endfor %}
      </div>
    """, rows=rows)
    return render_page("Charities", body)


def _calc_totals(charity):
    paid = db.session.query(db.func.coalesce(db.func.sum(Entry.number), 0)).filter_by(charity_id=charity.id, paid=True).scalar()
    goal = getattr(charity, "goal_amount", 1000)
    pct = min(100, round((paid/goal*100) if goal else 0, 1))
    return {"raised": int(paid), "goal": int(goal), "pct": pct}

@app.route("/<slug>", methods=["GET"])
def charity_page(slug):
    charity = Charity.query.filter_by(slug=slug).first_or_404()
    totals = _calc_totals(charity)
    last_entry = Entry.query.filter_by(charity_id=charity.id).order_by(Entry.id.desc()).first()
    body = render_template_string("""
      <div class="grid md:grid-cols-3 gap-6">
        <section class="md:col-span-2 card p-6">
          <div class="flex items-start gap-3">
            {% if charity.logo_data_uri %}
              <img src="{{ charity.logo_data_uri }}" class="h-9 w-auto" alt="{{ charity.name }} logo">
            {% endif %}
            <div>
              <h1 class="text-2xl font-semibold">{{ charity.name }}</h1>
              {% if charity.tagline %}<p class="muted text-sm mt-1">{{ charity.tagline }}</p>{% endif %}
            </div>
          </div>
          <div class="mt-6">
            <form method="post" action="{{ url_for('create_entry', slug=charity.slug) }}">
              <button class="btn w-full md:w-auto">Get my number</button>
            </form>
            {% if last_entry %}
              <p class="text-xs muted mt-2">Last number drawn: <b>{{ last_entry.number }}</b></p>
            {% endif %}
          </div>
          {% if charity.description %}
            <p class="text-sm muted mt-6">{{ charity.description }}</p>
          {% endif %}
        </section>
        <aside class="card p-6">
          <div class="text-sm muted">Raised so far</div>
          <div class="text-3xl font-semibold mb-2">£{{ totals.raised }}</div>
          <div class="h-2 bg-gray-200 rounded-full overflow-hidden">
            <div class="h-2 bg-[var(--accent)]" style="width: {{ totals.pct }}%"></div>
          </div>
          <div class="text-xs muted mt-2">{{ totals.pct }}% of £{{ totals.goal }}</div>
        </aside>
      </div>
    """, charity=charity, totals=totals, last_entry=last_entry)
    return render_page(f"{charity.name} Raffle", body)


@app.route("/<slug>/success", methods=["GET"])
def success(slug):
    charity = Charity.query.filter_by(slug=slug).first_or_404()
    n = session.get("last_num")
    body = render_template_string("""
      <section class="card p-6 text-center">
        <h2 class="text-2xl font-semibold mb-2">You got <span class="text-[var(--accent)]">#{{ n }}</span></h2>
        <p class="muted">Donate <b>£{{ n }}</b> to complete your entry.</p>
        <div class="mt-5 flex gap-3 justify-center">
          {% if session.get('last_entry_id') %}
          <a class="btn" href="{{ url_for('create_checkout', slug=charity.slug, entry_id=session.get('last_entry_id')) }}">Pay by card</a>
          {% endif %}
          <a class="btn-ghost" href="{{ charity.donation_url or url_for('charity_page', slug=charity.slug) }}">Charity page</a>
        </div>
      </section>
    """, n=n, charity=charity)
    return render_page("Success", body)



# --- Safe totals helper (appended) -------------------------------------------
def _calc_totals(charity):
    try:
        q = db.session.query(db.func.coalesce(db.func.sum(Entry.number), 0))
        q = q.filter(Entry.charity_id == charity.id)
        if hasattr(Entry, "paid"):
            q = q.filter(Entry.paid.is_(True))
        elif hasattr(Entry, "status"):
            q = q.filter(Entry.status == "paid")
        paid_sum = int(q.scalar() or 0)
    except Exception:
        paid_sum = 0
    goal = int(getattr(charity, "goal_amount", 1000) or 1000)
    try:
        pct = min(100, round((paid_sum / goal * 100) if goal else 0, 1))
    except Exception:
        pct = 0
    return {"raised": paid_sum, "goal": goal, "pct": pct}


@app.route("/sw.js", methods=["GET"])
def sw_js():
    js = "self.addEventListener('install', e=>self.skipWaiting()); self.addEventListener('fetch', ()=>{});"
    from flask import Response
    return Response(js, mimetype="application/javascript")
