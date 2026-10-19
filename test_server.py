"""MacTracker acceptance checks. Run: .venv/bin/python test_server.py

Plain `assert` functions named test_*; the runner below calls each one,
prints `FAIL <name>: <exception>` per failure and exits 1 if any fail.
"""
import io
import json
import re
from datetime import datetime
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class Running:
    """Run a copy of server.py (and index.html) as `python server.py` in a temp dir,
    so token.txt and the DB land there and never in the repo."""

    def __init__(self, token=None):
        import server  # noqa: F401  (fails with ModuleNotFoundError until server.py exists)

        self.dir = Path(tempfile.mkdtemp())
        shutil.copy(server.__file__, self.dir / "server.py")
        if (ROOT / "index.html").exists():
            shutil.copy(ROOT / "index.html", self.dir / "index.html")
        if token is not None:
            (self.dir / "token.txt").write_text(token)

    def __enter__(self):
        import socket

        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", 8000)) == 0:
                shutil.rmtree(self.dir, ignore_errors=True)
                raise AssertionError("port 8000 already in use (another server.py running?) - stop it and rerun")
        self.proc = subprocess.Popen(
            [sys.executable, "server.py"], cwd=self.dir,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        deadline = time.time() + 10
        while True:
            if self.proc.poll() is not None:
                shutil.rmtree(self.dir, ignore_errors=True)
                raise AssertionError(f"server.py exited {self.proc.returncode}: {self.proc.stderr.read().decode()}")
            try:
                urllib.request.urlopen("http://127.0.0.1:8000/", timeout=1).close()
                return self
            except Exception:
                if time.time() > deadline:
                    self.__exit__()
                    raise AssertionError("server.py did not answer on 127.0.0.1:8000 within 10s")
                time.sleep(0.1)

    def __exit__(self, *exc):
        self.proc.terminate()
        self.proc.wait(5)
        shutil.rmtree(self.dir, ignore_errors=True)


class InProcess:
    """Run server.make_server() in a thread on a free port with a temp DB and token.
    Use for route tests: `with InProcess() as s: s.url, s.token, s.db`.
    token_text, if given, is written to token.txt before the server starts."""

    def __init__(self, token_text=None):
        self.token_text = token_text

    def __enter__(self):
        import server

        self.dir = Path(tempfile.mkdtemp())
        self.db = self.dir / "test.db"
        if self.token_text is not None:
            (self.dir / "token.txt").write_text(self.token_text)
        self.srv = server.make_server(("127.0.0.1", 0), self.db, self.dir / "token.txt")
        self.token = self.srv.token
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)


def status_of(method, url, headers=None, data=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data, headers or {}, method=method)) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


# AC1 — bound to 127.0.0.1:8000 only
def test_bind_localhost():
    import server

    assert server.ADDR == ("127.0.0.1", 8000), server.ADDR
    with Running() as s:
        out = subprocess.run(
            ["lsof", "-nP", "-a", "-p", str(s.proc.pid), "-iTCP", "-sTCP:LISTEN"],
            capture_output=True, text=True,
        ).stdout
        listens = [line.split()[8] for line in out.splitlines()[1:]]
        assert listens == ["127.0.0.1:8000"], listens


# AC2 — GET / returns index.html
def test_get_root():
    with Running() as s:
        with urllib.request.urlopen("http://127.0.0.1:8000/") as r:
            assert r.status == 200, r.status
            assert r.read() == (s.dir / "index.html").read_bytes()


# AC13 — .gitignore ignores *.db and token.txt
def test_gitignore():
    lines = (ROOT / ".gitignore").read_text().splitlines()
    assert "*.db" in lines and "token.txt" in lines, lines
    for name in ("tracker.db", "token.txt"):  # git allows -q with one path only
        r = subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT)
        assert r.returncode == 0, f"git does not ignore {name}"


# AC33 — no token.txt: server writes a random one
def test_token_created():
    tokens = []
    for _ in range(2):
        with Running() as s:
            t = (s.dir / "token.txt").read_text().strip()
            assert len(t) >= 16, f"token too short: {t!r}"
            tokens.append(t)
    assert tokens[0] != tokens[1], "token is not random"


# AC34 — existing token.txt is left unchanged
def test_token_kept():
    with Running(token="keep-me-0123456789abcdef\n") as s:
        assert (s.dir / "token.txt").read_text() == "keep-me-0123456789abcdef\n"


# Plan Step 1 — Host allowlist (DNS-rebinding guard)
def test_host_allowlist():
    with InProcess() as s:
        assert status_of("GET", s.url + "/") == 200
        assert status_of("GET", s.url + "/", {"Host": f"localhost:{s.port}"}) == 200
        assert status_of("GET", s.url + "/", {"Host": f"evil.example:{s.port}"}) == 403
        assert status_of("POST", s.url + "/", {"Host": "evil.example"}, b"{}") == 403


# Plan Step 1 — request-body size cap
def test_body_cap():
    import http.client

    import server

    with InProcess() as s:
        c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=5)
        c.putrequest("POST", "/api/ingest")
        c.putheader("Content-Length", str(server.MAX_BODY + 1))
        c.endheaders()  # body never sent: the server must refuse on the header alone
        assert c.getresponse().status == 413


# AC62 — course identifiers normalize to `SUBJECT NNNN`
def test_course_key():
    import server

    cases = {
        "PHYSICS_1C03_schmim10_2269": "PHYSICS 1C03",  # Avenue Code
        "MATH 1ZA3 - Fall 2026": "MATH 1ZA3",  # Crowdmark name: first two words
        "Math 1B03 F26": "MATH 1B03",  # childsmath
        "Math 1A03/1ZA3": "MATH 1A03",  # joint label: first code
    }
    got = {s: server.course_key(s) for s in cases}
    assert got == cases, got


# AC36 — title normalize: lowercase, course code removed, punctuation stripped (to a space), whitespace collapsed
def test_norm_title():
    import server

    a = server.norm_title("MATH 1ZA3: Assignment  #2", "MATH 1ZA3")
    b = server.norm_title("assignment 2", "MATH 1ZA3")
    assert a == b == "assignment 2", (a, b)
    # Session default: punctuation becomes a space (not deleted)
    c = server.norm_title("Lab-2", "MATH 1ZA3")
    d = server.norm_title("Lab 2", "MATH 1ZA3")
    assert c == d == "lab 2", (c, d)


# AC11 — kind is the first keyword in order exam, midterm, test, quiz, lab, assignment, homework
def test_kind_first_keyword():
    import server

    cases = {
        "Lab Quiz 3": "quiz",
        "Midterm Exam": "exam",
        "Midterm 1": "midterm",
        "Term Test 1": "test",
        "Lab 4": "lab",
        "Assignment #2": "assignment",
        "Homework 5": "homework",
    }
    got = {t: server.kind(t) for t in cases}
    assert got == cases, got


# AC39 — no keyword gives `other`
def test_kind_other():
    import server

    assert server.kind("Reading Response") == "other", server.kind("Reading Response")


# AC46 — outline category names map to kinds; plurals count
def test_kind_category_names():
    import server

    got = (server.kind("Assignments"), server.kind("Weekly Quizzes"))
    assert got == ("assignment", "quiz"), got


# AC80 — keywords match only at the start of a word
def test_kind_word_start():
    import server

    got = (server.kind("Prelab 2"), server.kind("latest results"))
    assert got == ("other", "other"), got


# ---- Step 3: ingest, status, raw items ----------------------------------------

FIELDS = ("course", "source", "source_id", "title", "kind", "due", "weight", "done", "mark", "out_of", "url")


def call(s, method, path, body=None, token=True):
    """JSON request to the in-process server. token=True sends the right X-Token, a str sends that, None sends none."""
    headers = {"Content-Type": "application/json"}
    if token is True:
        headers["X-Token"] = s.token
    elif token is not None:
        headers["X-Token"] = token
    data = None if body is None else json.dumps(body).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(s.url + path, data, headers, method=method)) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


def ingest(s, source, items, status="ok", token=True, **extra):
    code, _ = call(s, "POST", "/api/ingest", {"source": source, "items": items, "status": status, **extra}, token)
    return code


def ingest_ok(s, source, items, status="ok", **extra):
    code = ingest(s, source, items, status, **extra)
    assert 200 <= code < 300, f"POST /api/ingest -> {code}"


def get_items(s):
    code, body = call(s, "GET", "/api/items")
    assert code == 200, f"GET /api/items -> {code}"
    return body["items"]


def get_status(s):
    code, body = call(s, "GET", "/api/status")
    assert code == 200, f"GET /api/status -> {code}"
    return body if isinstance(body, dict) else {row["source"]: row for row in body}


def db_dump(s):
    with sqlite3.connect(s.db) as c:
        return list(c.iterdump())


def item(sid, **kw):
    return {"course": "MATH 1ZA3", "source_id": sid, "title": f"Assignment {sid}", "due": "2026-10-20T23:59:00Z", **kw}


def by_sid(items, source):
    return {i["source_id"]: i for i in items if i["source"] == source}


# AC4 — no X-Token: 401 and nothing stored or changed
def test_ingest_no_token():
    with InProcess() as s:
        before = db_dump(s)
        code = ingest(s, "avenue", [item("1")], token=None)
        assert code == 401, f"unauthorized ingest -> {code}"
        assert db_dump(s) == before, "DB changed on an unauthorized ingest"
        ingest_ok(s, "avenue", [item("1", mark=5)])
        before = db_dump(s)
        code = ingest(s, "avenue", [item("1", mark=9), item("2")], status="error", error="x", token=None)
        assert code == 401, f"unauthorized ingest -> {code}"
        assert db_dump(s) == before, "DB changed on an unauthorized ingest"


# AC4 — wrong X-Token: 401 and nothing stored or changed
def test_ingest_bad_token():
    with InProcess() as s:
        before = db_dump(s)
        code = ingest(s, "avenue", [item("1")], token="wrong")
        assert code == 401, f"unauthorized ingest -> {code}"
        assert db_dump(s) == before, "DB changed on an unauthorized ingest"
        ingest_ok(s, "avenue", [item("1", mark=5)])
        before = db_dump(s)
        code = ingest(s, "avenue", [item("1", mark=9), item("2")], status="error", error="x", token="wrong")
        assert code == 401, f"unauthorized ingest -> {code}"
        assert db_dump(s) == before, "DB changed on an unauthorized ingest"


# AC5 + AC3 — correct token: item stored with every field, status recorded
def test_ingest_stores():
    with InProcess() as s:
        sent = item("42", title="Assignment 2", weight=5, done=True, mark=8, out_of=10, url="https://x/a2")
        ingest_ok(s, "avenue", [sent])
        items = get_items(s)
        assert len(items) == 1, items
        got = items[0]
        missing = [f for f in FIELDS if f not in got]
        assert not missing, f"fields missing from /api/items: {missing}"
        assert (got["source"], got["source_id"], got["title"], got["course"]) == ("avenue", "42", "Assignment 2", "MATH 1ZA3"), got
        assert (got["due"], got["weight"], bool(got["done"]), got["mark"], got["out_of"], got["url"]) == (
            "2026-10-20T23:59:00Z", 5, True, 8, 10, "https://x/a2"), got
        assert get_status(s)["avenue"]["status"] == "ok"


# AC6 — same (source, source_id) updates in place
def test_upsert_in_place():
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("7", title="Assignment 7")])
        first = get_items(s)
        ingest_ok(s, "avenue", [item("7", title="Assignment 7 (revised)", mark=3)])
        items = get_items(s)
        assert len(items) == 1, items
        assert items[0]["title"] == "Assignment 7 (revised)" and items[0]["mark"] == 3, items[0]
        if "id" in first[0]:
            assert items[0]["id"] == first[0]["id"], (first, items)


# AC63 — every source's course identifier is stored under the one AC62 key
def test_course_key_on_store():
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("1", course="PHYSICS_1C03_x_2269")])
        ingest_ok(s, "childsmath", [item("a", course="Physics 1C03 F26")])
        courses = {i["course"] for i in get_items(s)}
        assert courses == {"PHYSICS 1C03"}, courses


# AC11 + AC39 — kind is derived when an item is stored
def test_kind_on_store():
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("1", title="Lab Quiz 3"), item("2", title="Reading Response")])
        kinds = {i["source_id"]: i["kind"] for i in get_items(s)}
        assert kinds == {"1": "quiz", "2": "other"}, kinds


# AC12 — per-source status, error message, last-sync time
def test_status():
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("1")])
        ingest_ok(s, "crowdmark", [], status="login needed")
        ingest_ok(s, "childsmath", [], status="error", error="boom: parse failed")
        st = get_status(s)
        assert {k: st[k]["status"] for k in ("avenue", "crowdmark", "childsmath")} == {
            "avenue": "ok", "crowdmark": "login needed", "childsmath": "error"}, st
        assert st["childsmath"]["error"] == "boom: parse failed", st["childsmath"]
        for k in ("avenue", "crowdmark", "childsmath"):
            assert st[k].get("synced_at"), f"no synced_at for {k}: {st[k]}"


# AC43 — an ok ingest that omits a stored item keeps the row and flags it; other sources untouched
def test_missing_flag():
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("1"), item("2")])
        ingest_ok(s, "crowdmark", [item("c1")])
        ingest_ok(s, "avenue", [item("1")])
        items = get_items(s)
        av, cm = by_sid(items, "avenue"), by_sid(items, "crowdmark")
        assert set(av) == {"1", "2"}, "omitted row was deleted"
        assert av["2"]["missing"] and not av["1"]["missing"], av
        assert not cm["c1"]["missing"], cm


