from flask import Flask, render_template, request, redirect, session, url_for, send_from_directory
import re
import sqlite3
import os
import json
import base64
import io
from urllib.parse import quote as urlquote
import random
import secrets
import string
import hmac
import hashlib
from datetime import datetime, timedelta
from functools import wraps
import time

import qrcode
import requests

from num2words import num2words
from xml.sax.saxutils import escape as xml_escape

try:
    from weasyprint import HTML as WeasyHTML
except OSError as e:
    raise SystemExit(
        "\n\n"
        "ERROR: WeasyPrint could not load required system graphics libraries\n"
        "(Pango/Cairo/GDK-Pixbuf) needed to generate the Marathi PDF receipts.\n"
        "This is separate from 'pip install' - it's a one-time OS-level install.\n\n"
        "Windows fix: download and run the GTK3 runtime installer from:\n"
        "  https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases\n"
        "  (get the latest gtk3-runtime...-installer.exe, run it, then restart this app)\n\n"
        "Linux fix: sudo apt-get install libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0\n\n"
        f"Original error: {e}\n"
    )

from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

app = Flask(__name__)
# Without this, Flask trusts only the raw connection it sees, which behind a
# reverse proxy (GitHub Codespaces port forwarding, Render, etc.) is often
# just "localhost" - making request.host_url (used to build the QR code
# link) wrong. ProxyFix reads the standard X-Forwarded-* headers such
# proxies set, so the QR code always encodes the real public URL.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

DB = 'donations.db'

RAZORPAY_ORDER_URL = "https://api.razorpay.com/v1/orders"

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'fonts')
DEVANAGARI_REGULAR_PATH = os.path.join(FONTS_DIR, 'NotoSansDevanagari-Regular.ttf')
DEVANAGARI_BOLD_PATH = os.path.join(FONTS_DIR, 'NotoSansDevanagari-Bold.ttf')
DEVANAGARI_AVAILABLE = os.path.exists(DEVANAGARI_REGULAR_PATH) and os.path.exists(DEVANAGARI_BOLD_PATH)


# ================= DATABASE =================

def get_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def _add_column_if_missing(c, table, column, coltype):
    c.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in c.fetchall()]
    if column not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


# ================= RECEIPT TEXT TEMPLATE (shared) =================

DEFAULT_RECEIPT_SUBTITLE = "(Voluntary Donation)"
DEFAULT_RECEIPT_LINE1 = "श्री। श्रीमती {NAME}"
DEFAULT_RECEIPT_LINE2 = "यांनी खालील तपशीलाप्रमाणे ब्राम्हणव्दारा/पूरोहितव्दारा देवकृत्य केले."


def resolve_receipt_lines(temple, name):
    """Applies the temple's saved receipt template (or defaults) and inserts
    the devotee's name, returning ready-to-render, already-escaped strings.

    A field that was NEVER saved (NULL) falls back to the default text. A
    field the admin explicitly cleared and saved as blank stays genuinely
    blank - it does NOT silently revert to the default.
    """
    raw_subtitle = temple['receipt_subtitle']
    raw_line1 = temple['receipt_line1']
    raw_line2 = temple['receipt_line2']

    subtitle = DEFAULT_RECEIPT_SUBTITLE if raw_subtitle is None else raw_subtitle.strip()
    line1_t = DEFAULT_RECEIPT_LINE1 if raw_line1 is None else raw_line1.strip()
    line2_t = DEFAULT_RECEIPT_LINE2 if raw_line2 is None else raw_line2.strip()

    line1_was_deliberately_blanked = (raw_line1 is not None and raw_line1.strip() == '')

    if not line1_was_deliberately_blanked and '{NAME}' not in line1_t and '{NAME}' not in line2_t:
        line1_t = line1_t.rstrip() + " {NAME}"

    name_escaped = xml_escape((name or '').upper())
    line1_final = xml_escape(line1_t).replace('{NAME}', f"<b>{name_escaped}</b>") if line1_t else ''
    line2_final = xml_escape(line2_t).replace('{NAME}', f"<b>{name_escaped}</b>") if line2_t else ''
    subtitle_final = xml_escape(subtitle) if subtitle else ''

    return subtitle_final, line1_final, line2_final


