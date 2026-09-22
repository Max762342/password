"""
Sychos Net — Oracle Server v2.1
Zentraler Server: Users, Credits (2-4 dynamisch), AI-Chat, Admin
"""
import os, sys, json, time, hmac, hashlib, uuid, threading
import sqlite3, urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

HOST = os.environ.get("ORACLE_HOST", "0.0.0.0")
PORT = int(os.environ.get("ORACLE_PORT", "7777"))
API_KEY = os.environ.get("ORACLE_API_KEY", "sychos-oracle-2024")
ADMIN_KEY = os.environ.get("ORACLE_ADMIN_KEY", "sychos-admin-2024")
DB_PATH = os.environ.get("ORACLE_DB", "sychos.db")

GEMINI_KEYS = {
    "gemini-pro": "AQ.Ab8RN6Kb5jrnYfwoqO1_Bqs5oNarbQnKYkF3r-3nvuBwaQo2bA",
}

GROQ_KEYS = {
    "groq-llama": "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",
}

MODELS = {
    "gemini-pro": {"name": "Gemini Pro", "base_cost": 2, "provider": "gemini"},
    "groq-llama": {"name": "Groq Llama 3.3 70B (Free)", "base_cost": 1, "provider": "groq"},
}

STRENGTHS = {
    "low": {"name": "Low", "max_tokens": 512, "temp": 0.3},
    "medium": {"name": "Medium", "max_tokens": 2048, "temp": 0.7},
    "high": {"name": "High", "max_tokens": 4096, "temp": 1.0},
    "extra": {"name": "Extra", "max_tokens": 8192, "temp": 1.3},
}

# Dynamische Auslastungs-Tracker
request_times = []
LOAD_LOCK = threading.Lock()

def get_dynamic_cost(base_cost=2):
    """Berechnet dynamische Credits (2-4) je nach Auslastung."""
    with LOAD_LOCK:
        now = time.time()
        # Nur Anfragen der letzten 60 Sekunden zählen
        recent = [t for t in request_times if now - t < 60]
        request_times.clear()
        request_times.extend(recent)
        load = len(recent)
        request_times.append(now)
    if load < 5:
        return base_cost
    elif load < 15:
        return base_cost + 1
    else:
        return min(base_cost + 2, 4)

START_TIME = time.time()