# AC49 — login needed / error ingests neither set nor clear flags
def test_missing_untouched_on_error():
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("1"), item("2")])
        ingest_ok(s, "avenue", [item("1")], status="login needed")
        ingest_ok(s, "avenue", [], status="error", error="x")
        av = by_sid(get_items(s), "avenue")
        assert not av["1"]["missing"] and not av["2"]["missing"], av
        ingest_ok(s, "avenue", [item("1")])  # now 2 is flagged
        ingest_ok(s, "avenue", [item("2")], status="error", error="x")
        ingest_ok(s, "avenue", [], status="login needed")
        av = by_sid(get_items(s), "avenue")
        assert av["2"]["missing"] and not av["1"]["missing"], av


# AC50 — reappearing item has its flag cleared
def test_missing_cleared():
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("1"), item("2")])
        ingest_ok(s, "avenue", [item("1")])
        assert by_sid(get_items(s), "avenue")["2"]["missing"]
        ingest_ok(s, "avenue", [item("1"), item("2")])
        assert not by_sid(get_items(s), "avenue")["2"]["missing"]


# AC76 (server side) — an ingest without a `mark` key leaves the stored mark alone
def test_partial_upsert_keeps_mark():
    with InProcess() as s:
        ingest_ok(s, "childsmath", [item("t1", mark=12.5, out_of=15)])
        ingest_ok(s, "childsmath", [item("t1", title="Test #1")])
        got = by_sid(get_items(s), "childsmath")["t1"]
        assert (got["mark"], got["out_of"], got["title"]) == (12.5, 15, "Test #1"), got



# Plan Step 3 — a malformed ingest body is a 400 and writes nothing
def test_ingest_bad_body():
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("1")])
        before = db_dump(s)
        for body in ([], {"source": "avenue", "items": [], "status": "weird"},
                     {"source": "avenue", "items": [{"title": "no id"}], "status": "ok"},
                     {"source": "avenue", "items": [item("1", mark={"x": 1})], "status": "ok"}):
            code, _ = call(s, "POST", "/api/ingest", body)
            assert code == 400, (body, code)
        req = urllib.request.Request(s.url + "/api/ingest", b"{not json", {"X-Token": s.token}, method="POST")
        assert status_of("POST", req.full_url, dict(req.headers), req.data) == 400
        assert db_dump(s) == before, "DB changed on a malformed ingest"


# ---- Step 4: overrides and cross-source dedup ---------------------------------

def override(s, item_id, fields, ctype="application/json"):
    """What the page does: no X-Token, JSON body of edited fields."""
    req = urllib.request.Request(s.url + f"/api/items/{item_id}", json.dumps(fields).encode(),
                                 {"Content-Type": ctype}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def override_ok(s, item_id, fields):
    code = override(s, item_id, fields)
    assert 200 <= code < 300, f"POST /api/items/{item_id} -> {code}"


def overrides_of(row):
    o = row.get("overridden") or {}
    return json.loads(o) if isinstance(o, str) else o


def in_course(items, course):
    return [i for i in items if i["course"] == course]


# Same assessment as seen by two sources (AC7/AC36/AC62): titles and course ids differ in form only.
AV = {"course": "MATH_1ZA3_x_2269", "source_id": "dropbox:1", "title": "MATH 1ZA3: Assignment  #2",
      "due": "2026-10-20T23:59:00Z"}
CM = {"course": "MATH 1ZA3 Fall 2026", "source_id": "cm-77", "title": "Assignment 2",
      "due": "2026-10-21T03:59:00-04:00"}  # 8h after AV's due, other zone notation
FAR = "2026-11-30T23:59:00Z"  # > 1 day from CM's due: keeps the rows apart until re-ingested


# AC10 — override stores the values and records each in `overridden` with its edit time
def test_override_recorded():
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("1", mark=5, out_of=10)])
        rid = get_items(s)[0]["id"]
        override_ok(s, rid, {"mark": 9, "title": "Assignment One"})
        row = get_items(s)[0]
        assert (row["mark"], row["title"]) == (9, "Assignment One"), row
        o = overrides_of(row)
        assert set(o) == {"mark", "title"}, o
        assert o["mark"]["value"] == 9 and o["title"]["value"] == "Assignment One", o
        for f in o:
            datetime.fromisoformat(o[f]["at"])  # raises if the edit time is not an ISO time


# AC9 — edited fields keep the user's value through a re-ingest; other fields take the ingest
def test_override_survives_ingest():
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("1", mark=5, out_of=10)])
        rid = get_items(s)[0]["id"]
        override_ok(s, rid, {"mark": 9})
        ingest_ok(s, "avenue", [item("1", mark=6, out_of=12, due="2026-10-22T23:59:00Z")])
        row = get_items(s)[0]
        assert (row["mark"], row["out_of"], row["due"]) == (9, 12, "2026-10-22T23:59:00Z"), row


# Plan Step 4 — CSRF guard: a non-JSON content type is refused and stores nothing
def test_override_rejects_non_json():
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("1", mark=5)])
        rid = get_items(s)[0]["id"]
        override_ok(s, rid, {"mark": 7})  # control: the same request as JSON is accepted
        before = db_dump(s)
        code = override(s, rid, {"mark": 9}, ctype="text/plain")
        assert 400 <= code < 500, f"text/plain override -> {code}"
        assert db_dump(s) == before, "DB changed on a non-JSON override"


# Plan Step 4 — only title/kind/due/weight/done/mark/out_of can be overridden
def test_override_whitelist():
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("1")])
        rid = get_items(s)[0]["id"]
        override_ok(s, rid, {"mark": 9})  # control: a whitelisted field is accepted
        override(s, rid, {"source": "evil", "source_id": "x", "course": "EVIL 0000"})
        row = get_items(s)[0]
        assert (row["source"], row["source_id"], row["course"], row["mark"]) == ("avenue", "1", "MATH 1ZA3", 9), row
        assert set(overrides_of(row)) == {"mark"}, overrides_of(row)


# AC7 — Avenue + Crowdmark, same course and normalized title, dues <= 1 day apart: one row
def test_dedup_merges():
    with InProcess() as s:
        ingest_ok(s, "avenue", [AV])
        ingest_ok(s, "crowdmark", [CM])
        rows = in_course(get_items(s), "MATH 1ZA3")
        assert len(rows) == 1, rows


# AC8 — same course and normalized title, dues > 1 day apart: two rows
def test_dedup_far_dates():
    with InProcess() as s:
        ingest_ok(s, "avenue", [AV, dict(AV, source_id="dropbox:2", course="CHEM_1A03_x_2269", title="Assignment 2")])
        ingest_ok(s, "crowdmark", [dict(CM, due="2026-10-25T23:59:00Z"), dict(CM, source_id="cm-88", course="CHEM 1A03 F26")])
        items = get_items(s)
        assert len(in_course(items, "CHEM 1A03")) == 1, "control pair (dues 8h apart) did not merge"
        rows = in_course(items, "MATH 1ZA3")
        assert len(rows) == 2, rows


# AC37 — merge keeps the Avenue row and fills its empty fields from Crowdmark
def test_merge_fills_empty():
    with InProcess() as s:
        ingest_ok(s, "avenue", [dict(AV, done=True)])
        av_id = get_items(s)[0]["id"]
        ingest_ok(s, "crowdmark", [dict(CM, mark=8, out_of=10, url="https://app.crowdmark.com/x", done=False)])
        rows = in_course(get_items(s), "MATH 1ZA3")
        assert len(rows) == 1, rows
        r = rows[0]
        assert (r["id"], r["source"], r["source_id"], r["title"], r["due"]) == (
            av_id, "avenue", "dropbox:1", AV["title"], AV["due"]), r
        assert (r["mark"], r["out_of"], r["url"]) == (8, 10, "https://app.crowdmark.com/x"), r
        assert bool(r["done"]) is True, r


# Fix — mark and out_of are filled as a pair: an unmarked Avenue gradebook row (out of 20) merged
# with a marked childsmath row (13/13) shows 13/13, not 13/20 (MATH 1B03 Assignment 1)
def test_merge_mark_out_of_pair():
    with InProcess() as s:
        ingest_ok(s, "avenue", [{"course": "MATH_1B03_x_2269", "source_id": "grade:1", "title": "Assignment 1", "out_of": 20}])
        ingest_ok(s, "childsmath", [{"course": "Math 1B03 F26", "source_id": "Math 1B03 F26:Assignment #1",
                                     "title": "Assignment #1", "mark": 13, "out_of": 13}])
        rows = in_course(get_items(s), "MATH 1B03")
        assert len(rows) == 1, rows
        assert (rows[0]["source"], rows[0]["mark"], rows[0]["out_of"]) == ("avenue", 13, 13), rows[0]


def two_overridden_rows(s, av_fields, cm_fields):
    """Ingest AV far from CM so both rows are visible, override each, then re-ingest AV so they merge."""
    ingest_ok(s, "avenue", [dict(AV, due=FAR)])
    ingest_ok(s, "crowdmark", [CM])
    items = get_items(s)
    override_ok(s, by_sid(items, "avenue")["dropbox:1"]["id"], av_fields)
    time.sleep(1.1)  # distinct edit times even at one-second resolution
    override_ok(s, by_sid(items, "crowdmark")["cm-77"]["id"], cm_fields)
    ingest_ok(s, "avenue", [AV])
    rows = in_course(get_items(s), "MATH 1ZA3")
    assert len(rows) == 1, rows
    return rows[0]


# AC38 — overrides on different fields from both rows are all kept
def test_merge_overrides_union():
    with InProcess() as s:
        r = two_overridden_rows(s, {"mark": 7}, {"weight": 15})
        assert r["source"] == "avenue", r
        assert (r["mark"], r["weight"]) == (7, 15), r
        assert set(overrides_of(r)) >= {"mark", "weight"}, overrides_of(r)


# AC48 — both rows override the same field: the later edit wins (here the Crowdmark one)
def test_merge_override_later_wins():
    with InProcess() as s:
        r = two_overridden_rows(s, {"mark": 1}, {"mark": 2})
        assert r["source"] == "avenue", r
        assert r["mark"] == 2, r
        assert overrides_of(r)["mark"]["value"] == 2, overrides_of(r)



