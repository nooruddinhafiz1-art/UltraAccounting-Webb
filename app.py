"""
UltraAccounting - Multi-Shop Billing Software (Backend)
Developer: 7602292599 | support@ultraaccounting.com
"""
import os
import re
import json
import time
import secrets
import hashlib
from datetime import datetime, timedelta, timezone
import sqlite3
from flask import Flask, request, jsonify, send_from_directory, Response
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IST = timezone(timedelta(hours=5, minutes=30))

from urllib.parse import quote_plus, unquote


def _clean(v):
    """Render me paste karte waqt aa jaane wale quotes / space / newline hata do."""
    return (v or "").strip().strip('"').strip("'").strip()


DATABASE_URL = _clean(os.environ.get("DATABASE_URL")) or None
_DB_HOST = _clean(os.environ.get("DB_HOST"))
if _DB_HOST:  # alag-alag variables: password me special character ho to bhi chalega
    DATABASE_URL = "postgresql://%s:%s@%s:%s/%s" % (
        quote_plus(_clean(os.environ.get("DB_USER")) or "postgres"),
        quote_plus(os.environ.get("DB_PASSWORD", "").strip()),
        _DB_HOST, _clean(os.environ.get("DB_PORT")) or "5432", _clean(os.environ.get("DB_NAME")) or "postgres")
SQLITE_FILE = os.environ.get("SQLITE_FILE", "ultra_accounting.db")

if DATABASE_URL:
    import psycopg2
    import psycopg2.pool
    import threading
    PH = "%s"
else:
    PH = "?"

app = Flask(__name__, static_folder=None)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def _web_dir():
    for d in (os.path.join(BASE_DIR, "static"), BASE_DIR):
        if os.path.exists(os.path.join(d, "index.html")):
            return d
    return BASE_DIR


WEB_DIR = _web_dir()


def _explain_db_error(err):
    """Connection fail ho to Render logs me saaf bataye ki kya galat hai (password print nahi hota)."""
    try:
        m = re.match(r"^[a-z]+://([^:@/]*):(.*)@([^@/:]+):?(\d*)/", DATABASE_URL)
        user, pw, host, port = (unquote(m.group(1)), unquote(m.group(2)), m.group(3), m.group(4)) if m else ("?", "", "?", "")
        print("=" * 60, flush=True)
        print("DB CHECK -> user=%r host=%r port=%r password_length=%d" % (user, host, port, len(pw)), flush=True)
        if "pooler.supabase.com" in host and "." not in user:
            print("FIX: Pooler ke liye username 'postgres.<project-ref>' hona chahiye.", flush=True)
        if host.startswith("db.") and host.endswith(".supabase.co"):
            print("FIX: Direct connection (IPv6) hai - Render par Session pooler wala host use karein.", flush=True)
        if pw.startswith("[") or pw.endswith("]"):
            print("FIX: Password me [ ] brackets hain - hata dein.", flush=True)
        print("FIX: Password galat ho to Supabase > Project Settings > Database > Reset database password.", flush=True)
        print("=" * 60, flush=True)
    except Exception:
        pass


_pool = None
_pool_pid = None
_pool_lock = None
_last_used = {}
PG_POOL_MAX = int(os.environ.get("PG_POOL_MAX", "4"))
PG_IDLE_PING = 20  # itne second idle rehne wale connection ko use se pehle check karo


class _PooledConn:
    """psycopg2 connection ka wrapper: close() = connection pool me wapas (har request par naya connection nahi banta)."""

    def __init__(self, c, pooled):
        self._c = c
        self._pooled = pooled

    def cursor(self, *a, **k):
        return self._c.cursor(*a, **k)

    def commit(self):
        self._c.commit()

    def rollback(self):
        self._c.rollback()

    def close(self):
        c, self._c = self._c, None
        if c is None:
            return
        try:
            if not c.closed:
                try:
                    c.rollback()
                except Exception:
                    pass
        finally:
            if self._pooled:
                try:
                    _last_used[id(c)] = time.time()
                    _pool.putconn(c, close=bool(c.closed))
                except Exception:
                    pass
            else:
                try:
                    c.close()
                except Exception:
                    pass

    def __del__(self):  # agar koi code path close() bhool jaye to bhi connection wapas aa jaye
        try:
            self.close()
        except Exception:
            pass


