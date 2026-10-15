"""MacTracker local server. Run: .venv/bin/python server.py"""
import hmac
import io
import json
import os
import re
import secrets
import socketserver
import sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pypdf

HERE = Path(__file__).resolve().parent
ADDR = ("127.0.0.1", 8000)
MAX_BODY = 20 * 1024 * 1024  # outline PDFs are the largest bodies
MAX_PDF_PAGES = 50  # course outlines are a few pages; bounds pypdf work on untrusted bytes

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY, course TEXT, source TEXT, source_id TEXT, title TEXT, kind TEXT,
  due TEXT, weight REAL, done INTEGER, mark REAL, out_of REAL, url TEXT, progress TEXT,
  closed INTEGER, missing INTEGER DEFAULT 0, overridden TEXT DEFAULT '{}',
  UNIQUE(source, source_id));
CREATE TABLE IF NOT EXISTS courses(code TEXT PRIMARY KEY, weights TEXT);
CREATE TABLE IF NOT EXISTS status(source TEXT PRIMARY KEY, status TEXT, error TEXT, synced_at TEXT);
"""

STATUSES = ("ok", "login needed", "error")
ITEM_FIELDS = ("course", "title", "due", "weight", "done", "mark", "out_of", "url", "progress", "closed")
OVERRIDABLE = ("title", "kind", "due", "weight", "done", "mark", "out_of")
SCALARS = (str, int, float, bool, type(None))
KINDS = ("exam", "midterm", "test", "quiz", "lab", "assignment", "homework")


def course_key(s):
    """AC62: first `SUBJECT NNNN` code, uppercased; else the first two words."""
    m = re.search(r"([A-Za-z]+)[ _]*(\d[A-Za-z0-9]{3})", s)
    return f"{m[1]} {m[2]}".upper() if m else " ".join(s.split()[:2]).upper()


def norm_title(title, course):
    """AC36: lowercase, course code removed, punctuation stripped, whitespace collapsed."""
    t = re.sub(re.escape(course).replace(r"\ ", "[ _]*"), "", title, flags=re.I) if course else title
    return " ".join(re.sub(r"[^\w\s]|_", " ", t.lower()).split())


def kind(title):
    """AC90 `practice` first, then AC11/AC80: first keyword in KINDS order that starts a word; else `other`."""
    if re.search(r"\bpractice", title, re.I):
        return "practice"
    return next((k for k in KINDS if re.search(r"\b" + k, title, re.I)), "other")


def category_kind(name):
    """AC46: an outline category's kind; a `practice` category maps to no kind (AC90), so `other`."""
    k = kind(name)
    return "other" if k == "practice" else k