# Plan Step 4 — CSRF guard: a CORS preflight is never granted
def test_no_cors_preflight():
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("1")])
        rid = get_items(s)[0]["id"]
        req = urllib.request.Request(s.url + f"/api/items/{rid}", method="OPTIONS", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"})
        try:
            with urllib.request.urlopen(req) as r:
                code, headers = r.status, r.headers
        except urllib.error.HTTPError as e:
            code, headers = e.code, e.headers
        assert code >= 400, f"OPTIONS -> {code}"
        allow = [h for h in headers if h.lower().startswith("access-control-")]
        assert not allow, allow
        assert override(s, 999, {"mark": 1}) == 404


def raw_request(s, method, path, headers, data=None):
    req = urllib.request.Request(s.url + path, data, headers, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.headers


def acao_headers(headers):
    return [h for h in headers if h.lower().startswith("access-control-")]


EXT_ORIGIN = "moz-extension://abc"
PREFLIGHT = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type,x-token"}


# Zen run fix — the extension's CORS preflight on the token routes is granted, to its exact origin only
def test_extension_preflight():
    with InProcess() as s:
        for path in ("/api/ingest", "/api/outline?course=X"):
            code, h = raw_request(s, "OPTIONS", path, {"Origin": EXT_ORIGIN, **PREFLIGHT})
            assert code == 204, (path, code)
            assert h["Access-Control-Allow-Origin"] == EXT_ORIGIN, (path, dict(h))
            assert h["Access-Control-Allow-Methods"] == "POST", dict(h)
            assert h["Access-Control-Allow-Headers"] == "Content-Type, X-Token", dict(h)
            assert h["Access-Control-Max-Age"] == "600", dict(h)
            assert h["Vary"] == "Origin", dict(h)
            for origin in ({"Origin": "https://evil.example"}, {}):
                code, h = raw_request(s, "OPTIONS", path, {**origin, **PREFLIGHT})
                assert code >= 400 and not acao_headers(h), (path, origin, code, dict(h))
        ingest_ok(s, "avenue", [item("1")])
        rid = get_items(s)[0]["id"]
        for path in (f"/api/items/{rid}", "/api/items", "/api/status", "/"):
            code, h = raw_request(s, "OPTIONS", path, {"Origin": EXT_ORIGIN, **PREFLIGHT})
            assert code >= 400 and not acao_headers(h), (path, code, dict(h))
        code, h = raw_request(s, "OPTIONS", "/api/ingest", {"Origin": EXT_ORIGIN, "Host": "evil.example", **PREFLIGHT})
        assert code == 403 and not acao_headers(h), (code, dict(h))


# Zen run fix — token-route POST responses carry ACAO for the extension origin; other routes never do
def test_extension_post_acao():
    with InProcess() as s:
        body = json.dumps({"source": "avenue", "items": [item("1")], "status": "ok"}).encode()
        hdr = {"Origin": EXT_ORIGIN, "Content-Type": "application/json", "X-Token": s.token}
        code, h = raw_request(s, "POST", "/api/ingest", hdr, body)
        assert code == 200 and h["Access-Control-Allow-Origin"] == EXT_ORIGIN and h["Vary"] == "Origin", (code, dict(h))
        code, h = raw_request(s, "POST", "/api/ingest", {**hdr, "X-Token": "wrong"}, body)
        assert code == 401, code  # token check unchanged
        code, h = raw_request(s, "POST", "/api/ingest", {**hdr, "Origin": "https://evil.example"}, body)
        assert code == 200 and not acao_headers(h), dict(h)
        rid = get_items(s)[0]["id"]
        code, h = raw_request(s, "POST", f"/api/items/{rid}", {"Origin": EXT_ORIGIN, "Content-Type": "application/json"},
                              json.dumps({"mark": 1}).encode())
        assert code == 200 and not acao_headers(h), dict(h)
        for path in ("/api/items", "/api/status", "/"):
            code, h = raw_request(s, "GET", path, {"Origin": EXT_ORIGIN})
            assert code == 200 and not acao_headers(h), (path, dict(h))


# ---- Step 5: outlines -----------------------------------------------------------

OUTLINE_TEXT = """MATH 1ZA3 Course Outline
Instructor office hours: Monday 2pm
Assignments ...................... 20%
Midterm Test ...... 30%
Final Exam ......... 50%
"""


def tiny_pdf(lines):
    """A one-page PDF whose text layer is `lines`, built by hand (no fixture files)."""
    ops = "BT /F1 11 Tf 72 720 Td 14 TL " + " ".join(
        "(" + ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") '" for ln in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(ops)} >>\nstream\n{ops}\nendstream",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def post_outline(s, course, body, ctype, token=True):
    headers = {"Content-Type": ctype}
    if token:
        headers["X-Token"] = s.token
    path = "/api/outline" + ("?course=" + urllib.parse.quote(course) if ctype != "application/json" else "")
    data = body if isinstance(body, bytes) else json.dumps({"course": course, "categories": body}).encode()
    return status_of("POST", s.url + path, headers, data)


def post_outline_ok(s, course, body, ctype="application/json"):
    code = post_outline(s, course, body, ctype)
    assert 200 <= code < 300, f"POST /api/outline -> {code}"


def course_weights(s, code):
    with sqlite3.connect(s.db) as c:
        row = c.execute("SELECT weights FROM courses WHERE code = ?", (code,)).fetchone()
    assert row and row[0], f"no weights stored for {code}"
    return json.loads(row[0])


def cats(*rows):
    return [{"name": n, "weights": list(w)} for n, *w in rows]


class no_pypdf:
    """server.pypdf raises if touched (AC64)."""

    def __enter__(self):
        import server

        class Boom:
            def __getattr__(self, name):
                raise AssertionError(f"pypdf.{name} called on the JSON outline path")

        self.server, self.old = server, server.__dict__.get("pypdf")
        server.pypdf = Boom()

    def __exit__(self, *exc):
        if self.old is None:
            del self.server.pypdf
        else:
            self.server.pypdf = self.old


# AC14 — `<Name> ... NN%` lines give category -> NN; other lines are ignored
def test_outline_regex():
    import server

    got = server.parse_outline_text(OUTLINE_TEXT)
    assert got == {"Assignments": 20, "Midterm Test": 30, "Final Exam": 50}, got
    # Session default: the separator is any run of dots, spaces or tabs
    got = server.parse_outline_text("Final Exam 50%\nLabs\t15%\nQuizzes . . 10%")
    assert got == {"Final Exam": 50, "Labs": 15, "Quizzes": 10}, got


# AC57 (parse side) — a range takes its upper bound and is flagged approximate
def test_pct_range():
    import server

    assert server.parse_pct("40-55%") == (55, True), server.parse_pct("40-55%")
    assert server.parse_pct("20%") == (20, False), server.parse_pct("20%")


# AC58 — footnote markers are stripped before parsing
def test_pct_footnote():
    import server

    assert server.parse_pct("0-5%**") == server.parse_pct("0-5%") == (5, True), server.parse_pct("0-5%**")
    assert server.parse_pct("10%*") == (10, False), server.parse_pct("10%*")
    assert server.parse_pct("1.5 %") == (1.5, False), server.parse_pct("1.5 %")  # live bug: space before %


# AC15 — a PDF outline is read with pypdf and its weights stored on the course
def test_outline_pdf():
    with InProcess() as s:
        post_outline_ok(s, "MATH 1ZA3", tiny_pdf(OUTLINE_TEXT.splitlines()), "application/pdf")
        w = course_weights(s, "MATH 1ZA3")
        assert w["schemes"] == [{"Assignments": 20, "Midterm Test": 30, "Final Exam": 50}], w


# Plan Step 5 — unreadable PDF bytes are a 400 and store nothing
def test_outline_bad_pdf():
    with InProcess() as s:
        post_outline_ok(s, "MATH 1ZA3", cats(("Assignments", "100%")))  # control: route exists
        before = db_dump(s)
        code = post_outline(s, "MATH 1ZA3", b"%PDF-1.4 not really a pdf", "application/pdf")
        assert code == 400, f"bad PDF -> {code}"
        assert db_dump(s) == before, "DB changed on a bad PDF"


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def tiny_docx(document_xml=None):
    """Minimal .docx: a weight paragraph and a two-row weight table (cells `Name` | `NN%`)."""
    import zipfile

    def p(text):
        return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"

    def row(*cells):
        return "<w:tr>" + "".join(f"<w:tc>{p(c)}</w:tc>" for c in cells) + "</w:tr>"

    if document_xml is None:
        document_xml = (f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:body>'
                        + p("Course Outline") + p("Assignments ...... 20%")
                        + "<w:tbl>" + row("Midterm Test", "30%") + row("Final Exam", "50%") + "</w:tbl>"
                        + "</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="xml" ContentType="application/xml"/></Types>')
        z.writestr("word/document.xml", document_xml)
    return buf.getvalue()


# AC14/AC15 (DOCX, live bug COMPSCI 1MD3) — a .docx outline's paragraphs and table rows give weights (stdlib only)
def test_outline_docx():
    with InProcess() as s:
        post_outline_ok(s, "COMPSCI 1MD3", tiny_docx(), DOCX)
        w = course_weights(s, "COMPSCI 1MD3")
        assert w["schemes"] == [{"Assignments": 20, "Midterm Test": 30, "Final Exam": 50}], w


# DOCX guard — garbage, a zip without word/document.xml, or an oversized document.xml is a 400 that stores nothing
def test_outline_bad_docx():
    import zipfile

    with InProcess() as s:
        post_outline_ok(s, "COMPSCI 1MD3", cats(("Assignments", "100%")))  # control: route exists
        before = db_dump(s)
        assert post_outline(s, "COMPSCI 1MD3", b"PK\x03\x04 not really a docx", DOCX) == 400, "garbage DOCX accepted"
        empty = io.BytesIO()
        with zipfile.ZipFile(empty, "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
        assert post_outline(s, "COMPSCI 1MD3", empty.getvalue(), DOCX) == 400, "DOCX without word/document.xml accepted"
        big = (f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>Final Exam 50%</w:t></w:r></w:p>'
               + " " * (11 * 1024 * 1024) + "</w:body></w:document>")
        data = tiny_docx(big)
        assert len(data) < 1024 * 1024, "fixture should be small on the wire"
        assert post_outline(s, "COMPSCI 1MD3", data, DOCX) == 400, "oversized word/document.xml accepted"
        no_weights = tiny_docx(f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:body></w:document>')
        assert post_outline(s, "COMPSCI 1MD3", no_weights, DOCX) == 400, "DOCX with no weight lines accepted"
        assert db_dump(s) == before, "DB changed on a bad DOCX"


# DOCX (extension side) — a File topic's outline is sent as PDF or DOCX by its type; anything else is skipped
def test_outline_file_types():
    body = js_function(ext("background.js"), "outline")
    assert DOCX in body, "outline() never sends the DOCX content type"
    assert "application/pdf" in body, "outline() no longer sends PDFs"
    assert "outline is not a PDF or DOCX" in body, "an unknown outline file type is not skipped with a warning"

# Plan Step 5 (Ambiguity 5) — the outline route needs X-Token
def test_outline_needs_token():
    with InProcess() as s:
        post_outline_ok(s, "MATH 1ZA3", cats(("Assignments", "100%")))  # control: with token it works
        before = db_dump(s)
        code = post_outline(s, "MATH 1ZA3", cats(("Assignments", "50%")), "application/json", token=False)
        assert code == 401, f"outline without token -> {code}"
        assert db_dump(s) == before, "DB changed on an unauthorized outline"


# AC64 + AC54 (server side) — JSON {course, categories} stored without pypdf, under the AC62 course key
# Review — a "Total 100%" line or row is not a category (PDF/DOCX text and Simple Syllabus JSON)
def test_outline_total_skipped():
    with InProcess() as s:
        post_outline_ok(s, "MATH 1ZA3", tiny_pdf((OUTLINE_TEXT + "Total ........ 100%\n").splitlines()), "application/pdf")
        assert course_weights(s, "MATH 1ZA3")["schemes"] == [{"Assignments": 20, "Midterm Test": 30, "Final Exam": 50}]
        post_outline_ok(s, "COMPSCI 1MD3", cats(("Labs", "40%"), ("Exam", "60%"), ("Total:", "100%")))
        assert course_weights(s, "COMPSCI 1MD3")["schemes"] == [{"Labs": 40, "Exam": 60}]
    import server
    assert server.outline_categories("Labs\t40%\nTotal\t100%")[-1]["name"] == "Total"  # the row is read; storage skips it
    with InProcess() as s:
        post_outline_ok(s, "COMPSCI 1MD3", tiny_docx(), DOCX)  # control
        doc = (f'<w:document xmlns:w="{W}"><w:body><w:tbl>'
               + "".join(f"<w:tr><w:tc><w:p><w:r><w:t>{a}</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>{b}</w:t></w:r></w:p></w:tc></w:tr>"
                         for a, b in (("Labs", "40%"), ("Exam", "60%"), ("Total", "100%")))
               + "</w:tbl></w:body></w:document>")
        post_outline_ok(s, "COMPSCI 1MD3", tiny_docx(doc), DOCX)
        assert course_weights(s, "COMPSCI 1MD3")["schemes"] == [{"Labs": 40, "Exam": 60}]


# Review AC29 — a renewal re-sync the server rejected is not reported as ok
def test_rerun_rejected_is_error():
    body = js_function(ext("background.js"), "rerun")
    assert re.search(r"ingest\(", body), "rerun() does not post through ingest()"
    assert re.search(r"\?\s*body\.status\s*:\s*'error'|if\s*\(\s*!\s*(?:await\s+)?\w+\s*\)\s*status\s*=\s*'error'", body), \
        "rerun() reports the posted status even when the server rejected it"


def test_outline_json():
    with InProcess() as s, no_pypdf():
        post_outline_ok(s, "MATH_1ZA3_x_2269", cats(("Assignments", "20%"), ("Final Exam", "80%")))
        w = course_weights(s, "MATH 1ZA3")
        assert w["schemes"] == [{"Assignments": 20, "Final Exam": 80}], w
        assert not w.get("approximate"), w


# AC55 — `Term Test 1 | 10% | 15%` gives 10 in scheme 1 and 15 in scheme 2
def test_outline_two_schemes():
    with InProcess() as s:
        post_outline_ok(s, "MATH 1ZA3", cats(("Term Test 1", "10%", "15%"), ("Final Exam", "90%", "85%")))
        w = course_weights(s, "MATH 1ZA3")
        assert w["schemes"] == [{"Term Test 1": 10, "Final Exam": 90}, {"Term Test 1": 15, "Final Exam": 85}], w


# AC57 + AC58 (stored) — a range stores its upper bound and marks the course approximate; footnotes ignored
def test_outline_range_approximate():
    with InProcess() as s:
        post_outline_ok(s, "MATH 1ZA3", cats(("Final Exam", "40-55%"), ("Bonus", "0-5%**"), ("Assignments", "40%")))
        w = course_weights(s, "MATH 1ZA3")
        assert w["schemes"] == [{"Final Exam": 55, "Bonus": 5, "Assignments": 40}], w
        assert w["approximate"] is True, w


# AC47 — a category matching no kind gets exactly one placeholder item; weights still add up
def test_placeholder_once():
    with InProcess() as s:
        outline = cats(("Assignments", "90%"), ("Participation", "10%"))
        post_outline_ok(s, "MATH 1ZA3", outline)
        post_outline_ok(s, "MATH 1ZA3", outline)
        rows = [i for i in in_course(get_items(s), "MATH 1ZA3") if i["title"] == "Participation"]
        assert len(rows) == 1, rows
        assert (rows[0]["weight"], rows[0]["due"], rows[0]["kind"]) == (10, None, "other"), rows[0]
        assert not [i for i in get_items(s) if i["title"] == "Assignments"], "placeholder made for a kind-matching category"
        assert sum(course_weights(s, "MATH 1ZA3")["schemes"][0].values()) == 100


# AC72 (server side) — ingest category_marks are stored per course, keyed by normalized name
def test_category_mark_stored():
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("1")], category_marks=[
            {"course": "MATH_1ZA3_x_2269", "name": "Online Homework", "value": 87.5}])
        w = course_weights(s, "MATH 1ZA3")
        assert w["category_marks"] == {"online homework": 87.5}, w


# ---- Step 6: item weights and course grades -------------------------------------

def get_courses(s):
    code, body = call(s, "GET", "/api/items")
    assert code == 200, f"GET /api/items -> {code}"
    return body["courses"]


def near(a, b):
    return a is not None and abs(a - b) < 1e-6


# Assignments 20 / Midterm Test 30 / Final Exam 50 (the AC14 outline)
GRADED = [item("a1", title="Assignment 1", mark=8, out_of=10),
          item("a2", title="Assignment 2", mark=6, out_of=10),
          item("mt", title="Midterm", mark=15, out_of=20),
          item("fx", title="Final Exam")]


def graded_course(s):
    post_outline_ok(s, "MATH 1ZA3", cats(("Assignments", "20%"), ("Midterm Test", "30%"), ("Final Exam", "50%")))
    ingest_ok(s, "avenue", GRADED)


# AC40 — category weight W over N matching items gives W/N each
def test_item_weight_split():
    with InProcess() as s:
        graded_course(s)
        w = {sid: i["weight"] for sid, i in by_sid(get_items(s), "avenue").items()}
        assert all(near(w[k], v) for k, v in {"a1": 10, "a2": 10, "mt": 30, "fx": 50}.items()), w


# AC52 — an ingested (gradebook) Weight is ignored; the outline weight is used
def test_gradebook_weight_ignored():
    with InProcess() as s:
        post_outline_ok(s, "MATH 1ZA3", cats(("Assignments", "20%"), ("Final Exam", "80%")))
        ingest_ok(s, "avenue", [item("grade:1", title="Assignment 1", weight=99), item("grade:2", title="Assignment 2", weight=1)])
        w = {sid: i["weight"] for sid, i in by_sid(get_items(s), "avenue").items()}
        assert near(w["grade:1"], 10) and near(w["grade:2"], 10), w


# Plan Step 6 — a user weight override wins over the derived weight
def test_weight_override_wins():
    with InProcess() as s:
        graded_course(s)
        override_ok(s, by_sid(get_items(s), "avenue")["a1"]["id"], {"weight": 12})
        w = {sid: i["weight"] for sid, i in by_sid(get_items(s), "avenue").items()}
        assert near(w["a1"], 12) and near(w["a2"], 10), w


# AC29 — course summary: weighted mark over graded items, and % of course weight graded
def test_grade_basic():
    with InProcess() as s:
        graded_course(s)
        c = get_courses(s)["MATH 1ZA3"]
        # (10*.8 + 10*.6 + 30*.75) / 50 = 73 ; graded 50 of 100
        assert near(c["grade"], 73) and near(c["graded_pct"], 50), c
        assert c["approximate"] is False, c


# AC56 — several schemes: computed under each, the higher one shown
def test_grade_two_schemes():
    with InProcess() as s:
        two = cats(("Assignments", "20%", "40%"), ("Final Exam", "80%", "60%"))
        post_outline_ok(s, "MATH 1ZA3", two)
        post_outline_ok(s, "CHEM 1A03", two)
        ingest_ok(s, "avenue", [
            item("m-a", title="Assignment 1", mark=10, out_of=10), item("m-x", title="Final Exam", mark=50, out_of=100),
            item("c-a", course="CHEM 1A03", title="Assignment 1", mark=0, out_of=10),
            item("c-x", course="CHEM 1A03", title="Final Exam", mark=100, out_of=100)])
        c = get_courses(s)
        assert near(c["MATH 1ZA3"]["grade"], 70), c  # s1 60, s2 70
        assert near(c["CHEM 1A03"]["grade"], 80), c  # s1 80, s2 60
        assert near(by_sid(get_items(s), "avenue")["m-a"]["weight"], 20), "item weight is not scheme 1"


# AC57 (server) — a range-derived weight marks the course grade approximate
def test_grade_approximate():
    with InProcess() as s:
        post_outline_ok(s, "MATH 1ZA3", cats(("Assignments", "45-60%"), ("Final Exam", "40%")))
        post_outline_ok(s, "CHEM 1A03", cats(("Assignments", "60%"), ("Final Exam", "40%")))
        ingest_ok(s, "avenue", [item("m", title="Assignment 1", mark=5, out_of=10),
                                item("c", course="CHEM 1A03", title="Assignment 1", mark=5, out_of=10)])
        c = get_courses(s)
        assert (c["MATH 1ZA3"]["approximate"], c["CHEM 1A03"]["approximate"]) == (True, False), c


def homework_course(s, items):
    post_outline_ok(s, "MATH 1ZA3", cats(("Assignments", "90%"), ("Online Homework", "10%")))
    ingest_ok(s, "avenue", [item("a1", title="Assignment 1", mark=9, out_of=10), *items],
              category_marks=[{"course": "MATH 1ZA3", "name": "Online Homework", "value": 80}])


# AC73 — category mark with no item marks counts for the whole category weight
def test_category_mark_used():
    with InProcess() as s:
        homework_course(s, [])
        c = get_courses(s)["MATH 1ZA3"]
        # (90*.9 + 10*.8) / 100
        assert near(c["grade"], 89) and near(c["graded_pct"], 100), c


# AC74 — any item mark in the category: the category mark is ignored
def test_category_mark_ignored():
    with InProcess() as s:
        homework_course(s, [item("h1", title="Homework 1", mark=5, out_of=10), item("h2", title="Homework 2")])
        c = get_courses(s)["MATH 1ZA3"]
        # items 5 + 5 weight; graded: 90*.9 + 5*.5 = 83.5 over 95
        assert near(c["grade"], 83.5 / 95 * 100) and near(c["graded_pct"], 95), c


# Session default — re-posting an outline without a category removes its placeholder unless overridden
def test_placeholder_removed_on_repost():
    with InProcess() as s:
        post_outline_ok(s, "MATH 1ZA3", cats(("Assignments", "80%"), ("Participation", "10%"), ("Attendance", "10%")))
        att = [i for i in get_items(s) if i["title"] == "Attendance"]
        assert len(att) == 1, att
        override_ok(s, att[0]["id"], {"mark": 9})
        post_outline_ok(s, "MATH 1ZA3", cats(("Assignments", "100%")))
        titles = [i["title"] for i in in_course(get_items(s), "MATH 1ZA3")]
        assert "Participation" not in titles, titles
        assert titles.count("Attendance") == 1, titles



# Session default — nothing graded yet: grade None, graded_pct 0
def test_grade_nothing_graded():
    with InProcess() as s:
        post_outline_ok(s, "MATH 1ZA3", cats(("Assignments", "40%"), ("Final Exam", "60%")))
        ingest_ok(s, "avenue", [item("a1", title="Assignment 1")])
        c = get_courses(s)["MATH 1ZA3"]
        assert c["grade"] is None and c["graded_pct"] == 0, c



# Quick — a table row "<Name>\t<pct>\t<description>" (COMPSCI 1MD3's DOCX) gives its name and weight
def test_outline_tab_rows():
    import server

    text = ("Assignment 0 (week 2)\t1%\tJust to make sure you understand auto-grading\n"
            "Midterm (Nov. 3rd)\t25%\tHeld during the evening. The lecture on Oct 30th will be a review.\n"
            "Exam (Exam week)\t45%\tCumulative.\n"
            "Weekly readings\tsee Avenue\tnot graded")
    got = server.parse_outline_text(text)
    assert got == {"Assignment 0 (week 2)": 1, "Midterm (Nov. 3rd)": 25, "Exam (Exam week)": 45}, got


# Quick — a course with no outline is graded from its item weights (MATH 1A03, weights entered by hand)
def test_grade_manual_weights():
    with InProcess() as s:
        ingest_ok(s, "childsmath", [item("m1", course="Math 1A03 F26", title="Assignment #1", mark=8, out_of=10),
                                    item("m2", course="Math 1A03 F26", title="Assignment #2"),
                                    item("m3", course="Math 1A03 F26", title="Test #1", mark=30, out_of=40)])
        ids = {i["source_id"]: i["id"] for i in get_items(s)}
        override_ok(s, ids["m1"], {"weight": 10})
        override_ok(s, ids["m2"], {"weight": 10})
        override_ok(s, ids["m3"], {"weight": 30})
        c = get_courses(s)["MATH 1A03"]
        # graded: 10 * 0.8 + 30 * 0.75 = 30.5 of 40 -> 76.25%; 40 of 50 weight graded -> 80%
        assert near(c["grade"], 76.25) and near(c["graded_pct"], 80), c


# Session default — a placeholder gives way to a real item of the same `other` category
def test_placeholder_gives_way():
    with InProcess() as s:
        post_outline_ok(s, "MATH 1ZA3", cats(("Assignments", "90%"), ("Participation", "10%")))
        ingest_ok(s, "avenue", [item("p", title="Participation", mark=9, out_of=10)])
        av = by_sid(get_items(s), "avenue")["p"]
        assert near(av["weight"], 10), av
        c = get_courses(s)["MATH 1ZA3"]
        assert near(c["grade"], 90) and near(c["graded_pct"], 10), c


# ---- Step 7: extension parse.js (node) -------------------------------------------

# AC25, AC59, AC60, AC69, AC70, AC71, AC76, AC77 — see test_extension.js
def test_extension_js():
    node = shutil.which("node")
    assert node, "node not found on PATH (needed for test_extension.js)"
    r = subprocess.run([node, "test_extension.js"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, (r.stdout + r.stderr).strip()[-2000:]


# ---- Step 10: extension shell (static checks; the live run is manual in Zen) ------

EXT = ROOT / "extension"
HOSTS = ("avenue.cllmcmaster.ca", "mcmaster.simplesyllabusca.com", "app.crowdmark.com", "www.childsmath.ca", "127.0.0.1:8000")


def ext(name):
    return (EXT / name).read_text()


def js_function(src, name):
    """Body of `function name(...) {...}` / `const name = (...) => {...}` (brace-matched; strings not parsed)."""
    m = re.search(rf"(?:async\s+)?function\s+{name}\s*\(|(?:const|let|var)\s+{name}\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>\s*\{{", src)
    assert m, f"no function {name} in background.js"
    i = src.index("{", m.end() - 1 if src[m.end() - 1] == "{" else src.index(")", m.end() - 1))
    depth = 0
    for j in range(i, len(src)):
        depth += {"{": 1, "}": -1}.get(src[j], 0)
        if depth == 0:
            return src[i:j + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def call_args(src, start):
    """Text of the parenthesised arguments that open at src[start] == '('."""
    depth = 0
    for j in range(start, len(src)):
        depth += {"(": 1, ")": -1}.get(src[j], 0)
        if depth == 0:
            return src[start:j + 1]
    raise AssertionError("unbalanced parens")


def fetch_calls(src):
    return [call_args(src, m.end() - 1) for m in re.finditer(r"(?<![\w.])fetch\s*\(", src)]


def server_names(src):
    """Identifiers bound to a string pointing at the local server, plus the literal itself."""
    return ["127.0.0.1"] + re.findall(r"(?:const|let|var)\s+(\w+)\s*=\s*['\"`]http://127\.0\.0\.1:8000", src)


# AC17 (+ AC18 no popup, AC35 options page) — manifest shape
def test_manifest():
    m = json.loads(ext("manifest.json"))
    assert m["manifest_version"] == 3, m.get("manifest_version")
    gecko = m["browser_specific_settings"]["gecko"]
    assert gecko.get("id"), gecko
    assert gecko.get("data_collection_permissions") == {"required": ["none"]}, gecko
    scripts = m["background"]["scripts"]
    assert "parse.js" in scripts and "background.js" in scripts, scripts
    assert scripts.index("parse.js") < scripts.index("background.js"), scripts
    assert {"alarms", "storage"} <= set(m["permissions"]), m["permissions"]
    assert "cookies" not in m["permissions"], m["permissions"]
    for host in HOSTS:
        assert any(host in p for p in m["host_permissions"]), f"no host permission for {host}"
    # Spec 0002 AC17/AC18/AC20: renewal adds notifications + tabs only; no content scripts, no IdP hosts
    assert set(m["permissions"]) == {"alarms", "storage", "notifications", "tabs"}, m["permissions"]
    assert "content_scripts" not in m, m.get("content_scripts")
    idp = [p for p in m["host_permissions"] + m.get("optional_host_permissions", [])
           if "cap.mcmaster.ca" in p or "microsoftonline" in p]
    assert not idp, f"IdP host permission: {idp}"
    assert "action" in m, "no toolbar button (manifest `action`)"
    assert "default_popup" not in m["action"], m["action"]
    page = m["options_ui"]["page"]
    assert (EXT / page).exists(), f"options page {page} missing"
    for f in scripts:
        assert (EXT / f).exists(), f"background script {f} missing"


# AC35 — options page stores the token in browser.storage; posts carry it as X-Token
def test_options_token():
    html = ext(json.loads(ext("manifest.json"))["options_ui"]["page"])
    assert re.search(r"<script[^>]*\bsrc=['\"]options\.js['\"]", html), "options page does not load options.js"
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", html), "inline script (blocked by MV3 CSP)"
    assert "browser.storage.local.set" in ext("options.js"), "options.js does not save to browser.storage.local"
    bg = ext("background.js")
    assert "browser.storage.local.get" in bg, "background.js does not read the token from browser.storage.local"
    posts = [a for a in fetch_calls(bg) if any(n in a for n in server_names(bg))]
    assert posts, "no fetch to the local server"
    assert all("X-Token" in a for a in posts) or "X-Token" in js_function(bg, "post"), "server post without X-Token"


# AC19 — 3-hour alarm runs sync
def test_alarm_period():
    bg = ext("background.js")
    assert re.search(r"browser\.alarms\.create\([^)]*periodInMinutes\s*:\s*180\b", bg), "no 180-minute alarm"
    assert re.search(r"browser\.alarms\.onAlarm\.addListener\([^\n]*\bsync\b", bg), "onAlarm does not call sync"


# AC18 + AC45 — toolbar click runs sync immediately
def test_toolbar_click():
    bg = ext("background.js")
    assert re.search(r"browser\.action\.onClicked\.addListener\([^\n]*\bsync\b", bg), "toolbar click does not call sync"


# AC22 — source fetches use credentials 'include'; no cookie access anywhere
def test_no_cookies():
    for f in EXT.iterdir():
        assert "cookie" not in f.read_text().lower(), f"`cookie` in extension/{f.name}"
    assert not fetch_calls(ext("parse.js")), "parse.js must stay pure (no fetch)"
    bg = ext("background.js")
    calls = fetch_calls(bg)
    srv = server_names(bg)
    include = [a for a in calls if re.search(r"credentials\s*:\s*['\"]include['\"]", a)]
    assert include, "no fetch with credentials: 'include'"
    assert re.search(r"credentials\s*:\s*['\"]include['\"]", js_function(bg, "get")), "get() does not use credentials: 'include'"
    stray = [a for a in calls if a not in include and not any(n in a for n in srv)]
    assert not stray, f"source fetch without credentials 'include': {stray}"


# AC78 — childsmath is read with GET only; its forms are never submitted
def test_childsmath_get_only():
    bg = ext("background.js")
    body = js_function(bg, "childsmath")
    assert not re.search(r"method\s*:|\.submit\s*\(|['\"]POST['\"]", body, re.I), "non-GET request in childsmath()"
    posting = {n for n in re.findall(r"(?:function\s+|(?:const|let|var)\s+)(\w+)", bg)
               if n != "childsmath" and re.search(r"method\s*:|['\"]POST['\"]", _safe_body(bg, n) or "")}
    called = {n for n in posting if re.search(rf"\b{n}\s*\(", body)}
    assert not called, f"childsmath() calls POST-capable helpers {called}"


def test_direct_syllabus_docs_deferred():
    """Quick: direct Simple Syllabus doc links (MATH 1B03 on Avenue, MATH 1A03 on childsmath's
    announce.php) are collected while sources sync and read only after syncAll, once an earlier
    LTI launch has opened the Simple Syllabus session."""
    bg = ext("background.js")
    cm = js_function(bg, "childsmath")
    assert "announce.php" in cm and "ssDocCodes(" in cm, "childsmath() does not look for outline links on announce.php"
    assert "docCategories(" not in cm, "childsmath() reads a Simple Syllabus doc before the session exists"
    assert "docCategories(" not in js_function(bg, "outline"), "outline() reads a direct doc before the session exists"
    s = js_function(bg, "sync")
    assert "syncAll(" in s and re.search(r"syncAll\(.*?(readSyllabusDocs|docCategories)\(", s, re.S), \
        "sync() does not read collected Simple Syllabus docs after syncAll"


def _safe_body(src, name):
    try:
        return js_function(src, name)
    except AssertionError:
        return None


# Implementer requirement — avenue() puts the course on each categoryMarks entry (server rejects ingest without it)
def test_category_marks_course():
    body = js_function(ext("background.js"), "avenue")
    windows = [body[m.start():m.start() + 200] for m in re.finditer(r"categoryMarks", body)]
    assert any(re.search(r"\bcourse\b", w) for w in windows), "avenue() does not attach course to categoryMarks"


# Implementer requirement — a rejected post to the server is logged, not swallowed
def test_sync_logs_rejected_posts():
    bg = ext("background.js")
    post = next((_safe_body(bg, n) for n in re.findall(r"(?:function\s+|(?:const|let|var)\s+)(\w+)", bg)
                 if "X-Token" in (_safe_body(bg, n) or "")), None)
    assert post, "no function sending X-Token"
    assert re.search(r"\.ok\b|\.status\b", post), "server post does not check the response"
    assert re.search(r"console\.(error|warn)", post), "rejected server post is not logged"


# ---- Step 11: index.html (static; rendering is checked manually in Zen) -----------

# AC2/AC26-31/AC35/AC41/AC42/AC51/AC83 — what can be proven without a browser
def test_index_static():
    html = (ROOT / "index.html").read_text()
    assert "localStorage" in html, "upcoming window not kept in localStorage (AC42/AC51)"
    assert "/api/items" in html and "/api/status" in html, "page does not read /api/items and /api/status"
    calls = fetch_calls(html)
    assert calls, "page makes no fetch"
    for a in calls:
        url = re.match(r"\(\s*([`'\"])(.*?)\1", a)
        assert url and url.group(2).startswith(("/api/items", "/api/status")), f"unexpected request: fetch{a[:80]}"
        assert "localStorage" not in a and not re.search(r"upcoming|window", a, re.I), f"window sent to server: {a[:120]}"
    edits = [a for a in calls if "/api/items/" in a]
    assert edits, "no inline-edit POST to /api/items/<id> (AC31)"
    for a in edits:
        assert re.search(r"method\s*:\s*['\"]POST['\"]", a) and "application/json" in a, f"edit POST not JSON: {a[:160]}"
    assert not re.search(r"x-token|token\.txt", html, re.I), "page references the ingest token (AC35: only the extension holds it)"
    assert "const UPCOMING_DAYS = 7;" in html and "UPCOMING_DAYS * 864e5" in html, "upcoming window is not 7 days (AC41)"
    assert "login needed" in html, "no login-needed banner text (AC30)"
    assert "—" in html or "&mdash;" in html, "no — for an ungraded course (AC83)"
    assert not re.search(r"""(?:src|href)\s*=\s*['"]?(?:https?:)?//|@import""", html, re.I), "external resource (page must work offline)"


# ---- Live-run fixes (bugs 3-7; ACs from AC85) ------------------------------------

# Bug 3 — any item with a mark is done, whatever done the source sent
def test_marked_item_done():
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("m1", mark=5, out_of=10, done=False), item("m2", mark=0, out_of=5),
                                item("u1", done=False)])
        a = by_sid(get_items(s), "avenue")
        assert bool(a["m1"]["done"]) is True, a["m1"]
        assert bool(a["m2"]["done"]) is True, a["m2"]
        assert not a["u1"]["done"], a["u1"]


LAB = "Lab 1: Excel Exercises"


# Bug 4 — one due missing: same course + title still merges; Avenue kept, due filled from the other
def test_dedup_one_due_missing():
    with InProcess() as s:
        ingest_ok(s, "avenue", [{"course": "MATH_1ZA3_x_2269", "source_id": "grade:9", "title": LAB, "mark": 1, "out_of": 1}])
        ingest_ok(s, "crowdmark", [{"course": "MATH 1ZA3 Fall 2026", "source_id": "cm-9", "title": LAB, "due": "2026-09-20T03:59:00Z"}])
        rows = in_course(get_items(s), "MATH 1ZA3")
        assert len(rows) == 1, rows
        assert (rows[0]["source"], rows[0]["due"]) == ("avenue", "2026-09-20T03:59:00Z"), rows[0]


# Bug 4 — both dues missing, different sources: merges
def test_dedup_both_dues_missing():
    with InProcess() as s:
        ingest_ok(s, "avenue", [{"course": "MATH 1ZA3", "source_id": "grade:9", "title": LAB}])
        ingest_ok(s, "crowdmark", [{"course": "MATH 1ZA3", "source_id": "cm-9", "title": LAB}])
        rows = in_course(get_items(s), "MATH 1ZA3")
        assert len(rows) == 1 and rows[0]["source"] == "avenue", rows


# Bug 4 — rows from the same source never merge
def test_dedup_same_source_never():
    with InProcess() as s:
        ingest_ok(s, "avenue", [{"course": "MATH 1ZA3", "source_id": "grade:9", "title": LAB},
                                {"course": "MATH 1ZA3", "source_id": "dropbox:9", "title": LAB},
                                {"course": "MATH 1ZA3", "source_id": "quiz:9", "title": LAB, "due": "2026-09-20T03:59:00Z"},
                                {"course": "MATH 1ZA3", "source_id": "quiz:10", "title": LAB, "due": "2026-09-20T05:59:00Z"}])
        assert len(in_course(get_items(s), "MATH 1ZA3")) == 4


# AC88 — three matching rows from three sources merge into one Avenue row, filled from the others
def test_dedup_three_sources():
    with InProcess() as s:
        ingest_ok(s, "childsmath", [{"course": "Math 1ZA3 F26", "source_id": "Math 1ZA3 F26:" + LAB, "title": LAB, "progress": "3/8"}])
        ingest_ok(s, "crowdmark", [{"course": "MATH 1ZA3 Fall 2026", "source_id": "cm-9", "title": LAB, "url": "https://app.crowdmark.com/x"}])
        ingest_ok(s, "avenue", [{"course": "MATH_1ZA3_x_2269", "source_id": "grade:9", "title": LAB, "mark": 1, "out_of": 1}])
        rows = in_course(get_items(s), "MATH 1ZA3")
        assert len(rows) == 1, rows
        r = rows[0]
        assert (r["source"], r["url"], r["progress"], r["mark"]) == ("avenue", "https://app.crowdmark.com/x", "3/8", 1), r


# AC88 — priority crowdmark > childsmath when there is no Avenue row; fill applies to that pair too
def test_dedup_priority_crowdmark_over_childsmath():
    with InProcess() as s:
        ingest_ok(s, "childsmath", [{"course": "Math 1ZA3 F26", "source_id": "Math 1ZA3 F26:" + LAB, "title": LAB,
                                     "due": "2026-09-20T03:59:00Z", "progress": "8/8"}])
        ingest_ok(s, "crowdmark", [{"course": "MATH 1ZA3 Fall 2026", "source_id": "cm-9", "title": LAB, "mark": 7, "out_of": 8}])
        rows = in_course(get_items(s), "MATH 1ZA3")
        assert len(rows) == 1, rows
        r = rows[0]
        assert (r["source"], r["due"], r["progress"], r["mark"]) == ("crowdmark", "2026-09-20T03:59:00Z", "8/8", 7), r


# AC90 — a "practice" outline category becomes a placeholder (no kind bucket); practice items are not stored
def test_practice_category_placeholder():
    with InProcess() as s:
        post_outline_ok(s, "MATH 1ZA3", cats(("Assignments", "95%"), ("Practice Quizzes", "5%")))
        ingest_ok(s, "avenue", [item("a1", title="Assignment 1", mark=6, out_of=10),
                                item("p1", title="Practice Quiz 1", mark=10, out_of=10)])
        items = in_course(get_items(s), "MATH 1ZA3")
        ph = [i for i in items if i["title"] == "Practice Quizzes"]
        assert len(ph) == 1 and ph[0]["source"] == "outline", ph
        assert "p1" not in by_sid(items, "avenue"), items
        assert sum(course_weights(s, "MATH 1ZA3")["schemes"][0].values()) == 100
        c = get_courses(s)["MATH 1ZA3"]
        assert near(c["grade"], 60), c  # only Assignment 1 counts: 6/10


# Bug 5 — "practice" is checked before every other keyword
def test_kind_practice():
    import server

    got = [server.kind(t) for t in ("Practice Test 1", "Practice Exam", "Unit 3 - Atomic Structure - Practice Problems")]
    assert got == ["practice"] * 3, got


# Bug 6 / AC90 — synced practice items are dropped and do not count, even beside a "Tests" category
def test_practice_no_weight():
    with InProcess() as s:
        post_outline_ok(s, "MATH 1ZA3", cats(("Tests", "50%"), ("Assignments", "50%")))
        ingest_ok(s, "avenue", [item("t1", title="Test 1", mark=40, out_of=50),
                                item("p1", title="Practice Test 1", mark=10, out_of=10),
                                item("a1", title="Assignment 1", mark=6, out_of=10)])
        a = by_sid(get_items(s), "avenue")
        assert "p1" not in a, a["p1"]
        assert near(a["t1"]["weight"], 50), a["t1"]
        c = get_courses(s)["MATH 1ZA3"]
        assert near(c["grade"], 70) and near(c["graded_pct"], 100), c  # (50*.8 + 50*.6) / 100
        # a stored item retitled to practice is removed on that sync
        ingest_ok(s, "avenue", [item("a1", title="Practice Assignment 1")])
        assert "a1" not in by_sid(get_items(s), "avenue")


# Bug 7 — ingest course_info is stored per course and returned as courses[code].info; absent leaves it alone
def test_course_info_stored():
    info = [{"name": "Lab Room", "value": "ABB 102"}, {"name": "Test 1 Room", "value": ""}]
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("1")], course_info=[{"course": "MATH_1ZA3_x_2269", **i} for i in info])
        assert get_courses(s)["MATH 1ZA3"]["info"] == info, get_courses(s)["MATH 1ZA3"]
        ingest_ok(s, "avenue", [item("1")])
        assert get_courses(s)["MATH 1ZA3"]["info"] == info, "ingest without course_info dropped the info"
        ingest_ok(s, "avenue", [item("1")], course_info=[{"course": "MATH 1ZA3", "name": "Lab Section", "value": "L07"}])
        assert get_courses(s)["MATH 1ZA3"]["info"] == [{"name": "Lab Section", "value": "L07"}], "course_info not replaced as a whole"


# Bug 7 (page) — the course header renders courses[...].info
def test_index_course_info():
    html = (ROOT / "index.html").read_text()
    assert re.search(r"\.info\b|\[\s*['\"]info['\"]\s*\]", html), "index.html never reads courses[code].info"



# ---- Review findings ---------------------------------------------------------------

# Review MAJOR 1 — an empty or blank token.txt is replaced by a fresh token, never an empty one
def test_empty_token_file_regenerated():
    for text in ("", "  \n"):
        with InProcess(token_text=text) as s:
            t = (s.dir / "token.txt").read_text().strip()
            assert len(t) >= 16 and s.token == t, (text, t, s.token)
            body = {"source": "avenue", "items": [], "status": "ok"}
            assert call(s, "POST", "/api/ingest", body, token=None)[0] == 401
            assert call(s, "POST", "/api/ingest", body, token="")[0] == 401


# Review MAJOR 1 — an empty X-Token is refused outright
def test_empty_x_token_refused():
    with InProcess() as s:
        assert ingest(s, "avenue", [item("1")], token="") == 401


# Review MAJOR 2 — override values are type-checked per field; a bad one is a 400 and writes nothing
def test_override_field_types():
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("1", mark=5, out_of=10)])
        rid = get_items(s)[0]["id"]
        before = db_dump(s)
        for bad in ({"mark": "abc"}, {"weight": True}, {"out_of": "10"}, {"done": "yes"}, {"done": 1},
                    {"title": 5}, {"due": 20261020}, {"kind": ["quiz"]}):
            assert override(s, rid, bad) == 400, bad
        assert db_dump(s) == before, "DB changed on a bad override"
        assert get_items(s)[0]["mark"] == 5
        override_ok(s, rid, {"mark": None, "done": True, "title": "T", "due": None, "weight": 2.5})