def _pg_connect():
    global _pool, _pool_pid, _pool_lock
    if _pool_lock is None:
        _pool_lock = threading.Lock()
    with _pool_lock:
        if _pool is None or _pool_pid != os.getpid():
            _pool = psycopg2.pool.ThreadedConnectionPool(
                1, PG_POOL_MAX, DATABASE_URL, connect_timeout=15,
                keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)
            _pool_pid = os.getpid()
    for _ in range(3):
        try:
            c = _pool.getconn()
        except psycopg2.pool.PoolError:  # pool bhar gaya - alag se ek connection do
            return _PooledConn(psycopg2.connect(DATABASE_URL, connect_timeout=15), False)
        if c.closed:
            _pool.putconn(c, close=True)
            continue
        if time.time() - _last_used.get(id(c), 0) > PG_IDLE_PING:
            try:  # purana connection server ne band kar diya ho to yahin pata chal jayega
                cur = c.cursor()
                cur.execute("SELECT 1")
                c.rollback()
            except Exception:
                _pool.putconn(c, close=True)
                continue
        return _PooledConn(c, True)
    return _PooledConn(psycopg2.connect(DATABASE_URL, connect_timeout=15), False)


def get_db():
    if DATABASE_URL:
        try:
            return _pg_connect()
        except psycopg2.Error as e:
            _explain_db_error(e)
            raise
    return sqlite3.connect(SQLITE_FILE, timeout=20)


def init_db():
    conn = get_db()
    cur = conn.cursor()
    # Shops table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS shops (
            id TEXT PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            owner TEXT,
            mobile TEXT,
            email TEXT,
            address TEXT,
            gst_no TEXT,
            logo TEXT,
            bill_prefix TEXT DEFAULT 'INV',
            print_size TEXT DEFAULT 'A4',
            plan TEXT DEFAULT 'trial',
            active_until BIGINT,
            created_at BIGINT,
            active INTEGER DEFAULT 1
        )
    """)
    # Users table (shop admins, managers, cashiers)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            shop_id TEXT NOT NULL,
            username TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            name TEXT,
            active INTEGER DEFAULT 1,
            created_at BIGINT,
            perms TEXT,
            UNIQUE(shop_id, username)
        )
    """)
    # Records table (all data: bills, items, customers, etc.)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS records (
            shop_id TEXT NOT NULL,
            store TEXT NOT NULL,
            id TEXT NOT NULL,
            data TEXT NOT NULL,
            updated_at BIGINT NOT NULL,
            PRIMARY KEY (shop_id, store, id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS records_updated_idx ON records (shop_id, updated_at)")
    # Super admin
    cur.execute("""
        CREATE TABLE IF NOT EXISTS super_admin (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at BIGINT
        )
    """)
    conn.commit()
    conn.close()

    # Migration: users.perms (staff permissions) - purane database me column add karo
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE users ADD COLUMN perms TEXT")
        conn.commit()
    except Exception:
        conn.rollback()
    conn.close()

    # Default super admin
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM super_admin")
    if cur.fetchone()[0] == 0:
        cur.execute(
            "INSERT INTO super_admin (id, username, password_hash, created_at) VALUES (" + ",".join([PH]*4) + ")",
            ("sa-1", "superadmin", hash_pw("ultra@2026"), int(time.time()))
        )
        conn.commit()
    conn.close()


def hash_pw(pw):
    salt = secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100000).hex()


def check_pw(pw, stored):
    try:
        return secrets.compare_digest(hash_pw_part(pw, stored.split("$")[0]), stored)
    except Exception:
        return False


