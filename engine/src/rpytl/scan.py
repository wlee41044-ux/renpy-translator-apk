"""게임 폴더 스캔 → 항목 수집 → DB 와 비교(신규/변경/삭제) 반영."""

import difflib
import os

from . import db
from .model import DIALOGUE, STRING, src_hash
from .rpyc import RpycError, extract_from_rpyc
from .rpyparse import extract_from_rpy, tlid_config_override

SKIP_DIRS = {"tl", "saves", "cache", "__pycache__"}


def _load_key(base):
    # renpy.script.sort_script_files: 확장자 뺀 경로 이름순 (숫자 등 "A" 미만으로 시작하면 먼저)
    fn = base.rsplit(".", 1)[0]
    return (fn >= "A", fn)


def collect_items(game_dir, tlid_only_say=False, py_strings=True):
    """game_dir (게임의 game/ 폴더 사본) 아래 스크립트에서 항목 수집.

    같은 경로의 .rpy 와 .rpyc 가 있으면 .rpy 사용. 반환: (items, files, errors)
    tlid_only_say: Ren'Py 8.6+ 식별자 규칙. 스크립트가 config 를 직접 설정하면 그 값을 따른다.
    """
    sources = {}
    for root, dirs, files in os.walk(game_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            low = name.lower()
            rel = os.path.relpath(os.path.join(root, name), game_dir).replace(os.sep, "/")
            if low.endswith((".rpy", ".rpym")):
                sources[rel] = rel
            elif low.endswith((".rpyc", ".rpymc")):
                base = rel[:-1]
                sources.setdefault(base, rel)
    texts = {}
    for base, rel in sources.items():
        if not rel.lower().endswith("c"):
            with open(os.path.join(game_dir, rel), "rb") as f:
                texts[rel] = f.read().decode("utf-8", "replace")
            ov = tlid_config_override(texts[rel])
            if ov is not None:
                tlid_only_say = ov
    items, errors, used = [], [], []
    global_ids = set()
    for base in sorted(sources, key=_load_key):
        rel = sources[base]
        path = os.path.join(game_dir, rel)
        try:
            if rel in texts:
                got = extract_from_rpy(texts[rel], base, tlid_only_say, global_ids, py_strings)
            else:
                with open(path, "rb") as f:
                    got = extract_from_rpyc(f.read(), base, global_ids, py_strings)
        except (RpycError, RecursionError, ValueError) as e:
            errors.append("%s: %s" % (rel, e))
            continue
        except Exception as e:  # 파일 하나 때문에 전체 스캔을 멈추지 않는다
            errors.append("%s: %s: %s" % (rel, type(e).__name__, e))
            continue
        used.append(rel)
        items.extend(got)
    return dedupe(items), used, errors


def dedupe(items):
    seen = set()
    out = []
    for it in items:
        key = (it.kind, it.ident)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def apply_scan(con, game_id, items):
    """DB 반영. 반환: {added, changed, removed, unchanged, reactivated}"""
    t = db.now()
    counts = dict(added=0, changed=0, removed=0, unchanged=0, reactivated=0)
    with db.tx(con):
        scan_no = con.execute("SELECT COUNT(*) FROM scans WHERE game_id = ?",
                              (game_id,)).fetchone()[0] + 1

        # 파일 내 순서
        seq_by_file = {}
        for it in items:
            n = seq_by_file.get(it.file, 0)
            it.seq = n
            seq_by_file[it.file] = n + 1
            it.hash = src_hash(it.source)

        old = {(r["kind"], r["ident"]): dict(r) for r in
               con.execute("SELECT * FROM entries WHERE game_id = ?", (game_id,))}

        for it in items:
            con.execute("""INSERT OR IGNORE INTO texts(game_id, src_hash, source, status, updated_at)
                           VALUES (?,?,?,?,?)""", (game_id, it.hash, it.source, db.PENDING, t))

        matched_old = set()
        unmatched_new = []
        for it in items:
            o = old.get((it.kind, it.ident))
            if o is None:
                unmatched_new.append(it)
                continue
            matched_old.add(o["id"])
            if o["src_hash"] != it.hash:
                # 명시적 id 등으로 식별자는 같은데 원문이 바뀜
                _record_change(con, game_id, o, it, t)
                counts["changed"] += 1
            elif not o["active"]:
                counts["reactivated"] += 1
            else:
                counts["unchanged"] += 1
            _update_entry(con, o["id"], it, scan_no)

        # 위치 기반 변경 판정: 파일별로 이전 활성 항목 순서와 새 순서를 정렬 비교
        changed_pairs = _pair_changes(
            [o for o in old.values() if o["active"] and o["id"] not in matched_old],
            unmatched_new, old, items)
        paired_new = set()
        for o, it in changed_pairs:
            _record_change(con, game_id, o, it, t)
            _update_entry(con, o["id"], it, scan_no)
            matched_old.add(o["id"])
            paired_new.add(id(it))
            counts["changed"] += 1

        for it in unmatched_new:
            if id(it) in paired_new:
                continue
            con.execute("""INSERT INTO entries(game_id, kind, ident, src_hash, speaker, file, line,
                               label, seq, tmpl, active, first_scan, last_scan)
                           VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                        (game_id, it.kind, it.ident, it.hash, it.speaker, it.file, it.line,
                         it.label, it.seq, db.dumps(it.tmpl), scan_no, scan_no))
            counts["added"] += 1

        for o in old.values():
            if o["id"] not in matched_old and o["active"]:
                con.execute("UPDATE entries SET active = 0 WHERE id = ?", (o["id"],))
                counts["removed"] += 1

        con.execute("""INSERT INTO scans(game_id, at, added, changed, removed, unchanged, reactivated)
                       VALUES (?,?,?,?,?,?,?)""",
                    (game_id, t, counts["added"], counts["changed"], counts["removed"],
                     counts["unchanged"], counts["reactivated"]))
        con.execute("UPDATE games SET last_scan_at = ?, updated_at = ? WHERE id = ?",
                    (t, t, game_id))
    return counts


def _update_entry(con, entry_id, it, scan_no):
    con.execute("""UPDATE entries SET ident = ?, src_hash = ?, speaker = ?, file = ?, line = ?,
                       label = ?, seq = ?, tmpl = ?, active = 1, last_scan = ?
                   WHERE id = ?""",
                (it.ident, it.hash, it.speaker, it.file, it.line, it.label, it.seq,
                 db.dumps(it.tmpl), scan_no, entry_id))


def _record_change(con, game_id, o, it, t):
    tr = con.execute("SELECT source, translation FROM texts WHERE game_id = ? AND src_hash = ?",
                     (game_id, o["src_hash"])).fetchone()
    con.execute("""INSERT INTO history(game_id, kind, old_ident, new_ident, old_hash, new_hash,
                       old_source, new_source, old_translation, changed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (game_id, it.kind, o["ident"], it.ident, o["src_hash"], it.hash,
                 tr["source"] if tr else None, it.source, tr["translation"] if tr else None, t))


def _pair_changes(old_left, new_left, old_all, items):
    """같은 파일·같은 종류에서, 이전 순서와 새 순서를 비교해 '바뀐 자리'의 항목끼리 짝짓는다."""
    if not old_left or not new_left:
        return []
    old_left_ids = {o["id"] for o in old_left}
    new_left_ids = {id(i) for i in new_left}
    pairs = []
    groups = {}
    for o in old_all.values():
        if o["active"]:
            groups.setdefault((o["kind"], o["file"]), [[], []])[0].append(o)
    for it in items:
        groups.setdefault((it.kind, it.file), [[], []])[1].append(it)
    for (_kind, _file), (olds, news) in groups.items():
        olds.sort(key=lambda o: o["seq"] or 0)
        a = [o["ident"] for o in olds]
        b = [i.ident for i in news]
        sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op != "replace":
                continue
            ol = [o for o in olds[i1:i2] if o["id"] in old_left_ids]
            nl = [n for n in news[j1:j2] if id(n) in new_left_ids]
            for o, n in zip(ol, nl):
                pairs.append((o, n))
    return pairs