# Review MAJOR 2 — ingest values are type-checked per field; a bad one is a 400 and writes nothing
def test_ingest_field_types():
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("1", mark=5)])
        before = db_dump(s)
        for bad in ({"mark": "abc"}, {"out_of": True}, {"weight": "5"}, {"done": "true"}, {"done": 1},
                    {"closed": "no"}, {"title": 3}, {"due": 1}, {"url": 1}, {"progress": 3}, {"course": 1}):
            code, _ = call(s, "POST", "/api/ingest", {"source": "avenue", "status": "ok", "items": [item("2", **bad)]})
            assert code == 400, (bad, code)
        assert db_dump(s) == before, "DB changed on a bad ingest"


# Review MINOR 3 — a non-ASCII error detail still gets a 400 response (no dropped connection)
def test_error_detail_non_ascii():
    with InProcess() as s:
        code = post_outline(s, "MATH 1ZA3", cats(("Final Exam", "40\u201455%")), "application/json")
        assert code == 400, code


# Review MINOR 6 — an ingest naming a course replaces its category marks and info as a whole; a bare
# {course} entry names a course with none, which clears it; courses not named are left alone
def test_course_marks_and_info_replaced():
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("1")],
                  category_marks=[{"course": "MATH 1ZA3", "name": "Online Homework", "value": 80},
                                  {"course": "CHEM 1A03", "name": "Labs", "value": 70}],
                  course_info=[{"course": "MATH 1ZA3", "name": "Lab Room", "value": "ABB 102"},
                               {"course": "CHEM 1A03", "name": "Lab Section", "value": "L07"}])
        ingest_ok(s, "avenue", [item("1")],
                  category_marks=[{"course": "MATH 1ZA3", "name": "Engagement", "value": 90}, {"course": "CHEM 1A03"}],
                  course_info=[{"course": "MATH 1ZA3"}])
        assert course_weights(s, "MATH 1ZA3")["category_marks"] == {"engagement": 90}
        assert course_weights(s, "CHEM 1A03")["category_marks"] == {}
        c = get_courses(s)
        assert c["MATH 1ZA3"]["info"] == [] and c["CHEM 1A03"]["info"] == [{"name": "Lab Section", "value": "L07"}], c
        ingest_ok(s, "avenue", [item("1")])
        assert course_weights(s, "MATH 1ZA3")["category_marks"] == {"engagement": 90}, "ingest without the key changed marks"


