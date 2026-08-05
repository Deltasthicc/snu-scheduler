"""SQLite persistence for plans, historical observations and job metadata.
Schema is versioned with forward migrations."""
from __future__ import annotations
import json, os, sqlite3, tempfile, time, uuid
from contextlib import contextmanager

SCHEMA_VERSION = 2
DEFAULT_DB = os.environ.get("SNU_DB", os.path.join(tempfile.gettempdir(), "snu.db"))

_MIGRATIONS = [
    # v1
    """
    CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS plans (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, payload TEXT NOT NULL,
      created_at REAL NOT NULL, updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS observations (
      id TEXT PRIMARY KEY, round TEXT, observed_at REAL NOT NULL,
      course_code TEXT NOT NULL, seats INTEGER, bidders INTEGER,
      my_bid INTEGER, clearing_price INTEGER, outcome TEXT, notes TEXT
    );
    """,
    # v2 - job history
    """
    CREATE TABLE IF NOT EXISTS job_history (
      job_id TEXT PRIMARY KEY, input_hash TEXT, state TEXT,
      created_at REAL, finished_at REAL, runtime_ms REAL,
      courses INTEGER, trials INTEGER, cache_hit INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_obs_course ON observations(course_code);
    """,
]


@contextmanager
def conn(path: str = None):
    c = sqlite3.connect(path or DEFAULT_DB)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def migrate(path: str = None) -> int:
    with conn(path) as c:
        cur = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'")
        have = cur.fetchone() is not None
        current = 0
        if have:
            row = c.execute("SELECT version FROM schema_meta").fetchone()
            current = row["version"] if row else 0
        for i, sql in enumerate(_MIGRATIONS, start=1):
            if i > current:
                c.executescript(sql)
        c.execute("DELETE FROM schema_meta")
        c.execute("INSERT INTO schema_meta(version) VALUES(?)", (SCHEMA_VERSION,))
        return SCHEMA_VERSION


# ---------------- plans ----------------
def save_plan(name: str, payload: dict, plan_id: str | None = None, path=None) -> dict:
    now = time.time()
    pid = plan_id or uuid.uuid4().hex[:12]
    with conn(path) as c:
        existing = c.execute("SELECT id FROM plans WHERE id=?", (pid,)).fetchone()
        if existing:
            c.execute("UPDATE plans SET name=?, payload=?, updated_at=? WHERE id=?",
                      (name, json.dumps(payload), now, pid))
        else:
            c.execute("INSERT INTO plans(id,name,payload,created_at,updated_at) VALUES(?,?,?,?,?)",
                      (pid, name, json.dumps(payload), now, now))
    return {"id": pid, "name": name, "created_at": now, "updated_at": now}


def list_plans(path=None) -> list[dict]:
    with conn(path) as c:
        return [{"id": r["id"], "name": r["name"], "created_at": r["created_at"],
                 "updated_at": r["updated_at"]}
                for r in c.execute("SELECT id,name,created_at,updated_at FROM plans ORDER BY updated_at DESC")]


def get_plan(pid: str, path=None) -> dict | None:
    with conn(path) as c:
        r = c.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()
        if not r:
            return None
        return {"id": r["id"], "name": r["name"], "payload": json.loads(r["payload"]),
                "created_at": r["created_at"], "updated_at": r["updated_at"]}


def delete_plan(pid: str, path=None) -> bool:
    with conn(path) as c:
        return c.execute("DELETE FROM plans WHERE id=?", (pid,)).rowcount > 0


def duplicate_plan(pid: str, new_name: str, path=None) -> dict | None:
    p = get_plan(pid, path)
    if not p:
        return None
    return save_plan(new_name, p["payload"], path=path)


# ---------------- observations ----------------
def add_observation(o: dict, path=None) -> dict:
    oid = uuid.uuid4().hex[:12]
    with conn(path) as c:
        c.execute("""INSERT INTO observations
            (id,round,observed_at,course_code,seats,bidders,my_bid,clearing_price,outcome,notes)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
                  (oid, o.get("round"), o.get("observed_at", time.time()), o["course_code"],
                   o.get("seats"), o.get("bidders"), o.get("my_bid"),
                   o.get("clearing_price"), o.get("outcome"), o.get("notes")))
    return {"id": oid, **o}


def list_observations(course_code: str | None = None, path=None) -> list[dict]:
    with conn(path) as c:
        if course_code:
            rows = c.execute("SELECT * FROM observations WHERE course_code=? ORDER BY observed_at DESC",
                             (course_code,))
        else:
            rows = c.execute("SELECT * FROM observations ORDER BY observed_at DESC")
        return [dict(r) for r in rows]


def calibration(course_code: str, path=None) -> dict:
    """Aggregate observations. Sample size is always returned so a caller cannot
    mistake two data points for a trend."""
    obs = [o for o in list_observations(course_code, path) if o.get("bidders") is not None]
    if not obs:
        return {"course_code": course_code, "n": 0,
                "note": "no observations recorded; stress defaults still apply"}
    ratios = [o["bidders"] / o["seats"] for o in obs if o.get("seats")]
    prices = [o["clearing_price"] for o in obs if o.get("clearing_price") is not None]
    return {
        "course_code": course_code, "n": len(obs),
        "mean_bidders_per_seat": round(sum(ratios) / len(ratios), 3) if ratios else None,
        "mean_clearing_price": round(sum(prices) / len(prices), 1) if prices else None,
        "note": ("Sample of %d. Too small to override stress defaults; shown for context only."
                 % len(obs)) if len(obs) < 5 else
                ("Sample of %d observations." % len(obs)),
    }


def record_job(j: dict, path=None) -> None:
    with conn(path) as c:
        c.execute("""INSERT OR REPLACE INTO job_history
            (job_id,input_hash,state,created_at,finished_at,runtime_ms,courses,trials,cache_hit)
            VALUES(?,?,?,?,?,?,?,?,?)""",
                  (j["job_id"], j.get("input_hash"), j.get("state"), j.get("created_at"),
                   time.time(), j.get("runtime_ms"), j.get("courses"), j.get("trials"),
                   1 if j.get("cache_hit") else 0))


def list_jobs(limit: int = 50, path=None) -> list[dict]:
    with conn(path) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM job_history ORDER BY created_at DESC LIMIT ?", (limit,))]
