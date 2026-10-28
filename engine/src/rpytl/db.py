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

-- 캐릭터(화자)별 분석 결과. name '__story__' 는 작품 전체 요약. auto=1 이면 모델이 쓴 것, 0 이면 사용자가 고친 것.
CREATE TABLE IF NOT EXISTS profiles (
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    profile TEXT NOT NULL,
    auto INTEGER NOT NULL DEFAULT 1,
    lines INTEGER,
    updated_at REAL NOT NULL,
    PRIMARY KEY (game_id, name)
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
        SELECT t.src_hash, t.source, MIN(e.file || ':' || printf('%%08d', e.line)) AS pos,
               e.kind AS kind, e.speaker AS speaker, e.file AS file, e.seq AS seq
        FROM texts t JOIN entries e ON e.game_id = t.game_id AND e.src_hash = t.src_hash
        WHERE t.game_id = ? AND e.active = 1 AND t.status IN (%s)
        GROUP BY t.src_hash ORDER BY pos""" % ",".join("?" * len(statuses))
    # MIN() 집계와 함께 쓴 일반 열(kind/speaker/file/seq)은 SQLite 가 최소값을 가진 그 행의 값으로 채운다
    return [dict(r) for r in con.execute(q, (game_id, *statuses))]


def dialogue_index(con, game_id):
    """(file, seq) → (speaker, source). 번역할 줄의 앞 대사를 문맥으로 보여 주기 위한 색인."""
    return {(r["file"], r["seq"]): (r["speaker"] or "", r["source"]) for r in con.execute(
        """SELECT e.file, e.seq, e.speaker, t.source FROM entries e
           JOIN texts t ON t.game_id = e.game_id AND t.src_hash = e.src_hash
           WHERE e.game_id = ? AND e.kind = 'dialogue' AND e.active = 1""", (game_id,))}


def set_translation(con, game_id, src_hash, translation, engine, status=DONE, note=None):
    con.execute("""UPDATE texts SET translation = ?, engine = ?, status = ?, note = ?, updated_at = ?
                   WHERE game_id = ? AND src_hash = ?""",
                (translation, engine, status, note, now(), game_id, src_hash))


def reset_status(con, game_id, from_status, to_status=PENDING):
    con.execute("UPDATE texts SET status = ?, updated_at = ? WHERE game_id = ? AND status = ?",
                (to_status, now(), game_id, from_status))


def review_summary(con, game_id, samples=3):
    """검수필요를 사유별로 묶어 개수와 예시를 돌려준다."""
    cats = {}
    for r in con.execute("""SELECT source, translation, note FROM texts
                            WHERE game_id = ? AND status = 'review' ORDER BY rowid""", (game_id,)):
        note = r["note"] or ""
        if note.startswith("placeholders mismatch"):
            name = "번역 중 태그/변수 표시가 사라지거나 바뀜"
        elif note.startswith("unknown placeholder"):
            name = "번역 중 태그/변수 표시가 바뀜"
        elif note.startswith("태그/변수 구조"):
            name = "복원 후 태그/변수가 원문과 다름 (게임이 멈출 수 있어 제외)"
        elif note.startswith("괄호"):
            name = "괄호 개수가 원문과 다름 (게임이 멈출 수 있어 제외)"
        elif "empty" in note:
            name = "번역 결과가 비어 있음"
        else:
            name = note[:40] or "기타"
        c = cats.setdefault(name, {"name": name, "count": 0, "samples": []})
        c["count"] += 1
        if len(c["samples"]) < samples:
            c["samples"].append({"source": r["source"], "translation": r["translation"], "note": note})
    return sorted(cats.values(), key=lambda c: -c["count"])


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


# ---------------------------------------------------------------- 캐릭터 분석

STORY_KEY = "__story__"


def speaker_stats(con, game_id):
    """화자별 대사 줄 수 (활성 대사만, 해석된 이름 기준). [(name, count)] 많은 순."""
    rows = con.execute("""SELECT speaker, COUNT(*) AS n FROM entries
                          WHERE game_id = ? AND kind = 'dialogue' AND active = 1
                            AND speaker IS NOT NULL AND speaker != ''
                          GROUP BY speaker ORDER BY n DESC""", (game_id,))
    return [(r["speaker"], r["n"]) for r in rows]


def speaker_lines(con, game_id, name=None):
    """화자(None 이면 전체)의 원문 대사를 파일/순서대로. [(speaker, source)]"""
    q = """SELECT e.speaker AS speaker, t.source AS source FROM entries e
           JOIN texts t ON t.game_id = e.game_id AND t.src_hash = e.src_hash
           WHERE e.game_id = ? AND e.kind = 'dialogue' AND e.active = 1"""
    args = [game_id]
    if name is not None:
        q += " AND e.speaker = ?"
        args.append(name)
    q += " ORDER BY e.file, e.seq"
    return [(r["speaker"], r["source"]) for r in con.execute(q, args)]


def get_profiles(con, game_id):
    return {r["name"]: r["profile"] for r in
            con.execute("SELECT name, profile FROM profiles WHERE game_id = ?", (game_id,))}


def list_profiles(con, game_id):
    return [dict(r) for r in con.execute(
        "SELECT name, profile, auto, lines FROM profiles WHERE game_id = ? ORDER BY lines DESC, name", (game_id,))]


def set_profile(con, game_id, name, profile, auto=0, lines=None):
    profile = (profile or "").strip()
    if not profile:
        con.execute("DELETE FROM profiles WHERE game_id = ? AND name = ?", (game_id, name))
        return
    con.execute("""INSERT INTO profiles(game_id, name, profile, auto, lines, updated_at) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(game_id, name) DO UPDATE SET profile = excluded.profile, auto = excluded.auto,
                       lines = COALESCE(excluded.lines, profiles.lines), updated_at = excluded.updated_at""",
                (game_id, name, profile, auto, lines, now()))


def reset_translations(con, game_id, engine=None):
    """번역을 지우고 대기(pending) 로 되돌린다 (원문/위치/캐릭터 분석은 그대로).

    engine 이 주어지면 그 엔진이 번역한 것만 (예: 'google'), None 이면 번역된 것 전부.
    번역할 게 없어서 그대로 둔 줄('copy')은 지우지 않는다. 검수필요(review)도 함께 되돌린다.
    반환: 되돌린 줄 수.
    """
    q = """UPDATE texts SET status = 'pending', translation = NULL, engine = NULL, note = NULL, updated_at = ?
           WHERE game_id = ? AND status IN ('done', 'review') AND COALESCE(engine, '') != 'copy'"""
    args = [now(), game_id]
    if engine:
        q += " AND engine = ?"
        args.append(engine)
    with tx(con):
        return con.execute(q, args).rowcount


def engine_counts(con, game_id):
    """엔진별 번역된 줄 수 (확인 창에 보여 주기 위함). {'google': n, 'local': n, ...}"""
    return {r["engine"] or "?": r["n"] for r in con.execute(
        """SELECT engine, COUNT(*) AS n FROM texts WHERE game_id = ? AND status IN ('done', 'review')
           AND COALESCE(engine, '') != 'copy' GROUP BY engine""", (game_id,))}