# Review MINOR 4 — the LTI replay only submits forms to Avenue or Simple Syllabus and checks each hop
def test_lti_replay_checks():
    body = js_function(ext("background.js"), "syllabusCategories")
    assert re.search(r"\.origin\b", body) and "AVENUE" in body and "SYLLABUS" in body, "no form-action origin allowlist"
    assert re.search(r"throw", body), "a disallowed form action is not refused"
    hop = body[body.index("for ("):]
    assert re.search(r"\.ok\b|checked\(|get\(", hop), "an LTI hop response is not checked"


# AC90 (2026-10-18) — practice is not tracked, so the page no longer offers it as a type
def test_index_kinds_practice():
    m = re.search(r"const KINDS\s*=\s*\[([^\]]*)\]", (ROOT / "index.html").read_text())
    assert m and "'practice'" not in m.group(1), m and m.group(1)


# Review MINOR 7 — the sync alarm is created once, not on every background start
def test_alarm_created_once():
    bg = ext("background.js")
    create = bg.index("browser.alarms.create(")
    before = bg[max(0, create - 200):create]
    assert re.search(r"onInstalled|alarms\.get\(", before), "alarm re-created on every background start"


# ---- Spec 0002 Step 4: renewal glue (static; the live run is manual in Zen) ---------

