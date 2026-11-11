import os, json, sqlite3, secrets, html, time, threading, smtplib, datetime as dt
from email.message import EmailMessage
from fastapi import FastAPI, Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

DB = os.environ.get("DB_PATH", "chasr.db")
app = FastAPI(title="Chasr")
esc = html.escape


DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    import psycopg2, psycopg2.extras


class _Res:
    def __init__(self, cur, lastrowid=None):
        self.cur, self.lastrowid = cur, lastrowid

    def fetchone(self): return self.cur.fetchone()
    def fetchall(self): return self.cur.fetchall()


class PG:
    def __init__(self):
        self.c = psycopg2.connect(DATABASE_URL)

    def execute(self, sql, params=()):
        cur = self.c.cursor(cursor_factory=psycopg2.extras.DictCursor)
        sql = sql.replace("?", "%s")
        ret = sql.strip().upper().startswith("INSERT INTO INVOICES")
        if ret: sql += " RETURNING id"
        cur.execute(sql, params)
        return _Res(cur, cur.fetchone()[0] if ret else None)

    def commit(self): self.c.commit()


def db():
    if DATABASE_URL:
        return PG()
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init():
    c = db()
    if DATABASE_URL:
        c.execute("CREATE TABLE IF NOT EXISTS accounts(key TEXT PRIMARY KEY, name TEXT, created DOUBLE PRECISION)")
        c.execute("""CREATE TABLE IF NOT EXISTS invoices(
          id SERIAL PRIMARY KEY, pub TEXT UNIQUE, key TEXT, number TEXT, client_name TEXT, client_email TEXT,
          currency TEXT, items TEXT, tax DOUBLE PRECISION, due TEXT, notes TEXT, status TEXT,
          created DOUBLE PRECISION, last_reminded DOUBLE PRECISION, reminders INTEGER DEFAULT 0)""")
    else:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS accounts(key TEXT PRIMARY KEY, name TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS invoices(
          id INTEGER PRIMARY KEY AUTOINCREMENT, pub TEXT UNIQUE, key TEXT, number TEXT,
          client_name TEXT, client_email TEXT, currency TEXT, items TEXT, tax REAL,
          due TEXT, notes TEXT, status TEXT, created REAL, last_reminded REAL, reminders INTEGER DEFAULT 0);
        """)
    c.commit()


init()


# ---------- helpers ----------
def today():
    return dt.date.today().isoformat()


def totals(inv):
    items = json.loads(inv["items"])
    sub = sum(float(i.get("qty", 1)) * float(i.get("unit_price", 0)) for i in items)
    tax = sub * float(inv["tax"] or 0) / 100
    return sub, tax, sub + tax


def eff_status(inv):
    if inv["status"] == "sent" and inv["due"] and inv["due"] < today():
        return "overdue"
    return inv["status"]


def money(cur, v):
    return f"{cur} {v:,.2f}"


def base(request):
    return os.environ.get("BASE_URL") or str(request.base_url).rstrip("/")


CSS = """<style>
body{font-family:system-ui,sans-serif;max-width:900px;margin:32px auto;padding:0 16px;color:#111}
h1{margin:0 0 4px}a{color:#4f46e5}table{width:100%;border-collapse:collapse;margin:16px 0}
td,th{padding:8px;border-bottom:1px solid #eee;text-align:left}
.b{padding:2px 8px;border-radius:99px;font-size:12px;background:#eee}
.paid{background:#d1fae5}.overdue{background:#fee2e2}.sent{background:#dbeafe}
input,textarea,select{width:100%;padding:8px;margin:4px 0 12px;box-sizing:border-box;font:inherit}
button{background:#4f46e5;color:#fff;border:0;padding:10px 16px;border-radius:8px;cursor:pointer;font:inherit}
.card{border:1px solid #e5e7eb;border-radius:12px;padding:16px;margin:12px 0}
code{background:#f3f4f6;padding:2px 6px;border-radius:6px;word-break:break-all}
.stats{display:flex;gap:12px}.stats .card{flex:1}
</style>"""


def page(title, body):
    return HTMLResponse(f"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
                        f"<title>{esc(title)}</title>{CSS}{body}")


def acct_from(request):
    k = request.cookies.get("key", "")
    c = db()
    return c.execute("SELECT * FROM accounts WHERE key=?", (k,)).fetchone()


# ---------- web ----------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return page("Chasr", """<h1>Chasr</h1><p><b>Invoices that chase themselves.</b>
    Connect your Claude, say "invoice Acme for the March design work, due in 14 days" and get a live,
    editable invoice link that automatically reminds your client when it's overdue.</p>
    <div class=card><form method=post action=/signup><label>Business name</label>
    <input name=name required><button>Create workspace</button></form></div>
    <div class=card><form method=post action=/login><label>Already have a key?</label>
    <input name=key placeholder="ck_..." required><button>Log in</button></form></div>""")


@app.post("/signup")
def signup(name: str = Form(...)):
    key = "ck_" + secrets.token_urlsafe(18)
    c = db()
    c.execute("INSERT INTO accounts VALUES(?,?,?)", (key, name, time.time()))
    c.commit()
    r = RedirectResponse("/app", status_code=303)
    r.set_cookie("key", key, max_age=31536000, httponly=True)
    return r


@app.post("/login")
def login(key: str = Form(...)):
    a = db().execute("SELECT 1 FROM accounts WHERE key=?", (key.strip(),)).fetchone()
    if not a:
        return page("Login", "<p>Invalid key. <a href=/>Back</a></p>")
    r = RedirectResponse("/app", status_code=303)
    r.set_cookie("key", key.strip(), max_age=31536000, httponly=True)
    return r


@app.get("/app", response_class=HTMLResponse)
def dashboard(request: Request):
    a = acct_from(request)
    if not a:
        return RedirectResponse("/")
    rows = db().execute("SELECT * FROM invoices WHERE key=? ORDER BY id DESC", (a["key"],)).fetchall()
    out = paid = 0
    trs = ""
    for i in rows:
        st = eff_status(i)
        t = totals(i)[2]
        if st == "paid": paid += t
        elif st != "draft": out += t
        trs += (f"<tr><td>{esc(i['number'])}</td><td>{esc(i['client_name'])}</td><td>{money(i['currency'], t)}</td>"
                f"<td>{esc(i['due'] or '')}</td><td><span class='b {st}'>{st}</span></td>"
                f"<td><a href='/app/invoice/{i['id']}'>Edit</a> · <a href='/i/{i['pub']}' target=_blank>Client link</a></td></tr>")
    mcp = f"{base(request)}/mcp/{a['key']}"
    return page("Dashboard", f"""<h1>{esc(a['name'])}</h1>
    <div class=card><b>Connect to Claude</b><br>Claude → Settings → Connectors → Add custom connector → paste:<br>
    <code>{esc(mcp)}</code></div>
    <div class=stats><div class=card>Outstanding<h2>{out:,.2f}</h2></div><div class=card>Paid<h2>{paid:,.2f}</h2></div></div>
    <table><tr><th>#</th><th>Client</th><th>Total</th><th>Due</th><th>Status</th><th></th></tr>{trs or '<tr><td colspan=6>No invoices yet. Ask Claude to create one.</td></tr>'}</table>""")


def get_inv(request, iid):
    a = acct_from(request)
    if not a: return None
    return db().execute("SELECT * FROM invoices WHERE id=? AND key=?", (iid, a["key"])).fetchone()


@app.get("/app/invoice/{iid}", response_class=HTMLResponse)
def edit_form(request: Request, iid: int):
    i = get_inv(request, iid)
    if not i: return RedirectResponse("/")
    lines = "\n".join(f"{x['description']} | {x.get('qty',1)} | {x.get('unit_price',0)}" for x in json.loads(i["items"]))
    opts = "".join(f"<option {'selected' if i['status']==s else ''}>{s}</option>" for s in ["draft", "sent", "paid"])
    return page("Edit", f"""<a href=/app>← Back</a><h1>{esc(i['number'])}</h1>
    <form method=post><label>Client name</label><input name=client_name value="{esc(i['client_name'])}">
    <label>Client email (for reminders)</label><input name=client_email value="{esc(i['client_email'] or '')}">
    <label>Currency</label><input name=currency value="{esc(i['currency'])}">
    <label>Due date (YYYY-MM-DD)</label><input name=due value="{esc(i['due'] or '')}">
    <label>Tax %</label><input name=tax value="{i['tax']}">
    <label>Line items — one per line: description | qty | unit price</label><textarea name=items rows=6>{esc(lines)}</textarea>
    <label>Notes</label><textarea name=notes rows=3>{esc(i['notes'] or '')}</textarea>
    <label>Status</label><select name=status>{opts}</select><button>Save</button></form>
    <p>Reminders sent: {i['reminders']} · <a href='/i/{i['pub']}' target=_blank>Open client link</a></p>""")


@app.post("/app/invoice/{iid}")
def edit_save(request: Request, iid: int, client_name: str = Form(""), client_email: str = Form(""),
              currency: str = Form("INR"), due: str = Form(""), tax: float = Form(0), items: str = Form(""),
              notes: str = Form(""), status: str = Form("draft")):
    i = get_inv(request, iid)
    if not i: return RedirectResponse("/")
    parsed = []
    for ln in items.splitlines():
        p = [x.strip() for x in ln.split("|")]
        if p and p[0]:
            parsed.append({"description": p[0], "qty": float(p[1]) if len(p) > 1 and p[1] else 1,
                           "unit_price": float(p[2]) if len(p) > 2 and p[2] else 0})
    c = db()
    c.execute("UPDATE invoices SET client_name=?,client_email=?,currency=?,due=?,tax=?,items=?,notes=?,status=? WHERE id=?",
              (client_name, client_email, currency, due, tax, json.dumps(parsed), notes, status, iid))
    c.commit()
    return RedirectResponse("/app", status_code=303)


@app.get("/i/{pub}", response_class=HTMLResponse)
def public_invoice(pub: str):
    i = db().execute("SELECT * FROM invoices WHERE pub=?", (pub,)).fetchone()
    if not i: return page("Not found", "<p>Invoice not found.</p>")
    a = db().execute("SELECT name FROM accounts WHERE key=?", (i["key"],)).fetchone()
    sub, tax, tot = totals(i)
    rows = "".join(f"<tr><td>{esc(x['description'])}</td><td>{x.get('qty',1)}</td>"
                   f"<td>{money(i['currency'], float(x.get('unit_price',0)))}</td>"
                   f"<td>{money(i['currency'], float(x.get('qty',1))*float(x.get('unit_price',0)))}</td></tr>"
                   for x in json.loads(i["items"]))
    st = eff_status(i)
    return page(f"Invoice {i['number']}", f"""<h1>Invoice {esc(i['number'])}</h1>
    <p>From <b>{esc(a['name'])}</b> to <b>{esc(i['client_name'])}</b> · Due {esc(i['due'] or '—')} · <span class='b {st}'>{st}</span></p>
    <table><tr><th>Item</th><th>Qty</th><th>Price</th><th>Amount</th></tr>{rows}</table>
    <p>Subtotal: {money(i['currency'], sub)}<br>Tax ({i['tax']}%): {money(i['currency'], tax)}<br>
    <b style='font-size:20px'>Total: {money(i['currency'], tot)}</b></p><p>{esc(i['notes'] or '')}</p>
    <button onclick='window.print()'>Print / Save PDF</button>""")


# ---------- MCP (hand-rolled Streamable HTTP, stateless, no AI) ----------
ITEMS = {"type": "array", "items": {"type": "object", "properties": {
    "description": {"type": "string"}, "qty": {"type": "number"}, "unit_price": {"type": "number"}},
    "required": ["description", "unit_price"]}}
TOOLS = [
    {"name": "create_invoice", "description": "Create an invoice. Returns an edit link for the owner and a public link for the client.",
     "inputSchema": {"type": "object", "properties": {
         "client_name": {"type": "string"}, "client_email": {"type": "string"}, "items": ITEMS,
         "currency": {"type": "string", "description": "e.g. INR, USD"}, "due_date": {"type": "string", "description": "YYYY-MM-DD"},
         "tax_percent": {"type": "number"}, "notes": {"type": "string"},
         "status": {"type": "string", "enum": ["draft", "sent"]}}, "required": ["client_name", "items"]}},
    {"name": "list_invoices", "description": "List invoices, optionally filtered by status (draft, sent, paid, overdue).",
     "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}}}},
    {"name": "update_invoice", "description": "Update an invoice's status, due date, or client email by id.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"},
         "status": {"type": "string", "enum": ["draft", "sent", "paid"]}, "due_date": {"type": "string"},
         "client_email": {"type": "string"}}, "required": ["id"]}},
]


def run_tool(key, name, a, root):
    c = db()
    if name == "create_invoice":
        n = c.execute("SELECT COUNT(*) FROM invoices WHERE key=?", (key,)).fetchone()[0] + 1
        pub = secrets.token_urlsafe(8)
        items = [{"description": x["description"], "qty": x.get("qty", 1), "unit_price": x["unit_price"]} for x in a["items"]]
        due = a.get("due_date") or (dt.date.today() + dt.timedelta(days=14)).isoformat()
        cur = c.execute("INSERT INTO invoices(pub,key,number,client_name,client_email,currency,items,tax,due,notes,status,created) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pub, key, f"INV-{n:04d}", a["client_name"], a.get("client_email", ""), a.get("currency", "INR"),
                         json.dumps(items), a.get("tax_percent", 0), due, a.get("notes", ""), a.get("status", "sent"), time.time()))
        c.commit()
        iid = cur.lastrowid
        i = c.execute("SELECT * FROM invoices WHERE id=?", (iid,)).fetchone()
        return (f"Created {i['number']} for {i['client_name']}: {money(i['currency'], totals(i)[2])}, due {due}.\n"
                f"Edit (owner, log in first): {root}/app/invoice/{iid}\nClient link: {root}/i/{pub}")
    if name == "list_invoices":
        rows = c.execute("SELECT * FROM invoices WHERE key=? ORDER BY id DESC", (key,)).fetchall()
        res = []
        for i in rows:
            st = eff_status(i)
            if a.get("status") and a["status"] != st: continue
            res.append(f"#{i['id']} {i['number']} | {i['client_name']} | {money(i['currency'], totals(i)[2])} | due {i['due']} | {st}")
        return "\n".join(res) or "No invoices."
    if name == "update_invoice":
        i = c.execute("SELECT * FROM invoices WHERE id=? AND key=?", (a["id"], key)).fetchone()
        if not i: raise ValueError("Invoice not found")
        for col, k in [("status", "status"), ("due", "due_date"), ("client_email", "client_email")]:
            if k in a: c.execute(f"UPDATE invoices SET {col}=? WHERE id=?", (a[k], a["id"]))
        c.commit()
        return f"Updated {i['number']}."
    raise ValueError("Unknown tool")


def handle(key, m, root):
    mid, meth = m.get("id"), m.get("method", "")
    if mid is None:
        return None  # notification
    ok = lambda r: {"jsonrpc": "2.0", "id": mid, "result": r}
    if meth == "initialize":
        return ok({"protocolVersion": (m.get("params") or {}).get("protocolVersion", "2025-06-18"),
                   "capabilities": {"tools": {}}, "serverInfo": {"name": "chasr", "version": "1.0"}})
    if meth == "ping": return ok({})
    if meth == "tools/list": return ok({"tools": TOOLS})
    if meth == "tools/call":
        p = m.get("params") or {}
        try:
            txt = run_tool(key, p.get("name"), p.get("arguments") or {}, root)
            return ok({"content": [{"type": "text", "text": txt}]})
        except Exception as e:
            return ok({"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}}


@app.post("/mcp/{key}")
async def mcp(key: str, request: Request):
    if not db().execute("SELECT 1 FROM accounts WHERE key=?", (key,)).fetchone():
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "Invalid key"}}, status_code=401)
    body = await request.json()
    root = base(request)
    msgs = body if isinstance(body, list) else [body]
    out = [r for r in (handle(key, m, root) for m in msgs) if r]
    if not out: return Response(status_code=202)
    return JSONResponse(out if isinstance(body, list) else out[0])


@app.get("/mcp/{key}")
def mcp_get(key: str):
    return Response(status_code=405)


# ---------- background reminders (works after the chat ends) ----------
def send_mail(to, subject, body):
    h = os.environ.get("SMTP_HOST")
    if not h: return
    m = EmailMessage(); m["To"] = to; m["From"] = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", ""))
    m["Subject"] = subject; m.set_content(body)
    with smtplib.SMTP(h, int(os.environ.get("SMTP_PORT", 587))) as s:
        s.starttls(); s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"]); s.send_message(m)


def remind_once(root):
    c = db()
    for i in c.execute("SELECT * FROM invoices WHERE status='sent' AND due<? AND reminders<3", (today(),)).fetchall():
        if i["last_reminded"] and time.time() - i["last_reminded"] < 3 * 86400: continue
        try:
            if i["client_email"]:
                send_mail(i["client_email"], f"Reminder: invoice {i['number']} is overdue",
                          f"Hi {i['client_name']},\n\nInvoice {i['number']} ({money(i['currency'], totals(i)[2])}) was due {i['due']}.\n"
                          f"View it: {root}/i/{i['pub']}\n")
        except Exception as e:
            print("mail error", e)
        c.execute("UPDATE invoices SET reminders=reminders+1,last_reminded=? WHERE id=?", (time.time(), i["id"]))
        c.commit()


def loop():
    while True:
        try: remind_once(os.environ.get("BASE_URL", ""))
        except Exception as e: print("reminder loop", e)
        time.sleep(3600)


threading.Thread(target=loop, daemon=True).start()