def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    # ---- temples ----
    c.execute('''
        CREATE TABLE IF NOT EXISTS temples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            username TEXT UNIQUE,
            password TEXT,
            upi_id TEXT,
            expiry_date TEXT
        )
    ''')
    _add_column_if_missing(c, 'temples', 'address', 'TEXT')
    _add_column_if_missing(c, 'temples', 'phone', 'TEXT')
    _add_column_if_missing(c, 'temples', 'razorpay_key_id', 'TEXT')
    _add_column_if_missing(c, 'temples', 'razorpay_key_secret', 'TEXT')
    _add_column_if_missing(c, 'temples', 'hide_printed_on_counter', 'INTEGER DEFAULT 0')
    _add_column_if_missing(c, 'temples', 'receipt_subtitle', 'TEXT')
    _add_column_if_missing(c, 'temples', 'receipt_line1', 'TEXT')
    _add_column_if_missing(c, 'temples', 'receipt_line2', 'TEXT')
    _add_column_if_missing(c, 'temples', 'public_base_url', 'TEXT')

    # ---- sevas / categories ----
    c.execute('''
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            temple_id INTEGER,
            name TEXT,
            amount REAL
        )
    ''')
    _add_column_if_missing(c, 'categories', 'active', 'INTEGER DEFAULT 1')

    # ---- seva price overrides (daily / custom date-range pricing) ----
    c.execute('''
        CREATE TABLE IF NOT EXISTS seva_price_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER,
            start_date TEXT,
            end_date TEXT,
            amount REAL,
            note TEXT
        )
    ''')

    # ---- multiple selectable amount tiers per seva (e.g. Rs 20/51/151/201/1001) ----
    c.execute('''
        CREATE TABLE IF NOT EXISTS seva_amount_options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER,
            amount REAL,
            sort_order INTEGER DEFAULT 0
        )
    ''')

    # ---- combo packages bundling multiple sevas as one selectable option ----
    c.execute('''
        CREATE TABLE IF NOT EXISTS seva_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            temple_id INTEGER,
            name TEXT,
            active INTEGER DEFAULT 1
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS seva_template_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER,
            category_id INTEGER,
            amount REAL
        )
    ''')

    # ---- receipts (one booking, can contain many sevas) ----
    c.execute('''
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            temple_id INTEGER,
            receipt_no TEXT UNIQUE,
            name TEXT,
            phone TEXT,
            remarks TEXT,
            total_amount REAL,
            payment_method TEXT,
            payment_id TEXT,
            payment_status TEXT,
            created_at TEXT,
            printed INTEGER DEFAULT 0,
            pdf_path TEXT
        )
    ''')
    _add_column_if_missing(c, 'receipts', 'email', 'TEXT')
    _add_column_if_missing(c, 'receipts', 'seq_no', 'INTEGER')
    _add_column_if_missing(c, 'receipts', 'subtitle_text', 'TEXT')
    _add_column_if_missing(c, 'receipts', 'line1_text', 'TEXT')
    _add_column_if_missing(c, 'receipts', 'line2_text', 'TEXT')
    _add_column_if_missing(c, 'receipts', 'payment_proof_path', 'TEXT')
    _add_column_if_missing(c, 'receipts', 'payment_verified', 'INTEGER DEFAULT 0')
    _add_column_if_missing(c, 'receipts', 'verified_by', 'TEXT')
    _add_column_if_missing(c, 'receipts', 'verified_at', 'TEXT')

    # ---- line items for each receipt ----
    c.execute('''
        CREATE TABLE IF NOT EXISTS receipt_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER,
            category_id INTEGER,
            category_name TEXT,
            amount REAL
        )
    ''')

    # legacy table kept for backward compatibility with old data
    c.execute('''
        CREATE TABLE IF NOT EXISTS donations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            temple_id INTEGER,
            name TEXT,
            phone TEXT,
            category TEXT,
            amount REAL,
            date TEXT
        )
    ''')

    # ---- staff/user accounts (admin / user / printing_user) ----
    c.execute('''
        CREATE TABLE IF NOT EXISTS temple_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            temple_id INTEGER,
            username TEXT UNIQUE,
            password TEXT,
            role TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT
        )
    ''')

    # ---- login attempt tracking (brute-force lockout protection) ----
    c.execute('''
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            attempted_at TEXT
        )
    ''')

    # backfill seq_no for any receipts created before this column existed
    c.execute("SELECT id, temple_id FROM receipts WHERE seq_no IS NULL ORDER BY temple_id, id")
    rows = c.fetchall()
    counters = {}
    for rid, tid in rows:
        counters[tid] = counters.get(tid, 0) + 1
        c.execute("UPDATE receipts SET seq_no=? WHERE id=?", (counters[tid], rid))

    # Razorpay payments were already cryptographically signature-verified at
    # the time of payment - no need to make staff re-verify those manually
    c.execute("""
        UPDATE receipts SET payment_verified=1
        WHERE payment_method='razorpay' AND payment_verified=0
    """)

    # backfill resolved receipt text for any receipts created before the
    # editable receipt-template feature existed
    conn.row_factory = sqlite3.Row
    c2 = conn.cursor()
    c2.execute("SELECT id, temple_id, name FROM receipts WHERE line1_text IS NULL")
    missing = c2.fetchall()
    temple_cache = {}
    for row in missing:
        tid = row['temple_id']
        if tid not in temple_cache:
            c2.execute("SELECT * FROM temples WHERE id=?", (tid,))
            temple_cache[tid] = c2.fetchone()
        temple_row = temple_cache[tid]
        if temple_row is None:
            continue
        subtitle_final, line1_final, line2_final = resolve_receipt_lines(temple_row, row['name'])
        c2.execute(
            "UPDATE receipts SET subtitle_text=?, line1_text=?, line2_text=? WHERE id=?",
            (subtitle_final, line1_final, line2_final, row['id'])
        )

    conn.commit()
    conn.close()


init_db()


# ================= HELPERS =================