def _listeners(src):
    return [call_args(src, m.end() - 1) for m in re.finditer(r"\.addListener\s*\(", src)]


def _object_keys(arg):
    """Top-level keys of the object literal in a call's argument text `({a: 1, b})`, or None if not a literal."""
    inner = arg.strip()[1:-1].strip()
    if not inner.startswith("{"):
        return None
    body, depth, top = inner[1:inner.rindex("}")], 0, ""
    for ch in body:
        depth += {"{": 1, "[": 1, "(": 1, "}": -1, "]": -1, ")": -1}.get(ch, 0)
        top += ch if depth == 0 or ch in "{[(" and depth == 1 else ""
    return [k.strip().strip("'\"[]") for k in re.findall(r"(?:^|,)\s*([\w'\"\[\]]+)\s*(?=:|,|$)", top)]


# Spec 0002 AC2 — the renewal tab opens inactive, so the student's tab stays active
def test_renew_tab_inactive():
    bg = ext("background.js")
    assert re.search(r"tabs\.create\s*\(", bg), "no tabs.create"
    body = js_function(bg, "openRenewalTab")
    creates = [call_args(body, m.end() - 1) for m in re.finditer(r"tabs\.create\s*\(", body)]
    assert creates, "openRenewalTab() does not call tabs.create"
    assert all(re.search(r"active\s*:\s*false\b", a) for a in creates), f"renewal tab not inactive: {creates}"


# Spec 0002 AC3/AC10 — the tab is watched for a logged-in URL and closed; a one-shot alarm gives the 45 s timeout
def test_renewal_tab_watch():
    bg = ext("background.js")
    assert re.search(r"browser\.tabs\.onUpdated\.addListener\s*\(", bg), "no tabs.onUpdated listener"
    assert re.search(r"\bloggedInUrl\s*\(", bg), "background.js never calls loggedInUrl"
    assert re.search(r"tabs\.remove\s*\(", bg), "a renewed tab is never closed (no tabs.remove)"
    alarms = [call_args(bg, m.end() - 1) for m in re.finditer(r"alarms\.create\s*\(", bg)]
    one_shot = [a for a in alarms if "periodInMinutes" not in a and re.search(r"\bwhen\b|\bdelayInMinutes\b", a)]
    assert one_shot, f"no one-shot browser.alarms timer: {alarms}"
    secs45 = r"45\s*\*\s*1000|45_?000|0\.75|45\s*/\s*60"
    assert any(re.search(secs45, a) for a in one_shot) or re.search(rf"=\s*(?:{secs45})\b", bg), \
        f"renewal timeout is not 45 s: {one_shot}"


# Spec 0002 AC1/AC2/AC9 — sync() posts every source first, then renews the `login needed` ones, with state in storage.session
def test_sync_renews_after_posting():
    bg = ext("background.js")
    body = js_function(bg, "sync")
    assert "syncAll(" in body, "sync() no longer calls syncAll"
    assert re.search(r"\brenewAll\s*\(", body), "sync() does not call renewAll"
    assert body.index("syncAll(") < re.search(r"\brenewAll\s*\(", body).start(), "renewAll runs before syncAll posts"
    assert "login needed" in body, "sync() does not pick the `login needed` sources"
    assert re.search(r"storage\.session\.get\s*\(", bg), "renewal state is not read from browser.storage.session"


# Spec 0002 AC24 — avenue() finds the Crowdmark entry URL and keeps it in storage.session
def test_avenue_records_crowdmark_entry():
    bg = ext("background.js")
    assert re.search(r"\bcrowdmarkEntry\s*\(", js_function(bg, "avenue")), "avenue() does not call crowdmarkEntry"
    sets = [call_args(bg, m.end() - 1) for m in re.finditer(r"storage\.session\.set\s*\(", bg)]
    keys = [k for a in sets for k in (_object_keys(a) or [])]
    assert any("crowdmark" in k.lower() for k in keys), f"no Crowdmark entry URL key in storage.session.set: {keys}"
    # AC24: the stored Crowdmark URL is re-checked before a renewal tab opens
    assert "avenueLtiUrl(" in js_function(bg, "entry"), "entry() does not re-check the stored Crowdmark URL"


# Spec 0002 AC18 — nothing is injected into any page
def test_no_injection():
    for f in EXT.iterdir():
        text = f.read_text()
        for word in ("executeScript", "insertCSS", "registerContentScripts", "content_scripts"):
            assert word not in text, f"`{word}` in extension/{f.name}"
        assert not re.search(r"\bscripting\b", text), f"`scripting` in extension/{f.name}"


# Spec 0002 AC20 — the extension never fetches an IdP or a login entry URL itself; sign-in happens only in a tab
def test_no_idp_fetch():
    bg = ext("background.js")
    for a in fetch_calls(bg):
        for bad in ("cap.mcmaster", "microsoftonline", "avenue.mcmaster.ca", "SITES", ".login", "entry("):
            assert bad not in a, f"fetch to a sign-in URL ({bad}): {a}"


# Spec 0002 AC19/AC27 — renewal keeps no secrets: no new local/sync storage, only allowed session keys, no URL logged
def test_renewal_keeps_no_secrets():
    bg = ext("background.js")
    bodies = [js_function(bg, n) for n in ("openRenewalTab", "rerun", "sync")] + _listeners(bg)
    for b in bodies:
        for m in re.finditer(r"console\.\w+\s*\(", b):
            args = call_args(b, m.end() - 1)
            assert not re.search(r"url", args, re.I), f"console call mentions a URL: {args}"
    assert not re.search(r"storage\.(local|sync)\.set\s*\(", bg), "background.js writes browser.storage.local/sync"
    sets = [call_args(bg, m.end() - 1) for m in re.finditer(r"storage\.session\.set\s*\(", bg)]
    assert sets, "renewal state is never saved to browser.storage.session"
    for a in sets:
        keys = _object_keys(a)
        assert keys is not None, f"storage.session.set argument is not an object literal; keys cannot be checked: {a}"
        extra = [k for k in keys if k not in ("fallback", "skipped") and "crowdmark" not in k.lower()]
        assert not extra, f"storage.session.set stores other keys {extra}: {a}"


# ---- Spec 0002 Step 5: fallback glue (static; the live run is manual in Zen) --------

def _listener(src, event):
    """Argument text of `browser.<event>.addListener(...)`, plus the bodies of named functions it calls (one level)."""
    m = re.search(rf"browser\.{re.escape(event)}\.addListener\s*\(", src)
    assert m, f"no browser.{event} listener"
    args = call_args(src, m.end() - 1)
    called = set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", args)) | set(re.findall(r"^\(\s*([A-Za-z_]\w*)\s*\)$", args))
    return args + "".join(_safe_body(src, n) or "" for n in sorted(called))


# Spec 0002 AC10/AC11/AC19 — one browser notification names the sources; its text carries no URL
def test_fallback_notification():
    bg = ext("background.js")
    calls = [call_args(bg, m.end() - 1) for m in re.finditer(r"notifications\.create\s*\(", bg)]
    assert calls, "no notifications.create"
    for a in calls:
        assert re.match(r"\(\s*['\"`][\w-]+['\"`]\s*,", a), f"notification without a fixed id (one per sync, replaced): {a}"
        assert not re.search(r"url|https?:|\bSITES\b|\.login\b|\bentry\s*\(", a, re.I), f"notification text may carry a URL: {a}"


# Spec 0002 AC12/AC14 — only a notification click focuses the fallback tab (or opens Avenue's entry for Crowdmark)
def test_notification_focuses_tab():
    bg = ext("background.js")
    body = _listener(bg, "notifications.onClicked")
    upd = [call_args(body, m.end() - 1) for m in re.finditer(r"tabs\.update\s*\(", body)]
    assert any(re.search(r"active\s*:\s*true\b", a) for a in upd), f"click does not make the fallback tab active: {upd}"
    wins = [call_args(body, m.end() - 1) for m in re.finditer(r"windows\.update\s*\(", body)]
    assert any(re.search(r"focused\s*:\s*true\b", a) for a in wins), "click does not focus the fallback tab's window"
    creates = [call_args(body, m.end() - 1) for m in re.finditer(r"tabs\.create\s*\(", body)]
    assert any(re.search(r"active\s*:\s*true\b", a) for a in creates), \
        "click with no Crowdmark fallback tab does not open an active tab"
    assert re.search(r"avenue", body), "Crowdmark click target is not Avenue's entry URL"
    rest = bg
    for m in re.finditer(r"browser\.notifications\.onClicked\.addListener\s*\(", bg):
        rest = rest.replace(call_args(bg, m.end() - 1), "")
    for n in set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", _listener(bg, "notifications.onClicked"))):
        b = _safe_body(bg, n)
        if b and n not in ("sync", "renewAll", "fallbackSignedIn"):
            rest = rest.replace(b, "")
    for api, flag in (("tabs.update", "active"), ("windows.update", "focused"), ("tabs.create", "active")):
        for m in re.finditer(rf"{re.escape(api)}\s*\(", rest):
            a = call_args(rest, m.end() - 1)
            assert not re.search(rf"{flag}\s*:\s*true\b", a), f"{api} with {flag}: true outside the notification click: {a}"


