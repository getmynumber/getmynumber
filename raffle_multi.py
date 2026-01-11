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
    url_for, session, flash, abort, send_file, jsonify
)
import os, random, csv, io, json
from datetime import datetime, timedelta
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint, inspect, text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash
from urllib.parse import urlparse
from markupsafe import Markup

import base64

import stripe

# Stripe config
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

POSTAL_ENTRY_ADDRESS = "PO Box 12345, London, United Kingdom (replace this later)"

# Amount to temporarily hold on the card (in pence) – e.g. 1000 = £10
HOLD_AMOUNT_PENCE = 20000



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

# --- Hotlink protection (lightweight) ---
# Blocks other domains from embedding your image/static files directly.
# Note: This only affects files served by YOUR app (e.g. /static/... or *.png endpoints).
ALLOWED_HOTLINK_HOSTS = set(
    h.strip().lower()
    for h in os.getenv("HOTLINK_ALLOWED_HOSTS", "").split(",")
    if h.strip()
)

def _same_site_referer_ok() -> bool:
    ref = request.headers.get("Referer", "")
    if not ref:
        # If no Referer, allow (some browsers/extensions strip it)
        return True
    try:
        ref_host = (urlparse(ref).netloc or "").lower()
        host = (request.host or "").lower()
        # allow same host or explicitly allowed hosts
        return (ref_host == host) or (ref_host in ALLOWED_HOTLINK_HOSTS)
    except Exception:
        return True

@app.before_request
def block_hotlinking():
    path = (request.path or "").lower()

    # Only apply to likely "asset" paths
    is_asset = (
        path.startswith("/static/")
        or path.endswith(".png") or path.endswith(".jpg") or path.endswith(".jpeg")
        or path.endswith(".gif") or path.endswith(".webp") or path.endswith(".svg")
        or path.endswith(".ico")
    )

    if is_asset and not _same_site_referer_ok():
        # Return 403 to stop hotlinking
        return ("Hotlinking not allowed.", 403)

# --- Security headers (CSP, etc.) ---
@app.after_request
def add_security_headers(resp):
    # IMPORTANT: Because your app uses inline <style> / <script> inside LAYOUT,
    # we must allow 'unsafe-inline'. If you later move scripts/styles to files,
    # you can tighten this significantly.

    csp = [
        "default-src 'self'",
        # Allow Stripe + DMCA scripts
        "script-src 'self' 'unsafe-inline' https://js.stripe.com https://images.dmca.com",
        # Allow inline CSS (your LAYOUT uses inline styles) + optional Google fonts if you ever add later
        "style-src 'self' 'unsafe-inline'",
        # Images: self + data: (for embedded images) + DMCA badge host
        "img-src 'self' data: https://images.dmca.com",
        # Stripe API calls
        "connect-src 'self' https://api.stripe.com",
        # Stripe may open in frame/popup flows depending on product; safe to allow Stripe frames
        "frame-src 'self' https://js.stripe.com https://hooks.stripe.com",
        # Prevent your site being iframed by others
        "frame-ancestors 'self'",
        # Lock down base-uri
        "base-uri 'self'",
    ]

    resp.headers["Content-Security-Policy"] = "; ".join(csp)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    # Clickjacking protection (extra)
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"

    return resp

def _load_text_file(path: str) -> str:
	try:
		with open(path, "r", encoding="utf-8") as f:
			return (f.read() or "").strip()
	except Exception:
		return ""

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

KEHILLA_LOGO_DATA_URI = _load_text_file(
	os.path.join(BASE_DIR, "kehilla_logo_data_uri.txt")
)

# ====== MODELS ================================================================

class Charity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), unique=True, nullable=False)        # URL slug
    name = db.Column(db.String(200), nullable=False)
    donation_url = db.Column(db.String(500), nullable=True)
    max_number = db.Column(db.Integer, nullable=False, default=500)     # 1..max
    draw_at = db.Column(db.DateTime, nullable=True)   # raffle draw date/time (optional)
    is_live = db.Column(db.Boolean, nullable=False, default=True)  # campaign on/off
    logo_data = db.Column(db.Text, nullable=True)  # data URI for uploaded logo
    poster_data = db.Column(db.Text, nullable=True)  # data URI for optional campaign poster
    tile_about = db.Column(db.Text, nullable=True)   # short 1–2 sentence “about” for homepage tile
    prizes_json = db.Column(db.Text, nullable=True)  # JSON array of prizes (strings)
    campaign_status = db.Column(db.String(20), nullable=False, default="live")    
    hold_amount_pence = db.Column(db.Integer, nullable=False, default=20000)
    is_sold_out = db.Column(db.Boolean, nullable=False, default=False)
    is_coming_soon = db.Column(db.Boolean, nullable=False, default=False)
    free_entry_enabled = db.Column(db.Boolean, nullable=False, default=False)
    postal_entry_enabled = db.Column(db.Boolean, nullable=False, default=False)
    optional_donation_enabled = db.Column(db.Boolean, nullable=False, default=False)
    # ===== Skill-based entry (optional) =====
    skill_enabled = db.Column(db.Boolean, nullable=False, default=False)
    skill_question = db.Column(db.Text, nullable=True)
    skill_image_data = db.Column(db.Text, nullable=True)
    skill_answers_json = db.Column(db.Text, nullable=True)
    skill_correct_answer = db.Column(db.Text, nullable=True)
    # How many options to show on the frontend (default 4)
    skill_display_count = db.Column(db.Integer, nullable=False, default=4)
    # ===== Stripe Connect (per-charity payouts) =====
    stripe_account_id = db.Column(db.String(64), nullable=True)  # e.g. acct_123...

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
    payment_intent_id = db.Column(db.String(255))   # ← NEW
    stripe_account_id = db.Column(db.String(64), nullable=True)  # snapshot of charity acct at time of hold
    hold_amount_pence = db.Column(db.Integer, nullable=True)  # authorised hold amount for this entry

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
<meta name="color-scheme" content="light dark">
<style>
  :root{
    --bg:#f3fafc;
    --bg-soft:#e4f3f7;
    --card:#ffffff;
    --card-2:#f8feff;
    --text:#12313d;
    --muted:#6a8893;
    --brand:#00b8a9;
    --brand-2:#27c6d6;
    --ok:#1ea97a;
    --warn:#f5a623;
    --danger:#e94f37;
    --border:#cfe3ea;
    --shadow:0 18px 45px rgba(3,46,66,0.16);
    --radius:18px;
    --radius-sm:12px;
    --transition-fast:150ms ease-out;
  }

  *{box-sizing:border-box;margin:0;padding:0}

  html,body{
    min-height:100%;
    font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    background:
      radial-gradient(900px 700px at 0% 0%, #e0f7fa 0, transparent 60%),
      radial-gradient(900px 700px at 100% 0%, #d0f0f6 0, transparent 60%),
      var(--bg);
    color:var(--text);
  }

  a{
    color:var(--brand);
    text-decoration:none;
    transition:color var(--transition-fast), opacity var(--transition-fast);
  }
  a:hover{color:var(--brand-2);}

  .wrap{
    max-width:1100px;
    margin:0 auto;
    padding:0 18px 32px;
  }

  .nav{
    position:static;
    top:auto;
    z-index:20;
    display:flex;
    align-items:center;
    justify-content:space-between;

    /* FULL WIDTH BAR */
    width:100vw;
    margin-left:calc(50% - 50vw);
    margin-right:calc(50% - 50vw);

    margin-top:0;

    margin-bottom:20px;
    padding:18px 32px;
    border-radius:0;

    background:#ffffff;
    border:0;
    border-bottom:1px solid var(--border);
    box-shadow:0 16px 35px rgba(3,46,66,0.15);
    backdrop-filter:blur(12px);
  }

  .flow-progress {
    margin: -10px 0 14px;
    padding: 0 12px;
  }

  .flow-progress-track {
    height: 10px;
    border-radius: 999px;
    background: rgba(207,227,234,0.75);
    overflow: hidden;
    border: 1px solid rgba(207,227,234,0.9);
  }
  .flow-progress-fill {
    height: 100%;
    width: 0%;
    background: linear-gradient(90deg, var(--brand), var(--brand-2));
  }

/* Narrow layout: keep the nav full width, but make progress + main card tighter */
.layout-narrow .flow-progress,
.layout-narrow .main-card{
  padding:22px 18px 0;   /* bottom padding removed so the soft-panel becomes the true bottom section */
  text-align:center;
}

/* Make entry bars span almost full width of the main card */
.layout-narrow .card form input[type="text"],
.layout-narrow .card form input[type="email"],
.layout-narrow .card form input[type="tel"]{
  width:100%;
  box-sizing:border-box;
}

/* Skill page: compact side-by-side actions */
.skill-actions{
  display:flex;
  justify-content:center;
  gap:12px;
  margin-top:14px;
  flex-wrap:wrap; /* safe on small screens */
}

/* Opt-out of full-width buttons for skill page */
.skill-actions .btn-skill{
  width:auto;              /* override .layout-narrow .btn{ width:100% } */
  padding:8px 18px;
  font-size:14px;
  border-radius:14px;
}

/* Progress bar should match card width cleanly */
.layout-narrow .flow-progress{
  padding:0;
}

/* Make the main card feel tighter and more "form-card" premium */
.layout-narrow .main-card{
  padding:22px 18px;
  text-align:center;
}

/* Make form elements align like the Stitch card */
.layout-narrow .main-card form{
  margin-top:12px;
}
.layout-narrow .main-card label{
  text-align:left;
}
.layout-narrow .main-card input{
  text-align:left;
}

/* Force entry form labels + inputs left-aligned (Name / Email / Phone) */
.layout-narrow .card form label{
  text-align:left;
  align-items:flex-start;
}

.layout-narrow .card form input::placeholder{
  text-align:left;
}

/* Button same width as inputs */
.layout-narrow .main-card .btn{
  width:100%;
  justify-content:center;
}

/* Tickets Claimed = true bottom of the main card */
.soft-panel{
  margin-top:16px;

  /* stretch to card edges */
  margin-left:-18px;
  margin-right:-18px;

  /* pull to absolute bottom of card */
  margin-bottom:-22px;

  padding:16px 18px 20px;

  /* inherit card bottom rounding */
  border-radius:0 0 22px 22px;

  background: var(--card-2);

  /* clean divider from form */
  border:0;
  border-top:1px solid rgba(207,227,234,0.9);
}

  .banner-remaining{
  background: rgba(0,0,0,0.06);
  border: 1px solid rgba(0,0,0,0.08);
}

.flow-progress{
  margin: 10px 0 14px 0;
}

.flow-progress-meta{
  display:flex;
  justify-content:space-between;
  align-items:center;
  gap:12px;
  margin-bottom:8px;
  font-size:12px;
  font-weight:700;
  opacity:0.9;
}

.flow-progress-left,
.flow-progress-right{
  white-space:nowrap;
}

.flow-progress-track{
  height:10px;
  border-radius:999px;
  background: rgba(255,255,255,0.10);
  overflow:hidden;
}

.flow-progress-fill{
  height:100%;
  border-radius:999px;
}

  .section-title{
    text-align:center;
    margin:6px 0 8px;
  }
  .section-subtitle{
    text-align:center;
    max-width:780px;
    margin:0 auto 16px;
  }

  .tiles-grid{
    display:grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 320px));
    justify-content:center;          /* keeps tiles centered as you add more */
    gap:14px;
    margin-top:14px;
  }

  .cause-tile{
    position:relative;
    background:linear-gradient(180deg, rgba(255,255,255,0.95), rgba(248,254,255,0.95));
    border:1px solid rgba(207,227,234,0.95);
    border-radius:18px;
    box-shadow:0 16px 40px rgba(3,46,66,0.10);
    overflow:hidden;
    padding:16px;
    display:flex;
    flex-direction:column;
    min-height: 360px;
  }

  .cause-top{
    display:flex;
    align-items:center;
    gap:12px;
    margin-bottom:10px;
  }

  .cause-img{
    width:54px;height:54px;
    border-radius:14px;
    border:1px solid rgba(207,227,234,0.95);
    background:rgba(228,243,247,0.8);
    display:flex;align-items:center;justify-content:center;
    overflow:hidden;
    flex:0 0 auto;
  }
  .cause-img img{width:100%;height:100%;object-fit:cover;display:block;}

  .cause-name{
    font-weight:900;
    font-size:15px;
    line-height:1.2;
  }

  .cause-prizes{
    font-size:13px;
    margin-top:6px;
    line-height:1.35;
  }

  .cause-about{
    margin-top:10px;
    font-size:13px;
    line-height:1.45;
    color:var(--muted);
    flex: 1 1 auto;
  }

  .cause-poster{
    margin-top:10px;
    border-radius:16px;
    overflow:hidden;
    border:1px solid rgba(207,227,234,0.95);
    background:rgba(228,243,247,0.55);
  }

  .cause-poster img{
    width:100%;
    height:110px;          /* keeps it compact */
    object-fit:contain;    /* NO CROPPING */
    display:block;
    background:#fff;  
  }

  .tile-progress{
    margin-top:12px;
  }
  .progress-track{
    height:8px;
    border-radius:999px;
    background:rgba(207,227,234,0.75);
    overflow:hidden;
    border:1px solid rgba(207,227,234,0.9);
  }
  .progress-fill{
    height:100%;
    width:0%;
    background:linear-gradient(90deg, var(--brand), var(--brand-2));
  }

  .tile-cta{
    margin-top:12px;
  }
  .tile-cta .btn{
    width:100%;
    justify-content:center;
  }
  .btn[disabled]{
    opacity:0.55;
    cursor:not-allowed;
  }

  .ribbon{
    position:absolute;
    top:12px; right:12px;
    padding:6px 10px;
    font-size:12px;
    font-weight:800;
    border-radius:999px;
    background:rgba(18,49,61,0.92);
    color:#fff;
    border:1px solid rgba(255,255,255,0.15);
  }

