"""번역 기록 DB (SQLite). 스키마는 docs/ARCHITECTURE.md 참고."""

import json
import sqlite3
import time

SCHEMA_VERSION = 1

PENDING = "pending"
DONE = "done"
REVIEW = "review"
UNUSED = "unused"  # 표시용: entries.active = 0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    folder_uri TEXT NOT NULL UNIQUE,
    language TEXT NOT NULL DEFAULT 'korean',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_scan_at REAL
);

CREATE TABLE IF NOT EXISTS texts (
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    src_hash TEXT NOT NULL,
    source TEXT NOT NULL,
    translation TEXT,
    engine TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (game_id, src_hash)
);
CREATE INDEX IF NOT EXISTS texts_status ON texts(game_id, status);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    ident TEXT NOT NULL,
    src_hash TEXT NOT NULL,
    speaker TEXT,
    file TEXT NOT NULL,
    line INTEGER,
    label TEXT,
    seq INTEGER,
    tmpl TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    first_scan INTEGER,
    last_scan INTEGER,
    UNIQUE (game_id, kind, ident)
);
CREATE INDEX IF NOT EXISTS entries_hash ON entries(game_id, src_hash);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    old_ident TEXT, new_ident TEXT,
    old_hash TEXT, new_hash TEXT,
    old_source TEXT, new_source TEXT,
    old_translation TEXT,
    changed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    at REAL NOT NULL,
    added INTEGER, changed INTEGER, removed INTEGER, unchanged INTEGER, reactivated INTEGER
);
"""


def connect(path):
    con = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit, 트랜잭션은 명시
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")   # 서비스 쓰기 중 UI 읽기 가능, 강제종료에도 안전
    con.execute("PRAGMA synchronous = NORMAL")
    con.executescript(_SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    return con


class tx:
    """with tx(con): ...  — BEGIN IMMEDIATE / COMMIT / ROLLBACK"""

    def __init__(self, con):
        self.con = con

    def __enter__(self):
        self.con.execute("BEGIN IMMEDIATE")
        return self.con

    def __exit__(self, et, ev, tb):
        self.con.execute("COMMIT" if et is None else "ROLLBACK")
        return False


def now():
    return time.time()


# ---------------------------------------------------------------- games

def add_game(con, name, folder_uri, language="korean"):
    t = now()
    row = con.execute("SELECT id FROM games WHERE folder_uri = ?", (folder_uri,)).fetchone()
    if row:
        return row["id"]
    cur = con.execute(
        "INSERT INTO games(name, folder_uri, language, created_at, updated_at) VALUES (?,?,?,?,?)",
        (name, folder_uri, language, t, t))
    return cur.lastrowid


def list_games(con):
    return [dict(r) for r in con.execute("SELECT * FROM games ORDER BY name")]


def get_game(con, game_id):
    r = con.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    return dict(r) if r else None


def set_language(con, game_id, language):
    con.execute("UPDATE games SET language = ?, updated_at = ? WHERE id = ?",
                (language, now(), game_id))


def delete_game(con, game_id):
    con.execute("DELETE FROM games WHERE id = ?", (game_id,))


# ---------------------------------------------------------------- 현황

def stats(con, game_id):
    """화면 표시용 줄 수 (활성 항목 기준 + 미사용)."""
    rows = con.execute("""
        SELECT e.active AS active, t.status AS status, COUNT(*) AS n
        FROM entries e JOIN texts t ON t.game_id = e.game_id AND t.src_hash = e.src_hash
        WHERE e.game_id = ? GROUP BY e.active, t.status""", (game_id,)).fetchall()
    s = {"total": 0, DONE: 0, PENDING: 0, REVIEW: 0, UNUSED: 0}
    for r in rows:
        if not r["active"]:
            s[UNUSED] += r["n"]
            continue
        s["total"] += r["n"]
        s[r["status"]] = s.get(r["status"], 0) + r["n"]
    s["remaining"] = s[PENDING]
    s["progress"] = (s[DONE] / s["total"]) if s["total"] else 0.0
    return s


def pending_texts(con, game_id, limit, statuses=(PENDING,)):
    """번역 대기 원문 (활성 항목에서 쓰이는 것만). 파일/줄 순서로 꺼내서 문맥이 이어지게."""
    q = """
        SELECT t.src_hash, t.source, MIN(e.file || ':' || printf('%%08d', e.line)) AS pos
        FROM texts t JOIN entries e ON e.game_id = t.game_id AND e.src_hash = t.src_hash
        WHERE t.game_id = ? AND e.active = 1 AND t.status IN (%s)
        GROUP BY t.src_hash ORDER BY pos LIMIT ?""" % ",".join("?" * len(statuses))
    return [dict(r) for r in con.execute(q, (game_id, *statuses, limit))]


def pending_all(con, game_id, statuses=(PENDING,)):
    """번역 대기 원문 전부를 파일/줄 순서로 한 번에 읽는다 (배치마다 다시 조회하지 않기 위함)."""
    q = """
        SELECT t.src_hash, t.source, MIN(e.file || ':' || printf('%%08d', e.line)) AS pos
        FROM texts t JOIN entries e ON e.game_id = t.game_id AND e.src_hash = t.src_hash
        WHERE t.game_id = ? AND e.active = 1 AND t.status IN (%s)
        GROUP BY t.src_hash ORDER BY pos""" % ",".join("?" * len(statuses))
    return [dict(r) for r in con.execute(q, (game_id, *statuses))]


def set_translation(con, game_id, src_hash, translation, engine, status=DONE, note=None):
    con.execute("""UPDATE texts SET translation = ?, engine = ?, status = ?, note = ?, updated_at = ?
                   WHERE game_id = ? AND src_hash = ?""",
                (translation, engine, status, note, now(), game_id, src_hash))


def reset_status(con, game_id, from_status, to_status=PENDING):
    con.execute("UPDATE texts SET status = ?, updated_at = ? WHERE game_id = ? AND status = ?",
                (to_status, now(), game_id, from_status))


def revalidate(con, game_id, check):
    """완료(done) 번역 중 check(source, translation) 이 문제 사유를 돌려주는 것을 검수필요로 돌린다.
    반환: 바뀐 줄 수. 이미 저장된 번역을 다시 번역하지 않고 안전하게 걸러내는 용도."""
    bad = []
    for r in con.execute("""SELECT src_hash, source, translation FROM texts
                            WHERE game_id = ? AND status = 'done' AND translation IS NOT NULL""", (game_id,)):
        if r["translation"] == r["source"]:
            continue
        why = check(r["source"], r["translation"])
        if why:
            bad.append((why, r["src_hash"]))
    if bad:
        with tx(con):
            for why, h in bad:
                con.execute("""UPDATE texts SET status = 'review', note = ?, updated_at = ?
                               WHERE game_id = ? AND src_hash = ?""", (why, now(), game_id, h))
    return len(bad)


def entries_for_output(con, game_id):
    return [dict(r) for r in con.execute("""
        SELECT e.kind, e.ident, e.file, e.line, e.seq, e.tmpl, e.speaker,
               t.source, t.translation, t.status
        FROM entries e JOIN texts t ON t.game_id = e.game_id AND t.src_hash = e.src_hash
        WHERE e.game_id = ? AND e.active = 1
        ORDER BY e.file, e.seq""", (game_id,))]


def history(con, game_id, limit=200):
    return [dict(r) for r in con.execute(
        "SELECT * FROM history WHERE game_id = ? ORDER BY id DESC LIMIT ?", (game_id, limit))]


def review_list(con, game_id, limit=500):
    return [dict(r) for r in con.execute("""
        SELECT t.src_hash, t.source, t.translation, t.note FROM texts t
        WHERE t.game_id = ? AND t.status = 'review' LIMIT ?""", (game_id, limit))]


def dumps(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))