# Spec 0002 AC13/AC29/AC30 — a tracked fallback tab reaching its logged-in URL re-syncs that source by itself
def test_fallback_sign_in_resyncs():
    bg = ext("background.js")
    body = _listener(bg, "tabs.onUpdated")
    assert re.search(r"\bloggedInUrl\s*\(", body), "tabs.onUpdated does not check loggedInUrl"
    assert re.search(r"\bfallbackSignedIn\s*\(", body), "a fallback tab reaching its logged-in URL does not call fallbackSignedIn"


# Spec 0002 AC15 (+ implementer gap) — closing a fallback tab forgets it; closing a renewal tab ends that renewal
def test_fallback_tab_closed():
    bg = ext("background.js")
    body = _listener(bg, "tabs.onRemoved")
    assert re.search(r"\bdelete\b[^;\n]*fallback|fallback[^;\n]*\bdelete\b", body), \
        "a closed fallback tab is not removed from state.fallback"
    assert re.search(r"storage\.session\.set\s*\(|\bsave\w*\s*\(", body), "the removed fallback entry is not saved"
    assert re.search(r"renew", body, re.I), "a closed renewal tab does not end its renewal"


# ---- Spec 0002 Step 6: index.html sign-in link (static; the click is checked manually) ----

def _site_login(source):
    m = re.search(rf"\b{source}\s*:\s*\{{[^}}]*?\blogin\s*:\s*'([^']+)'", ext("parse.js"))
    assert m, f"no SITES.{source}.login in parse.js"
    return m.group(1)


# Spec 0002 AC16 — the `login needed` banner links to the source's login entry URL (Crowdmark -> Avenue's)
def test_index_login_links():
    html = (ROOT / "index.html").read_text()
    urls = {s: _site_login(s) for s in ("avenue", "childsmath")}
    for url in urls.values():
        assert url in html, f"{url} not in index.html"
    m = re.search(r"const\s+LOGIN\s*=\s*\{([^}]*)\}", html)
    assert m, "no LOGIN map in index.html"
    vals = dict(re.findall(r"\b(avenue|crowdmark|childsmath)\s*:\s*([^,\n}]+)", m.group(1)))
    assert set(vals) == {"avenue", "crowdmark", "childsmath"}, f"LOGIN keys: {sorted(vals)}"
    lit = lambda v: v.strip().strip("'\"`")
    assert lit(vals["avenue"]) == urls["avenue"], f"LOGIN.avenue {vals['avenue']}"
    assert lit(vals["childsmath"]) == urls["childsmath"], f"LOGIN.childsmath {vals['childsmath']}"
    assert vals["crowdmark"].strip() == vals["avenue"].strip() or lit(vals["crowdmark"]) == urls["avenue"], \
        f"LOGIN.crowdmark is not Avenue's entry URL: {vals['crowdmark']}"
    near = [html[max(0, i.start() - 600):i.start() + 600] for i in re.finditer(r"login needed", html)]
    link = r"(?:\$\(\s*['\"]a['\"]|createElement\(\s*['\"]a['\"])[^;]*?href\s*[:=]\s*LOGIN\[|\.href\s*=\s*LOGIN\["
    assert any(re.search(link, w, re.S) for w in near), "the login-needed status builds no <a> with href from LOGIN"
    assert 'id="banners"' not in html, "top banners are back (developer removed them 2026-10-18)"
    assert any(re.search(r"noopener", w) for w in near), "sign-in link opened without rel=noopener"
    assert "innerHTML" not in html, "index.html uses innerHTML"


# Live 0.1.3 bug — a renewal/fallback tab counts as signed in only once its logged-in page has finished loading
def test_renewal_waits_for_load():
    bg = ext("background.js")
    body = _listener(bg, "tabs.onUpdated")
    complete = re.search(r"status\s*[!=]==?\s*['\"]complete['\"]", body)
    assert complete, "tabs.onUpdated does not wait for status 'complete'"
    for call in ("endRenewal(", "fallbackSignedIn("):
        i = body.find(call)
        assert i > complete.start(), f"{call} reachable before the status 'complete' check"
    assert not re.search(r"\bloggedInUrl\s*\([^)]*change\w*\.url", body), \
        "loggedInUrl checks the changed URL, not the loaded tab's URL"
    assert re.search(r"\bloggedInUrl\s*\([^)]*\btab\.url", body), "loggedInUrl does not check tab.url"


# Live bug — a sync the server rejects (400) replaces the stale status with a status-only error, once
def test_rejected_sync_surfaces():
    bg = ext("background.js")
    assert re.search(r"status\s*===?\s*400", bg), "a 400 from /api/ingest is not handled"
    assert "server rejected sync" in bg, "no status-only error ingest for a rejected sync"
    assert re.search(r"\brejectReason\s*\(", bg), "the reason is not taken from the server's message"
    for name in ("sync", "rerun"):
        assert not re.search(r"post\(\s*['\"]/api/ingest", js_function(bg, name)), \
            f"{name}() posts ingest without the rejected-sync handling"



# ---- Manual items (AC105-AC110, developer-requested) ------------------------------