@media (max-width:600px){
  .nav{
    padding:16px 16px;
    border-radius:0;
  }
}

  .logo{
    display:flex;
    gap:14px;
    align-items:center;
    color:var(--text);
  }

 .logo-badge{
    width:44px;
    height:44px;
    border-radius:14px;
    display:grid;
    place-items:center;

    background:linear-gradient(135deg,var(--brand),var(--brand-2));
    color:white;

    font-size:22px;
    position:relative;
    top:-1px;

    /* Clean teal glow */
    box-shadow:
      0 8px 18px rgba(0,184,169,0.35),
      0 0 10px rgba(0,184,169,0.45);

    /* No animation, no movement */
    transition:box-shadow .25s ease;
   }


  .logo strong{
    letter-spacing:0.06em;
    font-size:20px;
  }

  .nav-links{
    display:flex;
    gap:6px;
    align-items:center;
  }

  .nav-links a{
    color:var(--muted);
    padding:10px 16px;
    border-radius:999px;
    border:1px solid transparent;
    font-size:15px;
    white-space:nowrap;
  }

  .nav-links a:hover{
    border-color:var(--border);
    color:var(--text);
    background:rgba(0,184,169,0.06);
  }

  .card{
    margin-top:10px;

    background:#ffffff;

    border:1px solid var(--border);
    border-radius:var(--radius);
    padding:24px 20px 22px;
    box-shadow:var(--shadow);
  }


  .hero{
    display:flex;
    flex-direction:column;
    gap:10px;
  }

  .hero h1{
    margin:0;
    font-size:26px;
    letter-spacing:0.01em;
  }

  .hero p{
    margin:0;
    color:var(--muted);
    font-size:14px;
  }

  h2{
    font-size:20px;
    margin-bottom:6px;
  }

  .stack{
    display:flex;
    flex-wrap:wrap;
    gap:8px;
  }

  .row{
    display:flex;
    flex-wrap:wrap;
    gap:10px;
    align-items:center;
  }

  .pill,
  .btn{
    display:inline-flex;
    align-items:center;
    justify-content:center;
    gap:8px;
    padding:8px 13px;
    border-radius:999px;
    border:1px solid var(--border);
    color:var(--text);
    background:#f8fafc;   /* very light neutral grey */
    cursor:pointer;
    font-size:13px;
    line-height:1.1;
    transition:
      background var(--transition-fast),
      border-color var(--transition-fast),
      box-shadow var(--transition-fast),
      transform var(--transition-fast),
      color var(--transition-fast),
      opacity var(--transition-fast);
  }

  .pill:hover{
    background:#e5f6f7;
    border-color:#9ad7e0;
    transform:translateY(-1px);
    box-shadow:0 10px 24px rgba(3,46,66,0.16);
  }

  .btn{
    background:linear-gradient(135deg,var(--brand),var(--brand-2));
    border:none;
    color:#ffffff;
    font-weight:700;
    padding-inline:16px;
    text-decoration:none; /* important for <a class="btn"> */
  }

  .btn:hover{
    box-shadow:0 10px 24px rgba(0,184,169,0.35);
    transform:translateY(-1px);
  }

  .btn.secondary{
    background:transparent;
    border:1px solid var(--border);
    color:var(--brand);
  }

  button[disabled],
  button:disabled{
    opacity:0.7;
    cursor:default;
    box-shadow:none;
    transform:none;
  }

  form{
    display:flex;
    flex-direction:column;
    gap:8px;
    margin-top:10px;
  }

  label{
    font-size:13px;
    color:var(--muted);
    display:flex;
    flex-direction:column;
    gap:4px;
  }

  input,select,textarea{
    font:inherit;
    padding:11px 14px;
    border-radius:12px;
    border:1px solid var(--border);
    background:#f8fafc;   /* lighter neutral grey */
    color:var(--text);
    outline:none;
    transition:border-color var(--transition-fast), box-shadow var(--transition-fast), background var(--transition-fast);
  }

  textarea{
    border-radius:var(--radius-sm);
    min-height:80px;
    resize:vertical;
  }

  input:focus,select:focus,textarea:focus{
    border-color:var(--brand);
    box-shadow:0 0 0 1px rgba(0,184,169,0.25);
    background:#ffffff;
  }

  /* Outline pill buttons (used on final confirmation page) */
  .btn.outline{
    background: transparent;
    border: 1.5px solid var(--brand);
    color: var(--brand);
    box-shadow: none;
  }

  .btn.outline:hover{
    background: rgba(0,184,169,0.08);
    box-shadow: none;
  }

  .muted{color:var(--muted);}
  .sep{height:10px;}

  table{
    width:100%;
    border-collapse:collapse;
    margin-top:14px;
    border-radius:var(--radius-sm);
    overflow:hidden;
    font-size:13px;
    background:#ffffff;
    border:1px solid var(--border);
  }

  thead th{
    background:#e8f5f7;
    color:var(--muted);
    font-weight:600;
  }

  th,td{
    padding:9px 10px;
    border-bottom:1px solid #e0edf2;
    text-align:left;
    vertical-align:top;
  }

  tbody tr:nth-child(even) td{
    background:#f7fcfd;
  }

  tbody tr:hover td{
    background:#eaf7f8;
  }

  .badge{
    display:inline-flex;
    align-items:center;
    padding:4px 9px;
    border-radius:12px;
    border:1px solid #9ad7e0;
    color:#0f3f4a;
    font-size:12px;
    background:#e5f6f7;
  }

  .badge.ok{
    background:rgba(30,169,122,0.08);
    border-color:#1ea97a;
    color:#0c6a4c;
  }

  .badge.warn{
    background:rgba(245,166,35,0.08);
    border-color:#f5a623;
    color:#8a5c12;
  }

  .badge.danger{
    background:rgba(233,79,55,0.08);
    border-color:#e94f37;
    color:#7b2315;
  }

  .progress{
    height:10px;
    background:#e4f2f6;
    border:1px solid #cfe3ea;
    border-radius:999px;
    overflow:hidden;
    box-shadow:inset 0 0 6px rgba(3,46,66,0.12);
  }

  .progress > i{
    display:block;
    height:100%;
    background:linear-gradient(90deg,var(--brand),var(--brand-2));
    box-shadow:0 0 14px rgba(0,184,169,0.45);
    transition:width 220ms ease-out;
  }

  .steps-grid{
    margin-top:18px;
    display:grid;
    gap:14px;
  }

  @media (min-width:720px){
  .steps-grid{
    grid-template-columns:repeat(2,minmax(0,1fr));
  }
  }

 @media (min-width:1040px){
  .steps-grid{
    grid-template-columns:repeat(4,minmax(0,1fr));
  }
 }

 .step-card{
   background:var(--card);
   border-radius:20px;
   padding:14px 14px 16px;
   border:1px solid rgba(207,227,234,0.8);
   box-shadow:0 10px 30px rgba(3,46,66,0.08);
   display:flex;
   flex-direction:column;
   gap:8px;
 }

 .step-header{
  display:flex;
  align-items:center;
  gap:8px;
 }

 .step-icon{
   width:32px;
   height:32px;
   border-radius:999px;
   display:grid;
   place-items:center;
   background:var(--bg-soft);
   color:var(--brand);
   font-size:18px;
   flex-shrink:0;
 }

 .step-label{
   font-size:11px;
   text-transform:uppercase;
   letter-spacing:0.06em;
   color:var(--muted);
 }

 .step-title{
   font-size:14px;
   font-weight:600;
   color:var(--text);
 }

 .step-body{
   font-size:13px;
   color:var(--muted);
   line-height:1.5;
 }

   .countdown-card{
    margin-top:12px;
    padding:12px 14px;
    border-radius:16px;
    border:1px dashed rgba(0,184,169,0.45);
    background:var(--bg-soft);
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:10px;
    flex-wrap:wrap;
  }

  .countdown-label{
    font-size:13px;
    color:var(--text-soft);
  }

  .countdown-label .step-label{
    margin-bottom:2px;
  }

  .countdown-timer{
    display:flex;
    gap:8px;
  }

  .cd-part{
    min-width:58px;
    padding:6px 8px;
    border-radius:12px;
    background:#ffffff;
    text-align:center;
    box-shadow:0 4px 12px rgba(3,46,66,0.08);
  }

  .cd-value{
    font-weight:600;
    font-size:15px;
    color:var(--text);
  }

  .cd-caption{
    font-size:11px;
    color:var(--muted);
  }

  @media (max-width:600px){
    .countdown-card{
      align-items:flex-start;
    }
    .countdown-timer{
      width:100%;
      justify-content:flex-start;
      flex-wrap:wrap;
    }
  }

  .footer{
    color:var(--muted);
    text-align:center;
    margin-top:16px;
    font-size:12px;
    opacity:0.85;
  }

  .banner{
    padding:14px 16px;
    border-radius:16px;
    text-align:center;
    font-weight:800;
    letter-spacing:.08em;
    text-transform:uppercase;
    margin: 12px auto 18px;
    max-width: 720px;
 }
 .banner-soldout{ background: rgba(0,0,0,.10); }
 .banner-comingsoon{ background: rgba(0,0,0,.08); }

 .form-disabled{
   opacity:.55;
   filter: grayscale(0.15);
 }
 .form-disabled input,
 .form-disabled button{
   pointer-events:none;
 }

  .step-kicker { font-size:12px; letter-spacing:.08em; text-transform:uppercase; opacity:.75; margin-bottom:6px; }

  .big-number { font-size:58px; font-weight:800; letter-spacing:.02em; }

    .hold-ok{
    display:flex;
    gap:10px;
    justify-content:center;
    align-items:flex-start;

    /* IMPORTANT: make it a “layout wrapper”, not a separate box */
    padding:0;
    background: transparent;
    border: 0;
    box-shadow: none;

    /* keep the nice fade-in motion */
    opacity: 0;
    transform: translateY(6px);
    animation: holdFadeIn 420ms ease-out forwards;

    max-width:none;
    margin:0;
  }

  .tick{
    width:26px;
    height:26px;
    border-radius:999px;
    display:inline-flex;
    align-items:center;
    justify-content:center;
    font-weight:900;
    background: rgba(0,184,169,0.04);
    border: 1px solid rgba(0,184,169,0.08);
    color: var(--ok);
  }

  /* Nicer wheel with pointer */
  .wheel-wrap{
    position:relative;
    width:220px; height:220px;
    margin:0 auto;
  }
  
.wheel{
  position:absolute;
  inset:0;
  border-radius:999px;

  /* Premium outer ring */
  border:10px solid rgba(0,184,169,.18);

  /* Depth without harsh contrast */
  box-shadow:
    0 18px 40px rgba(3,46,66,.12),
    inset 0 0 0 1px rgba(255,255,255,.35);

  /* Subtle casino-style surface (no harsh slices) */
  background:
    /* soft highlight */
    radial-gradient(circle at 30% 25%, rgba(255,255,255,.35), transparent 55%),

    /* inner depth ring */
    radial-gradient(circle at center,
      transparent 60%,
      rgba(18,49,61,.10) 61% 70%,
      transparent 71%
    ),

    /* very subtle segmented feel */
    repeating-conic-gradient(
      from -90deg,
      rgba(0,184,169,.18) 0 12deg,
      rgba(39,198,214,.10) 12deg 24deg
    );

  overflow:hidden;
  transform: rotate(0deg);
}

.wheel::before{
  content:"";
  position:absolute;
  inset:12px;
  border-radius:999px;
  background:
    repeating-conic-gradient(
      from -90deg,
      rgba(255,255,255,.18) 0 1deg,
      transparent 1deg 12deg
    );
  opacity:.22;
  pointer-events:none;
}

.wheel::after{
  content:"";
  position:absolute;
  inset:0;
  border-radius:999px;
  background:
    radial-gradient(circle at 50% 30%, rgba(255,255,255,.22), transparent 55%),
    radial-gradient(circle at 50% 65%, rgba(18,49,61,.08), transparent 65%);
  pointer-events:none;
}