def connect(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def ingest(db, body):
    """Store one source's sync. Raises ValueError on a malformed body (nothing written)."""
    source, items, status = body.get("source"), body.get("items"), body.get("status")
    if not isinstance(source, str) or status not in STATUSES or not isinstance(items, list):
        raise ValueError("need source, status, items[]")
    if not all(isinstance(i, dict) and isinstance(i.get("source_id"), (str, int)) for i in items):
        raise ValueError("each item needs a source_id")
    if not all(isinstance(v, SCALARS) for i in items for v in i.values()):
        raise ValueError("item values must be scalars")
    marks = body.get("category_marks") or []
    if not isinstance(marks, list) or not all(
        isinstance(m, dict) and isinstance(m.get("course"), str) and isinstance(m.get("name"), str)
        and isinstance(m.get("value"), (int, float)) and not isinstance(m["value"], bool) for m in marks
    ):
        raise ValueError("category_marks must be [{course, name, value}]")
    info = body.get("course_info") or []
    if not isinstance(info, list) or not all(
        isinstance(i, dict) and isinstance(i.get("course"), str) and isinstance(i.get("name"), str)
        and isinstance(i.get("value", ""), (str, int, float)) and not isinstance(i.get("value"), bool) for i in info
    ):
        raise ValueError("course_info must be [{course, name, value}]")
    info_by_course = {}  # AC91: a course's info is replaced as a whole, in sent order
    for i in info:
        info_by_course.setdefault(course_key(i["course"]), []).append({"name": i["name"], "value": i.get("value", "")})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    c = connect(db)
    try:
        with c:  # one transaction
            c.execute("BEGIN IMMEDIATE")
            for m in marks:  # AC72: keyed by the AC36-normalized category name
                code = course_key(m["course"])
                update_weights(c, code, lambda w: w["category_marks"].update({norm_title(m["name"], code): m["value"]}))
            for code, entries in info_by_course.items():
                update_weights(c, code, lambda w, e=entries: w.update(info=e))
            if status == "ok":  # AC43/AC50: flag everything, the upserts below clear what is present
                c.execute("UPDATE items SET missing = 1 WHERE source = ?", (source,))
            for i in items:
                row = {k: i[k] for k in ITEM_FIELDS if k in i}  # AC76: only keys sent are updated
                if row.get("course") is not None:
                    row["course"] = course_key(str(row["course"]))
                if "title" in row:
                    row["kind"] = kind(str(row["title"]))
                if status == "ok":
                    row["missing"] = 0
                cols = ["source", "source_id", *row]
                sets = ", ".join(f"{k} = excluded.{k}" for k in row) or "source = source"
                c.execute(
                    f"INSERT INTO items ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
                    f"ON CONFLICT(source, source_id) DO UPDATE SET {sets}",
                    (source, str(i["source_id"]), *row.values()),
                )
            c.execute(
                "INSERT OR REPLACE INTO status VALUES (?, ?, ?, ?)",
                (source, status, body.get("error") if status == "error" else None, now),
            )
    finally:
        c.close()


def rows(db, sql):
    c = connect(db)
    try:
        return [dict(r) for r in c.execute(sql)]
    finally:
        c.close()


def set_override(db, item_id, fields):
    """AC10: record user edits as {field: {value, at}}. Returns False if the item does not exist."""
    if not fields or not all(k in OVERRIDABLE and isinstance(v, SCALARS) for k, v in fields.items()):
        raise ValueError(f"only {', '.join(OVERRIDABLE)} can be edited, as scalars")
    at = datetime.now(timezone.utc).isoformat(timespec="microseconds")  # fixed width: sorts as text
    c = connect(db)
    try:
        c.execute("BEGIN IMMEDIATE")  # read-modify-write of `overridden` without lost updates
        row = c.execute("SELECT overridden FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            c.rollback()
            return False
        o = json.loads(row["overridden"] or "{}")
        o.update({k: {"value": v, "at": at} for k, v in fields.items()})
        c.execute("UPDATE items SET overridden = ? WHERE id = ?", (json.dumps(o), item_id))
        c.commit()
        return True
    finally:
        c.close()


def parse_due(s):
    """ISO time to an aware datetime (naive = local time), or None."""
    try:
        d = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.astimezone()


def same_assessment(kept, r):
    """AC7/AC8/AC88: a source not yet in the kept group, same course key and norm_title, and dues
    within 1 day when both have one. Outline placeholders never merge (AC84 governs them)."""
    if r["source"] in kept["_sources"] or "outline" in (r["source"], kept["source"]) or kept["course"] != r["course"]:
        return False
    if norm_title(kept["title"] or "", kept["course"] or "") != norm_title(r["title"] or "", r["course"] or ""):
        return False
    da, db_ = parse_due(kept["due"]), parse_due(r["due"])
    return not (da and db_) or abs(da - db_) <= timedelta(days=1)


def apply_overrides(row):
    row.update({k: o["value"] for k, o in row["overridden"].items()})
    return row


def merged_items(db):
    """Raw rows with overrides applied, deduped across sources (AC7/8/9/37/38/48)."""
    out = []
    # AC88: the kept row is the first by source priority avenue > crowdmark > childsmath.
    raw = rows(db, "SELECT * FROM items ORDER BY CASE source WHEN 'avenue' THEN 0 WHEN 'crowdmark' THEN 1 "
                   "WHEN 'childsmath' THEN 2 ELSE 3 END, id")
    for r in raw:
        r["overridden"] = json.loads(r["overridden"] or "{}")
        apply_overrides(r)
        # ponytail: O(n^2) scan; fine for a student's few hundred items
        kept = next((k for k in out if same_assessment(k, r)), None)
        if kept is None:
            r["_sources"] = {r["source"]}
            out.append(r)
            continue
        kept["_sources"].add(r["source"])
        for f, v in r.items():  # AC37/AC89: fill the kept row's empty fields, due included
            if kept.get(f) is None:
                kept[f] = v
        for f, o in r["overridden"].items():  # AC38/AC48: union, later edit wins
            if f not in kept["overridden"] or o["at"] > kept["overridden"][f]["at"]:
                kept["overridden"][f] = o
        apply_overrides(kept)
    for r in out:
        del r["_sources"]
        if r["mark"] is not None:  # AC87: a marked item is done, a mark of 0 included
            r["done"] = True
    return sorted(out, key=lambda r: r["id"])


def bucket_of(name, course, k):
    """AC40/AC46: categories and items of one kind share a bucket; each `other` name is its own."""
    return k if k != "other" else "other:" + norm_title(name or "", course or "")


def course_view(db):
    """GET /api/items body: merged items with outline-derived weights, plus per-course grades."""
    items = merged_items(db)
    outlines = {r["code"]: json.loads(r["weights"]) for r in rows(db, "SELECT * FROM courses") if r["weights"]}
    by_course = {}
    for it in items:
        # AC90: practice items get no bucket, so no weight and no grade
        it["_bucket"] = None if it["kind"] == "practice" else bucket_of(it["title"], it["course"], it["kind"])
        by_course.setdefault(it["course"], []).append(it)

    # Item weights per scheme (AC40/AC52/AC56): bucket weight / N; a user weight override wins.
    for code, its in by_course.items():
        schemes = outlines.get(code, {}).get("schemes") or []
        if not schemes:
            for it in its:
                it["_ws"] = []
            continue  # no outline: keep the stored weight
        # Session default: a placeholder gives way to real items in its bucket (no weight, not graded).
        real = {it["_bucket"] for it in its if it["source"] != "outline"}
        for it in its:
            if it["source"] == "outline" and it["_bucket"] in real:
                it["_bucket"] = None
        n = {}
        for it in its:
            n[it["_bucket"]] = n.get(it["_bucket"], 0) + 1
        bucket_w = [{} for _ in schemes]
        for bw, scheme in zip(bucket_w, schemes):
            for name, w in scheme.items():
                b = bucket_of(name, code, category_kind(name))
                bw[b] = bw.get(b, 0) + w
        for it in its:
            if it["_bucket"] is None:
                it["_ws"], it["weight"] = [None] * len(schemes), None
            elif "weight" in it["overridden"]:
                it["_ws"] = [it["weight"]] * len(schemes)
            else:
                it["_ws"] = [bw[it["_bucket"]] / n[it["_bucket"]] if it["_bucket"] in bw else None for bw in bucket_w]
                it["weight"] = it["_ws"][0]  # AC52: never the ingested (gradebook) weight

    courses = {}
    for code in sorted(set(by_course) | set(outlines), key=str):
        w = outlines.get(code, {})
        marks = w.get("category_marks", {})
        its = by_course.get(code, [])
        best = (None, 0)
        for s, scheme in enumerate(w.get("schemes") or []):
            score = graded = 0
            marked = set()
            for it in its:
                wt = it["_ws"][s] if s < len(it["_ws"]) else None
                if wt and it["mark"] is not None and it["out_of"]:
                    score += wt * it["mark"] / it["out_of"]
                    graded += wt
                    marked.add(it["_bucket"])
            for name, cw in scheme.items():  # AC73/AC74: category mark only for unmarked buckets
                m = marks.get(norm_title(name, code))
                if m is not None and bucket_of(name, code, category_kind(name)) not in marked:
                    score += cw * m / 100
                    graded += cw
            total = sum(scheme.values())
            if graded and (best[0] is None or score / graded * 100 > best[0]):  # AC56: higher scheme
                best = (score / graded * 100, graded / total * 100 if total else 0)
        courses[code] = {"grade": best[0], "graded_pct": best[1], "approximate": bool(w.get("approximate")),
                         "info": w.get("info", [])}
    for it in items:
        del it["_bucket"], it["_ws"]
    return {"items": items, "courses": courses}


PCT = r"\d+(?:\.\d+)?(?:\s*[-\u2013]\s*\d+(?:\.\d+)?)?\s*%"
OUTLINE_LINE = re.compile(rf"^[ \t]*(\S.*?)[ .\t]+({PCT})\**[ \t]*$", re.M)  # AC14: `<Name> ... NN%`


def parse_pct(s):
    """AC57/AC58: '20%' -> (20, False); '40-55%' -> (55, True); trailing `*` footnotes ignored."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(?:\s*[-\u2013]\s*(\d+(?:\.\d+)?))?\s*%", str(s).strip().rstrip("*").strip())
    if not m:
        raise ValueError(f"not a percent: {s!r}")
    return float(m[2] or m[1]), m[2] is not None


def outline_categories(text):
    """Outline text -> [{name, weights: [pct string]}] for every `<Name> ... NN%` line."""
    return [{"name": m[1].strip(), "weights": [m[2]]} for m in OUTLINE_LINE.finditer(text)]


def parse_outline_text(text):
    """AC14: {category name: weight}."""
    return {c["name"]: parse_pct(c["weights"][0])[0] for c in outline_categories(text)}


def pdf_text(data):
    """Text of an outline PDF via pypdf. Raises ValueError for anything pypdf cannot read."""
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError(f"PDF has more than {MAX_PDF_PAGES} pages")
        return "\n".join(p.extract_text() or "" for p in reader.pages)
    except ValueError:
        raise
    except Exception as e:  # pypdf raises many types on malformed input; never logs content
        raise ValueError(f"unreadable PDF ({type(e).__name__})") from None


def update_weights(c, code, change):
    """Read-modify-write one course's weights JSON inside the caller's transaction."""
    row = c.execute("SELECT weights FROM courses WHERE code = ?", (code,)).fetchone()
    w = json.loads(row["weights"]) if row and row["weights"] else {"schemes": [], "approximate": False, "category_marks": {}}
    change(w)
    c.execute("INSERT OR REPLACE INTO courses (code, weights) VALUES (?, ?)", (code, json.dumps(w)))


def store_outline(db, course, categories):
    """AC15/AC55/AC57/AC64/AC47: store weight schemes and one placeholder per `other` category."""
    if not isinstance(course, str) or not course.strip():
        raise ValueError("need a course")
    if not isinstance(categories, list) or not categories or not all(
        isinstance(cat, dict) and isinstance(cat.get("name"), str) and cat["name"].strip()
        and isinstance(cat.get("weights"), list) and cat["weights"] for cat in categories
    ):
        raise ValueError("need categories [{name, weights: [pct, ...]}]")
    parsed = {cat["name"].strip(): [parse_pct(w) for w in cat["weights"]] for cat in categories}
    n = max(len(ws) for ws in parsed.values())
    # A row with fewer columns than the table repeats its last weight in the later schemes.
    schemes = [{name: ws[min(i, len(ws) - 1)][0] for name, ws in parsed.items()} for i in range(n)]
    approximate = any(a for ws in parsed.values() for _, a in ws)
    code = course_key(course)

    def change(w):
        w.update(schemes=schemes, approximate=approximate)

    c = connect(db)
    try:
        with c:
            c.execute("BEGIN IMMEDIATE")
            update_weights(c, code, change)
            keep = [f"{code}:{name}" for name in parsed if category_kind(name) == "other"]
            # Session default: a category gone from the re-posted outline loses its placeholder,
            # unless the user edited it.
            c.execute(
                f"DELETE FROM items WHERE source = 'outline' AND course = ? AND overridden = '{{}}' "
                f"AND source_id NOT IN ({', '.join('?' * len(keep))})",
                (code, *keep),
            )
            for name in parsed:
                if category_kind(name) == "other":
                    # source_id carries the course: two courses can both have "Participation".
                    c.execute(
                        "INSERT INTO items (course, source, source_id, title, kind, weight) "
                        "VALUES (?, 'outline', ?, ?, 'other', ?) ON CONFLICT(source, source_id) "
                        "DO UPDATE SET title = excluded.title, weight = excluded.weight",
                        (code, f"{code}:{name}", name, schemes[0][name]),
                    )
    finally:
        c.close()


def ensure_token(path):
    """Write a random token unless the file exists; return the token."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return Path(path).read_text().strip()
    token = secrets.token_hex(16)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


TOKEN_ROUTES = ("/api/ingest", "/api/outline")


class Handler(BaseHTTPRequestHandler):
    cors = None  # origin echoed in Access-Control-Allow-Origin for this request, if any

    def cors_origin(self):
        """The extension's origin on a token route, else None. The page and other sites never get CORS."""
        origin = self.headers.get("Origin", "")
        if origin.startswith("moz-extension://") and urlsplit(self.path).path in TOKEN_ROUTES:
            return origin
        return None

    def end_headers(self):
        if self.cors:
            self.send_header("Access-Control-Allow-Origin", self.cors)
            self.send_header("Vary", "Origin")
        super().end_headers()

    def do_OPTIONS(self):
        # Firefox preflights the extension's JSON/X-Token POSTs even with a host permission.
        self.cors = None
        if not self.host_ok():
            return
        origin = self.cors_origin()
        if not origin:
            return self.send_error(501, "Unsupported method ('OPTIONS')")
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
        self.send_header("Access-Control-Max-Age", "600")
        self.cors = origin
        self.end_headers()

    def host_ok(self):
        port = self.server.server_address[1]
        if self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return True
        self.send_error(403, "Host not allowed")  # DNS-rebinding guard
        return False

    def body(self):
        """Request body, or None after replying 413/400."""
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.send_error(400, "Bad Content-Length")
            return None
        if n < 0 or n > MAX_BODY:
            self.send_error(413, "Body too large")
            return None
        return self.rfile.read(n)

    def reply(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def json(self, code, obj):
        self.reply(code, json.dumps(obj).encode(), "application/json")

    def token_ok(self):
        sent = self.headers.get("X-Token", "").encode()
        if hmac.compare_digest(sent, self.server.token.encode()):
            return True
        self.send_error(401)
        return False

    def do_GET(self):
        self.cors = None
        if not self.host_ok():
            return
        db = self.server.db
        if self.path == "/":
            return self.reply(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/items":
            return self.json(200, course_view(db))
        if self.path == "/api/status":
            st = rows(db, "SELECT * FROM status")
            return self.json(200, {r.pop("source"): r for r in st})
        self.send_error(404)

    def do_POST(self):
        self.cors = None
        if not self.host_ok():
            return
        self.cors = self.cors_origin()
        data = self.body()
        if data is None:
            return
        url = urlsplit(self.path)
        m = re.fullmatch(r"/api/items/(\d+)", url.path)
        if m:
            return self.post_override(int(m[1]), data)
        if url.path not in ("/api/ingest", "/api/outline"):
            return self.send_error(404)
        if not self.token_ok():  # AC4: 401 before any write
            return
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        try:
            if url.path == "/api/outline" and ctype == "application/pdf":
                course = parse_qs(url.query).get("course", [""])[0]
                cats = outline_categories(pdf_text(data))
                if not cats:
                    raise ValueError("no `<Name> ... NN%` lines found in the PDF")
                store_outline(self.server.db, course, cats)
            else:
                body = json.loads(data)
                if not isinstance(body, dict):
                    raise ValueError("body must be an object")
                if url.path == "/api/ingest":
                    ingest(self.server.db, body)
                else:  # AC64: JSON outline, no pypdf
                    store_outline(self.server.db, body.get("course"), body.get("categories"))
        except ValueError as e:  # includes JSONDecodeError
            return self.send_error(400, str(e))
        self.json(200, {"ok": True})


    def post_override(self, item_id, data):
        # No token: the page has none. CSRF guard = JSON content type (forces a CORS preflight,
        # which do_OPTIONS refuses for this route: no Access-Control-* headers) + Host allowlist.
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self.send_error(415, "Content-Type must be application/json")
        try:
            fields = json.loads(data)
            if not isinstance(fields, dict):
                raise ValueError("body must be an object")
            found = set_override(self.server.db, item_id, fields)
        except ValueError as e:
            return self.send_error(400, str(e))
        if not found:
            return self.send_error(404)
        self.json(200, {"ok": True})


class Server(ThreadingHTTPServer):
    def server_bind(self):
        # Skip HTTPServer's socket.getfqdn() reverse lookup: it stalls ~35s on this Mac.
        socketserver.TCPServer.server_bind(self)


def make_server(addr=ADDR, db=HERE / "tracker.db", token=HERE / "token.txt"):
    """Create the schema and token, return an unstarted server. Tests pass port 0 and temp paths."""
    c = sqlite3.connect(db)
    c.executescript(SCHEMA)
    c.close()
    srv = Server(addr, Handler)
    srv.db, srv.token = str(db), ensure_token(token)
    return srv


if __name__ == "__main__":
    make_server().serve_forever()