def page_post(s, path, body, ctype="application/json", headers=None):
    """What the page does: no X-Token. body is JSON-encoded unless it is already bytes."""
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(s.url + path, data, {"Content-Type": ctype, **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, None, e.headers


def add_ok(s, body):
    code, out, _ = page_post(s, "/api/items", body)
    assert code == 201, f"POST /api/items -> {code}"
    return out["id"]


# AC105 — POST /api/items creates a manual item; given fields are user overrides, kind derived from the title
def test_manual_add():
    with InProcess() as s:
        due = "2026-10-20T23:59:00.000Z"
        rid = add_ok(s, {"course": "math 1za3 fall", "title": "Quiz 3", "due": due, "weight": 5, "mark": 4, "out_of": 5})
        rid2 = add_ok(s, {"course": "MATH 1ZA3", "title": "Lab report", "kind": "assignment"})
        items = {i["id"]: i for i in get_items(s)}
        r, r2 = items[rid], items[rid2]
        assert (r["source"], r["course"], r["title"], r["kind"], r["due"], r["weight"], r["mark"], r["out_of"]) == (
            "manual", "MATH 1ZA3", "Quiz 3", "quiz", due, 5, 4, 5), r
        assert re.fullmatch(r"[0-9a-f]{32}", r["source_id"]) and r["source_id"] != r2["source_id"], (r, r2)
        o = overrides_of(r)
        assert set(o) == {"due", "weight", "mark", "out_of"}, o  # title and kind are plain columns
        assert o["mark"]["value"] == 4 and datetime.fromisoformat(o["mark"]["at"]), o
        assert r2["kind"] == "assignment" and set(overrides_of(r2)) == set(), r2
        assert not r["missing"], r


# AC106 — bad bodies are refused and write nothing
def test_manual_add_rejects():
    with InProcess() as s:
        add_ok(s, {"course": "MATH 1ZA3", "title": "Quiz 1"})  # control
        before = db_dump(s)
        for bad in ({"title": "Quiz 2"}, {"course": "MATH 1ZA3"}, {"course": "", "title": "Quiz 2"},
                    {"course": "MATH 1ZA3", "title": "  "}, {"course": "MATH 1ZA3", "title": "Quiz 2", "mark": "5"},
                    {"course": "MATH 1ZA3", "title": 7}, {"course": "MATH 1ZA3", "title": "Quiz 2", "source": "avenue"},
                    ["MATH 1ZA3", "Quiz 2"]):
            code, _, _ = page_post(s, "/api/items", bad)
            assert code == 400, (bad, code)
        code, _, _ = page_post(s, "/api/items", {"course": "MATH 1ZA3", "title": "Quiz 2"}, ctype="text/plain")
        assert code == 415, code
        assert page_post(s, "/api/items", b"{not json")[0] == 400
        assert db_dump(s) == before, "DB changed on a bad add"


# AC107 — same guard as overrides: no CORS preflight or Access-Control-* headers, Host allowlist
def test_manual_routes_guard():
    with InProcess() as s:
        rid = add_ok(s, {"course": "MATH 1ZA3", "title": "Quiz 1"})
        for path in ("/api/items", f"/api/items/{rid}/delete"):
            code, h = raw_request(s, "OPTIONS", path, {"Origin": EXT_ORIGIN, **PREFLIGHT})
            assert code >= 400 and not acao_headers(h), (path, code, dict(h))
        code, _, h = page_post(s, "/api/items", {"course": "MATH 1ZA3", "title": "Quiz 2"}, headers={"Origin": EXT_ORIGIN})
        assert code == 201 and not acao_headers(h), (code, dict(h))
        before = db_dump(s)
        for path in ("/api/items", f"/api/items/{rid}/delete"):
            code, _, _ = page_post(s, path, {"course": "MATH 1ZA3", "title": "Quiz 3"}, headers={"Host": "evil.example"})
            assert code == 403, (path, code)
        assert db_dump(s) == before


# AC108 — only manual items can be deleted
def test_manual_delete():
    with InProcess() as s:
        ingest_ok(s, "avenue", [item("1")])
        av = get_items(s)[0]["id"]
        rid = add_ok(s, {"course": "CHEM 1A03", "title": "Quiz 1"})
        before = db_dump(s)
        assert page_post(s, f"/api/items/{rid}/delete", {}, ctype="text/plain")[0] == 415
        assert page_post(s, f"/api/items/{av}/delete", {})[0] == 400
        assert page_post(s, "/api/items/999/delete", {})[0] == 404
        assert db_dump(s) == before, "DB changed on a refused delete"
        assert page_post(s, f"/api/items/{rid}/delete", {})[0] == 200
        assert [i["id"] for i in get_items(s)] == [av]


# AC109 — a manual item merges into a synced item (synced row kept); its overrides win, also after a re-sync
def test_manual_merges():
    with InProcess() as s:
        ingest_ok(s, "avenue", [AV])
        due = "2026-10-21T00:00:00.000Z"
        add_ok(s, {"course": "MATH 1ZA3", "title": "Assignment 2", "kind": "quiz", "due": due,
                   "mark": 9, "out_of": 10, "weight": 3})
        ingest_ok(s, "avenue", [dict(AV, mark=5, out_of=20)])
        rows = in_course(get_items(s), "MATH 1ZA3")
        assert len(rows) == 1, rows
        r = rows[0]
        assert (r["source"], r["source_id"], r["title"], r["kind"]) == ("avenue", "dropbox:1", AV["title"], "assignment"), r
        assert (r["due"], r["weight"], r["mark"], r["out_of"]) == (due, 3, 9, 10), r
        assert set(overrides_of(r)) == {"due", "weight", "mark", "out_of"}, overrides_of(r)


# AC110 — syncs never flag or modify manual items; an ingest cannot claim the `manual` source
def test_manual_untouched_by_sync():
    with InProcess() as s:
        rid = add_ok(s, {"course": "MATH 1ZA3", "title": "Quiz 9", "mark": 1, "out_of": 2})
        ingest_ok(s, "avenue", [item("1")])
        ingest_ok(s, "avenue", [])
        before = db_dump(s)
        sid = {i["id"]: i for i in get_items(s)}[rid]["source_id"]
        assert ingest(s, "manual", [{"source_id": sid, "title": "x", "mark": 0}]) == 400
        assert ingest(s, "manual", [], status="ok") == 400
        assert db_dump(s) == before
        r = {i["id"]: i for i in get_items(s)}[rid]
        assert not r["missing"] and (r["title"], r["mark"]) == ("Quiz 9", 1), r


# AC111 — the page has an add form and a delete button for manual rows only, both JSON POSTs
def test_index_manual_items():
    html = (ROOT / "index.html").read_text()
    calls = fetch_calls(html)
    adds = [a for a in calls if re.match(r"\(\s*['\"`]/api/items['\"`]\s*,", a)]
    assert adds, "no add POST to /api/items"
    dels = [a for a in calls if re.match(r"\(\s*`/api/items/\$\{item\.id\}/delete`", a)]
    assert dels, "no delete POST to /api/items/<id>/delete"
    for a in adds + dels:
        assert re.search(r"method\s*:\s*['\"]POST['\"]", a) and "application/json" in a, a[:160]
    assert re.search(r"source\s*===\s*['\"]manual['\"]", html), "delete not limited to manual rows"
    assert "Add item" in html and 'type="date"' in html and 'type="time"' in html and "innerHTML" not in html


# Quick (view/edit mode) — view mode by default, remembered in localStorage only; the add form and
# the row editors appear only in edit mode
def test_index_view_edit_mode():
    html = (ROOT / "index.html").read_text()
    assert re.search(r"localStorage\.getItem\(\s*['\"]editMode['\"]\s*\)", html), "edit mode not read from localStorage"
    assert re.search(r"localStorage\.setItem\(\s*['\"]editMode['\"]", html), "edit mode not saved to localStorage"
    assert not any("editMode" in a for a in fetch_calls(html)), "edit mode sent to the server"
    assert re.search(r"<button id=['\"]edit['\"]", html) and "'View' : 'Edit'" in html, "no Edit/View toggle button"
    assert html.index('id="edit"') > html.index('id="sources"'), "Edit button is not at the bottom"
    assert re.search(r"getElementById\(\s*['\"]add['\"]\s*\)\.hidden\s*=", html), "add form not hidden in view mode"
    row = re.search(r"function row\(item\)\s*\{(.*?)\n\}", html, re.S)[1]
    assert re.search(r"if\s*\(\s*!editing\(\)\s*\)", row), "row() has no plain view branch"


def test_index_calendar():
    html = (ROOT / "index.html").read_text()
    for id_ in ("calendar", "cal-back", "cal-fwd", "cal-today"):
        assert re.search(rf"id=['\"]{id_}['\"]", html), f"no #{id_}"
    cal = re.search(r"function calendar\(.*?\n\}", html, re.S)[0]
    assert "length: CAL_DAYS" in cal and "const CAL_DAYS = 14;" in html, "calendar is not 14 days"
    assert re.search(r"calOffset\s*-=\s*STEP", html) and re.search(r"calOffset\s*\+=\s*STEP", html) and "const STEP = 7;" in html, \
        "arrows do not move by a week"
    assert re.search(r"calOffset\s*=\s*0\b", html), "Today button does not reset the calendar"
    assert "onmouseenter: () => hl(true)" in html and "tr.dataset.day = day;" in html, "calendar hover does not highlight rows"
    assert "'Source'" not in html and "title: `From ${item.source}" in html, "source is not a row tooltip in edit mode"


def test_index_hide_courses():
    html = (ROOT / "index.html").read_text()
    assert re.search(r"localStorage\.getItem\(\s*['\"]hiddenCourses['\"]", html), "hidden courses not read from localStorage"
    assert re.search(r"localStorage\.setItem\(\s*['\"]hiddenCourses['\"]", html), "hidden courses not saved to localStorage"
    assert not any("hiddenCourses" in a for a in fetch_calls(html)), "hidden courses sent to the server"
    assert re.search(r"calendar\(data\.items\.filter\(\(i\) => visible\.includes\(", html), "calendar does not skip hidden courses"
    assert re.search(r"courses = editing\(\) \? all : visible", html), "view mode does not skip hidden courses"
    assert re.search(r"if \(!editing\(\)\) return b;", html), "eye button not limited to edit mode"
    assert re.search(r"all\.indexOf\(c\)", html), "course colours shift when a course is hidden"


def test_index_single_list():
    html = (ROOT / "index.html").read_text()
    for id_ in ("courses", "kinds", "statuses"):
        assert re.search(rf"<div id=['\"]{id_}['\"] class=['\"]chips['\"]", html), f"no #{id_} button row"
    assert re.search(r"\['', \.\.\.values\]\.map", html) and "v ? label(v) : 'All'" in html, "button rows do not start with All"
    assert "const shownKind = (i) => editing() || kindOf(i) !== 'other';" in html and "chips(typeButtons()," in html \
        and "shownKind(i) && !i.done" in html, "other items or their button show outside edit mode"
    assert re.search(r"const keep = \(i\) => shownKind\(i\) && !\(hideDone && i\.done\) && \(!kind \|\| kindOf\(i\) === kind\)", html), \
        "Hide done or Type filter not applied"
    assert "textContent: 'Hide done'" in html and "STATES" not in html, "status is not the single Hide done toggle"
    assert "tr.overdue { background: var(--overdue-bg); box-shadow: inset 3px 0 var(--overdue-text); }" in html, "overdue rows not red"
    assert ".chips { display: grid; grid-template-columns: 1fr;" in html, "filter groups are not single columns"
    head = re.search(r"textContent: '✓', title: 'Done'.*?\['Type', 'Course', 'Title', 'Due', 'Time left', 'Weight', 'Mark',", html, re.S)
    assert head, "list columns are not Done | Type | Course | Title | Due | Time left | Weight | Mark"
    assert "upcoming }" not in html and "Upcoming'" not in html, "Upcoming still offered as a status"
    assert html.index('id="calendar"') < html.index('id="courses"'), "calendar is not above the filters"
    assert re.search(r'<div id="layout">\s*<main>.*?</main>\s*<aside id="filters">', html, re.S), "filters are not a sidebar right of the list"
    assert re.search(r'<div id="cal-wrap">.*?id="calendar".*?<div id="layout">\s*<main>\s*<p id="summary"', html, re.S), \
        "top bar is not full width above the list and sidebar"
    assert "#filters { position: sticky; top: var(--head-top, 0);" in html, "sidebar does not stick below the calendar"
    assert "#cal-wrap { position: sticky;" in html, "calendar does not stay in view"
    assert "margin: 0 1.5rem 1.5rem;" in html and "#top { display: flex; gap: .8rem; align-items: flex-start; padding-top: 1rem; }" in html, \
        "page top margin makes the top bar jump when it pins"
    assert "grid-template-columns: repeat(7, var(--day)); grid-auto-rows: var(--day);" in html and "--day: 8.5rem;" in html, \
        "calendar days are not fixed squares"
    assert ".next { height: calc(2 * var(--day) + 1px);" in html and "#next-test { flex: 1;" in html, \
        "Next test box does not fill the rest of the top bar at the calendar's height"
    assert "dateTime(s.synced_at)" in html and "alignDots(document.getElementById('sources'))" in html, \
        "Last synced is not in the due-date style"
    assert re.search(r"\.items thead th \{ position: sticky; top: var\(--head-top", html), "column titles do not follow scrolling"
    assert 'id="windows"' not in html and "upcomingDays" not in html, "window buttons are back"
    top = re.search(r'<div id="top">(.*?)<div id="layout">', html, re.S)[1]
    assert top.index('id="calendar"') < top.index('id="cal-nav"') < top.index('id="next-test"'), "arrows are not right of the calendar"
    assert re.search(r'id="cal-back".*?id="cal-today".*?id="cal-fwd"', top, re.S) and "flex-direction: column; gap: .3rem; } /* up:" in html, \
        "arrows are not up / today / down in a column"
    assert "from.getDay() === 0 || from.getDay() === 6" in html and ".day.weekend { background: var(--weekend-bg); }" in html, \
        "weekend days not grey"
    assert ".sort((a, b) => !!a.done - !!b.done || dueMs(a) - dueMs(b))" in html, "done items not listed after open ones"
    assert re.search(r"<th>Website</th><th>Status</th><th>Last synced</th>", html), "no sync status table"
    assert html.index('id="sources"') > html.index("</main>"), "sync table is not below the item list"
    assert "typeButtons(), kind" in html and "TYPE_BUTTONS = KINDS.filter((k) => !['exam', 'midterm', 'homework'].includes(k))" in html, \
        "exam, midterm or homework still have type buttons"
    assert "const kindOf = (i) => (i.kind === 'exam' ? 'test' : i.kind);" in html, "exams are not shown as tests"
    assert re.search(r"const KINDS = \[[^\]]*'midterm'[^\]]*'homework'", html), "midterm or homework dropped from KINDS"
    assert ".items { table-layout: fixed;" in html, "item list columns not locked"
    assert html.index('id="tests"') < html.index('id="labs"') < html.index('id="items"'), "tables are not Tests, Labs, Coursework"
    assert "['tests', shown.filter((i) => TESTS.includes(i.kind))], ['labs', shown.filter(isLab)]" in html \
        and "['items', shown.filter((i) => !TESTS.includes(i.kind) && !isLab(i))]" in html, "tests and labs are not split out"
    assert re.search(r'<div id="pair">\s*<div id="tests-part">.*?<div id="labs-part">.*?</div>\s*</div>\s*<div id="work-part">', html, re.S) \
        and "#pair { display: flex;" in html, "tests and labs are not side by side"
    assert "const typed = id === 'items';" in html and "if (!typed) tr.children[1].remove();" in html, \
        "tests or labs table still has a Type column"
    assert "Progress" not in html and "<section" not in html, "progress column or per-course sections remain"
    assert "round2(item.weight), markText(item)" in html and "round2(i.mark)" in html, "weight and mark not rounded in view mode"


def test_index_due_format():
    html = (ROOT / "index.html").read_text()
    js = "\n".join(re.search(rf"^const {n} = .*?;$", html, re.M)[0] for n in ("MONTHS", "pad"))
    js += "\n" + re.search(r"^const dueText = .*?^\};$", html, re.M | re.S)[0]
    js += """
const now = new Date(2026, 9, 18);
const out = [dueText(null, now), dueText(new Date(2026, 9, 20, 9, 5).toISOString(), now),
  dueText(new Date(2027, 0, 3, 23, 59).toISOString(), now)];
console.log(JSON.stringify(out));"""
    got = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    assert got == [[], ["20 Oct", "09:05"], ["03 Jan 2027", "23:59"]], got
    assert "grid-template-columns: var(--date-w, max-content)" in html and "div.due {" in html and "--date-w" in html, "due dots not aligned"


def test_index_single_save():
    html = (ROOT / "index.html").read_text()
    row = re.search(r"function row\(item\)\s*\{(.*?)\n\}", html, re.S)[1]
    assert "textContent: 'Save'" not in row and "textContent: 'Delete'" not in row, "per-row Save or Delete button left"
    assert re.search(r'<aside id="filters">\s*<button id="save">', html), "no single Save button at the top of the sidebar"
    assert re.search(r"for \(const \[id, p\] of pending\)", html), "Save does not send every pending row"
    assert "icon(TRASH)" in row, "delete is not a trash icon"
    assert re.search(r'<aside id="filters">.*?<form id="add">.*?</aside>', html, re.S), "add-item form is not in the sidebar"
    assert re.search(r'<select name="course" required', html), "add-item course is not a select"
    assert "appearance: textfield" in html, "number inputs still show increment buttons"
    assert "#layout.editing #filters" not in html, "edit mode moves the filters out of the sidebar"
    assert ".chips button.course { border-left: 4px solid var(--c); }" in html and "var(--c, var(--text-faint))" not in html, \
        "grey edge on non-course buttons"
    assert "#add[hidden] { display: none; }" in html, "add form's display rule overrides hidden in view mode"


def test_index_next_blocks_and_row_hover():
    html = (ROOT / "index.html").read_text()
    top = re.search(r'<div id="top">(.*?)<div id="layout">', html, re.S)[1]
    assert top.index('id="calendar"') < top.index('id="next-test"') and 'id="next-up"' not in html, \
        "top bar is not the calendar then Next test, or Next up is back"
    assert "ahead.find((i) => TESTS.includes(i.kind))" in html, "Next test picks the wrong item"
    assert "?? 'TBD'}`" in html, "a test with no known room does not say TBD"
    assert "picked ? 'Selected test' : 'Next test'" in html and "tr.addEventListener('mouseenter', () => showTest(i));" in html \
        and "tr.addEventListener('mouseleave', () => showTest(null));" in html, "hovering a test does not show it in the box"
    assert "container-type: inline-size;" in html and ".next .title { font-size: clamp(" in html, "Next test text does not scale with its box"
    assert "id === 'tests' && h === 'Due' ? 'Time' : h" in html, "tests table still says Due"
    assert "querySelector(`#calendar .day[data-day=\"${day}\"]`)" in html and "cell.dataset.day = day;" in html, \
        "row hover does not highlight its calendar day"
    js = re.search(r"^const roomOf = .*?^\};$", html, re.M | re.S)[0] + """
const info = [{ name: 'Lab Room', value: 'ABB 126' }, { name: 'Test 1 Room', value: 'PGCLL B131' }, { name: 'Test 10 Room', value: 'X' }];
console.log(JSON.stringify([roomOf('Test 1', info), roomOf('Test 2', info), roomOf('Test 10', info), roomOf('Exam', [])]));"""
    got = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    assert got == ["PGCLL B131", None, "X", None], got


def test_index_grades_links_empty_unload_filters():
    html = (ROOT / "index.html").read_text()
    assert html.index('id="items"') < html.index('id="grades"') < html.index('id="sources"'), "grades table is not after the list"
    assert "<th>Course</th><th>Grade</th><th>Graded</th>" in html, "no Course | Grade | Graded table"
    assert "/^https?:\\/\\//i.test(item.url ?? '')" in html and "href: item.url" in html, "titles do not link to http(s) urls only"
    assert "No items match these filters." in html and "'Clear filters'" in html, "no empty state"
    assert "addEventListener('beforeunload'" in html and "if (pending.size) e.preventDefault()" in html, "no unsaved-edits warning"
    assert "localStorage.getItem('filters')" in html and "localStorage.setItem('filters'" in html, "filters not remembered"
    assert not any("filters" in a for a in fetch_calls(html)), "filters sent to the server"


def test_index_time_left_refresh():
    html = (ROOT / "index.html").read_text()
    body = re.search(r"^function tick\(\) \{.*?^\}$", html, re.M | re.S)[0]
    assert "tr.querySelector('.left').textContent = left(i)" in body and "setTimeout(tick," in body, "time left cells are not refreshed"
    assert "render()" not in body and "setInterval" not in html, "refresh rebuilds the page or polls"
    # the wait is the time until the next displayed change: hour steps over a day, minute steps under it
    js = """
const dueMs = (i) => i.due; let timed, tickTimer; const overdue = () => false, upcoming = () => false, left = () => '';
let waited; const setTimeout = (f, ms) => { waited = ms - 50; }; const clearTimeout = () => {};
const Date = { now: () => 0 };
const row = () => ({ querySelector: () => ({}), classList: { toggle() {} } });
""" + body + """
const out = [];
for (const due of [2 * 864e5 + 36e5 * 4 + 6e4 * 30, 36e5 * 3 + 6e4 * 20 + 1e3 * 15, -(36e5 + 6e4 * 10)]) {
  timed = [[row(), { due }]]; tick(); out.push(waited);
}
console.log(JSON.stringify(out));"""
    got = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    assert got == [6e4 * 30, 1e3 * 15, 6e4], got


if __name__ == "__main__":
    failed = 0
    for name, fn in [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            if "-v" in sys.argv:
                traceback.print_exc()
    sys.exit(1 if failed else 0)