.wheel-center{
  position:absolute;
  width:72px;
  height:72px;
  left:50%;
  top:50%;
  transform: translate(-50%,-50%);
  border-radius:999px;

  background:
    radial-gradient(circle at 30% 30%, #ffffff, #f2f6f8);

  border:7px solid rgba(0,184,169,.22);

  box-shadow:
    0 4px 10px rgba(3,46,66,.18),
    inset 0 0 0 2px rgba(255,255,255,.6);
}

.wheel-pointer{
  position:absolute;
  left:50%;
  top:-4px;
  transform: translateX(-50%);
  width:0;
  height:0;

  border-left:14px solid transparent;
  border-right:14px solid transparent;
  border-bottom:26px solid rgba(0,184,169,.55);

  filter: drop-shadow(0 6px 10px rgba(3,46,66,.18));
  z-index:5;
}


  /* quick spin while waiting for API */
  /* Casino-style spin (with wobble) */
  .wheel.wheel-spinning{
    animation:
      wheelSpinFast .6s linear infinite,
      wheelWobble .9s ease-in-out infinite;
    will-change: transform;
  }

  @keyframes wheelSpinFast {
    to { transform: rotate(360deg); }
  }

  @keyframes holdFadeIn {
    from {
      opacity: 0;
      transform: translateY(6px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }

  /* Tighter button spacing on mobile (confirmation page) */
  @media (max-width: 480px) {
    .row{
      gap:6px !important;
    }
    .wheel-wrap{
      width:190px;
      height:190px;
    }
    .wheel{
      border-width:9px;
    }
    .wheel-center{
      width:64px;
      height:64px;
      border-width:7px;
    }
  }

  @keyframes wheelWobble {
    0%   { filter: brightness(1); }
    50%  { filter: brightness(1.05); }
    100% { filter: brightness(1); }
  }
 </style>
   <script>
     document.addEventListener('DOMContentLoaded', () => {
       document.querySelectorAll('form[data-safe-submit]').forEach(f => {
         f.addEventListener('submit', (e) => {
           // The button that was actually clicked (important when you have multiple submit buttons)
           const submitter = e.submitter;

           // If the clicked button has a name/value (e.g. name="status" value="live"),
           // copy it into a hidden input BEFORE disabling the button,
           // otherwise Flask won't receive it.
           if (submitter && submitter.name) {
             let hidden = f.querySelector(`input[type="hidden"][name="${submitter.name}"]`);
             if (!hidden) {
               hidden = document.createElement("input");
               hidden.type = "hidden";
               hidden.name = submitter.name;
               f.appendChild(hidden);
             }
             hidden.value = submitter.value || "";
           }

           // Disable only the clicked button (not "the first submit button in the form")
           if (submitter) {
             submitter.disabled = true;
             submitter.textContent = "Working...";
           }
         });
       });
     });
   </script>

   </head>
   <body class="{{ layout_mode }}">
     <div class="wrap">
       <nav class="nav">
         <a class="logo" href="{{ url_for('home') }}">
           <span class="logo-badge">🎟️</span>
           <strong>Get My Number</strong>
         </a>
         <div class="nav-links">
           <a href="{{ url_for('admin_charities') }}">Admin</a>
           <a href="{{ url_for('partner_login') }}">Partner</a>
         </div>
       </nav>

       {% if flow_progress_pct is defined and flow_progress_pct is not none %}
         <div class="flow-progress">
           <div class="flow-progress-meta">
             <div class="flow-progress-left">Step {{ step_current }} of {{ step_total }}</div>
             <div class="flow-progress-right">{{ flow_progress_pct }}%</div>
           </div>

           <div class="flow-progress-track">
             <div class="flow-progress-fill" style="width: {{ flow_progress_pct }}%"></div>
           </div>
         </div>
       {% endif %}
 
    <section class="card {{ page_class or '' }}">
      {% with messages = get_flashed_messages() %}
        {% if messages %}
          <div class="stack" style="margin-bottom:10px">
            {% for m in messages %}<span class="badge warn">{{ m }}</span>{% endfor %}
          </div>
        {% endif %}
      {% endwith %}
      {{ body|safe }}
    </section>

    <footer class="footer" style="margin-top:18px">
      <div style="display:flex;gap:14px;flex-wrap:wrap;justify-content:center;align-items:center">
        <span>© {{ datetime.utcnow().year }} Get My Number. All rights reserved.</span>
        <a href="{{ url_for('terms') }}">Terms</a>
        <span class="muted">•</span>
        <a href="{{ url_for('privacy') }}">Privacy</a>
      </div>

      <div style="margin-top:10px;display:flex;justify-content:center">
        <a href="//www.dmca.com/Protection/Status.aspx?ID=6025ab3b-b51b-49b6-bfbb-c4640ef3d229"
           title="DMCA.com Protection Status" class="dmca-badge">
          <img src="https://images.dmca.com/Badges/dmca_protected_sml_120n.png?ID=6025ab3b-b51b-49b6-bfbb-c4640ef3d229"
               alt="DMCA.com Protection Status" />
        </a>
      </div>
    </footer>

    <script src="https://images.dmca.com/Badges/DMCABadgeHelper.min.js"></script>
  </div>
   <script>
     (function () {
       const allowCopy = {{ 'true' if allow_copy else 'false' }};
       if (allowCopy) return;

       // Disable right-click
       document.addEventListener('contextmenu', function (e) {
         e.preventDefault();
       });

       // Disable common copy shortcuts (Ctrl/Cmd+C, Ctrl/Cmd+U, Ctrl/Cmd+S)
       document.addEventListener('keydown', function (e) {
         const key = (e.key || '').toLowerCase();
         const isMac = navigator.platform.toUpperCase().indexOf('MAC') >= 0;
         const mod = isMac ? e.metaKey : e.ctrlKey;

         if (mod && (key === 'c' || key === 'u' || key === 's' || key === 'p')) {
           e.preventDefault();
         }
       });

       // Disable copy/cut (still allows your button that uses navigator.clipboard)
       document.addEventListener('copy', function (e) { e.preventDefault(); });
       document.addEventListener('cut', function (e) { e.preventDefault(); });
     })();
   </script>
</body></html>
"""

def build_ticks_block(items, wrap_card=True):
    """
    Shared UI partial: ticked lines stacked vertically.
    items: list[str] of HTML strings (already escaped/controlled).
    """
    lines_html = "\n".join(
        f"""
        <div style="display:flex;align-items:flex-start;gap:8px">
          <span class="tick">&#10003;</span>
          <span>{item}</span>
        </div>
        """.strip()
        for item in items
    )

    inner = f"""
    <div class="muted" style="display:flex;flex-direction:column;gap:8px;line-height:1.45;text-align:left">
      {lines_html}
    </div>
    """.strip()

    if not wrap_card:
        return inner

    return f"""
    <div class="card" style="margin-top:14px">
      {inner}
    </div>
    """.strip()

def render(body, **ctx):
    path = request.path or ""
    allow_copy = path.startswith("/admin") or path.startswith("/partner")
    ctx.setdefault("allow_copy", allow_copy)

    # Layout mode:
    # - wide: homepage + admin/partner pages
    # - narrow: public flow pages like /<slug>, /<slug>/authorise, /<slug>/reveal, etc
    layout_mode = "layout-wide"
    if not (path == "/" or path.startswith("/admin") or path.startswith("/partner")):
        layout_mode = "layout-narrow"
    ctx.setdefault("layout_mode", layout_mode)

    ctx.setdefault("HOLD_AMOUNT_PENCE", HOLD_AMOUNT_PENCE)
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

def _parse_skill_answers(raw: str):
    """
    Accepts either:
      - newline separated answers
      - OR JSON array string
    Returns: clean list[str]
    """
    if not raw:
        return []
    raw = raw.strip()
    if not raw:
        return []
    # Try JSON first
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            out = []
            for x in data:
                s = (str(x) if x is not None else "").strip()
                if s:
                    out.append(s)
            # de-dupe preserving order
            seen = set()
            dedup = []
            for s in out:
                if s.lower() in seen:
                    continue
                seen.add(s.lower())
                dedup.append(s)
            return dedup
    except Exception:
        pass

    # Fallback: newline list
    lines = []
    for line in raw.splitlines():
        s = line.strip()
        if s:
            lines.append(s)

    # de-dupe preserving order (case-insensitive)
    seen = set()
    dedup = []
    for s in lines:
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        dedup.append(s)
    return dedup

def _parse_prizes(raw: str):
    """
    Accepts either:
      - newline separated prizes
      - OR JSON array string
    Returns: clean list[str]
    """
    if not raw:
        return []
    raw = raw.strip()
    if not raw:
        return []

    # Try JSON first
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            out = []
            for x in data:
                s = (str(x) if x is not None else "").strip()
                if s:
                    out.append(s)
            # de-dupe preserving order (case-insensitive)
            seen = set()
            dedup = []
            for s in out:
                key = s.lower()
                if key in seen:
                    continue
                seen.add(key)
                dedup.append(s)
            return dedup[:20]
    except Exception:
        pass

    # Fallback: newline list
    lines = [ln.strip() for ln in raw.splitlines()]
    out = [ln for ln in lines if ln]
    seen = set()
    dedup = []
    for s in out:
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        dedup.append(s)
    return dedup[:20]

def get_connect_status(acct_id):
    """
    Returns a dict like:
      {"ok": True, "charges_enabled": True, "payouts_enabled": True, "due": [...]}
    """
    if not acct_id or not acct_id.startswith("acct_"):
        return {"ok": False}

    try:
        a = stripe.Account.retrieve(acct_id)
        req = a.get("requirements", {}) or {}
        due = req.get("currently_due", []) or []
        return {
            "ok": True,
            "charges_enabled": bool(a.get("charges_enabled")),
            "payouts_enabled": bool(a.get("payouts_enabled")),
            "due": due,
        }
    except Exception as e:
        app.logger.error(f"Connect status fetch failed for {acct_id}: {e}")
        return {"ok": False}

def _choose_skill_options(all_answers, correct_answer, display_count=4):
    """
    Returns a list of options containing correct_answer plus random distractors.
    Ensures correct_answer is included (if possible).
    """
    display_count = max(2, int(display_count or 4))
    all_answers = list(all_answers or [])
    correct_answer = (correct_answer or "").strip()

    # If correct isn't in the pool, still treat it as correct and include it
    pool = [a for a in all_answers if a.strip()]
    pool_lower = {a.lower() for a in pool}

    opts = []
    if correct_answer:
        opts.append(correct_answer)

    # add distractors (excluding correct by case-insensitive match)
    distractors = [a for a in pool if a.lower() != correct_answer.lower()]
    random.shuffle(distractors)

    for a in distractors:
        if len(opts) >= display_count:
            break
        opts.append(a)

    # shuffle final options
    random.shuffle(opts)
    return opts

def refresh_campaign_status(c: Charity) -> None:
    """
    Automatically set campaign_status='sold_out' once all tickets are taken.
    Do NOT auto-change is_live; that remains a manual toggle.
    """
    try:
        remaining = len(available_numbers(c))
        if remaining <= 0 and getattr(c, "campaign_status", "live") != "sold_out":
            c.campaign_status = "sold_out"
            db.session.commit()
    except Exception:
        db.session.rollback()

def partner_guard(slug):
    if not session.get("partner_ok"): return None
    if session.get("partner_slug") != slug: return None
    c = Charity.query.filter_by(slug=slug).first()
    if not c or session.get("partner_charity_id") != c.id: return None
    return c

def refresh_campaign_status(c: Charity) -> None:
    """Auto-set sold_out if no tickets remain. Never auto-change other statuses."""
    try:
        remaining = len(available_numbers(c))
        if remaining <= 0 and (getattr(c, "campaign_status", "live") != "sold_out"):
            c.campaign_status = "sold_out"
            db.session.commit()
    except Exception:
        db.session.rollback()

def build_tickbox(title: str, lines_html: list[str]) -> Markup:
    """
    Shared tick-box UI used across authorise / hold-success / confirm-payment.
    lines_html should contain HTML strings (already escaped or using entities like &pound;).
    """
    items = "\n".join(
        f'<div style="display:flex;align-items:flex-start;gap:10px;margin-top:8px;">'
        f'  <span class="tick">&#10003;</span>'
        f'  <div class="muted" style="margin:0;line-height:1.45;">{line}</div>'
        f'</div>'
        for line in lines_html
    )

    html = f"""
    <div class="hold-ok" style="align-items:flex-start;">
      <div style="text-align:left;">
        <div><strong>{title}</strong></div>
        {items}
      </div>
    </div>
    """
    return Markup(html)

def compute_hold_amount_pence(charity) -> int:
    """
    Ensure the Stripe authorisation is always at least the maximum possible ticket value
    (max_number pounds), so capture never exceeds the authorised amount.
    """
    min_hold = int(getattr(charity, "max_number", 0) or 0) * 100
    configured = int(getattr(charity, "hold_amount_pence", 0) or 0)
    return max(min_hold, configured if configured > 0 else 0)

# ====== PUBLIC ================================================================

@app.route("/")
def home():
    charities = Charity.query.order_by(Charity.name.asc()).all()

    tiles = []
    for c in charities:
        maxn = int(getattr(c, "max_number", 0) or 0)
        rem = len(available_numbers(c)) if maxn > 0 else 0
        sold = max(0, maxn - rem)
        pct = int(round((sold / maxn) * 100)) if maxn > 0 else 0
        pct = max(0, min(100, pct))

        status = (getattr(c, "campaign_status", "live") or "live").strip()
        # fall back to older boolean flags if present
        if getattr(c, "is_sold_out", False):
            status = "sold_out"
        if getattr(c, "is_coming_soon", False):
            status = "coming_soon"
        # IMPORTANT: do NOT override campaign_status with legacy is_live here.
        # campaign_status is the single source of truth for public display.

        banner = ""
        if status == "sold_out":
            banner = "SOLD OUT"
        elif status == "coming_soon":
            banner = "COMING SOON"
        elif status == "inactive":
            banner = "INACTIVE"

        blocked = status in ("inactive", "sold_out", "coming_soon")

        about = (getattr(c, "tile_about", None) or "").strip()

        prizes = []
        try:
            prizes = _parse_prizes(getattr(c, "prizes_json", "") or "")
        except Exception:
            prizes = []

        tiles.append({
            "slug": c.slug,
            "name": c.name,
            "img": getattr(c, "logo_data", None),
            "poster": getattr(c, "poster_data", None),
            "about": about,
            "prizes": prizes,
            "pct": pct,
            "banner": banner,
            "blocked": blocked,
            "status": status,
        })

    body = """
    <div class="hero" style="text-align:center;">
      <h1 class="section-title">Choose a Charity to Support</h1>
      <p class="muted section-subtitle">
        Your contribution matters. Select a charity below to ensure your ticket donation supports a cause you believe in.
      </p>
    </div>

    <div class="tiles-grid">
      {% for t in tiles %}
        <div class="cause-tile">
          {% if t.banner %}
            <div class="ribbon">{{ t.banner }}</div>
          {% endif %}

          <div class="cause-top">
            <div class="cause-img">
              {% if t.img %}
                <img src="{{ t.img }}" alt="{{ t.name }} logo">
              {% else %}
                <span style="font-size:18px">🤍</span>
              {% endif %}
            </div>
            <div>
              <div class="cause-name">{{ t.name }}</div>

              {% if t.prizes and (t.prizes|length) > 0 %}
                <div class="cause-prizes">
                  <strong>{% if (t.prizes|length) == 1 %}Prize{% else %}Prizes{% endif %}:</strong>
                  {{ t.prizes[:2] | join(" • ") }}{% if (t.prizes|length) > 2 %} • …{% endif %}
                </div>
              {% endif %}
            </div>
          </div>

          {% if t.poster %}
            <div class="cause-poster">
              <img src="{{ t.poster }}" alt="{{ t.name }} poster">
            </div>
          {% endif %}
          <div class="cause-about">
            {% if t.about %}
              {{ t.about }}
            {% else %}
              Support this campaign by taking part and confirming your donation after your number is revealed.
            {% endif %}
          </div>

          <div class="tile-progress">
            <div class="muted" style="font-size:12px;margin-bottom:6px;">Tickets Sold</div>
            <div class="progress-track">
              <div class="progress-fill" style="width: {{ t.pct }}%;"></div>
            </div>
          </div>

          <div class="tile-cta">
            {% if not t.blocked %}
              <a class="btn" href="{{ url_for('charity_page', slug=t.slug) }}">Select this Cause</a>
            {% else %}
              <button class="btn" type="button" disabled>
                {% if t.status == "sold_out" %}Sold out{% elif t.status == "coming_soon" %}Coming Soon{% else %}Unavailable{% endif %}
              </button>
            {% endif %}
          </div>
        </div>
      {% else %}
        <p class="muted" style="text-align:center;">
          No charities yet. Use <a href="{{ url_for('admin_charities') }}">Admin</a> to add one.
        </p>
      {% endfor %}
    </div>

    <hr style="margin:28px 0 20px; border:none; border-top:1px solid rgba(207,227,234,0.9);">

    <div class="hero" style="text-align:center;">
      <h1 class="section-title">How Get My Number Works</h1>
      <p class="muted section-subtitle">
        A simple, transparent flow where a temporary hold is first taken, then you confirm your donation amount.
      </p>
    </div>

    <div class="steps-grid">
      <!-- keep your existing step cards exactly as they were -->
      """ + """
      <div class="step-card">
        <div class="step-header">
          <div class="step-icon">👤</div>
          <div>
            <div class="step-label">Step 1</div>
            <div class="step-title">Enter your Details</div>
          </div>
        </div>
        <div class="step-body">
          Choose your charity and enter your name, email and (optionally) phone number.
        </div>
      </div>

      <div class="step-card">
        <div class="step-header">
          <div class="step-icon">🧠</div>
          <div>
            <div class="step-label">Step 2</div>
            <div class="step-title">Optional Multiple-Choice Question</div>
          </div>
        </div>
        <div class="step-body">
          Some campaigns include an optional multiple-choice question before you proceed.
          If it is enabled for that charity, you will be recquired to answer the question correctly to continue.
        </div>
      </div>

      <div class="step-card">
        <div class="step-header">
          <div class="step-icon">💳</div>
          <div>
            <div class="step-label">Step 3</div>
            <div class="step-title">Temporary Card Hold</div>
          </div>
        </div>
        <div class="step-body">
          You will be redirected to our secure Stripe checkout where a temporary hold
          is placed on your card. This is an authorisation only – no money is taken at this point.
          Alternatively, there is a free postal route.
        </div>
      </div>

      <div class="step-card">
        <div class="step-header">
          <div class="step-icon">🏷️</div>
          <div>
            <div class="step-label">Step 4</div>
            <div class="step-title">Receive your Ticket Number</div>
          </div>
        </div>
        <div class="step-body">
          Once the hold is confirmed, you are redirected back to Get My Number and assigned
          a unique ticket number for your chosen charity.
        </div>
      </div>

      <div class="step-card">
        <div class="step-header">
          <div class="step-icon">✅</div>
          <div>
            <div class="step-label">Step 5</div>
            <div class="step-title">Donate your Ticket Amount</div>
          </div>
        </div>
        <div class="step-body">
          Confirm your donation for an amount equal to your ticket number.
          We capture only this amount from the original card hold.
        </div>
      </div>

      <div class="step-card">
        <div class="step-header">
          <div class="step-icon">💷</div>
          <div>
            <div class="step-label">Step 6</div>
            <div class="step-title">Remaining Hold is Released</div>
          </div>
        </div>
        <div class="step-body">
          Any difference between the original hold and your ticket amount is released
          by your bank.
        </div>
      </div>
    </div>
    """

    return render(
        body,
        tiles=tiles,
        title="Get My Number",
        flow_progress_pct=None,
        step_current=None,
        step_total=None,
    )

@app.route("/terms")
def terms():
    body = """
    <div class="hero">
      <h1>Terms of Service</h1>
      <p class="muted">Last updated: {{ datetime.utcnow().strftime('%Y-%m-%d') }}</p>
    </div>

    <div class="stack">
      <p><strong>1) About the service</strong><br>
      Get My Number provides a platform for running charity-linked campaigns (“Campaigns”).
      Campaign availability may be paused, changed, or ended at any time.</p>

      <p><strong>2) Donations</strong><br>
      Where shown, we may place a temporary card authorisation (“hold”) via Stripe before issuing a number.
      Your bank may show this as a pending amount.</p>

      <p><strong>3) No guarantees</strong><br>
      We do not guarantee uninterrupted availability, or that a Campaign will remain live until a specific time,
      and we may stop a Campaign when tickets are sold out.</p>

      <p><strong>4) Acceptable use</strong><br>
      You agree not to misuse the site, attempt to access admin/partner areas without authorisation,
      or interfere with security or performance.</p>

      <p><strong>5) Use of Site Content</strong><br>
      All content on this website, including text, branding, layout, design, logos, graphics,
      and underlying code, is owned by or licensed to Get My Number.</p>

      <p><strong>6) No Cloning, Scraping or Mirroring</strong><br>
      You may not copy, reproduce, distribute, mirror, frame, scrape, reverse engineer,
      or create derivative works of any part of this site (including using automated tools,
      AI tools, or “website builders” that attempt to recreate a site from a URL),
      especially for the purpose of creating a competing product or service, without our
      prior written consent.</p>

      <p><strong>7) Prohibited Activities</strong><br>
      Automated scraping, crawling, mirroring, or downloading of site content. Attempting to 
      bypass security controls or access restricted areas.Interfering with site performance, 
      integrity, or security. Impersonating Get My Number or any campaign.<p>

      <hr class="sep">
      <h2>Campaign Terms &amp; Conditions</h2>

      <p>1. The T&amp;Cs shall apply to all the campaigns advertised on Our Website. Our campaigns are typically intended to raise funds for charities. GetMyNumber, also known as &ldquo;We&rdquo; or &ldquo;Us&rdquo;, will act as the administrator of all campaigns unless otherwise stated. Each campaign will have its own donation amount to donate to the charity, a closing date and time, and a draw date and time. Each campaign will state the maximum number of entries GetMyNumber will accept.</p>

      <p><strong>How To Enter:</strong><br>
      3. The campaign will be open until the date and time specified on Our Website for each campaign.<br>
      4. To enter, you must follow the entry instructions on Our Website. You will be required to provide your name, email, and phone number. You will also be required to authorise a temporary card hold via Stripe before being issued a number. Following this, you will be issued with a number. Your number will be the amount you will donate to the charity to confirm the entry.<br>
      5. By entering any campaign on Our Website, you accept these T&amp;Cs and any campaign specific rules set out on Our Website. We recommend that you print and keep safe, or save to your device, a copy of the T&amp;Cs for future reference.</p>

      <p>7. The winner (&ldquo;the Winner&rdquo;) of the campaign will be selected at random from all eligible entries received by the closing date and time. Random selection may be performed by a random number generator.</p>

      <p>8. Campaign entries made via the paid online route are non-refundable. Entries made via the free postal entry route are free. Paid entries increase the number of entries you have and therefore increase your chances of winning and does not affect the outcome of the random draw.</p>

      <p><strong>Cancellations Or Extensions:</strong><br>
      9. We may by notice on Our Website (and/or by email if you have provided an email address) extend the closing date of any campaign. We may also cancel a campaign at any time. If we cancel a campaign, we will notify entrants via Our Website and by email. If a campaign is cancelled, we may offer entrants a future credit in line with the refund policy in Schedule 2, but then GetMyNumber will refund all entry fees.</p>

      <p><strong>Eligibility:</strong><br>
      10. Our campaigns are open those of or over 18 years old, except any employees of GetMyNumber, their family, or anyone else professionally connected with the campaign.<br>
      11. You may not enter if you are located outside the United Kingdom or otherwise in breach of these T&amp;Cs. We may refuse entry and/or void an entry at our discretion.<br>
      12. We may require proof of age and identity. If you do not provide this, your entry may be void and you may forfeit the prize.</p>

      <p>13. You may enter via one or both routes:-<br>
      <br>
      Paid Online Campaign:<br>
      6.1 You may enter online via Our Website by completing the required details and completing the donation to the charity. You will be issued with a number after authorising a temporary hold. You must then complete donation to confirm your entry.<br>
      <br>
      Free Postal Entry:<br>
      6.2 You may enter by post by following the requirements set out in Annex 1. Postal entries must be received before the closing date and time. Postal entries have the same chance of winning as paid entries. No additional donation is required for postal entries.</p>

      <p><strong>The Prize:</strong><br>
      17. GetMyNumber does not take any responsibility for the Prize. This is arranged by the Campaign partner.</p>

      <p><strong>Winners:</strong><br>
      18. GetMyNumber decision as to all matters where we have discretion is final and no correspondence will be entered into.<br>
      19. We will contact the Winner using the contact details provided. If we cannot contact the Winner within 14 days, we reserve the right to select an alternative Winner.<br>
      20. The Winner must provide any additional information requested, including proof of identity and age.<br>
      21. GetMyNumber does not take any responsibility for the delivery of the prize. This is arranged by the campaign partner.</p>

      <p><strong>Limitation Of Liability:</strong><br>
      22. So as is permitted by law, GetMyNumber excludes all liability for any loss or damage arising out of or in connection with the campaign, whether in contract, tort (including negligence), breach of statutory duty, or otherwise, including but not limited to indirect or consequential loss.<br>
      23. Nothing in these T&amp;Cs shall limit or exclude our liability for death or personal injury caused by our negligence, fraud, or any other liability which cannot be excluded by law.<br>
      24. We do not accept responsibility for entries not successfully completed due to a technical fault, technical malfunction, computer hardware or software failure, satellite, network or server failure of any kind.<br>
      25. We shall not be liable for any loss or damage suffered by you as a result of any act or omission of any third party, including but not limited to the campaign partner or prize provider. Subject to that, your statutory rights are not affected.</p>

      <p><strong>Ownership Of Campaign Entries And Intellectual Property Rights:</strong><br>
      All content on this website, including text, branding, design, graphics, and campaign mechanics, are owned by GetMyNumber or licensed to us. You may not reproduce, scrape, mirror, copy, or otherwise use any part of the website or campaign content without our permission. This includes use of automated tools, AI, or scraping systems to recreate our content or processes. All rights are reserved under applicable intellectual property legislation from time to time in force, anywhere in the world.</p>

      <p><strong>Data Protection And Publicity:</strong><br>
      26. We will process your personal data in accordance with our Privacy Policy. By entering, you agree that we may use your details to administer the campaign, contact you, and (if you win) publish your name and general location (e.g. town/city) on Our Website and/or social media, unless you object on reasonable grounds.<br>