# ═══════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            uid TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT DEFAULT '',
            credits REAL DEFAULT 50.0,
            is_banned INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            created_at REAL DEFAULT 0,
            last_login REAL DEFAULT 0,
            referral_code TEXT UNIQUE,
            referred_by TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS chats (
            id TEXT PRIMARY KEY,
            uid TEXT NOT NULL,
            title TEXT DEFAULT 'Neuer Chat',
            model TEXT DEFAULT 'gemini-2.0-flash',
            created_at REAL DEFAULT 0,
            FOREIGN KEY (uid) REFERENCES users(uid)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            model TEXT DEFAULT '',
            tokens INTEGER DEFAULT 0,
            cost REAL DEFAULT 0,
            created_at REAL DEFAULT 0,
            FOREIGN KEY (chat_id) REFERENCES chats(id)
        );
    """)
    # Ensure admin user exists
    row = db.execute("SELECT uid FROM users WHERE is_admin=1").fetchone()
    if not row:
        uid = str(uuid.uuid4())
        pw = hashlib.sha256("admin".encode()).hexdigest()
        db.execute("INSERT INTO users (uid,email,password_hash,display_name,credits,is_admin,created_at) VALUES (?,?,?,?,?,?,?)",
                   (uid, "admin@sychos.net", pw, "Admin", 99999, 1, time.time()))
        db.commit()
    db.close()

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

# ═══════════════════════════════════════════════════════════
#  GEMINI AI PROXY
# ═══════════════════════════════════════════════════════════
def call_gemini(model, messages, api_key, strength="medium", language=""):
    """Sendet Chat an Gemini API mit Stärke und Spracheinstellung."""
    if not api_key:
        return {"ok": False, "error": f"Kein API Key für {model} konfiguriert"}

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    s = STRENGTHS.get(strength, STRENGTHS["medium"])

    contents = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})

    # Sprach-Hinweis hinzufügen
    if language:
        lang_note = f"\n\n[Antworte auf {language}]"
        if contents:
            contents[-1]["parts"][0]["text"] += lang_note

    payload = json.dumps({
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": s["max_tokens"],
            "temperature": s["temp"],
        }
    }).encode()
    req = urllib.request.Request(url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            tokens = data.get("usageMetadata", {}).get("totalTokenCount", 0)
            return {"ok": True, "text": text, "tokens": tokens}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def call_groq(messages, api_key):
    """Sendet Chat an Groq API."""
    if not api_key:
        return {"ok": False, "error": "Kein Groq API Key"}
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = json.dumps({
        "model": "llama-3.3-70b-versatile",
        "messages": messages,
        "max_tokens": 4096,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            text = data["choices"][0]["message"]["content"]
            tokens = data.get("usage", {}).get("total_tokens", 0)
            return {"ok": True, "text": text, "tokens": tokens}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def proxy_openai(provider, messages):
    """OpenAI-kompatibler Proxy für Open WebUI."""
    if provider == "groq":
        return call_groq(messages, GROQ_KEYS.get("groq-llama", ""))
    else:
        return call_gemini("gemini-pro", messages, GEMINI_KEYS.get("gemini-pro", ""))

# ═══════════════════════════════════════════════════════════
#  HTTP HANDLER
# ═══════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        ts = time.strftime("%H:%M:%S")
        sys.stderr.write(f"  [{ts}] {fmt % args}\n")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With, Accept")

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        ln = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(ln)) if ln else {}

    def _get_user(self):
        """Extract user from Bearer token (uid)."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        uid = auth[7:]
        db = get_db()
        row = db.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
        db.close()
        return dict(row) if row else None

    def _check_admin(self):
        auth = self.headers.get("Authorization", "")
        return auth.startswith("Bearer ") and auth[7:] == ADMIN_KEY

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ── GET ───────────────────────────────────────────────
    def do_GET(self):
        path = urlparse(self.path).path
        sys.stderr.write(f"  GET {path}\n")

        if path == "/status":
            self._json(200, {"ok": True, "server": "Sychos Oracle", "version": "2.1.0",
                "uptime": int(time.time() - START_TIME), "load": len(request_times)})
            return

        if path == "/models":
            self._json(200, {"ok": True, "models": [
                {"id": k, "name": v["name"], "base_cost": v["base_cost"],
                 "current_cost": get_dynamic_cost(v["base_cost"])}
                for k, v in MODELS.items()
            ]})
            return

        # OpenAI-kompatibler /v1/models Endpoint
        if path == "/v1/models":
            self._json(200, {
                "object": "list",
                "data": [
                    {"id": "gemini-pro", "object": "model", "owned_by": "sychos", "name": "Gemini Pro"},
                    {"id": "groq-llama", "object": "model", "owned_by": "sychos", "name": "Groq Llama 3.3 70B (Free)"},
                ]
            })
            return

        if path == "/me":
            user = self._get_user()
            if not user:
                self._json(401, {"ok": False, "error": "Nicht angemeldet"}); return
            if user["is_banned"]:
                self._json(403, {"ok": False, "error": "Account gesperrt"}); return
            self._json(200, {"ok": True, "uid": user["uid"], "email": user["email"],
                "display_name": user["display_name"], "credits": user["credits"],
                "is_admin": user["is_admin"], "referral_code": user.get("referral_code", "")})
            return

        if path == "/chats":
            user = self._get_user()
            if not user:
                self._json(401, {"ok": False, "error": "Nicht angemeldet"}); return
            db = get_db()
            rows = db.execute("SELECT * FROM chats WHERE uid=? ORDER BY created_at DESC", (user["uid"],)).fetchall()
            db.close()
            self._json(200, {"ok": True, "chats": [dict(r) for r in rows]})
            return

        if path.startswith("/messages/"):
            cid = path.split("/")[-1]
            user = self._get_user()
            if not user:
                self._json(401, {"ok": False, "error": "Nicht angemeldet"}); return
            db = get_db()
            rows = db.execute("SELECT * FROM messages WHERE chat_id=? ORDER BY created_at", (cid,)).fetchall()
            db.close()
            self._json(200, {"ok": True, "messages": [dict(r) for r in rows]})
            return

        if path == "/admin/users":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            db = get_db()
            rows = db.execute("SELECT uid,email,display_name,credits,is_banned,is_admin FROM users").fetchall()
            db.close()
            self._json(200, {"ok": True, "users": [dict(r) for r in rows]})
            return

        self._json(404, {"ok": False, "error": "Nicht gefunden"})

    # ── POST ──────────────────────────────────────────────
    def do_POST(self):
        path = urlparse(self.path).path
        sys.stderr.write(f"  POST {path}\n")
        body = self._read_body()

        # Register
        if path == "/register":
            email = body.get("email", "").strip().lower()
            pw = body.get("password", "")
            name = body.get("display_name", email.split("@")[0])
            ref_code = body.get("referral_code", "").strip()
            if not email or not pw:
                self._json(400, {"ok": False, "error": "Email und Passwort benötigt"}); return
            db = get_db()
            if db.execute("SELECT uid FROM users WHERE email=?", (email,)).fetchone():
                db.close()
                self._json(409, {"ok": False, "error": "Email bereits registriert"}); return
            uid = str(uuid.uuid4())
            my_ref = uuid.uuid4().hex[:8].upper()
            referred_by = ""
            start_credits = 50.0
            # Referral Bonus
            if ref_code:
                ref_user = db.execute("SELECT uid, credits FROM users WHERE referral_code=?", (ref_code,)).fetchone()
                if ref_user:
                    referred_by = ref_user["uid"]
                    db.execute("UPDATE users SET credits = credits + 25 WHERE uid=?", (ref_user["uid"],))
            db.execute("INSERT INTO users (uid,email,password_hash,display_name,credits,created_at,referral_code,referred_by) VALUES (?,?,?,?,?,?,?,?)",
                       (uid, email, hash_pw(pw), name, start_credits, time.time(), my_ref, referred_by))
            db.commit(); db.close()
            self._json(200, {"ok": True, "uid": uid, "display_name": name, "credits": start_credits, "referral_code": my_ref})
            return

        # Login
        if path == "/login":
            email = body.get("email", "").strip().lower()
            pw = body.get("password", "")
            db = get_db()
            row = db.execute("SELECT * FROM users WHERE email=? AND password_hash=?",
                             (email, hash_pw(pw))).fetchone()
            if not row:
                db.close()
                self._json(401, {"ok": False, "error": "Falsche Anmeldedaten"}); return
            user = dict(row)
            if user["is_banned"]:
                db.close()
                self._json(403, {"ok": False, "error": "Account gesperrt"}); return
            db.execute("UPDATE users SET last_login=? WHERE uid=?", (time.time(), user["uid"]))
            db.commit(); db.close()
            self._json(200, {"ok": True, "uid": user["uid"], "email": user["email"],
                "display_name": user["display_name"], "credits": user["credits"],
                "is_admin": user["is_admin"]})
            return

        # Neuer Chat
        if path == "/chat/new":
            user = self._get_user()
            if not user:
                self._json(401, {"ok": False, "error": "Nicht angemeldet"}); return
            cid = str(uuid.uuid4())
            title = body.get("title", "Neuer Chat")
            model = body.get("model", "gemini-2.0-flash")
            db = get_db()
            db.execute("INSERT INTO chats (id,uid,title,model,created_at) VALUES (?,?,?,?,?)",
                       (cid, user["uid"], title, model, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True, "chat_id": cid, "title": title, "model": model})
            return

        # Chat löschen
        if path == "/chat/delete":
            user = self._get_user()
            if not user:
                self._json(401, {"ok": False, "error": "Nicht angemeldet"}); return
            cid = body.get("chat_id", "")
            db = get_db()
            db.execute("DELETE FROM messages WHERE chat_id=?", (cid,))
            db.execute("DELETE FROM chats WHERE id=? AND uid=?", (cid, user["uid"]))
            db.commit(); db.close()
            self._json(200, {"ok": True})
            return

        # OpenAI-kompatibler /v1/chat/completions Endpoint
        if path == "/v1/chat/completions":
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                self._json(401, {"error": {"message": "No API key", "type": "auth_error"}}); return
            uid = auth[7:]
            user = None
            db = get_db()
            row = db.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
            db.close()
            if not row:
                self._json(401, {"error": {"message": "User nicht gefunden", "type": "auth_error"}}); return
            user = dict(row)
            if user["is_banned"]:
                self._json(403, {"error": {"message": "Account gesperrt", "type": "auth_error"}}); return
            if user["credits"] < 2:
                self._json(402, {"error": {"message": "Keine Credits", "type": "credit_error"}}); return
            messages = body.get("messages", [])
            model = body.get("model", "gemini-pro")
            provider = MODELS.get(model, {}).get("provider", "gemini")
            result = proxy_openai(provider, messages)
            if not result["ok"]:
                self._json(500, {"error": {"message": result["error"], "type": "api_error"}}); return
            base_cost = MODELS.get(model, {}).get("base_cost", 2)
            cost = get_dynamic_cost(base_cost)
            db = get_db()
            db.execute("UPDATE users SET credits = MAX(0, credits - ?) WHERE uid=?", (cost, uid))
            db.commit(); db.close()
            remaining = max(0, user["credits"] - cost)
            self._json(200, {
                "id": f"chatcmpl-{int(time.time())}",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": result["text"]}, "finish_reason": "stop"}],
                "usage": {"total_tokens": result.get("tokens", 0)},
                "credits_remaining": remaining,
                "credits_cost": cost,
            })
            return

        # AI Chat
        if path == "/chat/send":
            user = self._get_user()
            if not user:
                self._json(401, {"ok": False, "error": "Nicht angemeldet"}); return
            if user["is_banned"]:
                self._json(403, {"ok": False, "error": "Account gesperrt"}); return
            if user["credits"] < 2:
                self._json(402, {"ok": False, "error": "Nicht genug Credits (min. 2)"}); return
            cid = body.get("chat_id", "")
            msg = body.get("message", "").strip()
            model = body.get("model", "gemini")
            strength = body.get("strength", "medium")
            language = body.get("language", "")
            if not msg:
                self._json(400, {"ok": False, "error": "Leere Nachricht"}); return
            api_key = GEMINI_KEYS.get(model, "")
            db = get_db()
            db.execute("INSERT INTO messages (chat_id,role,content,model,created_at) VALUES (?,?,?,?,?)",
                       (cid, "user", msg, model, time.time()))
            rows = db.execute("SELECT role,content FROM messages WHERE chat_id=? ORDER BY created_at", (cid,)).fetchall()
            history = [{"role": r["role"], "content": r["content"]} for r in rows]
            db.commit(); db.close()
            result = call_gemini(model, history, api_key, strength, language)
            if not result["ok"]:
                self._json(500, {"ok": False, "error": result["error"]}); return
            # Save AI response & deduct dynamic credits
            base = MODELS.get(model, {}).get("base_cost", 2)
            cost = get_dynamic_cost(base)
            db = get_db()
            db.execute("INSERT INTO messages (chat_id,role,content,model,tokens,cost,created_at) VALUES (?,?,?,?,?,?,?)",
                       (cid, "assistant", result["text"], model, result.get("tokens", 0), cost, time.time()))
            db.execute("UPDATE users SET credits = MAX(0, credits - ?) WHERE uid=?", (cost, user["uid"]))
            db.commit(); db.close()
            self._json(200, {"ok": True, "text": result["text"], "tokens": result.get("tokens", 0),
                "cost": cost, "remaining_credits": max(0, user["credits"] - cost)})
            return

        # ── Admin ─────────────────────────────────────────
        if path == "/admin/credits":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            amount = body.get("amount", 0)
            db = get_db()
            db.execute("UPDATE users SET credits = credits + ? WHERE uid=?", (amount, uid))
            db.commit(); db.close()
            self._json(200, {"ok": True})
            return

        if path == "/admin/ban":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            ban = 1 if body.get("ban", True) else 0
            db = get_db()
            db.execute("UPDATE users SET is_banned=? WHERE uid=?", (ban, uid))
            db.commit(); db.close()
            self._json(200, {"ok": True, "banned": bool(ban)})
            return

        self._json(404, {"ok": False, "error": "Nicht gefunden"})


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════
def main():
    init_db()
    print(f"""
  ╔══════════════════════════════════════════════╗
  ║       S Y C H O S   O R A C L E  v2.0       ║
  ╠══════════════════════════════════════════════╣
  ║  Host     : {HOST}:{PORT}
  ║  API Key  : {API_KEY}
  ║  Admin Key: {ADMIN_KEY}
  ║  Datenbank: {DB_PATH}
  ╠══════════════════════════════════════════════╣
  ║  Admin Login: admin@sychos.net / admin       ║
  ╚══════════════════════════════════════════════╝
    """)
    print(f"  → http://{HOST}:{PORT}")
    print(f"  → Ctrl+C zum Beenden\n")
    server = HTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server gestoppt.")
        server.server_close()

if __name__ == "__main__":
    main()