def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def effective_amount(cat_row, on_date=None):
    """Return the default amount that applies for a seva on a given date,
    taking any active price override (daily or custom date range) into account."""
    on_date = on_date or today_str()

    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT amount FROM seva_price_overrides
        WHERE category_id = ?
          AND start_date <= ?
          AND end_date   >= ?
        ORDER BY id DESC LIMIT 1
    """, (cat_row['id'], on_date, on_date))
    row = c.fetchone()
    conn.close()

    if row:
        return row['amount']
    return cat_row['amount']


def get_amount_options(category_id):
    """Returns the list of selectable amount tiers for a seva, if any were
    configured (e.g. Rs 20/51/151/201/1001), ordered for display."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT amount FROM seva_amount_options
        WHERE category_id=? ORDER BY sort_order, amount
    """, (category_id,))
    rows = c.fetchall()
    conn.close()
    return [r['amount'] for r in rows]


ALLOWED_PROOF_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'pdf'}
MAX_PROOF_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB


def save_payment_proof(file_storage, receipt_no):
    """Saves an optional devotee-uploaded payment screenshot/receipt,
    validating type and size. Returns the saved path, or None if no valid
    file was provided (this feature is always optional, never required)."""
    if not file_storage or not file_storage.filename:
        return None

    ext = file_storage.filename.rsplit('.', 1)[-1].lower() if '.' in file_storage.filename else ''
    if ext not in ALLOWED_PROOF_EXTENSIONS:
        return None

    file_storage.seek(0, os.SEEK_END)
    size = file_storage.tell()
    file_storage.seek(0)
    if size > MAX_PROOF_SIZE_BYTES or size == 0:
        return None

    os.makedirs("static/payment_proofs", exist_ok=True)
    safe_name = secure_filename(f"{receipt_no}.{ext}")
    save_path = f"static/payment_proofs/{safe_name}"
    file_storage.save(save_path)
    return save_path


def build_upi_qr_data_uri(upi_id, payee_name, amount, note="Seva Donation"):
    """Builds a standard UPI deep link (upi://pay?...) that any UPI app
    (GPay, PhonePe, Paytm, etc.) understands, renders it as a QR code, and
    returns it as a data: URI so it can be shown directly in an <img> tag
    without needing to save a file to disk."""
    upi_link = (
        f"upi://pay?pa={urlquote(upi_id)}&pn={urlquote(payee_name)}"
        f"&am={amount:.2f}&cu=INR&tn={urlquote(note)}"
    )
    img = qrcode.make(upi_link)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    b64 = base64.b64encode(buf.getvalue()).decode('ascii')
    return upi_link, f"data:image/png;base64,{b64}"


def generate_receipt_no():
    # Cryptographically secure random token (not a small guessable number) so
    # receipt URLs cannot realistically be brute-forced to view another
    # devotee's name, phone number, or donation amount.
    token = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(10))
    return f"TEMPLE-{token}"


def amount_to_words(amount):
    """e.g. 100 -> 'ONE HUNDRED ONLY.' matching the legacy receipt format."""
    rupees = int(amount)
    paise = round((amount - rupees) * 100)
    words = num2words(rupees).upper()
    if paise:
        words += " AND " + num2words(paise).upper() + " PAISE"
    return words + " ONLY."


def build_receipt_pdf(temple_name, receipt_no, seq_no, phone, remarks, items, total, created_at,
                       subtitle_text, line1_text, line2_text):
    """6x4 inch receipt in Marathi, matching the temple's existing dot-matrix
    receipt book layout. Rendered via WeasyPrint (HTML/CSS -> PDF) because it
    correctly shapes Devanagari conjuncts (e.g. "श्री") the same way a
    browser does, unlike simpler PDF libraries.

    receipt_no is the secure random token used for the filename/URL (kept
    unguessable). seq_no is the simple sequential number (1, 2, 3...) shown
    to the devotee on the printed receipt.

    subtitle_text/line1_text/line2_text must already be resolved (via
    resolve_receipt_lines) and XML-escaped.
    """
    os.makedirs("static/receipts", exist_ok=True)
    pdf_path = f"static/receipts/{receipt_no}.pdf"

    date_only = created_at.split(' ')[0] if ' ' in created_at else created_at

    font_family = "'Devanagari', sans-serif" if DEVANAGARI_AVAILABLE else "sans-serif"
    font_face_css = ""
    if DEVANAGARI_AVAILABLE:
        regular_uri = 'file://' + DEVANAGARI_REGULAR_PATH.replace(os.sep, '/')
        bold_uri = 'file://' + DEVANAGARI_BOLD_PATH.replace(os.sep, '/')
        font_face_css = f"""
        @font-face {{
            font-family: 'Devanagari';
            src: url('{regular_uri}');
            font-weight: normal;
        }}
        @font-face {{
            font-family: 'Devanagari';
            src: url('{bold_uri}');
            font-weight: bold;
        }}
        """

    rows_html = ""
    for i, it in enumerate(items, start=1):
        rows_html += f"""
        <tr>
            <td>{i}</td>
            <td>{xml_escape(it['name'])}</td>
            <td class="right">{it['amount']:.2f}</td>
        </tr>
        """

    subtitle_html = f'<div class="center">{subtitle_text}</div>' if subtitle_text else ''
    line1_html = f'<div class="line">{line1_text}</div>' if line1_text else ''
    line2_html = f'<div class="line">{line2_text}</div>' if line2_text else ''
    phone_html = f'<div class="small">मो. : {xml_escape(phone)}</div>' if phone else ''
    remarks_html = f'<div class="small">शेरा : {xml_escape(remarks)}</div>' if remarks else ''

    html_content = f"""
    <html>
    <head>
    <style>
        {font_face_css}
        @page {{
            size: 6in 4in;
            margin: 0.14in 0.19in;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            font-family: {font_family};
            font-size: 10.5pt;
            margin: 0;
            padding: 0;
        }}
        .title {{
            text-align: center;
            font-weight: bold;
            font-size: 13pt;
            margin-bottom: 4pt;
        }}
        .header-row {{
            display: flex;
            justify-content: space-between;
            margin-bottom: 4pt;
        }}
        .center {{ text-align: center; margin-bottom: 6pt; }}
        .line {{ margin: 0 0 2pt 0; }}
        .small {{ font-size: 8pt; margin: 0 0 2pt 0; }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 6pt;
            font-size: 10pt;
        }}
        th, td {{
            border: 0.5pt solid black;
            padding: 3pt 5pt;
            text-align: left;
        }}
        th {{ background: #f5f5f5; }}
        .right {{ text-align: right; }}
        .footer-row {{
            display: flex;
            justify-content: space-between;
            margin-top: 8pt;
        }}
    </style>
    </head>
    <body>
        <div class="title">{xml_escape(temple_name)}</div>
        <div class="header-row">
            <span>तारीख : {date_only}</span>
            <span>पावती नंबर: <b>{seq_no}</b></span>
        </div>
        {subtitle_html}
        {line1_html}
        {line2_html}
        {phone_html}
        {remarks_html}
        <table>
            <thead>
                <tr><th>क्रमांक</th><th>देवकृत्य</th><th class="right">रक्कम</th></tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>
        <div class="footer-row">
            <span>रक्कम रु : {amount_to_words(total)}</span>
            <span><b>Total : {total:.2f}</b></span>
        </div>
    </body>
    </html>
    """

    WeasyHTML(string=html_content).write_pdf(pdf_path)
    return pdf_path


def require_login():
    return session.get('temple_id') is not None


LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 15


def record_failed_login(username):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO login_attempts (username, attempted_at) VALUES (?, ?)",
              (username, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def is_login_locked_out(username):
    conn = get_conn()
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(minutes=LOGIN_LOCKOUT_MINUTES)).isoformat()
    c.execute("""
        SELECT COUNT(*) FROM login_attempts
        WHERE username=? AND attempted_at > ?
    """, (username, cutoff))
    count = c.fetchone()[0]
    conn.close()
    return count >= LOGIN_MAX_ATTEMPTS


def clear_failed_logins(username):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM login_attempts WHERE username=?", (username,))
    conn.commit()
    conn.close()


def login_required(roles=None):
    """Decorator: require login, and optionally restrict to specific roles.
    roles=None means any logged-in role is fine (admin/user/printing_user)."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not require_login():
                return redirect('/login')
            if roles and session.get('role') not in roles:
                return "Access denied: your account role does not have permission to view this page.", 403
            return f(*args, **kwargs)
        return wrapper
    return decorator


@app.context_processor
def inject_role_context():
    return dict(current_role=session.get('role'), current_username=session.get('staff_username'))


# ================= HOME =================

@app.route('/')
def home():
    # Public landing page = About Us. Staff log in at /login (not linked publicly).
    return _render_legal('about', 'About Us')


# ================= REGISTER =================

@app.route('/register', methods=['GET', 'POST'])
def register():
    # Registration is open only until the first admin exists
    _c = get_conn()
    _n = _c.execute("SELECT COUNT(*) FROM temples").fetchone()[0]
    _c.close()
    if _n > 0:
        return redirect('/login')
    if request.method == 'POST':
        name = request.form['name']
        username = request.form['username']
        password = request.form['password']
        upi_id = request.form.get('upi_id', '')

        expiry_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")

        conn = get_conn()
        c = conn.cursor()
        try:
            c.execute("""
                INSERT INTO temples (name, username, password, upi_id, expiry_date)
                VALUES (?, ?, ?, ?, ?)
            """, (name, username, password, upi_id, expiry_date))
            conn.commit()
        except Exception:
            conn.close()
            return "Username Already Exists"

        conn.close()
        return redirect('/login')

    return render_template('register.html')


# ================= LOGIN / LOGOUT =================

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']

        if is_login_locked_out(username):
            return render_template(
                'login.html',
                error=f"Too many failed login attempts for this account. "
                      f"Please wait {LOGIN_LOCKOUT_MINUTES} minutes and try again."
            )

        conn = get_conn()
        c = conn.cursor()

        c.execute("SELECT * FROM temples WHERE username=? AND password=?", (username, password))
        owner = c.fetchone()
        if owner:
            clear_failed_logins(username)
            session['temple_id'] = owner['id']
            session['role'] = 'admin'
            session['staff_username'] = owner['username']
            conn.close()
            return redirect('/dashboard')

        c.execute("""
            SELECT * FROM temple_users WHERE username=? AND password=? AND active=1
        """, (username, password))
        staff = c.fetchone()
        conn.close()

        if staff:
            clear_failed_logins(username)
            session['temple_id'] = staff['temple_id']
            session['role'] = staff['role']
            session['staff_username'] = staff['username']
            if staff['role'] == 'printing_user':
                return redirect('/counter')
            return redirect('/dashboard')

        record_failed_login(username)
        time.sleep(1)
        return render_template('login.html', error="Invalid Login")

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


# ================= ADMIN DASHBOARD =================

@app.route('/dashboard')
@login_required(['admin', 'user'])
def dashboard():
    temple_id = session['temple_id']
    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT COUNT(*), COALESCE(SUM(total_amount),0) FROM receipts WHERE temple_id=?", (temple_id,))
    count, total = c.fetchone()

    c.execute("""
        SELECT * FROM receipts WHERE temple_id=? ORDER BY id DESC LIMIT 10
    """, (temple_id,))
    recent = c.fetchall()

    c.execute("SELECT COUNT(*) FROM categories WHERE temple_id=? AND active=1", (temple_id,))
    active_sevas = c.fetchone()[0]

    conn.close()

    return render_template(
        'dashboard.html',
        total=total, count=count, recent=recent, active_sevas=active_sevas, temple_id=temple_id
    )


# ================= MANAGE SEVAS (admin) =================

@app.route('/manage_sevas', methods=['GET', 'POST'])
@login_required(['admin'])
def manage_sevas():
    temple_id = session['temple_id']
    conn = get_conn()
    c = conn.cursor()

    token_ok = False
    if request.method == 'POST':
        submitted_token = request.form.get('form_token', '')
        expected_token = session.get('manage_sevas_token', '')
        # Reject a POST that doesn't carry the exact token this page was
        # rendered with - this is what stops an accidental double-click (or
        # a resubmitted/duplicated request) from creating the same seva or
        # package twice. A fresh token is issued below on every render, so
        # the second copy of a double-submit always fails this check.
        token_ok = bool(submitted_token) and hmac.compare_digest(submitted_token, expected_token)
        if token_ok:
            session.pop('manage_sevas_token', None)

    if request.method == 'POST' and token_ok:
        action = request.form.get('action')

        if action == 'add_seva':
            name = request.form['name']
            amount = request.form['amount']
            c.execute("""
                INSERT INTO categories (temple_id, name, amount, active)
                VALUES (?, ?, ?, 1)
            """, (temple_id, name, amount))
            conn.commit()

        elif action == 'edit_seva':
            cat_id = request.form['category_id']
            name = request.form.get('name', '').strip()
            amount = request.form['amount']
            active = 1 if request.form.get('active') == 'on' else 0
            if name:
                c.execute("""
                    UPDATE categories SET name=?, amount=?, active=? WHERE id=? AND temple_id=?
                """, (name, amount, active, cat_id, temple_id))
            else:
                c.execute("""
                    UPDATE categories SET amount=?, active=? WHERE id=? AND temple_id=?
                """, (amount, active, cat_id, temple_id))
            conn.commit()

        elif action == 'add_override':
            cat_id = request.form['category_id']
            start_date = request.form['start_date']
            end_date = request.form.get('end_date') or start_date
            amount = request.form['override_amount']
            note = request.form.get('note', '')
            c.execute("""
                INSERT INTO seva_price_overrides
                (category_id, start_date, end_date, amount, note)
                VALUES (?, ?, ?, ?, ?)
            """, (cat_id, start_date, end_date, amount, note))
            conn.commit()

        elif action == 'add_amount_option':
            cat_id = request.form['category_id']
            amount = request.form['option_amount']
            c.execute("""
                INSERT INTO seva_amount_options (category_id, amount, sort_order)
                VALUES (?, ?, ?)
            """, (cat_id, amount, int(float(amount))))
            conn.commit()

        elif action == 'create_template':
            name = request.form['template_name']
            item_cat_ids = request.form.getlist('template_seva_id')
            item_amounts = request.form.getlist('template_seva_amount')
            # only keep rows where a seva was actually chosen, and only the
            # FIRST occurrence of each seva if the same one was picked more
            # than once (defense in depth - the form also blocks this client-side)
            seen = set()
            pairs = []
            for cid, amt in zip(item_cat_ids, item_amounts):
                if cid and cid not in seen:
                    seen.add(cid)
                    pairs.append((cid, amt))
            if name and pairs:
                c.execute("""
                    INSERT INTO seva_templates (temple_id, name, active) VALUES (?, ?, 1)
                """, (temple_id, name))
                template_id = c.lastrowid
                for cid, amt in pairs:
                    c.execute("""
                        INSERT INTO seva_template_items (template_id, category_id, amount)
                        VALUES (?, ?, ?)
                    """, (template_id, cid, amt))
                conn.commit()

        elif action == 'edit_template':
            template_id = request.form['template_id']
            name = request.form['template_name']
            item_cat_ids = request.form.getlist('template_seva_id')
            item_amounts = request.form.getlist('template_seva_amount')
            seen = set()
            pairs = []
            for cid, amt in zip(item_cat_ids, item_amounts):
                if cid and cid not in seen:
                    seen.add(cid)
                    pairs.append((cid, amt))

            # verify this template actually belongs to this temple before touching it
            c.execute("SELECT id FROM seva_templates WHERE id=? AND temple_id=?", (template_id, temple_id))
            owned = c.fetchone()
            if owned and name and pairs:
                c.execute("UPDATE seva_templates SET name=? WHERE id=?", (name, template_id))
                c.execute("DELETE FROM seva_template_items WHERE template_id=?", (template_id,))
                for cid, amt in pairs:
                    c.execute("""
                        INSERT INTO seva_template_items (template_id, category_id, amount)
                        VALUES (?, ?, ?)
                    """, (template_id, cid, amt))
                conn.commit()

    c.execute("SELECT * FROM categories WHERE temple_id=? ORDER BY id", (temple_id,))
    categories = c.fetchall()

    c.execute("""
        SELECT o.*, cat.name as category_name
        FROM seva_price_overrides o
        JOIN categories cat ON cat.id = o.category_id
        WHERE cat.temple_id=?
        ORDER BY o.start_date DESC
    """, (temple_id,))
    overrides = c.fetchall()

    c.execute("""
        SELECT ao.*, cat.name as category_name
        FROM seva_amount_options ao
        JOIN categories cat ON cat.id = ao.category_id
        WHERE cat.temple_id=?
        ORDER BY cat.name, ao.sort_order
    """, (temple_id,))
    amount_options = c.fetchall()

    c.execute("SELECT * FROM seva_templates WHERE temple_id=? ORDER BY id DESC", (temple_id,))
    templates = c.fetchall()
    templates_with_items = []
    for t in templates:
        c.execute("""
            SELECT ti.*, cat.name as category_name
            FROM seva_template_items ti
            JOIN categories cat ON cat.id = ti.category_id
            WHERE ti.template_id=?
        """, (t['id'],))
        t_items = c.fetchall()
        t_total = sum(item['amount'] for item in t_items)
        templates_with_items.append({'template': t, 'seva_items': t_items, 'total': t_total})

    conn.close()

    # a fresh token for this render - every form on the page embeds it, and
    # it's consumed (removed from session) the moment any one form is
    # successfully submitted, so an accidental duplicate submission of the
    # same form fails the token check instead of creating a duplicate row
    new_token = secrets.token_hex(16)
    session['manage_sevas_token'] = new_token

    return render_template(
        'manage_sevas.html',
        categories=categories, overrides=overrides, today=today_str(),
        amount_options=amount_options, templates_with_items=templates_with_items,
        form_token=new_token
    )


@app.route('/delete_category/<int:cat_id>')
@login_required(['admin'])
def delete_category(cat_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM categories WHERE id=? AND temple_id=?", (cat_id, session['temple_id']))
    c.execute("DELETE FROM seva_price_overrides WHERE category_id=?", (cat_id,))
    c.execute("DELETE FROM seva_amount_options WHERE category_id=?", (cat_id,))
    conn.commit()
    conn.close()
    return redirect('/manage_sevas')


@app.route('/delete_override/<int:override_id>')
@login_required(['admin'])
def delete_override(override_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        DELETE FROM seva_price_overrides
        WHERE id=? AND category_id IN (SELECT id FROM categories WHERE temple_id=?)
    """, (override_id, session['temple_id']))
    conn.commit()
    conn.close()
    return redirect('/manage_sevas')


@app.route('/delete_amount_option/<int:option_id>')
@login_required(['admin'])
def delete_amount_option(option_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        DELETE FROM seva_amount_options
        WHERE id=? AND category_id IN (SELECT id FROM categories WHERE temple_id=?)
    """, (option_id, session['temple_id']))
    conn.commit()
    conn.close()
    return redirect('/manage_sevas')


@app.route('/delete_template/<int:template_id>')
@login_required(['admin'])
def delete_template(template_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM seva_templates WHERE id=? AND temple_id=?", (template_id, session['temple_id']))
    c.execute("DELETE FROM seva_template_items WHERE template_id=?", (template_id,))
    conn.commit()
    conn.close()
    return redirect('/manage_sevas')


# ================= MANAGE USERS (admin only) =================

@app.route('/manage_users', methods=['GET', 'POST'])
@login_required(['admin'])
def manage_users():
    temple_id = session['temple_id']
    conn = get_conn()
    c = conn.cursor()

    error = None

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add_user':
            username = request.form['username'].strip()
            password = request.form['password']
            role = request.form['role']
            if role not in ('admin', 'user', 'printing_user'):
                role = 'user'
            created_at = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
            try:
                c.execute("""
                    INSERT INTO temple_users (temple_id, username, password, role, active, created_at)
                    VALUES (?, ?, ?, ?, 1, ?)
                """, (temple_id, username, password, role, created_at))
                conn.commit()
            except sqlite3.IntegrityError:
                error = "That username is already taken. Choose a different one."

        elif action == 'edit_user':
            user_id = request.form['user_id']
            role = request.form['role']
            active = 1 if request.form.get('active') == 'on' else 0
            new_password = request.form.get('new_password', '').strip()
            if role not in ('admin', 'user', 'printing_user'):
                role = 'user'
            if new_password:
                c.execute("""
                    UPDATE temple_users SET role=?, active=?, password=?
                    WHERE id=? AND temple_id=?
                """, (role, active, new_password, user_id, temple_id))
            else:
                c.execute("""
                    UPDATE temple_users SET role=?, active=?
                    WHERE id=? AND temple_id=?
                """, (role, active, user_id, temple_id))
            conn.commit()

    c.execute("SELECT * FROM temple_users WHERE temple_id=? ORDER BY id", (temple_id,))
    staff = c.fetchall()

    c.execute("SELECT username FROM temples WHERE id=?", (temple_id,))
    owner_username = c.fetchone()['username']

    conn.close()

    return render_template('manage_users.html', staff=staff, owner_username=owner_username, error=error)


@app.route('/delete_user/<int:user_id>')
@login_required(['admin'])
def delete_user(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM temple_users WHERE id=? AND temple_id=?", (user_id, session['temple_id']))
    conn.commit()
    conn.close()
    return redirect('/manage_users')


# ================= TEMPLE SETTINGS (admin) =================

@app.route('/temple_settings', methods=['GET', 'POST'])
@login_required(['admin'])
def temple_settings():
    temple_id = session['temple_id']
    conn = get_conn()
    c = conn.cursor()

    if request.method == 'POST':
        hide_printed = 1 if request.form.get('hide_printed_on_counter') == 'on' else 0
        c.execute("""
            UPDATE temples
            SET name=?, address=?, phone=?, upi_id=?, razorpay_key_id=?, razorpay_key_secret=?,
                hide_printed_on_counter=?, public_base_url=?
            WHERE id=?
        """, (
            request.form['name'],
            request.form.get('address', ''),
            request.form.get('phone', ''),
            request.form.get('upi_id', ''),
            request.form.get('razorpay_key_id', ''),
            request.form.get('razorpay_key_secret', ''),
            hide_printed,
            request.form.get('public_base_url', '').strip(),
            temple_id
        ))
        conn.commit()

    c.execute("SELECT * FROM temples WHERE id=?", (temple_id,))
    temple = c.fetchone()
    conn.close()

    return render_template('temple_settings.html', temple=temple)


# ================= RECEIPT TEXT TEMPLATE (admin) =================

@app.route('/receipt_template', methods=['GET', 'POST'])
@login_required(['admin'])
def receipt_template():
    temple_id = session['temple_id']
    conn = get_conn()
    c = conn.cursor()

    if request.method == 'POST':
        c.execute("""
            UPDATE temples SET receipt_subtitle=?, receipt_line1=?, receipt_line2=?
            WHERE id=?
        """, (
            request.form.get('receipt_subtitle', '').strip(),
            request.form.get('receipt_line1', '').strip(),
            request.form.get('receipt_line2', '').strip(),
            temple_id
        ))
        conn.commit()

    c.execute("SELECT * FROM temples WHERE id=?", (temple_id,))
    temple = c.fetchone()
    conn.close()

    return render_template(
        'receipt_template.html',
        temple=temple,
        default_subtitle=DEFAULT_RECEIPT_SUBTITLE,
        default_line1=DEFAULT_RECEIPT_LINE1,
        default_line2=DEFAULT_RECEIPT_LINE2,
    )


@app.route('/receipt_template/preview')
@login_required(['admin'])
def receipt_template_preview():
    temple_id = session['temple_id']
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM temples WHERE id=?", (temple_id,))
    temple = c.fetchone()
    conn.close()

    if not temple:
        return "Temple not found", 404

    dummy_items = [
        {'name': 'सिंहपुरुष अभिषेक नैवेद्य', 'amount': 100.0},
        {'name': 'अभिषेक', 'amount': 50.0},
    ]
    total = sum(it['amount'] for it in dummy_items)

    subtitle_text, line1_text, line2_text = resolve_receipt_lines(temple, "SAMPLE NAME")

    pdf_path = build_receipt_pdf(
        temple['name'], f"PREVIEW-{temple_id}", 1, "9999999999", "",
        dummy_items, total, datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
        subtitle_text, line1_text, line2_text,
    )
    response = send_from_directory(os.path.dirname(pdf_path), os.path.basename(pdf_path))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


# ================= QR CODE =================


# ---------------------------------------------------------------
# Public information pages: About Us, Terms & Conditions, Privacy
# ---------------------------------------------------------------
# Official contact details shown on the public Contact Us page
CONTACT_NAME = 'SHREE MAHALASA SAUNSTHAN'
CONTACT_ADDR = 'MARDOL, GOA - 403 404'
CONTACT_PHONE = '98221 33806'
CONTACT_EMAIL = 'info@shreemahalasasaunsthan.org'


def _render_legal(page, title):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM temples ORDER BY id LIMIT 1")
    t = c.fetchone()
    conn.close()
    return render_template(
        'legal.html', page=page, title=title,
        t_id=t['id'] if t else None,
        t_name=(t['name'] if t else 'Shree Mahalasa Narayani Sansthan'),
        t_addr=(t['address'] if t and t['address'] else ''),
        t_phone=(t['phone'] if t and t['phone'] else ''),
        t_upi=(t['upi_id'] if t and t['upi_id'] else ''),
        updated=datetime.now().strftime('%d %B %Y'),
        c_name=CONTACT_NAME, c_addr=CONTACT_ADDR,
        c_phone=CONTACT_PHONE, c_email=CONTACT_EMAIL,
    )


@app.route('/about')
def about_page():
    return _render_legal('about', 'About Us')


@app.route('/terms')
def terms_page():
    return _render_legal('terms', 'Terms & Conditions')


@app.route('/contact')
def contact_page():
    return _render_legal('contact', 'Contact Us')


@app.route('/privacy')
def privacy_page():
    return _render_legal('privacy', 'Privacy Policy')


@app.route('/qr')
@login_required(['admin', 'user'])
def qr():
    temple_id = session.get('temple_id')

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT public_base_url FROM temples WHERE id=?", (temple_id,))
    row = c.fetchone()
    conn.close()

    override = (row['public_base_url'] or '').strip() if row else ''

    if override:
        base = override.rstrip('/')
    else:
        base = request.host_url.rstrip('/')

    url = base + f"/donate/{temple_id}"

    img = qrcode.make(url)
    os.makedirs("static", exist_ok=True)
    qr_path = f"static/qr_{temple_id}.png"
    img.save(qr_path)

    cache_bust = int(time.time())

    return render_template('qr.html', qr_image=qr_path, donation_url=url, cache_bust=cache_bust)


# ================= DEVOTEE BOOKING PAGE (multi-seva) =================

@app.route('/donate/<int:temple_id>')
def donate(temple_id):
    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT * FROM categories WHERE temple_id=? AND active=1 ORDER BY id", (temple_id,))
    raw_categories = c.fetchall()

    c.execute("SELECT name FROM temples WHERE id=?", (temple_id,))
    temple = c.fetchone()

    if not temple:
        conn.close()
        return "Temple not found", 404

    today = today_str()
    categories = []
    for cat in raw_categories:
        options = get_amount_options(cat['id'])
        categories.append({
            'id': cat['id'],
            'name': cat['name'],
            'amount': effective_amount(cat, today),
            'options': options,  # if non-empty, devotee picks from these instead of the fixed amount
        })

    # combo packages (templates) bundling multiple sevas as one selectable option
    c.execute("SELECT * FROM seva_templates WHERE temple_id=? AND active=1 ORDER BY id", (temple_id,))
    template_rows = c.fetchall()
    templates = []
    for t in template_rows:
        c.execute("""
            SELECT ti.category_id, ti.amount, cat.name as category_name
            FROM seva_template_items ti
            JOIN categories cat ON cat.id = ti.category_id
            WHERE ti.template_id=?
        """, (t['id'],))
        items = c.fetchall()
        templates.append({
            'id': t['id'],
            'name': t['name'],
            'seva_items': [{'category_id': i['category_id'], 'name': i['category_name'], 'amount': i['amount']} for i in items],
            'total': sum(i['amount'] for i in items),
        })

    conn.close()

    return render_template(
        'book.html',
        categories=categories, temple_id=temple_id, temple_name=temple['name'], templates=templates
    )


# ================= CHECKOUT (build cart, go to gateway) =================

@app.route('/checkout', methods=['POST'])
def checkout():
    temple_id = request.form['temple_id']
    name = request.form['name']
    phone = request.form['phone']
    email = request.form.get('email', '').strip()[:120]
    if email and not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return "Invalid email address", 400
    remarks = request.form.get('remarks', '')

    # regular sevas: value format "categoryId:chosenAmount"
    selected_sevas = request.form.getlist('sevas')
    # combo templates: value is just the template id
    selected_templates = request.form.getlist('templates')

    if not selected_sevas and not selected_templates:
        return "Please select at least one seva", 400

    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT * FROM temples WHERE id=?", (temple_id,))
    temple = c.fetchone()

    items = []
    total = 0.0
    today = today_str()

    for entry in selected_sevas:
        try:
            cid_str, chosen_amount_str = entry.split(':', 1)
            cid = int(cid_str)
            chosen_amount = float(chosen_amount_str)
        except (ValueError, IndexError):
            continue

        c.execute("SELECT * FROM categories WHERE id=? AND temple_id=?", (cid, temple_id))
        cat = c.fetchone()
        if not cat:
            continue

        # validate the chosen amount is actually a legitimate option for this
        # seva (either its default/overridden price, or one of its configured
        # amount tiers) - prevents a devotee from tampering with the price
        valid_amounts = {round(effective_amount(cat, today), 2)}
        for opt in get_amount_options(cid):
            valid_amounts.add(round(opt, 2))

        if round(chosen_amount, 2) not in valid_amounts:
            conn.close()
            return "Invalid amount selected", 400

        items.append({'category_id': cat['id'], 'name': cat['name'], 'amount': chosen_amount})
        total += chosen_amount

    for tid_str in selected_templates:
        try:
            tid = int(tid_str)
        except ValueError:
            continue
        c.execute("SELECT * FROM seva_templates WHERE id=? AND temple_id=? AND active=1", (tid, temple_id))
        tmpl = c.fetchone()
        if not tmpl:
            continue
        c.execute("""
            SELECT ti.category_id, ti.amount, cat.name as category_name
            FROM seva_template_items ti
            JOIN categories cat ON cat.id = ti.category_id
            WHERE ti.template_id=?
        """, (tid,))
        t_items = c.fetchall()
        for ti in t_items:
            items.append({'category_id': ti['category_id'], 'name': ti['category_name'], 'amount': ti['amount']})
            total += ti['amount']

    conn.close()

    if not items:
        return "Invalid selection", 400

    cart = {
        'temple_id': temple_id,
        'name': name,
        'phone': phone,
        'email': email,
        'remarks': remarks,
        'items': items,
        'total': total,
    }
    cart_json = json.dumps(cart)

    use_gateway = bool(temple['razorpay_key_id'] and temple['razorpay_key_secret'])

    order_id = None
    key_id = None

    if use_gateway:
        try:
            resp = requests.post(
                RAZORPAY_ORDER_URL,
                auth=(temple['razorpay_key_id'], temple['razorpay_key_secret']),
                json={
                    "amount": int(round(total * 100)),
                    "currency": "INR",
                    "receipt": f"tmp-{random.randint(100000,999999)}",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                order_id = resp.json().get('id')
                key_id = temple['razorpay_key_id']
            else:
                use_gateway = False
        except requests.RequestException:
            use_gateway = False

    upi_link = None
    upi_qr_data_uri = None
    if not use_gateway and temple['upi_id']:
        upi_link, upi_qr_data_uri = build_upi_qr_data_uri(
            temple['upi_id'], temple['name'], total, note=f"Seva-{name}"
        )

    return render_template(
        'checkout.html',
        temple_name=temple['name'],
        items=items,
        total=total,
        cart_json=cart_json,
        use_gateway=use_gateway,
        order_id=order_id,
        key_id=key_id,
        name=name,
        phone=phone,
        upi_id=temple['upi_id'],
        upi_link=upi_link,
        upi_qr_data_uri=upi_qr_data_uri,
    )


# ================= CONFIRM PAYMENT (gateway callback or manual confirm) =================

@app.route('/confirm_payment', methods=['POST'])
def confirm_payment():
    cart_json = request.form['cart_json']
    cart = json.loads(cart_json)

    payment_method = request.form.get('payment_method', 'manual')
    payment_id = request.form.get('razorpay_payment_id', '')
    order_id = request.form.get('razorpay_order_id', '')
    signature = request.form.get('razorpay_signature', '')

    temple_id = cart['temple_id']

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM temples WHERE id=?", (temple_id,))
    temple = c.fetchone()

    payment_status = 'paid'

    if payment_method == 'razorpay' and temple['razorpay_key_secret']:
        expected = hmac.new(
            key=temple['razorpay_key_secret'].encode(),
            msg=f"{order_id}|{payment_id}".encode(),
            digestmod=hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            conn.close()
            return "Payment verification failed", 400
        # Razorpay payments are cryptographically confirmed right here via
        # the signature check above - no further manual verification needed.
        payment_verified = 1
    else:
        # Manual/UPI-QR "I have paid" self-reports (with or without an
        # uploaded screenshot) are NOT proof of payment - a screenshot can
        # always be faked. These stay unverified until a staff member checks
        # the temple's actual bank/UPI statement and confirms it themselves.
        payment_verified = 0

    created_at = datetime.now().strftime("%d-%m-%Y %H:%M:%S")

    c.execute("SELECT COALESCE(MAX(seq_no), 0) + 1 FROM receipts WHERE temple_id=?", (temple_id,))
    seq_no = c.fetchone()[0]

    subtitle_text, line1_text, line2_text = resolve_receipt_lines(temple, cart['name'])

    receipt_id = None
    for _attempt in range(5):
        receipt_no = generate_receipt_no()
        try:
            c.execute("""
                INSERT INTO receipts
                (temple_id, receipt_no, seq_no, name, phone, email, remarks, total_amount,
                 payment_method, payment_id, payment_status, created_at, printed,
                 subtitle_text, line1_text, line2_text, payment_verified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """, (
                temple_id, receipt_no, seq_no, cart['name'], cart['phone'], cart.get('email', ''), cart['remarks'],
                cart['total'], payment_method, payment_id, payment_status, created_at,
                subtitle_text, line1_text, line2_text, payment_verified
            ))
            receipt_id = c.lastrowid
            break
        except sqlite3.IntegrityError:
            continue

    if receipt_id is None:
        conn.close()
        return "Could not generate a receipt number, please try again", 500

    for it in cart['items']:
        c.execute("""
            INSERT INTO receipt_items (receipt_id, category_id, category_name, amount)
            VALUES (?, ?, ?, ?)
        """, (receipt_id, it['category_id'], it['name'], it['amount']))

    pdf_path = build_receipt_pdf(
        temple['name'], receipt_no, seq_no, cart['phone'], cart['remarks'],
        cart['items'], cart['total'], created_at,
        subtitle_text, line1_text, line2_text,
    )

    proof_file = request.files.get('payment_proof')
    proof_path = save_payment_proof(proof_file, receipt_no)

    c.execute("UPDATE receipts SET pdf_path=?, payment_proof_path=? WHERE id=?", (pdf_path, proof_path, receipt_id))

    conn.commit()
    conn.close()

    return redirect(url_for('receipt_view', receipt_no=receipt_no))


# ================= COUNTER / RECEIPT + PRINT SCREEN =================

@app.route('/receipt/<receipt_no>')
def receipt_view(receipt_no):
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        SELECT r.*, t.name as temple_name
        FROM receipts r JOIN temples t ON t.id = r.temple_id
        WHERE r.receipt_no=?
    """, (receipt_no,))
    receipt = c.fetchone()

    if not receipt:
        conn.close()
        return "Receipt not found", 404

    c.execute("SELECT * FROM receipt_items WHERE receipt_id=?", (receipt['id'],))
    items = c.fetchall()

    conn.close()

    amount_words = amount_to_words(receipt['total_amount'])

    return render_template('receipt_counter.html', receipt=receipt, items=items, amount_words=amount_words)


@app.route('/receipt/<receipt_no>/mark_printed', methods=['POST'])
def mark_printed(receipt_no):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE receipts SET printed=1 WHERE receipt_no=?", (receipt_no,))
    conn.commit()
    conn.close()
    return ('', 204)


@app.route('/receipt/<receipt_no>/verify', methods=['POST'])
@login_required(['admin', 'user'])
def verify_payment(receipt_no):
    """Staff-only: confirms a manual/UPI payment after actually checking the
    temple's real bank/UPI statement for the matching credit. This is a
    deliberate human action, never automatic, because an uploaded screenshot
    or typed reference number can always be faked - only the temple's own
    bank receiving the money is real proof."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT temple_id FROM receipts WHERE receipt_no=?", (receipt_no,))
    row = c.fetchone()
    if not row or row['temple_id'] != session['temple_id']:
        conn.close()
        return "Not found", 404

    c.execute("""
        UPDATE receipts SET payment_verified=1, verified_by=?, verified_at=?
        WHERE receipt_no=?
    """, (session.get('staff_username', ''), datetime.now().strftime("%d-%m-%Y %H:%M:%S"), receipt_no))
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for('receipt_view', receipt_no=receipt_no))


@app.route('/static/receipts/<path:filename>')
def serve_receipt_pdf(filename):
    return send_from_directory('static/receipts', filename)


# ================= COUNTER / PRINT STATION (search + reprint) =================

@app.route('/counter')
@login_required(['admin', 'user', 'printing_user'])
def counter():
    temple_id = session['temple_id']
    q = request.args.get('q', '').strip()

    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT hide_printed_on_counter FROM temples WHERE id=?", (temple_id,))
    hide_printed = bool(c.fetchone()['hide_printed_on_counter'])

    if q:
        like = f"%{q}%"
        c.execute("""
            SELECT * FROM receipts
            WHERE temple_id=? AND (
                receipt_no LIKE ? OR phone LIKE ? OR name LIKE ? OR CAST(seq_no AS TEXT) = ?
            )
            ORDER BY id DESC LIMIT 50
        """, (temple_id, like, like, like, q))
    else:
        today = today_str()
        if hide_printed:
            c.execute("""
                SELECT * FROM receipts
                WHERE temple_id=?
                  AND date(substr(created_at,7,4) || '-' || substr(created_at,4,2) || '-' || substr(created_at,1,2)) = date(?)
                  AND printed=0
                ORDER BY id DESC LIMIT 50
            """, (temple_id, today))
        else:
            c.execute("""
                SELECT * FROM receipts
                WHERE temple_id=?
                  AND date(substr(created_at,7,4) || '-' || substr(created_at,4,2) || '-' || substr(created_at,1,2)) = date(?)
                ORDER BY id DESC LIMIT 50
            """, (temple_id, today))

    results = c.fetchall()
    conn.close()

    return render_template('counter.html', results=results, q=q, hide_printed=hide_printed)


# ================= REPORTS (custom date range) =================

@app.route('/reports')
@login_required(['admin', 'user'])
def reports():
    temple_id = session['temple_id']

    from_date = request.args.get('from_date', today_str())
    to_date = request.args.get('to_date', today_str())
    seva_filter = request.args.get('seva_id', '')
    devotee_filter = request.args.get('devotee', '').strip()
    phone_filter = request.args.get('phone', '').strip()

    conn = get_conn()
    c = conn.cursor()

    query = """
        SELECT r.* FROM receipts r
        WHERE r.temple_id=?
          AND date(substr(r.created_at,7,4) || '-' || substr(r.created_at,4,2) || '-' || substr(r.created_at,1,2))
              BETWEEN date(?) AND date(?)
    """
    params = [temple_id, from_date, to_date]

    if devotee_filter:
        query += " AND r.name LIKE ?"
        params.append(f"%{devotee_filter}%")

    if phone_filter:
        query += " AND r.phone LIKE ?"
        params.append(f"%{phone_filter}%")

    c.execute(query, params)
    receipts = c.fetchall()

    report_rows = []
    grand_total = 0.0

    for r in receipts:
        c.execute("SELECT * FROM receipt_items WHERE receipt_id=?", (r['id'],))
        items = c.fetchall()
        if seva_filter and not any(str(it['category_id']) == seva_filter for it in items):
            continue
        report_rows.append({'receipt': r, 'line_items': items})
        grand_total += r['total_amount']

    c.execute("SELECT * FROM categories WHERE temple_id=?", (temple_id,))
    all_sevas = c.fetchall()

    conn.close()

    return render_template(
        'reports.html',
        rows=report_rows, grand_total=grand_total,
        from_date=from_date, to_date=to_date,
        temple_id=temple_id,
        all_sevas=all_sevas, seva_filter=seva_filter,
        devotee_filter=devotee_filter, phone_filter=phone_filter
    )


# ================= RUN =================

if __name__ == '__main__':
    print(app.url_map)
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