We may share your information with the campaign partner and third parties involved in administering the campaign, including payment providers (e.g. Stripe). We have no liability for the acts or omissions of any third-party.</p>

      <p><strong>General:</strong><br>
      27. If there is any reason to believe that you are in breach of these T&amp;Cs, We may, at our sole discretion, exclude you from participating in the campaign and/or void any entries.<br>
      28. We reserve the right to hold void, suspend, cancel, or amend the campaign where it becomes necessary to do so.<br>
      29. If any part of these T&amp;Cs is found to be invalid, illegal or unenforceable, the remainder shall continue in full force and effect.<br>
      30. These T&amp;Cs, and any disputes arising out of them, shall be governed by English law and subject to the exclusive jurisdiction in relation to any dispute over them.</p>

      <p><strong>Annex 1</strong><br>
      <strong>Postal Entry Requirements</strong><br>
      To enter a campaign by post, you must send a letter containing your full name, email address, phone number, the charity campaign slug/name, and confirmation that you accept these T&amp;Cs. You must send it to the postal address stated on the authorisation / campaign page (or other location where we publish it).<br>
      <br>
      Your postal entry must be received before the campaign closing date and time. Only one entry per envelope is permitted. Hand delivered entries are not allowed. Entries that are illegible, incomplete, late, or do not comply may be rejected at our discretion.<br>
      Postal entries are free and have the same chance of winning as paid entries. You will be allocated a number and included in the draw. No donation is required for postal entries.</p>

      <p><strong>Annex 2</strong><br>
      <strong>Force Majeure Events</strong><br>
      Force majeure event means any event beyond our reasonable control, including but not limited to:<br>
      1. Acts of God;<br>
      2. Flood, drought, earthquake or other natural disaster;<br>
      3. Epidemic or pandemic;<br>
      4. Terrorist attack, civil war, civil commotion or riots;<br>
      5. Any law or action taken by a government or public authority;<br>
      6. Collapse of buildings, fire, explosion or accident;<br>
      7. Interruption or failure of utility service.</p>

    </div>
    """
    return render(body, title="Terms")


@app.route("/privacy")
def privacy():
    body = """
    <div class="hero">
      <h1>Privacy Policy</h1>
      <p class="muted">Last updated: {{ datetime.utcnow().strftime('%Y-%m-%d') }}</p>
    </div>

    <div class="stack">
      <p><strong>1) What we collect</strong><br>
      When you enter a Campaign we may collect your name, email address, phone number (optional),
      and your assigned ticket number.</p>

      <p><strong>2) Donations</strong><br>
      Donations/authorisations are processed by Stripe. We do not store full card details on our servers.</p>

      <p><strong>3) Why we use data</strong><br>
      We use your details to administer Campaign entries, provide support, prevent fraud/abuse,
      and keep an audit trail of entries.</p>

      <p><strong>4) Sharing</strong><br>
      We may share entry information with the relevant Campaign organiser/charity solely for Campaign administration,
      and with service providers (e.g., Stripe) as required to operate the platform.</p>

      <p><strong>5) Retention</strong><br>
      We retain data only as long as necessary for Campaign administration, compliance, and dispute handling.</p>

      <p><strong>6) Your rights</strong><br>
      You may request access, correction, or deletion of your data where applicable.</p>

    </div>
    """
    return render(body, title="Privacy")

@app.route("/<slug>", methods=["GET","POST"])
def charity_page(slug):
    charity = get_charity_or_404(slug)
    charity_logo = getattr(charity, "logo_data", None) or (
        KEHILLA_LOGO_DATA_URI if charity.slug == "thekehilla" else None
    )
    poster_data = (getattr(charity, "poster_data", None) or "").strip() or None

    # Auto-switch to sold out if no tickets remain
    refresh_campaign_status(charity)

    status = (getattr(charity, "campaign_status", "live") or "live").strip()
    is_blocked = status in ("inactive", "sold_out", "coming_soon")

    # Tickets remaining banner (only when live)
    total = charity.max_number
    remaining = len(available_numbers(charity))
    taken = total - remaining
    pct = int((taken / total) * 100) if total else 0

    remaining_banner = None
    if status == "live" and remaining > 0:
        if remaining <= 25:
            remaining_banner = f"Only {remaining} ticket{'s' if remaining != 1 else ''} left"

    # --------------------
    # POST: start Stripe hold
    # --------------------
    if request.method == "POST":
        if is_blocked:
            flash("This campaign is not currently accepting entries.")
            return redirect(url_for("charity_page", slug=charity.slug))

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()

        if not name or not email:
            flash("Name and Email are required.")
        else:
            hold_amount_pence = compute_hold_amount_pence(charity)

            session["pending_entry"] = {
                "slug": charity.slug,
                "name": name,
                "email": email,
                "phone": phone,
                "hold_amount_pence": hold_amount_pence,
            }

            if getattr(charity, "skill_enabled", False):
                session.pop("skill_passed", None)
                session.pop("skill_options", None)
                session.pop("skill_slug", None)
                session["skill_attempts"] = 0
                return redirect(url_for("skill_gate", slug=charity.slug))

            # Always use the Authorise Hold page for every campaign
            return redirect(url_for("authorise_hold", slug=charity.slug))

    # --------------------
    # GET: stats + page render
    # --------------------
    total = charity.max_number
    remaining = len(available_numbers(charity))
    remaining_banner = None
    if status == "live":
        if remaining <= 0:
            remaining_banner = None  # sold_out banner will handle this
        elif remaining <= 10:
            remaining_banner = f"Only {remaining} ticket{'s' if remaining != 1 else ''} left"

    taken = total - remaining
    pct = int((taken / total) * 100) if total else 0
    draw_iso = charity.draw_at.isoformat() if charity.draw_at else None

    step_current, step_total = flow_step_meta(charity, "details")

    body = """

    <div class="hero" style="text-align:center;">
      {% if status == "sold_out" %}
        <div class="banner banner-soldout">Sold out</div>
      {% elif status == "coming_soon" %}
        <div class="banner banner-comingsoon">Coming soon</div>
      {% elif status == "inactive" %}
        <div class="banner banner-inactive">Inactive</div>
      {% endif %}

      {% if remaining_banner %}
        <div class="banner banner-remaining">{{ remaining_banner }}</div>
      {% endif %}

      <div style="display:flex;align-items:center;gap:12px;justify-content:center;flex-wrap:wrap;margin-top:6px;">
        {% if charity_logo %}
          <img src="{{ charity_logo }}" alt="{{ charity.name }} logo"
               style="width:52px;height:52px;border-radius:14px;border:1px solid var(--border);object-fit:cover;">
        {% endif %}
        <h1 style="margin:0">{{ charity.name }}</h1>
      </div>

      {% if poster_data %}
        <img src="{{ poster_data }}" alt="{{ charity.name }} campaign poster"
             style="
               display:block;
               margin:14px auto 0;
               max-width:220px;     /* similar scale to old logo */
               width:100%;
               height:auto;         /* preserves aspect ratio */
               border-radius:16px;
               border:1px solid var(--border);
               box-shadow:0 10px 24px rgba(3,46,66,0.12);
             ">
      {% endif %}

            <p>
        We place a temporary hold on your card before giving you a number.
        Once your donation is confirmed, the hold will be released.
      </p>

      {% if draw_iso %}
      <div class="countdown-card">
        <div class="countdown-label">
          <div class="step-label">Raffle draw</div>
          <div>Time left until the draw closes</div>
        </div>
        <div class="countdown-timer" data-target="{{ draw_iso }}">
          <div class="cd-part">
            <div class="cd-value" data-unit="days">--</div>
            <div class="cd-caption">days</div>
          </div>
          <div class="cd-part">
            <div class="cd-value" data-unit="hours">--</div>
            <div class="cd-caption">hours</div>
          </div>
          <div class="cd-part">
            <div class="cd-value" data-unit="minutes">--</div>
            <div class="cd-caption">mins</div>
          </div>
          <div class="cd-part">
            <div class="cd-value" data-unit="seconds">--</div>
            <div class="cd-caption">secs</div>
          </div>
        </div>
      </div>
      {% endif %}

      <div class="{% if is_blocked %}form-disabled{% endif %}">
        <form method="post" data-safe-submit>
          <label>Your Name
            <input type="text" name="name" required placeholder="e.g. Sarah Cohen" {% if is_blocked %}disabled{% endif %}>
          </label>

          <label>Email
            <input type="email" name="email" required placeholder="name@example.com" {% if is_blocked %}disabled{% endif %}>
          </label>

          <label>Phone (optional)
            <input type="tel" name="phone" placeholder="+44 7xxx xxxxxx" {% if is_blocked %}disabled{% endif %}>
          </label>

          <button class="btn" type="submit" style="margin-top:10px" {% if is_blocked %}disabled{% endif %}>
            {% if charity.preauth_page_enabled %}
              Continue
            {% else %}
              Place Hold &amp; Get My Number
            {% endif %}
          </button>
        </form>
      </div>

      <div class="soft-panel">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;">
          <div style="font-weight:900;letter-spacing:.08em;font-size:14px;opacity:.85;">
            Tickets Claimed
          </div>
          <div style="font-weight:800;font-size:14px;">
            {{ taken }} / {{ total }} ({{ pct }}%)
          </div>
        </div>

        <div class="progress" style="margin-top:10px">
          <i style="width:{{ pct }}%"></i>
        </div>

        <p class="muted" style="margin-top:10px;margin-bottom:0;">
          You will be assigned a random number from the remaining pool.<br>
          Your donation amount corresponds to your ticket number.
        </p>
      </div>

    {% if draw_iso %}
    <script>
      (function(){
        const el = document.querySelector('.countdown-timer');
        if (!el) return;
        const targetStr = el.dataset.target;
        const target = new Date(targetStr);

        function pad(n){ return n < 10 ? '0' + n : '' + n; }

        function update(){
          const now = new Date();
          let diff = target - now;
          if (diff <= 0){
            el.innerHTML = '<span class="muted">The raffle draw time has passed.</span>';
            clearInterval(timer);
            return;
          }
          const totalSeconds = Math.floor(diff / 1000);
          const days = Math.floor(totalSeconds / 86400);
          const hours = Math.floor((totalSeconds % 86400) / 3600);
          const mins = Math.floor((totalSeconds % 3600) / 60);
          const secs = totalSeconds % 60;

          const dEl = el.querySelector('[data-unit="days"]');
          const hEl = el.querySelector('[data-unit="hours"]');
          const mEl = el.querySelector('[data-unit="minutes"]');
          const sEl = el.querySelector('[data-unit="seconds"]');

          if (dEl) dEl.textContent = days;
          if (hEl) hEl.textContent = pad(hours);
          if (mEl) mEl.textContent = pad(mins);
          if (sEl) sEl.textContent = pad(secs);
        }

        update();
        const timer = setInterval(update, 1000);
      })();
    </script>
    {% endif %}
    </div>
    """
    return render(
        body,
        charity=charity,
        total=total,
        remaining=remaining,
        remaining_banner=remaining_banner,
        taken=taken,
        pct=pct,
        title=charity.name,
        step_current=step_current,
        step_total=step_total,
        flow_progress_pct=flow_progress_pct(charity, "details"),
        draw_iso=draw_iso,
        charity_logo=charity_logo,
        poster_data=poster_data,
        status=status,
        is_blocked=is_blocked
    )

@app.route("/<slug>/skill", methods=["GET", "POST"])
def skill_gate(slug):
    charity = get_charity_or_404(slug)

    pending = session.get("pending_entry")
    if not pending or pending.get("slug") != charity.slug:
        flash("We could not find your details. Please start again.")
        return redirect(url_for("charity_page", slug=charity.slug))

    if not getattr(charity, "skill_enabled", False):
        return _continue_after_skill(charity)

    step_current, step_total = flow_step_meta(charity, "skill")

    q = (getattr(charity, "skill_question", "") or "").strip()
    correct = (getattr(charity, "skill_correct_answer", "") or "").strip()
    answers = _parse_skill_answers(getattr(charity, "skill_answers_json", "") or "")
    display_count = int(getattr(charity, "skill_display_count", 4) or 4)

    # fail-safe: misconfigured => skip
    if (not q) or (not correct):
        return _continue_after_skill(charity)

    def make_options():
        return _choose_skill_options(answers, correct, display_count=display_count)

    def continue_url():
        # Always go to the Authorise Hold page after passing the skill gate
        # (Authorise page is GET, and it posts to /start-hold safely)
        return url_for("authorise_hold", slug=charity.slug)

    # -----------------------
    # GET: render page once
    # -----------------------
    if request.method == "GET":
        # On first load, if no options exist, create them
        session["skill_slug"] = charity.slug
        if session.get("skill_attempts") is None:
            session["skill_attempts"] = 0

        if not session.get("skill_options"):
            session["skill_options"] = make_options()

        remaining = max(0, 3 - int(session.get("skill_attempts", 0) or 0))

        body = """
        <style>
          /* Make the OUTER main card (the layout <section class="card">) pure white on /skill only */
          .page-skill{
            background:#ffffff !important;
            border:1px solid rgba(207,227,234,0.95) !important;
          }

          /* The INNER card on top of the main card = light grey */
          .skill-inner{
            background:#f1f4f6 !important;
            border:1px solid rgba(207,227,234,0.9) !important;
            box-shadow:none !important;
          }

          /* 2x2 grid for the 4 answers */
          #optionsWrap{
            display:grid;
            grid-template-columns:repeat(2, minmax(0, 1fr));
            gap:12px;
            margin-top:12px;
          }

          /* each answer tile */
          #optionsWrap label.pill{
            background:#fff;
            border:1px solid rgba(207,227,234,0.95);
            border-radius:14px;
            box-shadow:none;
          }

          /* Answer options: pure white, clearer, slightly larger */
          .skill-option{
            background:#ffffff !important;
            border:1px solid rgba(207,227,234,0.95) !important;
            border-radius:14px !important;
            padding:14px 14px !important;
          }

          .skill-option span{
            font-size:16px !important;
            color:#12313d !important;
          }

          /* Buttons: side-by-side and not full-width */
          .skill-actions{
            display:flex;
            gap:10px;
            justify-content:center;
            align-items:center;
            margin-top:14px;
            flex-wrap:wrap;
          }

          .btn.btn-skill{
            width:auto !important;
            min-width:160px;
            padding:12px 16px !important;
          }
 
          /* Terms below buttons */
          .skill-terms{
            margin-top:12px;
            font-size:12px;
            text-align:center;
            line-height:1.4;
          }

          /* mobile: keep readable */
          @media (max-width: 560px){
            #optionsWrap{
              grid-template-columns:1fr;
            }
          }
        </style>

        <div class="hero">
          <h1>Quick question before you continue to hold</h1>
          <p class="muted" style="margin-top:6px;line-height:1.45;">
            Please answer this multiple-choice question correctly to proceed.
          </p>
        </div>

        <div class="card skill-inner">
          <div class="muted" style="font-size:12px;margin-bottom:10px;">
            Attempts remaining: <strong id="attemptsRemaining">{{ remaining }}</strong> (max 3)
          </div>

          <div style="font-weight:700;margin-bottom:10px;font-size:16px;line-height:1.35;">
            {{ q }}
          </div>

          {% if img %}
            <img src="{{ img }}" alt="Question image"
                 style="display:block;margin:12px auto 16px auto;width:100%;max-width:360px;
                        border-radius:16px;border:1px solid rgba(207,227,234,0.9);">
          {% endif %}

          <div id="skillAlert" class="notice error" style="display:none;margin-bottom:12px;"></div>

          <form id="skillForm"
            <div id="optionsWrap" style="margin-top:6px;">
              {% for opt in options %}
                <label class="pill skill-option" style="display:flex;align-items:center;gap:10px;cursor:pointer;">
                  <input type="radio" name="answer" value="{{ opt }}" required>
                  <span>{{ opt }}</span>
                </label>
              {% endfor %}
            </div>

            <div class="skill-actions">
              <button id="skillSubmit" class="btn btn-skill" type="submit">
                Submit Answer
              </button>

              <a class="btn btn-skill secondary"
                 href="{{ url_for('charity_page', slug=charity.slug) }}">
                Cancel
              </a>
            </div>

            <p class="muted skill-terms">
              See
              <a href="/terms" target="_blank" rel="noopener noreferrer">Terms &amp; Conditions</a>.
            </p>
          </form>
        </div>

        <script>
        (function(){
          const form = document.getElementById("skillForm");
          const alertBox = document.getElementById("skillAlert");
          const optionsWrap = document.getElementById("optionsWrap");
          const remainingEl = document.getElementById("attemptsRemaining");
          const submitBtn = document.getElementById("skillSubmit") || form.querySelector('button[type="submit"]');

          function showError(msg){
            alertBox.style.display = "block";
            alertBox.textContent = msg;
          }
          function clearError(){
            alertBox.style.display = "none";
            alertBox.textContent = "";
          }
          function renderOptions(options){
            optionsWrap.innerHTML = "";
            (options || []).forEach(opt => {
              const label = document.createElement("label");
              label.className = "pill skill-option";
              label.style.cssText = "cursor:pointer;";
              label.innerHTML = `
                <input type="radio" name="answer" value="${String(opt).replace(/"/g,'&quot;')}" required>
                <span>${String(opt)}</span>
              `;
              optionsWrap.appendChild(label);
            });
          }

          form.addEventListener("submit", async function(e){
            e.preventDefault();
            clearError();

            const fd = new FormData(form);
            const chosen = fd.get("answer");
            if(!chosen){
              showError("Please select an answer.");
              return;
            }

            submitBtn.disabled = true;
            submitBtn.textContent = "Checking…";

            try{
              const res = await fetch(window.location.pathname, {
                method: "POST",
                headers: { "X-Requested-With": "fetch" },
                body: fd
              });

              // If server forces a redirect response, fall back
              if (res.redirected) {
                window.location.href = res.url;
                return;
              }

              const data = await res.json();

              if (data.ok && data.redirect){
                window.location.href = data.redirect;
                return;
              }

              if (data.locked && data.redirect){
                window.location.href = data.redirect;
                return;
              }

              if (typeof data.remaining !== "undefined"){
                remainingEl.textContent = String(data.remaining);
              }

              if (data.options){
                renderOptions(data.options);
              }

              if (data.message){
                showError(data.message);
              } else {
                showError("Incorrect answer. Please try again.");
              }

            } catch(err){
              showError("Something went wrong. Please try again.");
            } finally {
              submitBtn.disabled = false;
              submitBtn.textContent = "Submit answer";
            }
          });
        })();
        </script>
        """
        return render(
            body,
            charity=charity,
            q=q,
            img=getattr(charity, "skill_image_data", None),
            options=session.get("skill_options") or make_options(),
            step_current=step_current,
            step_total=step_total,
            flow_progress_pct=flow_progress_pct(charity, "skill"),
            remaining=remaining,
            page_class="page-skill",
            title=f"Skill check – {charity.name}",
        )

    # -----------------------
    # POST: AJAX validate
    # -----------------------
    wants_json = request.headers.get("X-Requested-With") == "fetch"

    # If not AJAX, just send back to GET (keeps behaviour sane)
    if not wants_json:
        return redirect(url_for("skill_gate", slug=charity.slug))

    posted = (request.form.get("answer") or "").strip()
    options = session.get("skill_options") or []
    if session.get("skill_slug") != charity.slug or not options:
        # reset session options if lost
        session["skill_slug"] = charity.slug
        session["skill_options"] = make_options()
        return {"ok": False, "remaining": max(0, 3 - int(session.get("skill_attempts", 0) or 0)),
                "options": session["skill_options"], "message": "Please try again."}, 200

    if posted not in options:
        return {"ok": False, "remaining": max(0, 3 - int(session.get("skill_attempts", 0) or 0)),
                "options": options, "message": "Please select one of the available answers."}, 200

    # Correct
    if posted.lower() == correct.lower():
        session["skill_passed"] = True
        session.pop("skill_options", None)
        return {"ok": True, "redirect": continue_url()}, 200

    # Incorrect
    attempts = int(session.get("skill_attempts", 0) or 0) + 1
    session["skill_attempts"] = attempts
    remaining = max(0, 3 - attempts)

    if attempts >= 3:
        # lock out (clear pending so they must restart)
        session.pop("pending_entry", None)
        session.pop("skill_options", None)
        session.pop("skill_slug", None)
        session.pop("skill_passed", None)
        session.pop("skill_attempts", None)
        return {"locked": True, "redirect": url_for("charity_page", slug=charity.slug)}, 200

    # regenerate fresh options for retry
    session["skill_options"] = make_options()
    return {
        "ok": False,
        "remaining": remaining,
        "options": session["skill_options"],
        "message": "Incorrect answer. Try again — the options have been refreshed."
    }, 200

def flow_step_meta(charity, page_key: str):
    """
    Centralised step numbering for the public flow.

    Keys:
      - details      (campaign page form)
      - skill        (skill gate page)
      - authorise    (authorise hold page)
      - reveal       (hold-success / reveal number page)
      - confirmed    (final confirmed donation page)
    """
    skill_on = bool(getattr(charity, "skill_enabled", False))
    total = 5 if skill_on else 4

    steps_no_skill = {
        "details":   1,
        "authorise": 2,
        "reveal":    3,
        "confirmed": 4,
    }
    steps_skill = {
        "details":   1,
        "skill":     2,
        "authorise": 3,
        "reveal":    4,
        "confirmed": 5,
    }

    current = (steps_skill if skill_on else steps_no_skill).get(page_key)
    return current, total

def flow_progress_pct(charity, page_key: str):
    current, total = flow_step_meta(charity, page_key)
    if not current or not total:
        return None
    # e.g. step 1/4 => 25, step 4/4 => 100
    pct = int(round((current / total) * 100))
    return max(0, min(100, pct))

def _continue_after_skill(charity):
    """
    After skill gate success (or if disabled), continue your existing flow:
      - if preauth_page_enabled => /<slug>/authorise
      - else => create Stripe checkout hold (same as current POST flow)
    """
    # Always go to Authorise Hold after passing the skill gate
    return redirect(url_for("authorise_hold", slug=charity.slug))

@app.route("/<slug>/authorise", methods=["GET"])
def authorise_hold(slug):
    charity = get_charity_or_404(slug)

    pending = session.get("pending_entry")
    if not pending or pending.get("slug") != charity.slug:
        flash("We could not find your details. Please start again.")
        return redirect(url_for("charity_page", slug=charity.slug))

    if getattr(charity, "skill_enabled", False) and not session.get("skill_passed"):
        return redirect(url_for("skill_gate", slug=charity.slug))

    hold_pence = int(pending.get("hold_amount_pence") or 0)
    min_hold = int(charity.max_number or 0) * 100
    if hold_pence < min_hold:
        hold_pence = min_hold

    hold_gbp = int(hold_pence // 100)

    ticks_block = build_ticks_block([
        f"&pound;<strong>{hold_gbp}</strong> will be temporarily held on your card",
        "You will only be charged your <strong>ticket number amount</strong>",
        "The remaining hold is <strong>released</strong> after you confirm",
    ])

    step_current, step_total = flow_step_meta(charity, "authorise")

    body = """
    <div class="hero">
      <h1>Confirm Your Entry</h1>
      <p style="margin-top:10px;line-height:1.5;">
        We will place a temporary card authorisation to reserve your entry.
      </p>

      <p style="margin-top:8px;line-height:1.5;">
        After your number is revealed, you will be invited to
        <strong>confirm your donation</strong> in support of the charity.
        Only your donation amount is taken — any remaining authorisation
        is released automatically.
      </p>

      <div class="card" style="margin-top:14px">
        <div style="display:flex;flex-direction:column;gap:10px;font-size:14px">
          {{ ticks_block|safe }}
        </div>

        <form method="post" action="{{ url_for('start_hold', slug=charity.slug) }}" style="margin-top:14px">
          <button class="btn" type="submit">
            Continue to Card Authorisation
          </button>
        </form>

        <div class="muted" style="margin-top:10px;font-size:12px;text-align:center">
          🔒 Secured by Stripe
        </div>
      </div>

      {% if charity.optional_donation_enabled%}
      <details style="margin-top:14px">
        <summary class="pill" style="cursor:pointer;display:inline-flex;align-items:center;gap:8px">
          Free Postal Entry
        </summary>
        <div class="card" style="margin-top:10px">
          <p class="muted" style="margin:0 0 10px 0">
            A free postal entry route is available for this campaign.
            This offers the same chance of winning and does not require a donation. 
            Your entry must be received before the closing time shown on this campaign page.
          </p>

          <p style="margin:0 0 10px 0"><strong>Send your postal entry to:</strong><br>
          {{ postal_address }}</p>

          <div class="muted" style="font-size:12px;line-height:1.5">
            <strong>Postal entry terms (summary):</strong><br>
            • Include your full name, email, phone (optional), and the campaign “{{ charity.slug }}”.<br>
            • One postal entry per envelope. Multiple entries in one envelope may be rejected.<br>
            • Entries must be legible and received before the draw time/closing date.<br>
            • We will confirm receipt by email where possible.<br>
            • No purchase or donation is required for postal entries.<br></p>

            <p class="small muted">
              Postal entries are governed by our
              <a href="/terms" target="_blank" rel="noopener noreferrer">
                Terms &amp; Conditions, including Annex&nbsp;1 (Postal Entry Requirements)
              </a>.
            </p>
          </div>
        </div>
      </details>
      {% endif %}
    </div>
    """
    return render(body, charity=charity, ticks_block=ticks_block, hold_gbp=hold_gbp, postal_address=POSTAL_ENTRY_ADDRESS, step_current=step_current, step_total=step_total, flow_progress_pct=flow_progress_pct(charity, "authorise"), title="Authorise hold")

@app.route("/<slug>/start-hold", methods=["POST"])
def start_hold(slug):
    charity = get_charity_or_404(slug)

    pending = session.get("pending_entry")
    if not pending or pending.get("slug") != charity.slug:
        flash("We could not find your details. Please start again.")
        return redirect(url_for("charity_page", slug=charity.slug))

    if getattr(charity, "skill_enabled", False) and not session.get("skill_passed"):
        return redirect(url_for("skill_gate", slug=charity.slug))

    hold_amount_pence = int(pending.get("hold_amount_pence") or 0)

    # Enforce minimum hold = max_number * 100 (always)
    min_hold = int(charity.max_number or 0) * 100
    if hold_amount_pence < min_hold:
        hold_amount_pence = min_hold

    acct = (getattr(charity, "stripe_account_id", None) or "").strip()
    if not acct.startswith("acct_"):
        flash("This charity is not connected for payouts yet. Please contact support.")
        return redirect(url_for("charity_page", slug=charity.slug))

    # Create Stripe Checkout Session for the hold (manual capture)
    try:
        checkout = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            customer_email=pending.get("email") or None,
            line_items=[{
                "price_data": {
                    "currency": "gbp",
                    "product_data": {"name": f"{charity.name} – Temporary card authorisation"},
                    "unit_amount": hold_amount_pence,
                },
                "quantity": 1,
            }],
            payment_intent_data={
                "capture_method": "manual",
                "metadata": {
                    "charity_slug": charity.slug,
                    "flow": "hold_then_capture",
                }
            },
            success_url=url_for("hold_success", slug=charity.slug, _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=url_for("charity_page", slug=charity.slug, _external=True),
            stripe_account=acct,
        )
    except Exception as e:
        app.logger.error(f"Stripe session create error (start_hold): {e}")
        flash("We could not start the card authorisation. Please try again.")
        return redirect(url_for("charity_page", slug=charity.slug))

    return redirect(checkout.url)

@app.route("/<slug>/hold-success")
def hold_success(slug):
    """
    Step 1 & 2:
      - Called as success_url of Stripe Checkout Session A (the hold)
      - Verifies the PaymentIntent is authorised
      - Assigns a raffle number
      - Creates an Entry storing the PaymentIntent ID
      - Shows a page with the number and a 'Confirm & Pay' button
        that will capture from the existing hold.
    """
    charity = get_charity_or_404(slug)

    session_id = request.args.get("session_id")
    if not session_id:
        flash("Missing donation information. Please try again.")
        return redirect(url_for("charity_page", slug=charity.slug))

    # Retrieve Checkout Session AND expand the PaymentIntent
    try:
        acct = (getattr(charity, "stripe_account_id", None) or "").strip()

        checkout_session = stripe.checkout.Session.retrieve(
            session_id,
            expand=["payment_intent"],
            stripe_account=acct,
        )

    except Exception as e:
        app.logger.error(f"Stripe retrieve error (hold_success): {e}")
        flash("We could not verify your card hold. Please try again.")
        return redirect(url_for("charity_page", slug=charity.slug))

    payment_intent = checkout_session.get("payment_intent")

    # For a hold, the PaymentIntent should be authorised
    valid_statuses = ("requires_capture", "succeeded")
    if not payment_intent or payment_intent.get("status") not in valid_statuses:
        app.logger.warning(
            f"Unexpected PaymentIntent status in hold_success: "
            f"{payment_intent.get('status') if payment_intent else 'none'}"
        )
        flash("Donation not completed. Please try again.")
        return redirect(url_for("charity_page", slug=charity.slug))

    # Get the details we stored before redirecting to Stripe
    pending = session.get("pending_entry")
    if not pending or pending.get("slug") != charity.slug:
        flash("We could not find your details. Please start again.")
        return redirect(url_for("charity_page", slug=charity.slug))

    name = pending["name"]
    email = pending["email"]
    phone = pending["phone"]

    # Assign a raffle number and create the entry
    num = assign_number(charity)
    if not num:
        flash("Sorry, all numbers are taken for this charity.")
        return redirect(url_for("charity_page", slug=charity.slug))

    # Create the Entry with a UNIQUE number even under concurrency
    entry = None
    max_retries = 12  # small burst retry for “two people clicked at once” scenarios

    for _ in range(max_retries):
        num = assign_number(charity)
        if not num:
            break  # sold out
        entry = Entry(
            charity_id=charity.id,
            name=name,
            email=email,
            phone=phone,
            number=num,
            payment_intent_id=payment_intent.get("id"),
            hold_amount_pence=int(payment_intent.get("amount") or 0),
            stripe_account_id=acct,
        )
        db.session.add(entry)

        try:
            db.session.commit()
            session["reveal_entry_id"] = entry.id
            # Attach entry_id to the PaymentIntent metadata (for webhooks + reconciliation)
            try:
                stripe.PaymentIntent.modify(
                    entry.payment_intent_id,
                    metadata={
                        "entry_id": str(entry.id),
                        "charity_slug": charity.slug,
                        "flow": "hold_then_capture",
                    },
                    stripe_account=acct,
                )
            except Exception as e:
                app.logger.error(f"Failed to set PI metadata for entry {entry.id}: {e}")
            break
        except IntegrityError:
            # Another request grabbed the same number first — retry with a fresh number
            db.session.rollback()
            entry = None
            continue

    if not entry:
        # If we failed to allocate after retries, treat as sold out / very high contention
        refresh_campaign_status(charity)
        flash("Tickets are selling fast — please try again.")
        return redirect(url_for("charity_page", slug=charity.slug))

    ticks_block = build_ticks_block([
        "&pound;<strong><span id='hold-amt'></span></strong> temporarily held on your card",
        "You will donate &pound;<strong><span id='pay-amt'></span></strong>",
        "&pound;<strong><span id='release-amt'></span></strong> will be released",
    ], wrap_card=True)

    # Clean up the session data used for pending
    session.pop("pending_entry", None)

    step_current, step_total = flow_step_meta(charity, "reveal")

    # Show a page with their number and a 'Confirm & Pay' button
    body = """
     <div class="hero">
       <h1>Your Ticket Number</h1>
       <p class="muted">Press the button to reveal your ticket number.</p>
     </div>

     <div class="card" style="text-align:center;">
       <button id="reveal-btn" class="btn" type="button">Get My Number</button>
     <div id="reveal-status" class="small muted" style="display:none;">
       Revealing your ticket number…
     </div>

     <div id="wheel-zone" style="display:none; margin:18px auto 0; width:220px;">
       <div class="wheel-wrap">
         <div class="wheel-pointer"></div>
         <div class="wheel" id="wheel"></div>
         <div class="wheel-center"></div>
       </div>
       <div class="muted" style="margin-top:10px;">Spinning...</div>
     </div>

     <div id="result" style="display:none; margin-top:18px;">
       <div class="big-number" id="ticket-num"></div>

       <div class="muted" style="margin-top:6px;">
         Ticket number = <strong>&pound;<span id="ticket-val"></span></strong>
       </div>

       <div class="muted" style="margin-top:10px; line-height:1.45; font-size:14px;">
         You have been allocated number <strong><span id="nudge-num"></span></strong>.
         Completing the <strong>&pound;<span id="nudge-amt"></span></strong> donation confirms this number in support of the charity.
       </div>

       <div id="confirm-card" class="card" style="display:none; margin-top:14px;">
         <div style="font-size:14px;">
           {{ ticks_block|safe }}
         </div>

         <form method="post" action="{{ url_for('confirm_payment', entry_id=entry.id) }}" data-safe-submit style="margin-top:18px;">

           {% if charity.optional_donation_enabled %}
             <label style="display:flex; align-items:center; gap:10px; justify-content:center; flex-wrap:wrap;">
               <span>Donation amount (GBP)</span>
               <input
                 id="donation-amount"
                 name="amount_gbp"
                 type="number"
                 min="0"
                 step="1"
                 required
                 style="max-width:140px; width:140px; text-align:center;"
               >
             </label>
           {% else %}
             {# No optional donation UI — keep a hidden field so the form always submits cleanly #}
             <input type="hidden" id="donation-amount" name="amount_gbp" value="">
           {% endif %}

           {% if charity.optional_donation_enabled %}
             <div id="match-nudge" class="card" style="display:none; margin:12px auto 0; max-width:520px; padding:12px;">
               <div class="muted" style="margin:0; line-height:1.45;">
                 <div style="margin-bottom:6px;">
                   Most supporters choose to match their number in full.
                 </div>
                 <div>
                   <strong>&pound;<span id="match-amt"></span></strong> keeps your entry aligned with your number.
                 </div>
               </div>
             </div>
           {% endif %}

           {% if charity.optional_donation_enabled %}
             <div class="row" style="gap:10px; flex-wrap:wrap; margin-top:10px;">
               <button type="button" class="pill" id="btn-default">
                 Use my number (£<span id="pay-amt-2"></span>)
               </button>
             </div>
           {% endif %}

           <button class="btn" type="submit" style="width:100%; margin-top:12px;">
             {% if charity.optional_donation_enabled %}Confirm Donation{% else %}Confirm &amp; Donate{% endif %}
           </button>
         </form>
       </div>

       <script>
       (function(){
         const amount = document.getElementById('donation-amount');
         const payAmt = document.getElementById('pay-amt');        // top "Hold confirmed" pay number
         const payAmt2 = document.getElementById('pay-amt-2');     // the pill label "Use my number (£X)"
         const ticketVal = document.getElementById('ticket-val');  // revealed ticket value (£ticket)
         const holdAmt = document.getElementById('hold-amt');      // revealed hold (£max)
         const releaseAmt = document.getElementById('release-amt');// NEW: released amount
         const freeEnabled = {{ 'true' if charity.optional_donation_enabled else 'false' }};
         const nudgeNum = document.getElementById('nudge-num');
         const nudgeAmt = document.getElementById('nudge-amt');
         const matchNudge = document.getElementById('match-nudge');
         const matchAmt = document.getElementById('match-amt');

         function updateMatchNudge() {
           if (!freeEnabled) return;
           if (!matchNudge || !amount) return;

           const ticket = parseInt((ticketVal && ticketVal.textContent) || "0", 10) || 0;
           const current = parseInt(amount.value || "0", 10) || 0;

           // Show only if user reduces below their allocated number (and ticket is known)
           if (ticket > 0 && current < ticket) {
             if (matchAmt) matchAmt.textContent = String(ticket);
             matchNudge.style.display = "block";
           } else {
             matchNudge.style.display = "none";
           }
         }

         const btnDefault = document.getElementById('btn-default');
         const btnZero = document.getElementById('btn-zero');

         // Server decides whether free entry is enabled for THIS charity:
         const freeEntryEnabled = {{ 'true' if charity.optional_donation_enabled else 'false' }};

         function intOr0(x){
           const n = parseInt(String(x || '0').replace(/[^\d]/g,''), 10);
           return Number.isFinite(n) ? n : 0;
         }

         function updateReleased(){
           const held = intOr0(holdAmt && holdAmt.textContent);
           const pay  = intOr0(payAmt && payAmt.textContent);
           const rel = Math.max(0, held - pay);
           if (releaseAmt) releaseAmt.textContent = String(rel);
         }

         function setAmount(v){
           // If free entry is NOT enabled, amount must equal the ticket number and be uneditable.
           if (!freeEntryEnabled){
             const t = intOr0(ticketVal && ticketVal.textContent);
             amount.value = String(t);
             payAmt.textContent = String(t);
             // IMPORTANT: the "Use my number" pill must ALWAYS show the ticket number (not the input)
             updateReleased();
             return;
           }

           // Free entry enabled => user may edit down to £0.
           const n = Math.max(0, intOr0(v));
           amount.value = String(n);
           payAmt.textContent = String(n);

           // IMPORTANT: keep the pill label tied to the ticket number, not the current input
           const t = intOr0(ticketVal && ticketVal.textContent);

           updateReleased();
         }

         // Once reveal fills ticket/hold, seed the input appropriately
         const observer = new MutationObserver(() => {
           const t = intOr0(ticketVal && ticketVal.textContent);
           if (!amount.value) setAmount(t);

           // lock the input if no free entry
           if (!freeEntryEnabled){
             amount.readOnly = true;
             amount.setAttribute('aria-readonly', 'true');
             // hide the £0 button if it exists
             if (btnZero) btnZero.style.display = 'none';
           } else {
             amount.readOnly = false;
             if (btnZero) btnZero.style.display = '';
           }

           updateReleased();
         });
         observer.observe(ticketVal, { childList:true, subtree:true });

         if (freeEnabled && amount) {
           amount.addEventListener("input", () => {
             updateMatchNudge();
           });
         }

         // Events
         if (freeEntryEnabled){
           if (amount) amount.addEventListener('input', () => setAmount(amount.value));
           btnZero && btnZero.addEventListener('click', () => setAmount(0));
         }

         // "Use my number" should always set the amount to the TICKET NUMBER
         if (btnDefault) btnDefault.addEventListener('click', () => setAmount(ticketVal.textContent));
         if (btnZero) btnZero.addEventListener('click', () => setAmount(0));

       })();
       </script>
     </div>
   </div>

   <script>
   (function() {
     const entryId = {{ entry.id|int }};
     const btn = document.getElementById("reveal-btn");
     const zone = document.getElementById("wheel-zone");
     const wheel = document.getElementById("wheel");
     const result = document.getElementById("result");
     const nudgeNum = document.getElementById("nudge-num");
     const nudgeAmt = document.getElementById("nudge-amt");
     const amount   = document.getElementById("amount"); // if present on this page

     function spinTo(deg) {
       wheel.classList.remove("wheel-spinning");
       void wheel.offsetWidth;
       wheel.style.transition = "transform 3.2s cubic-bezier(0.12, 0.75, 0.18, 1)";
       wheel.style.transform = `rotate(${(360 * 7) + deg}deg)`;
     }

     function setText(id, value) {
       const el = document.getElementById(id);
       if (el) el.textContent = value;
     }

     function hideStatus() {
       const status = document.getElementById("reveal-status");
       if (status) status.style.display = "none";
     }

     function showStatus() {
       const status = document.getElementById("reveal-status");
       if (status) status.style.display = "block";
     }

     // If any core UI bits are missing, don't attach a broken handler
     if (!btn) return;
     if (!zone || !wheel) {
       console.error("Reveal UI missing:", { zone, wheel });
       return;
     }

     let revealLocked = false;

     btn.addEventListener("click", async () => {
       // Prevent double-trigger
       if (revealLocked) return;
       revealLocked = true;

       // If already revealed, remove the button and ensure status is hidden
       const ticketNumEl = document.getElementById("ticket-num");
       if (ticketNumEl && ticketNumEl.textContent && ticketNumEl.textContent.trim() !== "") {
         btn.remove();
         hideStatus();
         return;
       }

       // Remove button immediately on click (your requirement)
       showStatus();
       btn.remove();

       // Show spinner / wheel
       zone.style.display = "block";
       wheel.style.transition = "none";
       wheel.style.transform = "rotate(0deg)";
       void wheel.offsetWidth;
       wheel.classList.add("wheel-spinning");

       let data = null;

       const controller = new AbortController();
       const timeoutMs = 12000;
       const t = setTimeout(() => controller.abort(), timeoutMs);

       try {
         const resp = await fetch(`/api/reveal-number/${entryId}`, {
           credentials: "same-origin",
           signal: controller.signal
         });

         if (!resp.ok) {
           throw new Error(`Server error (${resp.status})`);
         }

         data = await resp.json();
         if (!data.ok) throw new Error(data.error || "Could not reveal number");

       } catch (e) {
         clearTimeout(t);

         wheel.classList.remove("wheel-spinning");
         zone.style.display = "none";
         hideStatus();
         revealLocked = false;

         alert(e.name === "AbortError"
           ? "This is taking longer than expected. Please try again."
           : (e.message || "Could not reveal number.")
         );
         return;

       } finally {
         clearTimeout(t);
       }

       // Stop continuous spin and land
       wheel.classList.remove("wheel-spinning");
       const segments = 36;                 // keep in sync with wheel labels
       const step = 360 / segments;         // 10 degrees
       const base = (data.ticket_number * 23) % 360;

       // snap to nearest segment, then land in the middle of it
       const snapped = Math.round(base / step) * step;
       const landing = snapped + (step / 2);

       requestAnimationFrame(() => spinTo(landing));

       // Reveal after landing finishes
       setTimeout(() => {
         try {
           zone.style.display = "none";
           hideStatus(); // ✅ this removes “Revealing your number” after success

           setText("ticket-num", data.ticket_number);
           setText("ticket-val", data.ticket_value);
           setText("hold-amt", data.hold_amount);
           setText("pay-amt", data.ticket_value);
           setText("pay-amt-2", data.ticket_value);

           if (nudgeNum) nudgeNum.textContent = String(data.ticket_number);
           if (nudgeAmt) nudgeAmt.textContent = String(data.ticket_value);

           if (amount) amount.value = String(data.ticket_value);

           if (typeof updateMatchNudge === "function") {
             updateMatchNudge();
           }

           if (result) result.style.display = "block";

           const confirmCard = document.getElementById("confirm-card");
           if (confirmCard) confirmCard.style.display = "block";
         } catch (err) {
           console.error("Reveal render failed:", err);
           zone.style.display = "none";
           hideStatus();
           if (result) result.style.display = "block";
           const confirmCard = document.getElementById("confirm-card");
           if (confirmCard) confirmCard.style.display = "block";
         }
       }, 3400);
     });
   })();
   </script>
   """
    return render(
        body,
        charity=charity,
        entry=entry,
        name=name,
        ticks_block=ticks_block,
        step_current=step_current,
        flow_progress_pct=flow_progress_pct(charity, "reveal"),
        step_total=step_total,
        title="Hold Confirmed",
    )

@app.get("/api/reveal-number/<int:entry_id>")
def api_reveal_number(entry_id):
    # Only allow reveal for the entry created in THIS browser session
    if session.get("reveal_entry_id") != entry_id:
        return jsonify({"ok": False, "error": "Not authorised"}), 403

    # Block repeat reveals (prevents spamming / changing number attempts)
    if session.get("revealed_entry_id") == entry_id:
        return jsonify({"ok": False, "error": "Number already revealed"}), 409

    e = Entry.query.get_or_404(entry_id)

    session["revealed_entry_id"] = entry_id

    return jsonify({
        "ok": True,
        "ticket_number": int(e.number),
        "ticket_value": int(e.number),
        "hold_amount": int((e.hold_amount_pence or HOLD_AMOUNT_PENCE) // 100),
    })

@app.route("/confirm-payment/<int:entry_id>", methods=["POST"])
def confirm_payment(entry_id):
    """
    Called when user clicks 'Confirm & Donate' after their number is assigned.

    - Captures part of the original PaymentIntent (the hold),
      equal to the raffle number in pounds.
    - The rest of the authorised amount is released by the bank.
    - Then fetches the related Charge from Stripe to get a receipt_url.
    """
    entry = Entry.query.get_or_404(entry_id)
    charity = Charity.query.get_or_404(entry.charity_id)
    held = int(entry.hold_amount_pence or 0)
    optional_ok = bool(getattr(charity, "optional_donation_enabled", False))

    acct = (getattr(entry, "stripe_account_id", None) or getattr(charity, "stripe_account_id", None) or "").strip()
    if not acct.startswith("acct_"):
        flash("This donation cannot be processed because the charity payout account is missing.")
        return redirect(url_for("charity_page", slug=charity.slug))

    if entry.paid:
        flash("This entry is already marked as paid. Thank you!")
        return redirect(url_for("charity_page", slug=charity.slug))

    if not entry.payment_intent_id:
        flash("We could not find the original card authorisation. Please try again.")
        return redirect(url_for("charity_page", slug=charity.slug))

    # Amount is user-confirmed only if optional donations are enabled.
    # Otherwise we force the capture amount to the ticket number.
    if not optional_ok:
        amount_gbp = int(entry.number or 0)
    else:
        raw = (request.form.get("amount_gbp", "") or "").strip()
        try:
            amount_gbp = int(raw)
        except ValueError:
            amount_gbp = -1

    amount_pence = amount_gbp * 100
    paid_gbp = amount_gbp

    if (not optional_ok) and amount_gbp < 1:
        flash("This campaign requires a minimum donation of £1.")
        return redirect(url_for("hold_success", slug=charity.slug))

    paid_gbp = int(amount_pence // 100)

    # Total held amount (pence) – this MUST exist before we use it
    held_pence = int(entry.hold_amount_pence or HOLD_AMOUNT_PENCE or 0)
    held_gbp = int(held_pence // 100)

    released_gbp = max(0, held_gbp - paid_gbp)

    max_gbp = int((entry.hold_amount_pence or 0) // 100)
    if amount_gbp > max_gbp:
        flash(f"Donation cannot exceed £{max_gbp}.")
        return redirect(url_for("hold_success", slug=charity.slug))

    # Basic safety checks: positive and within the held amount
    held = int(entry.hold_amount_pence or 0)
    if held <= 0:
        held = HOLD_AMOUNT_PENCE  # fallback

    if amount_pence < 0 or amount_pence > held:
        app.logger.error(f"Invalid amount_to_capture for entry {entry.id}: {amount_pence} (held={held})")
        flash("Please enter a valid amount (0 or more).")
        return redirect(url_for("hold_success", slug=charity.slug))
    # If £0 donation (only allowed when optional donations are enabled),
    # cancel the PaymentIntent to release the authorisation
    if optional_ok and amount_pence == 0:
        try:
            stripe.PaymentIntent.cancel(
                entry.payment_intent_id,
                stripe_account=acct,  # IMPORTANT: this PaymentIntent lives on the connected account
            )
            entry.paid = True
            entry.paid_at = datetime.utcnow()
            db.session.commit()
            flash("Entry confirmed with no donation. Thank you!")
            return redirect(url_for("charity_page", slug=charity.slug))
        except Exception as e:
            app.logger.exception(e)
            flash("We couldn't release the hold automatically. Please contact support.")
            return redirect(url_for("charity_page", slug=charity.slug))

        app.logger.error(f"Invalid amount_to_capture for entry {entry.id}: {amount_pence} (held={held})")
        flash("Something went wrong with your raffle amount. Please contact us.")
        return redirect(url_for("charity_page", slug=charity.slug))

    # 1) Capture from the original hold
    try:
        captured_pi = stripe.PaymentIntent.capture(
            entry.payment_intent_id,
            amount_to_capture=amount_pence,
            stripe_account=acct,
        )
    except Exception as e:
        app.logger.error(
            f"Error capturing PaymentIntent {entry.payment_intent_id}: {e}"
        )
        flash(
            "We authorised your card but could not complete the charge. "
            "Please contact us or try again."
        )
        return redirect(url_for("charity_page", slug=charity.slug))

    # 2) Fetch the Charge explicitly, then get receipt_url
    receipt_url = None
    try:
        charges = stripe.Charge.list(
            payment_intent=entry.payment_intent_id,
            limit=1,
            stripe_account=acct,
        )
        if charges.data:
            charge = charges.data[0]
            receipt_url = charge.get("receipt_url") or getattr(charge, "receipt_url", None)
            app.logger.info(f"Stripe receipt_url for entry {entry.id}: {receipt_url}")
    except Exception as e:
        app.logger.warning(
            f"Could not fetch charge/receipt_url for entry {entry.id}: {e}"
        )

    # 3) Mark entry as paid in your DB
    entry.paid = True
    entry.paid_at = datetime.utcnow()
    db.session.commit()

    # 4) Final confirmation page with optional Stripe receipt button
        # 4) Final confirmation page with optional Stripe receipt button

    ticks_block_final = build_ticks_block([
        f"Paid &pound;<strong>{paid_gbp}</strong> to <strong>{charity.name}</strong>",
        f"&pound;<strong>{held_gbp}</strong> was temporarily held",
        f"&pound;<strong>{released_gbp}</strong> will be released",
    ], wrap_card=True)

    step_current, step_total = flow_step_meta(charity, "confirmed")
   
    body = """
    <div class="hero">
      <h1>Confirmed Donation</h1>
      <p class="muted">You’re all set — Good luck!</p>
    </div>

    <div class="card" style="padding:18px; max-width:720px; margin:0 auto;">
      <div style="font-weight:800; font-size:14px; margin-bottom:10px;">
        Donation Confirmed
      </div>

      <div style="font-size:14px;">
        {{ ticks_block_final|safe }}
      </div>

      {% if receipt_url %}
        <div class="row" style="margin-top:14px; gap:10px; justify-content:flex-start;">
          <form action="{{ receipt_url }}" method="GET" target="_blank" style="margin:0;">
            <button class="btn" type="submit">View Receipt</button>
          </form>
        </div>
      {% endif %}

      <div class="row" style="margin-top:14px; gap:10px; justify-content:center;">
        <a class="btn pill outline" href="{{ url_for('charity_page', slug=charity.slug) }}">Back to Campaign</a>
        <a class="btn pill outline" href="{{ url_for('home') }}">Back to Home</a>
      </div>
    </div>

    <!-- Optional confetti (keep it, but make it subtle) -->
    <canvas id="confetti-canvas"
            style="position:fixed; inset:0; pointer-events:none; z-index:9999;"></canvas>

    <script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.9.2/dist/confetti.browser.min.js"></script>
    <script>
      (function(){
        const canvas = document.getElementById('confetti-canvas');
        const myConfetti = confetti.create(canvas, { resize: true, useWorker: true });
        const end = Date.now() + 1200; // short + subtle

        (function frame() {
          myConfetti({ particleCount: 4, spread: 65, startVelocity: 28, origin: { x: Math.random(), y: 0 } });
          if (Date.now() < end) requestAnimationFrame(frame);
        })();
      })();
    </script>
    """
    return render(
        body,
        charity=charity,
        entry=entry,
        receipt_url=receipt_url,
        paid_gbp=paid_gbp,
        held_gbp=held_gbp,
        released_gbp=released_gbp,
        ticks_block_final=ticks_block_final,
        step_current=step_current,
        step_total=step_total,
        flow_progress_pct=flow_progress_pct(charity, "confirmed"),
        title=f"{charity.name} – Thank you",
    )

@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    # For Connect webhooks, Stripe will include this header for events from connected accounts
    connected_acct = request.headers.get("Stripe-Account", None)

    try:
        if webhook_secret:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        else:
            # Not recommended for live: allows unsigned events
            event = json.loads(payload)
    except Exception as e:
        app.logger.exception(e)
        return ("Bad webhook signature", 400)

    event_type = event["type"]
    obj = event["data"]["object"]

    # --- Payment succeeded (after capture) ---
    if event_type == "payment_intent.succeeded":
        try:
            md = obj.get("metadata", {}) or {}
            entry_id = md.get("entry_id")
            if entry_id:
                entry = Entry.query.get(int(entry_id))
                if entry and not entry.paid:
                    entry.paid = True
                    entry.paid_at = datetime.utcnow()

                    # Best effort: fetch receipt_url
                    receipt_url = None
                    try:
                        acct = connected_acct or getattr(entry, "stripe_account_id", None)
                        charges = stripe.Charge.list(
                            payment_intent=obj.get("id"),
                            limit=1,
                            stripe_account=acct if acct else None,
                        )
                        if charges.data:
                            receipt_url = charges.data[0].get("receipt_url")
                    except Exception:
                        receipt_url = None

                    if receipt_url:
                        entry.receipt_url = receipt_url  # only if you have this column
                    db.session.commit()
        except Exception as e:
            app.logger.exception(e)

    # --- Optional: payment failed ---
    elif event_type == "payment_intent.payment_failed":
        # You can log / notify if you want
        pass

    # --- Optional: checkout completed (authorisation done) ---
    elif event_type == "checkout.session.completed":
        # Typically not needed for you because you already handle hold_success()
        pass

    return ("OK", 200)

@app.route("/<slug>/success")
def success(slug):
    charity = get_charity_or_404(slug)

    charity_logo = getattr(charity, "logo_data", None) 

    if session.get("last_slug") != charity.slug or "last_num" not in session:
        return redirect(url_for("charity_page", slug=charity.slug))
    num = session.get("last_num"); name = session.get("last_name", "Friend")

    body = """
    <div class="hero">
      <h1>Thank you{{ ", %s" % name if name else "" }} 🎉</h1>
      <p>Your raffle number for <strong>{{ charity.name }}</strong> is:</p>
      <h2 style="margin:10px 0;"><span class="badge" style="font-size:22px">{{ num }}</span></h2>
      <p class="muted">Please donate <strong>£{{ num }}</strong> to complete your entry.</p>
      <div class="row" style="margin-top:12px">
        <a class="btn" href="{{ charity.donation_url }}" target="_blank" rel="noopener">Go to donation page</a>
        <button class="pill" onclick="navigator.clipboard.writeText('{{ num }}').then(()=>alert('Amount copied to clipboard'))">Copy amount ({{ num }})</button>
      </div>
    </div>
    """
    return render(body, charity=charity, num=num, name=name, flow_progress_pct=flow_progress_pct(charity, "confirmed"), title=charity.name)

# ====== ADMIN (env guarded) ===================================================

@app.route("/admin/charities", methods=["GET","POST"])
def admin_charities():
    admin_user = os.getenv("ADMIN_USERNAME", "admin")
    admin_pw   = os.getenv("ADMIN_PASSWORD", "")
    ok = session.get("admin_ok", False)
    last_login = session.get("admin_login_time")
    msg = None

    # Expire admin session if it's older than permanent_session_lifetime
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
            # Handle admin login
            submitted_user = request.form.get("username", "")
            submitted_pw   = request.form.get("password", "")

            # If you have env vars set, use those; otherwise fall back to "admin"/"admin"
            effective_user = admin_user or "admin"
            effective_pw   = admin_pw or "admin"

            if submitted_user == effective_user and submitted_pw == effective_pw:
                session.permanent = True
                session["admin_ok"] = True
                session["admin_login_time"] = datetime.utcnow().isoformat()
                ok = True
                flash("Logged in successfully.")
            else:
                msg = "Invalid username or password."
        else:
            # Already logged in – handle creating/saving a charity
            slug = request.form.get("slug", "").strip().lower()
            name = request.form.get("name", "").strip()
            url  = request.form.get("donation_url", "").strip()
            try:
                maxn = int(request.form.get("max_number", "500") or 500)
            except ValueError:
                maxn = 500

            # New: optional draw date/time
            draw_at = None
            draw_raw = request.form.get("draw_at", "").strip()
            if draw_raw:
                try:
                    draw_at = datetime.fromisoformat(draw_raw)
                except ValueError:
                    msg = "Invalid draw date/time."
            logo_data = None
            f = request.files.get("logo_file")
            if f and f.filename:
                raw = f.read()
                if raw:
                    mime = f.mimetype or "image/png"
                    b64 = base64.b64encode(raw).decode("ascii")
                    logo_data = f"data:{mime};base64,{b64}"

            poster_data = None
            pf = request.files.get("poster_file")
            if pf and pf.filename:
                raw = pf.read()
                if raw:
                    mime = pf.mimetype or "image/png"
                    b64 = base64.b64encode(raw).decode("ascii")
                    poster_data = f"data:{mime};base64,{b64}"

            tile_about = (request.form.get("tile_about") or "").strip()

            raw_prizes = (request.form.get("prizes") or "").strip()
            prizes_list = _parse_prizes(raw_prizes)
            prizes_json = json.dumps(prizes_list) if prizes_list else None

            if not slug or not name or not url:
                msg = "All fields are required."
            existing = Charity.query.filter_by(slug=slug).first()
            if existing:
                existing.name = name
                existing.donation_url = url
                existing.max_number = maxn
                existing.draw_at = draw_at
                if logo_data:
                    existing.logo_data = logo_data
                if poster_data:
                    existing.poster_data = poster_data
                existing.tile_about = tile_about
                existing.prizes_json = prizes_json
                db.session.commit()
                msg = f"Updated. Public page: /{slug}"

            elif msg:
                pass
            else:
                c = Charity(
                    slug=slug,
                    name=name,
                    donation_url=url,
                    max_number=maxn,
                    draw_at=draw_at,
                    logo_data=logo_data,
                    poster_data=poster_data,
                    tile_about=tile_about,
                    prizes_json=prizes_json,
                )
                db.session.add(c)
                db.session.commit()
                msg = f"Saved. Public page: /{slug}"

    charities = Charity.query.order_by(Charity.name.asc()).all()
    remaining = {c.id: len(available_numbers(c)) for c in charities}
    connect_status = {}
    for c in charities:
        acct = (getattr(c, "stripe_account_id", None) or "").strip()
        if acct.startswith("acct_"):
            connect_status[c.id] = get_connect_status(acct)
        else:
            connect_status[c.id] = {"ok": False}

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
        <form method="post" enctype="multipart/form-data" style="margin-bottom:12px">
        <label>Slug <input type="text" name="slug" placeholder="thekehilla" required></label>
        <label>Name <input type="text" name="name" placeholder="The Kehilla" required></label>
        <label>Donation URL <input type="url" name="donation_url" placeholder="https://www.charityextra.com/charity/kehilla" required></label>
        <label>Max number <input type="number" name="max_number" value="500" min="1"></label>
        <label>Draw date &amp; time (optional)
          <input type="datetime-local" name="draw_at">
        </label>
        <label>Tile image / logo (optional)
          <input type="file" name="logo_file" accept="image/*">
        </label>
       <label>Campaign poster (optional)
         <input type="file" name="poster_file" accept="image/*">
       </label>

        <label>Short “About” (homepage tile)
          <textarea name="tile_about" rows="3" placeholder="1–2 sentences about this cause..."></textarea>
        </label>

        <label>Prizes (one per line)
          <textarea name="prizes" rows="4" placeholder="Prize 1&#10;Prize 2&#10;Prize 3"></textarea>
        </label>
        <div style="margin-top:8px"><button class="btn">Add / Save</button></div>
      </form>
      <table>
        <thead><tr><th>Slug</th><th>Name</th><th>Status</th><th>Max</th><th>Remaining</th><th>Actions</th></tr></thead>
        <tbody>
          {% for c in charities %}
            <tr>
              <td>{{ c.slug }}</td>
              <td>{{ c.name }}</td>
              <td>
                {% set st = (c.campaign_status or 'live') %}
                {% if st == 'live' %}
                  <span class="badge ok">LIVE</span>
                {% elif st == 'sold_out' %}
                  <span class="badge warn">SOLD OUT</span>
                {% elif st == 'coming_soon' %}
                  <span class="badge warn">COMING SOON</span>
                {% else %}
                  <span class="badge warn">INACTIVE</span>
                {% endif %}

              </td>
              <td>{{ c.max_number }}</td>
              <td>{{ remaining[c.id] }}</td>
              <td>
                <a class="pill" href="{{ url_for('charity_page', slug=c.slug) }}">Open</a>
                <a class="pill" href="{{ url_for('edit_charity', slug=c.slug) }}">Edit</a>
                <a class="pill" href="{{ url_for('admin_charity_entries', slug=c.slug) }}">Entries</a>
                <a class="pill" href="{{ url_for('admin_charity_users', slug=c.slug) }}">Users</a>
                {# Stripe Connect status + onboarding button #}
                {% set acct = (c.stripe_account_id or '') %}
                {% if acct.startswith('acct_') %}
                  <span class="badge ok" style="margin-left:6px">STRIPE CONNECTED</span>
                  <form method="post" action="{{ url_for('admin_connect_stripe', slug=c.slug) }}"
                        style="display:inline" title="Re-open onboarding if the charity needs to update details">
                    <button class="pill" type="submit">Re-open Stripe</button>
                  </form>
                {% set cs = connect_status.get(c.id) %}
                {% if cs and cs.ok %}
                  {% if cs.charges_enabled and cs.payouts_enabled %}
                       <span class="badge ok" style="margin-left:6px">READY</span>
                  {% else %}
                       <span class="badge warn" style="margin-left:6px">NEEDS INFO</span>
                  {% endif %}
                {% endif %}
                {% else %}
                  <form method="post" action="{{ url_for('admin_connect_stripe', slug=c.slug) }}"
                        style="display:inline">
                    <button class="pill" type="submit">Connect Stripe</button>
                  </form>
                {% endif %}
                <form method="post" action="{{ url_for('admin_delete_charity', slug=c.slug) }}"
                      style="display:inline" onsubmit="return confirm('Delete this campaign and all its entries/users? This cannot be undone.');">
                  <button class="pill" type="submit">Delete</button>
                </form>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% endif %}
    """
    return render(
        body,
        ok=ok,
        msg=msg,
        charities=charities,
        remaining=remaining,
        connect_status=connect_status,
        flow_progress_pct=None,
        step_current=None,
        step_total=None,
        title="Manage Charities",
    )

@app.route("/admin/charity/<slug>/connect-stripe", methods=["POST"])
def admin_connect_stripe(slug):
    if not session.get("admin_ok"):
        return redirect(url_for("admin_charities"))

    charity = Charity.query.filter_by(slug=slug).first_or_404()

    # 1) Create connected account if it doesn't exist
    if not charity.stripe_account_id:
        acct = stripe.Account.create(
            type="express",
            country="GB",
            capabilities={
                "card_payments": {"requested": True},
                "transfers": {"requested": True},
            },
            metadata={
                "charity_slug": charity.slug,
                "charity_name": charity.name,
            },
        )
        charity.stripe_account_id = acct["id"]
        db.session.commit()

    # 2) Generate Stripe onboarding link
    refresh_url = url_for(
        "edit_charity",
        slug=charity.slug,
        _external=True
    ) + "?stripe=refresh"

    return_url = url_for(
        "edit_charity",
        slug=charity.slug,
        _external=True
    ) + "?stripe=return"

    link = stripe.AccountLink.create(
        account=charity.stripe_account_id,
        refresh_url=refresh_url,
        return_url=return_url,
        type="account_onboarding",
    )

    return redirect(link["url"])


@app.post("/admin/charity/<slug>/status")
def admin_set_campaign_status(slug):
    if not session.get("admin_ok"):
        return redirect(url_for("admin_charities"))

    charity = get_charity_or_404(slug)
    new_status = (request.form.get("status") or "").strip()

    if new_status not in ("live", "inactive", "sold_out", "coming_soon"):
        flash("Invalid status.")
        return redirect(url_for("edit_charity", slug=slug))

    # Hard safety: do not allow campaign to go LIVE unless Stripe Connect is set
    if new_status == "live":
        acct = (getattr(charity, "stripe_account_id", None) or "").strip()
        if not acct.startswith("acct_"):
            flash("Cannot set LIVE: this charity is not connected to Stripe for payouts yet.")
            return redirect(url_for("edit_charity", slug=slug))

    charity.campaign_status = new_status

    # Keep legacy boolean in sync (prevents homepage confusion if any code still checks is_live)
    charity.is_live = (new_status == "live")

    db.session.commit()

    flash(f"Status set to: {new_status.replace('_',' ')}")
    return redirect(url_for("edit_charity", slug=slug))

@app.post("/admin/charities/<slug>/toggle-live")
def admin_toggle_charity_live(slug):
    if not session.get("admin_ok"):
        return redirect(url_for("admin_charities"))

    c = Charity.query.filter_by(slug=slug).first()
    if not c:
        flash("Charity not found.")
        return redirect(url_for("admin_charities"))

    c.is_live = not getattr(c, "is_live", True)
    db.session.commit()
    flash(f"Campaign '{c.slug}' is now {'LIVE' if c.is_live else 'INACTIVE'}.")
    return redirect(url_for("admin_charities"))


@app.post("/admin/charities/<slug>/delete")
def admin_delete_charity(slug):
    if not session.get("admin_ok"):
        return redirect(url_for("admin_charities"))

    c = Charity.query.filter_by(slug=slug).first()
    if not c:
        flash("Charity not found.")
        return redirect(url_for("admin_charities"))

    # Delete dependent rows first (no cascade defined)
    Entry.query.filter_by(charity_id=c.id).delete(synchronize_session=False)
    CharityUser.query.filter_by(charity_id=c.id).delete(synchronize_session=False)

    db.session.delete(c)
    db.session.commit()
    flash(f"Deleted campaign '{slug}'.")
    return redirect(url_for("admin_charities"))

@app.route("/admin/logout")
def admin_logout():
    session.clear(); flash("Logged out.")
    return redirect(url_for("admin_charities"))

@app.route("/admin/charity/<slug>", methods=["GET","POST"])
def edit_charity(slug):
    if not session.get("admin_ok"): return redirect(url_for("admin_charities"))
    charity = get_charity_or_404(slug)
    msg = None

    if request.method == "POST":
        charity.name = request.form.get("name", charity.name).strip()
        charity.donation_url = request.form.get("donation_url", charity.donation_url).strip() or None
        # Stripe Connect account (acct_...)
        stripe_acct = (request.form.get("stripe_account_id") or "").strip()
        charity.stripe_account_id = stripe_acct or None


        # Optional: replace logo if a new one is uploaded
        f = request.files.get("logo_file")
        if f and f.filename:
            raw = f.read()
            if raw:
                mime = f.mimetype or "image/png"
                b64 = base64.b64encode(raw).decode("ascii")
                charity.logo_data = f"data:{mime};base64,{b64}"
        # Optional: upload campaign poster (stored as data URI)
        pf = request.files.get("poster_file")
        if pf and pf.filename:
            raw = pf.read()
            if raw:
                mime = pf.mimetype or "image/png"
                b64 = base64.b64encode(raw).decode("ascii")
                charity.poster_data = f"data:{mime};base64,{b64}"
        charity.tile_about = (request.form.get("tile_about") or "").strip()

        raw_prizes = (request.form.get("prizes") or "").strip()
        prizes_list = _parse_prizes(raw_prizes)
        charity.prizes_json = json.dumps(prizes_list) if prizes_list else None

        # New: update draw_at
        draw_raw = request.form.get("draw_at", "").strip()
        if draw_raw:
            try:
                charity.draw_at = datetime.fromisoformat(draw_raw)
            except ValueError:
                msg = "Invalid draw date/time format."
        else:
            charity.draw_at = None

                # --- numbers / toggles (set everything first, commit once) ---
        try:
            charity.max_number = int(request.form.get("max_number", charity.max_number))
        except ValueError:
            msg = "Invalid number format."

        # ===== Skill-based question config =====
        charity.skill_enabled = bool(request.form.get("skill_enabled"))

        charity.skill_question = (request.form.get("skill_question") or "").strip()

        # answers come from textarea; store as JSON array string
        raw_answers = (request.form.get("skill_answers") or "").strip()
        answers = _parse_skill_answers(raw_answers)
        charity.skill_answers_json = json.dumps(answers)

        charity.skill_correct_answer = (request.form.get("skill_correct_answer") or "").strip()

        # display count
        try:
            charity.skill_display_count = int(request.form.get("skill_display_count") or 4)
        except ValueError:
            charity.skill_display_count = 4

        # Optional: upload skill image (stored as data URI)
        sf = request.files.get("skill_image_file")
        if sf and sf.filename:
            raw = sf.read()
            if raw:
                mime = sf.mimetype or "image/png"
                b64 = base64.b64encode(raw).decode("ascii")
                charity.skill_image_data = f"data:{mime};base64,{b64}"

        # Validation: if enabled, correct answer must match one of the answers (case-insensitive)
        if charity.skill_enabled:
            if charity.skill_correct_answer and answers:
                bank_lower = {a.lower() for a in answers}
                if charity.skill_correct_answer.lower() not in bank_lower:
                    msg = "Skill question: the correct answer must exactly match one of the answers you entered."

        charity.is_live = bool(request.form.get("is_live"))
        charity.is_sold_out = bool(request.form.get("is_sold_out"))
        charity.is_coming_soon = bool(request.form.get("is_coming_soon"))
        charity.free_entry_enabled = bool(request.form.get("free_entry_enabled"))
        charity.postal_entry_enabled = bool(request.form.get("postal_entry_enabled"))
        charity.optional_donation_enabled = bool(request.form.get("optional_donation_enabled"))

        try:
            raw_hold = int(request.form.get("hold_amount_pence", charity.hold_amount_pence) or charity.hold_amount_pence)
            min_hold_pence = int(charity.max_number or 0) * 100
            charity.hold_amount_pence = max(raw_hold, min_hold_pence)
        except ValueError:
            msg = "Invalid hold amount."

        # Commit at the end so toggles persist
        if not msg:
            db.session.commit()
            msg = "Charity updated successfully."

    # Pre-populate datetime-local value
    draw_value = charity.draw_at.strftime("%Y-%m-%dT%H:%M") if charity.draw_at else ""
    min_hold_pence = int(charity.max_number or 0) * 100
    min_hold_gbp = int(min_hold_pence // 100)
    current_hold_gbp = int((int(getattr(charity, "hold_amount_pence", 0) or 0)) // 100)

    body = """
    <h2>Edit Charity</h2>
    {% if msg %}<div style="margin:6px 0;color:#ffd29f">{{ msg }}</div>{% endif %}
    <form method="post" enctype="multipart/form-data" data-safe-submit>
      <label>Name <input type="text" name="name" value="{{ charity.name }}" required></label>
      <label>
        Donation URL <span class="muted">(optional)</span>
        <input name="donation_url" value="{{ charity.donation_url or '' }}">
      </label>
      <label>Stripe Connected Account ID (acct_...)
        <input type="text" name="stripe_account_id" value="{{ charity.stripe_account_id or '' }}" placeholder="acct_123...">
        <small class="muted">This must match the connected charity account in Stripe Connect.</small>
      </label>
      <label>Max number <input type="number" name="max_number" value="{{ charity.max_number }}" min="1"></label>
      <label>Draw date &amp; time (optional)
        <input type="datetime-local" name="draw_at" value="{{ draw_value }}">
      </label>
      <label>Tile image / logo (optional)
        <input type="file" name="logo_file" accept="image/*">
      </label>

      <label>Campaign poster (optional)
        <input type="file" name="poster_file" accept="image/*">
      </label>

      {% if charity.poster_data %}
        <div class="muted" style="margin-top:6px;font-size:12px;">Current poster preview:</div>
        <img src="{{ charity.poster_data }}" alt="Poster preview"
             style="width:140px;height:86px;object-fit:cover;border-radius:12px;border:1px solid var(--border);display:block;margin-top:8px;">
      {% endif %}

      <label>Short “About” (homepage tile)
        <textarea name="tile_about" rows="3" placeholder="1–2 sentences about this cause...">{{ charity.tile_about or "" }}</textarea>
      </label>

      <label>Prizes (one per line)
        <textarea name="prizes" rows="5" placeholder="Prize 1&#10;Prize 2&#10;Prize 3">{{ prizes_text or "" }}</textarea>
      </label>

      <label>Hold amount (pence)
        <input
          type="number"
          name="hold_amount_pence"
          value="{{ charity.hold_amount_pence }}"
          min="{{ min_hold_pence }}"
          step="100"
        >
        <div class="muted" style="font-size:12px;margin-top:6px;line-height:1.4">
          Minimum enforced hold: <strong>£{{ min_hold_gbp }}</strong> ({{ min_hold_pence }}p) — this ensures the temporary Stripe authorisation is always at least the maximum possible ticket value.
        </div>
      </label>

      <label style="display:flex;gap:10px;align-items:center;margin-top:6px">
        <input type="checkbox" name="is_sold_out" {% if charity.is_sold_out %}checked{% endif %}>
        Sold out (shows banner, disables form)
      </label>

      <label style="display:flex;gap:10px;align-items:center;margin-top:6px">
        <input type="checkbox" name="is_coming_soon" {% if charity.is_coming_soon %}checked{% endif %}>
        Coming soon (shows banner, disables form)
      </label>

      <label style="display:flex;gap:10px;align-items:center;margin-top:6px">
        <input type="checkbox" name="optional_donation_enabled" {% if charity.optional_donation_enabled %}checked{% endif %}>
        Free entry available (optional donation)
      </label>

      <label style="display:flex;gap:10px;align-items:center;margin-top:6px">
        <input type="checkbox" name="postal_entry_enabled" {% if charity.postal_entry_enabled %}checked{% endif %}>
        Free postal entry available
      </label>

      <label style="display:flex;gap:10px;align-items:center;margin-top:6px">
        <input type="checkbox" name="optional_donation_enabled" {% if charity.optional_donation_enabled %}checked{% endif %}>
        Optional donation amount (allow £0 / editable donation)
      </label>

      <h3 style="margin-top:18px;">Stripe Connect (where donations get paid)</h3>

      <label style="margin-top:10px;display:block">
        Charity’s Stripe Connected Account ID
        <input
          type="text"
          name="stripe_account_id"
          value="{{ charity.stripe_account_id or '' }}"
          placeholder="acct_1234..."
        >
        <div class="muted" style="font-size:12px;margin-top:6px;line-height:1.4">
          This should be the charity’s Stripe Connect account ID (starts with <code>acct_</code>).
          Your code will use this to route captured donations to the correct charity.
        </div>
      </label>

      {% if charity.stripe_account_id %}
        <p class="muted" style="margin-top:8px">
          Currently set to: <strong>{{ charity.stripe_account_id }}</strong>
        </p>
      {% endif %}

      <h3 style="margin-top:18px;">Skill-Based Question</h3>

      <label style="display:flex;gap:8px;align-items:center;margin-top:8px">
        <input type="checkbox" name="skill_enabled" {% if charity.skill_enabled %}checked{% endif %}>
        Enable skill question before entry proceeds
      </label>

      <label style="margin-top:10px;display:block">
        Question text
        <textarea name="skill_question" rows="3" placeholder="Type your question here...">{% if charity.skill_question %}{{ charity.skill_question }}{% endif %}</textarea>
      </label>

      <label style="margin-top:10px;display:block">
        Upload question image (optional)
        <input type="file" name="skill_image_file" accept="image/*">
      </label>

      {% if charity.skill_image_data %}
        <div style="margin-top:10px">
          <div class="muted" style="font-size:12px;margin-bottom:6px">Current question image preview:</div>
          <img src="{{ charity.skill_image_data }}" alt="Skill question image"
               style="max-width:260px;border-radius:12px;border:1px solid rgba(207,227,234,0.9);">
        </div>
      {% endif %}

      <label style="margin-top:10px;display:block">
        Answer bank (one per line)
        <textarea name="skill_answers" rows="6" placeholder="Answer 1&#10;Answer 2&#10;Answer 3&#10;...">{% if skill_answers_text %}{{ skill_answers_text }}{% endif %}</textarea>
        <div class="muted" style="font-size:12px;margin-top:6px;line-height:1.4">
          You can enter many answers here. The frontend will show a rotating subset each time.
        </div>
      </label>

      <label style="margin-top:10px;display:block">
        Correct answer (must match one line above)
        <input type="text" name="skill_correct_answer" value="{{ charity.skill_correct_answer or '' }}" placeholder="Paste the exact correct answer here">
      </label>

      <label style="margin-top:10px;display:block">
        Number of options to show on the frontend
        <input type="number" name="skill_display_count" min="2" max="8" value="{{ charity.skill_display_count or 4 }}">
      </label>

      <h3 style="margin-top:16px;">Campaign Status</h3>

      <div style="display:flex; gap:8px; flex-wrap:wrap;">
        <button class="btn" type="submit"
                formmethod="post"
                formaction="{{ url_for('admin_set_campaign_status', slug=charity.slug) }}"
                name="status" value="live">Set Live</button>

        <button class="btn" type="submit"
                formmethod="post"
                formaction="{{ url_for('admin_set_campaign_status', slug=charity.slug) }}"
                name="status" value="inactive">Set Inactive</button>

        <button class="btn" type="submit"
                formmethod="post"
                formaction="{{ url_for('admin_set_campaign_status', slug=charity.slug) }}"
                name="status" value="coming_soon">Set Coming Soon</button>
 
        <button class="btn" type="submit"
                formmethod="post"
                formaction="{{ url_for('admin_set_campaign_status', slug=charity.slug) }}"
                name="status" value="sold_out">Set Sold Out</button>
      </div>

      <p class="muted" style="margin-top:8px;">
        Current: <strong>{{ charity.campaign_status or 'live' }}</strong>
      </p>

      {% if charity.logo_data %}
        <div style="margin-top:10px">
          <div class="muted" style="font-size:12px;margin-bottom:6px">Current logo preview:</div>
          <img src="{{ charity.logo_data }}" alt="Current logo"
               style="max-width:180px;border-radius:12px;">
        </div>
      {% endif %}
      <div style="margin-top:8px"><button class="btn">Save Changes</button></div>
    </form>
    <p><a class="pill" href="{{ url_for('admin_charities') }}">← Back to Manage Charities</a></p>
    """
    skill_answers_text = ""
    try:
        skill_answers_text = "\n".join(_parse_skill_answers(getattr(charity, "skill_answers_json", "") or ""))
    except Exception:
        skill_answers_text = ""

    prizes_text = ""
    try:
        prizes_text = "\n".join(_parse_prizes(getattr(charity, "prizes_json", "") or ""))
    except Exception:
        prizes_text = ""

    return render(
        body, 
        charity=charity,
        msg=msg, 
        draw_value=draw_value,
        skill_answers_text=skill_answers_text, 
        min_hold=min_hold_gbp,
        current_hold_gbp=current_hold_gbp,
        prizes_text=prizes_text,
        title=f"Edit {charity.name}",
    )

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
      <a class="pill" href="{{ url_for('admin_new_entry', slug=charity.slug) }}">Add Entry</a>
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
	    <th>Number</th><th>PI</th><th>Created</th><th>Paid</th><th>Actions</th>
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
              <td class="muted">
                {% if e.payment_intent_id %}
                  {{ e.payment_intent_id[:10] }}…
                {% else %}
                  -
                {% endif %}
              </td>           
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

@app.route("/admin/charity/<slug>/entries/new", methods=["GET","POST"])
def admin_new_entry(slug):
    if not session.get("admin_ok"): 
        return redirect(url_for("admin_charities"))
    charity = Charity.query.filter_by(slug=slug).first_or_404()
    msg = None

    if request.method == "POST":
        name = request.form.get("name","").strip()
        email = request.form.get("email","").strip()
        phone = request.form.get("phone","").strip()
        number_raw = request.form.get("number","").strip()

        if not name or not email:
            msg = "Name and Email required."
        else:
            # Use specific number if provided
            if number_raw:
                try:
                    num = int(number_raw)
                    if num < 1 or num > charity.max_number:
                        msg = f"Number must be between 1 and {charity.max_number}."
                except ValueError:
                    msg = "Number must be an integer."
                    num = None
            else:
                # Auto-assign a free number (retry on collisions)
                num = None
                for _ in range(12):
                    candidate = assign_number(charity)
                    if not candidate:
                        msg = "No numbers available."
                        break

                    try:
                        e = Entry(charity_id=charity.id, name=name, email=email, phone=phone, number=candidate)
                        db.session.add(e)
                        db.session.commit()
                        return redirect(url_for("admin_charity_entries", slug=charity.slug))
                    except IntegrityError:
                        db.session.rollback()
                        continue

                if not msg and num is None:
                    msg = "Tickets are selling fast — please try again."

    body = """
    <h2>Add Entry — {{ charity.name }}</h2>
    {% if msg %}<div style="margin:6px 0;color:#ffd29f">{{ msg }}</div>{% endif %}
    <form method="post" data-safe-submit>
      <label>Name <input type="text" name="name" required></label>
      <label>Email <input type="email" name="email" required></label>
      <label>Phone <input type="tel" name="phone"></label>
      <label>Number (leave blank to auto-assign) 
        <input type="number" name="number" min="1" max="{{ charity.max_number }}">
      </label>
      <div style="margin-top:8px">
        <button class="btn">Save</button>
        <a class="pill" href="{{ url_for('admin_charity_entries', slug=charity.slug) }}">Cancel</a>
      </div>
    </form>
    """
    return render(body, charity=charity, msg=msg, title=f"Add Entry – {charity.name}", charity_logo=charity_logo)


@app.route("/admin/charity/<slug>/entries.csv")
def admin_charity_entries_csv(slug):
    if not session.get("admin_ok"): return redirect(url_for("admin_charities"))
    charity = Charity.query.filter_by(slug=slug).first_or_404()
    entries = Entry.query.filter_by(charity_id=charity.id).order_by(Entry.id.asc()).all()
    output = io.StringIO(); w = csv.writer(output)
    w.writerow(["id","name","email","phone","number","payment_intent_id","created_at","paid","paid_at","charity_slug","charity_name"])
    for e in entries:
        w.writerow([
            e.id, e.name, e.email, e.phone, e.number,
            e.payment_intent_id or "",
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

    charity_logo = getattr(charity, "logo_data", None) 

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
          <th>ID</th><th>Name</th><th>Email</th><th>Phone</th>
          <th>No.</th><th>PI</th><th>Created</th><th>Paid</th><th>Actions</th>
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
            <td class="muted">
              {% if e.payment_intent_id %}
                {{ e.payment_intent_id[:10] }}…
              {% else %}
                -
              {% endif %}
            </td>
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
    try:
        with db.engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE charity ADD COLUMN draw_at DATETIME")
    except Exception as e:
        print("draw_at column:", e)
    try:
        with db.engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE charity ADD COLUMN is_live BOOLEAN DEFAULT 1")
    except Exception as e:
        print("is_live column:", e)
    try:
        with db.engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE charity ADD COLUMN logo_data TEXT")
    except Exception as e:
        print("logo_data column:", e)
    try:
        with db.engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE charity ADD COLUMN campaign_status VARCHAR(20) DEFAULT 'live'")
    except Exception as e:
        print("campaign_status column:", e)
    # Auto-migrate charity table (draw_at / is_live) if missing
    try:
        insp = inspect(db.engine)
        charity_cols = {c['name'] for c in insp.get_columns('charity')}
        with db.engine.begin() as conn:
            if 'draw_at' not in charity_cols:
                conn.execute(text("ALTER TABLE charity ADD COLUMN draw_at DATETIME"))
            if 'campaign_status' not in charity_cols:
                conn.execute(text("ALTER TABLE charity ADD COLUMN campaign_status VARCHAR(20) DEFAULT 'live'"))
            if 'is_live' not in charity_cols:
                conn.execute(text("ALTER TABLE charity ADD COLUMN is_live BOOLEAN DEFAULT 1"))
            if 'logo_data' not in charity_cols:
                conn.execute(text("ALTER TABLE charity ADD COLUMN logo_data TEXT"))
            if 'free_entry_enabled' not in charity_cols:
                conn.execute(text("ALTER TABLE charity ADD COLUMN free_entry_enabled BOOLEAN DEFAULT 0"))
            if 'postal_entry_enabled' not in charity_cols:
                conn.execute(text("ALTER TABLE charity ADD COLUMN postal_entry_enabled BOOLEAN DEFAULT 0"))
            if 'optional_donation_enabled' not in charity_cols:
                conn.execute(text("ALTER TABLE charity ADD COLUMN optional_donation_enabled BOOLEAN DEFAULT 0"))
            if 'skill_enabled' not in charity_cols:
                conn.execute(text("ALTER TABLE charity ADD COLUMN skill_enabled BOOLEAN DEFAULT 0"))
            if 'stripe_account_id' not in charity_cols:
                conn.execute(text("ALTER TABLE charity ADD COLUMN stripe_account_id VARCHAR(255)"))
            if 'skill_question' not in charity_cols:
                conn.execute(text("ALTER TABLE charity ADD COLUMN skill_question TEXT"))
            if 'skill_image_data' not in charity_cols:
                conn.execute(text("ALTER TABLE charity ADD COLUMN skill_image_data TEXT"))
            if 'skill_answers_json' not in charity_cols:
                conn.execute(text("ALTER TABLE charity ADD COLUMN skill_answers_json TEXT"))
            if 'skill_correct_answer' not in charity_cols:
                conn.execute(text("ALTER TABLE charity ADD COLUMN skill_correct_answer TEXT"))
            if 'skill_display_count' not in charity_cols:
                conn.execute(text("ALTER TABLE charity ADD COLUMN skill_display_count INTEGER DEFAULT 4"))
            if 'hold_amount_pence' not in cols:
                conn.execute(text("ALTER TABLE entry ADD COLUMN hold_amount_pence INTEGER"))
    except Exception as e:
        print("Charity auto-migration check failed:", e)
    return "Migration attempted. Go back to Entries and refresh."

# ====== DB INIT / SEED ========================================================

with app.app_context():
    db.create_all()
    try:
        insp = inspect(db.engine)

        # ---- entry table ----
        entry_cols = {c['name'] for c in insp.get_columns('entry')}
        with db.engine.begin() as conn:
            if 'paid' not in entry_cols:
                conn.execute(text("ALTER TABLE entry ADD COLUMN paid BOOLEAN DEFAULT 0"))
            if 'paid_at' not in entry_cols:
                conn.execute(text("ALTER TABLE entry ADD COLUMN paid_at DATETIME"))
            if 'stripe_account_id' not in entry_cols:
                conn.execute(text("ALTER TABLE entry ADD COLUMN stripe_account_id VARCHAR(64)"))

        # ---- charity table ----
        charity_cols = {c['name'] for c in insp.get_columns('charity')}
        with db.engine.begin() as conn:
            if 'stripe_account_id' not in charity_cols:
                conn.execute(text("ALTER TABLE charity ADD COLUMN stripe_account_id VARCHAR(64)"))
            if 'tile_about' not in charity_cols:
                conn.execute(text("ALTER TABLE charity ADD COLUMN tile_about TEXT"))
            if 'prizes_json' not in charity_cols:
                conn.execute(text("ALTER TABLE charity ADD COLUMN prizes_json TEXT"))
            if 'poster_data' not in charity_cols:
                conn.execute(text("ALTER TABLE charity ADD COLUMN poster_data TEXT"))

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

# ====================== PUBLIC PAGES ADDED LATER ======================
@app.route("/charities", methods=["GET"])
def charities():
    try:
        q = Charity.query
        if hasattr(Charity, "name"):
            q = q.order_by(Charity.name.asc())
        rows = q.all()
    except Exception:
        rows = []

    body = """
    <h2>Choose a charity</h2>
    <p class="muted">Pick a Charity raffle page below:</p>
    <div class="stack" style="margin-top:10px;flex-wrap:wrap;">
      {% for c in rows %}
        <a class="pill" href="{{ url_for('charity_page', slug=c.slug) }}">
          <strong>{{ c.name or c.slug or ("Charity #" ~ c.id) }}</strong>
        </a>
      {% else %}
        <p class="muted">No charities found yet.</p>
      {% endfor %}
    </div>
    """
    return render(body, rows=rows, title="Choose a charity")


@app.route("/how-it-works", methods=["GET"])
def how_it_works():
    body = """
    <div class="hero">
      <h1>How it works</h1>
      <p class="muted">A simple, transparent way to run charity raffles.</p>
      <ol style="padding-left:18px;font-size:14px;margin-top:10px;display:flex;flex-direction:column;gap:6px;">
        <li>Choose a charity raffle page (for example, <code>/thekehilla</code>).</li>
        <li>Enter your details and click <strong>“Get my number”</strong> to draw a random number.</li>
        <li>Donate exactly that amount to the charity’s donation page to complete your entry.</li>
      </ol>
      <p class="muted" style="font-size:12px;margin-top:16px;">
        Numbers are unique and random; donations are handled securely via your existing flow.
      </p>
    </div>
    """
    return render(body, title="How it works")


@app.route("/admin", methods=["GET"])
def admin_root():
    # Send anyone hitting /admin to the real dashboard
    return redirect("/admin/charities")

# ====== LOCAL RUNNER ==========================================================

if __name__ == "__main__":
    app.run(debug=True)