def hash_pw_part(pw, salt):
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100000).hex()


init_db()


# ============================ Pages ============================
@app.route("/")
def home():
    resp = send_from_directory(WEB_DIR, "index.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/manifest.json")
def manifest():
    return send_from_directory(WEB_DIR, "manifest.json", mimetype="application/manifest+json")


@app.route("/sw.js")
def sw():
    resp = send_from_directory(WEB_DIR, "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/icon.svg")
def icon():
    return send_from_directory(WEB_DIR, "icon.svg", mimetype="image/svg+xml")


_ICON_FILES = {
    "UltraAccounting.png": "image/png", "icon-192.png": "image/png", "icon-512.png": "image/png",
    "icon-maskable-512.png": "image/png", "apple-touch-icon.png": "image/png", "UltraAccounting.ico": "image/x-icon",
}


@app.route("/<fname>")
def icon_files(fname):
    if fname in _ICON_FILES:
        resp = send_from_directory(WEB_DIR, fname, mimetype=_ICON_FILES[fname])
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp
    return jsonify({"error": "not found"}), 404


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(WEB_DIR, "UltraAccounting.ico", mimetype="image/x-icon")


@app.route("/super")
def super_page():
    resp = send_from_directory(WEB_DIR, "super.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ============================ Health ============================
@app.route("/api/ping")
def ping():
    return jsonify({"ok": True, "app": "ultra-accounting"})


# ============================ Shop Login ============================
@app.route("/api/login", methods=["POST"])
def login():
    """Shop code + username + password se login."""
    j = request.get_json(force=True, silent=True) or {}
    code = str(j.get("code", "")).strip().upper()
    username = str(j.get("username", "")).strip().lower()
    password = str(j.get("password", ""))
    if not code or not username or not password:
        return jsonify({"error": "Shop code, username aur password zaroori hai"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, code, name, active FROM shops WHERE code = " + PH, (code,))
    shop = cur.fetchone()
    if not shop or not shop[3]:
        conn.close()
        return jsonify({"error": "Shop code galat hai ya shop inactive hai"}), 401
    shop_id, shop_code, shop_name = shop[0], shop[1], shop[2]
    cur.execute(
        "SELECT id, username, password_hash, role, name, active, perms FROM users WHERE shop_id = " + PH + " AND username = " + PH,
        (shop_id, username)
    )
    u = cur.fetchone()
    conn.close()
    if not u or not u[5] or not check_pw(password, u[2]):
        return jsonify({"error": "Username ya password galat hai"}), 401
    token = make_token({"user_id": u[0], "shop_id": shop_id, "role": u[3], "name": u[4], "username": u[1], "ts": time.time()})
    return jsonify({
        "ok": True,
        "token": token,
        "shop": {"id": shop_id, "code": shop_code, "name": shop_name},
        "user": {"id": u[0], "username": u[1], "role": u[3], "name": u[4], "perms": _perms_of(u[3], u[6])}
    })


def _secret():
    """Secret key: env SECRET_KEY > DATABASE_URL se derive > local file."""
    k = os.environ.get("SECRET_KEY")
    if k:
        return k
    if DATABASE_URL:
        return hashlib.sha256(("ua|" + DATABASE_URL).encode()).hexdigest()
    path = os.path.join(BASE_DIR, ".secret_key")
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        k = secrets.token_hex(32)
        try:
            with open(path, "w") as f:
                f.write(k)
        except OSError:
            pass
        return k


_ser = None
try:
    from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
    _ser = URLSafeTimedSerializer(_secret(), salt="ua-auth")
except ImportError:
    pass

SHOP_TOKEN_AGE = 86400 * 180
SUPER_TOKEN_AGE = 3600 * 12


def make_token(payload):
    """Stateless signed token (server restart / multi-worker me bhi chalega)."""
    if _ser:
        return _ser.dumps(payload)
    return secrets.token_hex(32)


_auth_cache = {}


def _auth(req):
    """Token se user verify karo."""
    tok = req.headers.get("X-Auth-Token") or (req.get_json(silent=True) or {}).get("_token")
    if not tok:
        return None

    sess = None
    if _ser:
        try:
            sess = _ser.loads(tok, max_age=SHOP_TOKEN_AGE)
        except Exception:
            return None
    else:
        return None

    if sess.get("super"):
        if time.time() - sess.get("ts", 0) > SUPER_TOKEN_AGE:
            return None
        return sess
    # Shop session: shop/user abhi bhi active hona chahiye (block karne ke ~20 sec ke andar band ho jata hai)
    ck = (sess.get("shop_id"), sess.get("user_id"))
    hit = _auth_cache.get(ck)
    if hit and hit > time.time():
        return sess
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT s.active, u.active FROM shops s JOIN users u ON u.shop_id = s.id WHERE s.id = " + PH + " AND u.id = " + PH,
        (sess.get("shop_id"), sess.get("user_id"))
    )
    r = cur.fetchone()
    conn.close()
    if not r or not r[0] or not r[1]:
        _auth_cache.pop(ck, None)
        return None
    _auth_cache[ck] = time.time() + 20
    return sess


ALL_PERMS = ["billing", "bills", "bill_edit", "bill_delete", "receive", "customers", "items", "purchases", "reports", "ledger", "expenses"]


def _perms_of(role, raw):
    if role == "admin":
        return ["*"]
    try:
        return [p for p in json.loads(raw or "[]") if p in ALL_PERMS]
    except (ValueError, TypeError):
        return []


def _shop_admin(req):
    sess = _auth(req)
    if not sess or sess.get("super") or sess.get("role") != "admin":
        return None
    return sess


# ============================ Staff (shop admin only) ============================
@app.route("/api/staff", methods=["GET"])
def staff_list():
    sess = _shop_admin(request)
    if not sess:
        return jsonify({"error": "Sirf shop admin ye kar sakta hai"}), 403
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, name, role, active, perms, created_at FROM users WHERE shop_id = " + PH + " ORDER BY created_at ASC", (sess["shop_id"],))
    out = [{"id": r[0], "username": r[1], "name": r[2], "role": r[3], "active": bool(r[4]),
            "perms": _perms_of(r[3], r[5]), "created_at": r[6]} for r in cur.fetchall()]
    conn.close()
    return jsonify(out)


def _clean_perms(v):
    return [p for p in (v if isinstance(v, list) else []) if p in ALL_PERMS]


@app.route("/api/staff", methods=["POST"])
def staff_create():
    sess = _shop_admin(request)
    if not sess:
        return jsonify({"error": "Sirf shop admin ye kar sakta hai"}), 403
    j = request.get_json(force=True, silent=True) or {}
    username = str(j.get("username", "")).strip().lower()
    name = str(j.get("name", "")).strip()
    pw = str(j.get("password", "")).strip()
    if not re.fullmatch(r"[a-z0-9_.-]{3,20}", username):
        return jsonify({"error": "Username 3-20 letters/numbers ka ho (spaces nahi)"}), 400
    if not name:
        return jsonify({"error": "Naam zaroori hai"}), 400
    if len(pw) < 6:
        return jsonify({"error": "Password kam se kam 6 character"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM users WHERE shop_id = " + PH + " AND username = " + PH, (sess["shop_id"], username))
    if cur.fetchone():
        conn.close()
        return jsonify({"error": "Ye username pehle se hai"}), 400
    cur.execute(
        "INSERT INTO users (id, shop_id, username, password_hash, role, name, active, created_at, perms) VALUES (" + ",".join([PH] * 9) + ")",
        ("user-" + secrets.token_hex(8), sess["shop_id"], username, hash_pw(pw), "staff", name, 1, int(time.time()),
         json.dumps(_clean_perms(j.get("perms"))))
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/staff/update", methods=["POST"])
def staff_update():
    sess = _shop_admin(request)
    if not sess:
        return jsonify({"error": "Sirf shop admin ye kar sakta hai"}), 403
    j = request.get_json(force=True, silent=True) or {}
    uid_ = str(j.get("id", "")).strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT role FROM users WHERE id = " + PH + " AND shop_id = " + PH, (uid_, sess["shop_id"]))
    r = cur.fetchone()
    if not r:
        conn.close()
        return jsonify({"error": "Staff nahi mila"}), 404
    if r[0] == "admin":
        conn.close()
        return jsonify({"error": "Admin ko yahan se nahi badal sakte"}), 400
    name = str(j.get("name", "")).strip()
    if not name:
        conn.close()
        return jsonify({"error": "Naam zaroori hai"}), 400
    cur.execute("UPDATE users SET name = " + PH + ", perms = " + PH + ", active = " + PH + " WHERE id = " + PH,
                (name, json.dumps(_clean_perms(j.get("perms"))), 1 if j.get("active", True) else 0, uid_))
    pw = str(j.get("password", "")).strip()
    if pw:
        if len(pw) < 6:
            conn.close()
            return jsonify({"error": "Password kam se kam 6 character"}), 400
        cur.execute("UPDATE users SET password_hash = " + PH + " WHERE id = " + PH, (hash_pw(pw), uid_))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/staff/delete", methods=["POST"])
def staff_delete():
    sess = _shop_admin(request)
    if not sess:
        return jsonify({"error": "Sirf shop admin ye kar sakta hai"}), 403
    j = request.get_json(force=True, silent=True) or {}
    uid_ = str(j.get("id", "")).strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id = " + PH + " AND shop_id = " + PH + " AND role <> 'admin'", (uid_, sess["shop_id"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ============================ Records Sync ============================
@app.route("/api/records", methods=["GET"])
def get_records():
    sess = _auth(request)
    if not sess:
        return jsonify({"error": "unauthorized"}), 401
    since = int(request.args.get("since", 0) or 0)
    limit = min(int(request.args.get("limit", 500) or 500), 1000)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT store, id, data, updated_at FROM records WHERE shop_id = " + PH +
        " AND updated_at > " + PH + " ORDER BY updated_at ASC LIMIT " + PH,
        (sess["shop_id"], since, limit)
    )
    rows = [
        {"store": r[0], "id": r[1], "data": json.loads(r[2]), "updated_at": int(r[3])}
        for r in cur.fetchall()
    ]
    conn.close()
    return jsonify(rows)


def _lock(cur):
    if DATABASE_URL:
        cur.execute("SELECT pg_advisory_xact_lock(7002)")
    else:
        cur.execute("BEGIN IMMEDIATE")


def _next_ts(cur, shop_id):
    cur.execute("SELECT COALESCE(MAX(updated_at), 0) FROM records WHERE shop_id = " + PH, (shop_id,))
    mx = int(cur.fetchone()[0] or 0)
    return max(int(time.time() * 1000), mx + 1)


@app.route("/api/records", methods=["POST"])
def post_records():
    sess = _auth(request)
    if not sess:
        return jsonify({"error": "unauthorized"}), 401
    rows = request.get_json(force=True, silent=True) or []
    if not isinstance(rows, list):
        return jsonify({"error": "list expected"}), 400
    shop_id = sess["shop_id"]
    conn = get_db()
    cur = conn.cursor()
    n = 0
    try:
        _lock(cur)
        ts = _next_ts(cur, shop_id)
        for r in rows:
            try:
                store, rid, data = str(r["store"]), str(r["id"]), r["data"]
                cu = int(data.get("_u") or r.get("updated_at") or 0)
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
            data["_u"] = cu
            cur.execute(
                "SELECT data FROM records WHERE shop_id = " + PH + " AND store = " + PH + " AND id = " + PH,
                (shop_id, store, rid)
            )
            old = cur.fetchone()
            if old:
                try:
                    ou = int(json.loads(old[0]).get("_u") or 0)
                except (ValueError, TypeError):
                    ou = 0
                if cu < ou:
                    continue
                cur.execute(
                    "UPDATE records SET data = " + PH + ", updated_at = " + PH +
                    " WHERE shop_id = " + PH + " AND store = " + PH + " AND id = " + PH,
                    (json.dumps(data), ts, shop_id, store, rid)
                )
            else:
                cur.execute(
                    "INSERT INTO records (shop_id, store, id, data, updated_at) VALUES (" + ",".join([PH]*5) + ")",
                    (shop_id, store, rid, json.dumps(data), ts)
                )
            ts += 1
            n += 1
        conn.commit()
    finally:
        conn.close()
    return jsonify({"saved": n})


@app.route("/api/summary")
def summary():
    sess = _auth(request)
    if not sess:
        return jsonify({"error": "unauthorized"}), 401
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT store, COUNT(*), MAX(updated_at) FROM records WHERE shop_id = " + PH + " GROUP BY store",
        (sess["shop_id"],)
    )
    out = {r[0]: {"count": int(r[1]), "max": int(r[2])} for r in cur.fetchall()}
    conn.close()
    return jsonify(out)


# ============================ Data Reset (Fresh Start) ============================
@app.route("/api/reset-data", methods=["POST"])
def reset_data():
    """Shop ka data delete karta hai (Cloud se bhi)."""
    sess = _shop_admin(request) 
    if not sess:
        return jsonify({"error": "Sirf Owner data delete kar sakta hai"}), 403
    
    j = request.get_json(force=True, silent=True) or {}
    mode = str(j.get("mode", "bills")).strip() 
    
    shop_id = sess["shop_id"]
    conn = get_db()
    cur = conn.cursor()
    
    try:
        if mode == "all":
            # Sab kuch delete
            cur.execute("DELETE FROM records WHERE shop_id = " + PH, (shop_id,))
        else:
            # Sirf Bills aur related data delete (Stock bachega)
            stores_to_delete = ('bills', 'customers', 'payments', 'parties', 'entries', 'expenses')
            for st in stores_to_delete:
                cur.execute("DELETE FROM records WHERE shop_id = " + PH + " AND store = " + PH, (shop_id, st))
        
        conn.commit()
        
        # Supabase / Postgres me space free karne ke liye VACUUM
        if DATABASE_URL:
            try:
                cur.execute("VACUUM")
                conn.commit()
            except Exception:
                conn.rollback()
            
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500
    
    conn.close()
    return jsonify({"ok": True, "message": "Data delete ho gaya"})


# ============================ Super Admin ============================
@app.route("/api/super/login", methods=["POST"])
def super_login():
    j = request.get_json(force=True, silent=True) or {}
    username = str(j.get("username", "")).strip().lower()
    password = str(j.get("password", ""))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, password_hash FROM super_admin WHERE username = " + PH, (username,))
    r = cur.fetchone()
    conn.close()
    if not r or not check_pw(password, r[1]):
        return jsonify({"error": "Galat username ya password"}), 401
    token = make_token({"super": True, "id": r[0], "username": username, "ts": time.time()})
    return jsonify({"ok": True, "token": token})


def _super_auth(req):
    sess = _auth(req)
    if not sess or not sess.get("super"):
        return None
    return sess


@app.route("/api/super/shops", methods=["GET"])
def super_shops():
    if not _super_auth(request):
        return jsonify({"error": "unauthorized"}), 401
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, code, name, owner, mobile, email, address, gst_no, logo, bill_prefix, print_size, plan, active_until, created_at, active FROM shops ORDER BY created_at DESC")
    rows = cur.fetchall()
    out = []
    for r in rows:
        cur.execute("SELECT COUNT(*) FROM records WHERE shop_id = " + PH + " AND store = 'bills'", (r[0],))
        bills = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM users WHERE shop_id = " + PH, (r[0],))
        users = cur.fetchone()[0]
        cur.execute("SELECT username FROM users WHERE shop_id = " + PH + " AND role = 'admin' ORDER BY created_at ASC", (r[0],))
        adm = cur.fetchone()
        cur.execute("SELECT MAX(updated_at) FROM records WHERE shop_id = " + PH, (r[0],))
        last = cur.fetchone()[0]
        out.append({
            "admin_username": adm[0] if adm else "",
            "last_activity": int(last) // 1000 if last else None,
            "id": r[0], "code": r[1], "name": r[2], "owner": r[3], "mobile": r[4],
            "email": r[5], "address": r[6], "gst_no": r[7], "logo": r[8],
            "bill_prefix": r[9], "print_size": r[10], "plan": r[11],
            "active_until": r[12], "created_at": r[13], "active": bool(r[14]),
            "bills_count": bills, "users_count": users
        })
    conn.close()
    return jsonify(out)


@app.route("/api/super/shops", methods=["POST"])
def super_create_shop():
    if not _super_auth(request):
        return jsonify({"error": "unauthorized"}), 401
    j = request.get_json(force=True, silent=True) or {}
    code = str(j.get("code", "")).strip().upper()
    name = str(j.get("name", "")).strip()
    owner = str(j.get("owner", "")).strip()
    mobile = str(j.get("mobile", "")).strip()
    address = str(j.get("address", "")).strip()
    gst_no = str(j.get("gst_no", "")).strip()
    plan = str(j.get("plan", "trial")).strip()
    try:
        months = max(1, min(int(j.get("months", 1)), 120))
    except (TypeError, ValueError):
        months = 1
    if plan not in ("trial", "basic", "pro"):
        plan = "trial"
    admin_username = (str(j.get("admin_username", "admin")).strip().lower() or "admin")
    admin_password = str(j.get("admin_password", "")).strip()
    if not code or not name or not admin_password:
        return jsonify({"error": "code, name, admin_password zaroori hai"}), 400
    if not re.fullmatch(r"[A-Z0-9_-]{3,20}", code):
        return jsonify({"error": "Shop code 3-20 letters/numbers ka ho (spaces nahi)"}), 400
    if len(admin_password) < 6:
        return jsonify({"error": "Password kam se kam 6 character"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM shops WHERE code = " + PH, (code,))
    if cur.fetchone():
        conn.close()
        return jsonify({"error": "Ye shop code pehle se hai"}), 400
    shop_id = "shop-" + secrets.token_hex(8)
    now = int(time.time())
    active_until = now + months * 30 * 86400
    cur.execute(
        "INSERT INTO shops (id, code, name, owner, mobile, email, address, gst_no, logo, bill_prefix, print_size, plan, active_until, created_at, active) VALUES (" + ",".join([PH]*15) + ")",
        (shop_id, code, name, owner, mobile, "", address, gst_no, "", "INV", "A4", plan, active_until, now, 1)
    )
    user_id = "user-" + secrets.token_hex(8)
    cur.execute(
        "INSERT INTO users (id, shop_id, username, password_hash, role, name, active, created_at) VALUES (" + ",".join([PH]*8) + ")",
        (user_id, shop_id, admin_username, hash_pw(admin_password), "admin", owner or name, 1, now)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "shop_id": shop_id, "code": code, "admin_username": admin_username})


@app.route("/api/super/reset-shop-password", methods=["POST"])
def super_reset_shop_pw():
    if not _super_auth(request):
        return jsonify({"error": "unauthorized"}), 401
    j = request.get_json(force=True, silent=True) or {}
    shop_id = str(j.get("shop_id", "")).strip()
    new_pw = str(j.get("new_password", "")).strip()
    if len(new_pw) < 6:
        return jsonify({"error": "Password kam se kam 6 character"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET password_hash = " + PH + " WHERE shop_id = " + PH + " AND role = 'admin'",
        (hash_pw(new_pw), shop_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/super/toggle-shop", methods=["POST"])
def super_toggle_shop():
    if not _super_auth(request):
        return jsonify({"error": "unauthorized"}), 401
    j = request.get_json(force=True, silent=True) or {}
    shop_id = str(j.get("shop_id", "")).strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT active FROM shops WHERE id = " + PH, (shop_id,))
    r = cur.fetchone()
    if not r:
        conn.close()
        return jsonify({"error": "shop not found"}), 404
    new_val = 0 if r[0] else 1
    cur.execute("UPDATE shops SET active = " + PH + " WHERE id = " + PH, (new_val, shop_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "active": bool(new_val)})


@app.route("/api/super/update-shop", methods=["POST"])
def super_update_shop():
    """Shop ki details / plan / validity edit karo."""
    if not _super_auth(request):
        return jsonify({"error": "unauthorized"}), 401
    j = request.get_json(force=True, silent=True) or {}
    shop_id = str(j.get("shop_id", "")).strip()
    name = str(j.get("name", "")).strip()
    if not name:
        return jsonify({"error": "Shop name zaroori hai"}), 400
    plan = str(j.get("plan", "trial")).strip()
    if plan not in ("trial", "basic", "pro"):
        return jsonify({"error": "Plan galat hai"}), 400
    active_until = None
    vt = str(j.get("valid_till", "")).strip()
    if vt:
        try:
            dt = datetime.strptime(vt, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=IST)
            active_until = int(dt.timestamp())
        except ValueError:
            return jsonify({"error": "Valid-till date galat hai"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM shops WHERE id = " + PH, (shop_id,))
    if not cur.fetchone():
        conn.close()
        return jsonify({"error": "shop not found"}), 404
    cur.execute(
        "UPDATE shops SET name = " + PH + ", owner = " + PH + ", mobile = " + PH + ", email = " + PH +
        ", address = " + PH + ", gst_no = " + PH + ", plan = " + PH + ", active_until = " + PH + " WHERE id = " + PH,
        (name, str(j.get("owner", "")).strip(), str(j.get("mobile", "")).strip(), str(j.get("email", "")).strip(),
         str(j.get("address", "")).strip(), str(j.get("gst_no", "")).strip().upper(), plan, active_until, shop_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/super/delete-shop", methods=["POST"])
def super_delete_shop():
    """Shop + uske saare users aur data permanently delete. Shop code type karke confirm karna padta hai."""
    if not _super_auth(request):
        return jsonify({"error": "unauthorized"}), 401
    j = request.get_json(force=True, silent=True) or {}
    shop_id = str(j.get("shop_id", "")).strip()
    confirm = str(j.get("confirm_code", "")).strip().upper()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT code FROM shops WHERE id = " + PH, (shop_id,))
    r = cur.fetchone()
    if not r:
        conn.close()
        return jsonify({"error": "shop not found"}), 404
    if r[0] != confirm:
        conn.close()
        return jsonify({"error": "Shop code match nahi hua"}), 400
    cur.execute("DELETE FROM records WHERE shop_id = " + PH, (shop_id,))
    cur.execute("DELETE FROM users WHERE shop_id = " + PH, (shop_id,))
    cur.execute("DELETE FROM shops WHERE id = " + PH, (shop_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/super/change-password", methods=["POST"])
def super_change_password():
    sess = _super_auth(request)
    if not sess:
        return jsonify({"error": "unauthorized"}), 401
    j = request.get_json(force=True, silent=True) or {}
    cur_pw = str(j.get("current_password", ""))
    new_pw = str(j.get("new_password", ""))
    if len(new_pw) < 8:
        return jsonify({"error": "Naya password kam se kam 8 character ka ho"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT password_hash FROM super_admin WHERE id = " + PH, (sess["id"],))
    r = cur.fetchone()
    if not r or not check_pw(cur_pw, r[0]):
        conn.close()
        return jsonify({"error": "Purana password galat hai"}), 400
    cur.execute("UPDATE super_admin SET password_hash = " + PH + " WHERE id = " + PH, (hash_pw(new_pw), sess["id"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)