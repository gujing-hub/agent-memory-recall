#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""recall — one read-only command that searches every place an agent's knowledge lives.

Why: an agent asked "what did we do last time?" (or about to claim "we don't have X")
should LOOK IT UP first. Knowledge in a long-running agent lives in at least five
places, and people routinely search one of them and call it done:

  1. conversation history   (SQLite store, FTS5)
  2. notes / knowledge base (markdown)
  3. hot memory files       (the always-injected memory)
  4. procedures / skills    (markdown)
  5. delivered artifacts    (markdown / html / json)

This script searches all of them and prints `file:line` (or session + timestamp) so the
answer can be checked against the source, not remembered.

  python3 recall.py "some query" [-n 5] [--days 90] [--files-only|--sessions-only]
  python3 recall.py "some query" --home ~/.hermes --roots ~/notes,~/wiki
  python3 recall.py "some query" --scope work         # only in the scopes you can act in
  python3 recall.py --scopes                          # show the resolved scope map

Scopes (optional). Once one agent works on more than one project, a single flat memory
starts mixing them: the wrong project's constraints come back as if they applied here.
Scopes make "this only applies to X" mechanical:

  * Ownership is declared in a JSON file (default `<home>/notes/recall-scopes.json`):

        {"work":     {"label": "Work",     "dirs": ["Projects/acme"], "hints": ["acme"]},
         "personal": {"label": "Personal"}}

    `dirs` are paths relative to `--home`; `hints` match a file's own name.
  * A hot-memory entry may start with a tag — `[[work]]` or `〔work〕` — meaning it
    belongs to that scope. **Untagged entries are shared** and visible everywhere.
  * `--scope work` then makes entries and files owned by *other* scopes invisible
    (not merely ranked lower). Conversation history is deliberately NOT filtered:
    it is the raw record, and filtering it would hide the evidence.

No scopes file and no `--scope` ⇒ nothing changes.

Notes on the implementation:
  * SQLite is opened read-only (mode=ro) — this tool must never mutate anything.
  * FTS5 trigram indexes can't match CJK strings shorter than 3 characters, which is a
    real trap for Chinese users: fall back to LIKE (a full scan is ~0.2s on a 600MB
    store, fine for interactive use).
  * ripgrep is used when available (Hermes ships one), otherwise a pure-Python walk.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time

SNIPPET = 220
SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", ".git", "dist", "build", "cache"}
# One shared scope for BOTH search backends: whether ripgrep is installed must not
# change which files are searched.
TEXT_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml", ".html", ".htm")
# Hot-memory entry tag, at the very start of an entry: [[scope]] or 〔scope〕.
SCOPE_TAG = re.compile(r"^[ \t]*(?:\[\[([^\]\n]{1,32})\]\]|〔([^〕\n]{1,32})〕)[ \t]*")

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize(s: str) -> str:
    """Strip ANSI/OSC escapes and control characters before printing.

    Retrieved content is untrusted input: a note, a session message or a filename can
    carry terminal escape sequences that forge or hide output (and some terminals act
    on OSC sequences). `\\s+` collapsing does not remove ESC.
    """
    return _CTRL.sub("", _ANSI.sub("", s or ""))


def pos_int(v: str) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be an integer")
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def default_home() -> pathlib.Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return pathlib.Path(env).expanduser()
    return pathlib.Path.home() / ".hermes"


# ─────────────────────────────── scopes ───────────────────────────────
def load_scopes(path: pathlib.Path) -> dict:
    """Read the scope map. A missing file is normal (no scopes configured)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        print(sanitize(f"warning: ignoring unreadable scopes file {path}: {e}"))
        return {}
    if not isinstance(raw, dict):
        return {}
    body = raw.get("scopes")
    if not isinstance(body, dict):
        body = raw
    out: dict = {}
    for name, conf in body.items():
        if not isinstance(conf, dict):
            continue
        dirs = conf.get("dirs") or []
        hints = conf.get("hints") or []
        out[str(name)] = {
            "label": str(conf.get("label") or name),
            "dirs": [str(x).rstrip("/") for x in dirs if isinstance(x, (str, bytes))],
            "hints": [str(x) for x in hints if isinstance(x, (str, bytes))],
        }
    return out


def parse_scope_arg(spec, scopes: dict):
    if not spec:
        return ()
    want, bad = [], []
    for x in re.split(r"[,，\s]+", str(spec).strip()):
        if not x:
            continue
        (want if x in scopes else bad).append(x)
    if bad:
        print(sanitize("unknown scope(s): %s (known: %s)"
                       % (", ".join(bad), ", ".join(sorted(scopes)) or "none configured")))
        raise SystemExit(2)
    return tuple(dict.fromkeys(want))


def owner_of(path_str: str, home: pathlib.Path, scopes: dict):
    """Which scope owns this path — by directory prefix, else by filename hint."""
    rel = path_str
    try:
        rel = str(pathlib.Path(path_str).relative_to(home))
    except (ValueError, OSError):
        pass
    name = rel.rsplit("/", 1)[-1].lower()
    for scope, conf in scopes.items():
        for d in conf["dirs"]:
            if rel == d or rel.startswith(d + "/"):
                return scope
        for h in conf["hints"]:
            if h.lower() in name:
                return scope
    return None


def scope_filter(hits, home: pathlib.Path, scopes: dict, keep):
    if not keep:
        return hits
    out = []
    for f, ln, text in hits:
        own = owner_of(f, home, scopes)
        if own is None or own in keep:
            out.append((f, ln, text))
    return out


def search_memories(home: pathlib.Path, q: str, limit: int, keep):
    """Entry-level search of the hot-memory files, honouring [[scope]] tags.

    Entries are separated by `§`. An entry whose tag names a scope we are not acting
    in is *invisible*, not merely ranked lower. Untagged entries are shared.
    """
    d = home / "memories"
    hits, hidden = [], 0
    if not d.exists():
        return hits, hidden
    for p in sorted(d.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        line = 1
        for chunk in text.split("§"):
            entry_line = line
            line += chunk.count("\n")
            body = chunk.strip()
            if not body:
                continue
            m = SCOPE_TAG.match(body)
            tag = (m.group(1) or m.group(2)) if m else None
            if tag and keep and tag not in keep:
                hidden += 1
                continue
            if q.lower() in body.lower():
                hits.append((sanitize(str(p)), str(entry_line),
                             sanitize(re.sub(r"\s+", " ", body))[:SNIPPET]))
    return hits[:limit], hidden


def print_scopes(home: pathlib.Path, scopes: dict, path: pathlib.Path) -> None:
    print(sanitize(f"scopes file: {path}"))
    if not scopes:
        print("  (none configured — every path and entry is shared)")
        return
    for name, conf in scopes.items():
        print(sanitize("  %-12s %-14s dirs=%s hints=%s"
                       % (name, conf["label"], conf["dirs"] or "-", conf["hints"] or "-")))


# ─────────────────────────── conversation history ───────────────────────────
def fts_ok(q: str) -> bool:
    """FTS5 needs a real token for latin text; trigram indexes need CJK >= 3 chars."""
    latin = re.sub(r"[^\x00-\x7f]", "", q).strip()
    cjk = re.sub(r"[^\u3000-\u9fff]", "", q)
    return bool(latin) or len(cjk) >= 3


def search_sessions(db: pathlib.Path, q: str, limit: int, days: int | None):
    if not db.exists():
        return [], "no session store"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15)
    except sqlite3.Error as e:
        return [], f"cannot open: {e}"
    cur = con.cursor()
    where = ""
    if days:
        # NB: f-string on purpose — strftime's % must not go through %-formatting
        where = f" and m.timestamp > strftime('%s','now')-{int(days)}*86400"
    rows, note = [], ""
    try:
        if fts_ok(q):
            tbl = "messages_fts_trigram" if re.search(r"[\u3000-\u9fff]", q) else "messages_fts"
            try:
                rows = cur.execute(
                    f"""select m.session_id, m.timestamp, m.role, m.content
                        from {tbl} f join messages m on m.rowid = f.rowid
                        where f.{tbl} match ? {where}
                        order by m.timestamp desc limit ?""",
                    ('"%s"' % q if " " not in q else q, limit),
                ).fetchall()
                note = f"FTS({tbl})"
            except sqlite3.Error as e:
                note, rows = f"FTS failed ({e}), falling back to LIKE", []
        if not rows:
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            rows = cur.execute(
                f"""select session_id, timestamp, role, content from messages m
                    where content like ? escape '\\' {where} order by timestamp desc limit ?""",
                (like, limit),
            ).fetchall()
            note = note or "LIKE"
    except sqlite3.Error as e:
        return [], f"query error: {e}"
    finally:
        con.close()
    out = []
    for sid, ts, role, content in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"
        body = re.sub(r"\s+", " ", (content or "")).strip()
        out.append((when, sid, role, body[:SNIPPET]))
    return out, note


# ─────────────────────────────── files ───────────────────────────────
def find_rg(home: pathlib.Path) -> str | None:
    p = shutil.which("rg")
    if p:
        return p
    for c in (home / "tools").glob("ripgrep-*/rg"):
        return str(c)
    return None


def search_dir(q: str, root: pathlib.Path, limit: int, rg: str | None):
    if not root.exists():
        return []
    if rg:
        globs: list[str] = []
        for s in TEXT_SUFFIXES:
            globs += ["--glob", "*" + s]
        for d in sorted(SKIP_DIRS):
            # '!**/name/**' — a bare '!name/**' only matches at the search root, so a
            # nested node_modules/ would still be searched (verified against rg 15.2).
            globs += ["--glob", "!**/" + d + "/**"]
        try:
            r = subprocess.run(
                [rg, "--no-heading", "--line-number", "--color", "never",
                 "--fixed-strings", "--max-count", str(max(1, limit)),
                 "--max-columns", "400", "--max-columns-preview", "-i"]
                + globs + ["--", q, str(root)],
                capture_output=True, text=True, timeout=60,
            )
        except Exception:
            return []
        hits = []
        for line in r.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            f, ln, text = parts
            hits.append((sanitize(f), ln, sanitize(re.sub(r"\s+", " ", text))[:SNIPPET]))
        return hits[:limit]
    hits: list[tuple[str, str, str]] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        try:
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if q.lower() in line.lower():
                    hits.append((sanitize(str(p)), str(i),
                                 sanitize(re.sub(r"\s+", " ", line))[:SNIPPET]))
                    break
        except Exception:
            continue
        if len(hits) >= limit:
            break
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="search every place an agent's knowledge lives (read-only)")
    ap.add_argument("query", nargs="?")
    ap.add_argument("-n", "--limit", type=pos_int, default=5, help="max hits per location (default 5)")
    ap.add_argument("--days", type=pos_int, default=None, help="only look back N days of conversation history")
    ap.add_argument("--home", default=None, help="agent home (default $HERMES_HOME or ~/.hermes)")
    ap.add_argument("--roots", default=None, help="comma-separated extra/override directories to search")
    ap.add_argument("--scope", default=None, help="restrict to scopes, comma-separated (see --scopes)")
    ap.add_argument("--scopes-file", default=None,
                    help="scope map JSON (default <home>/notes/recall-scopes.json)")
    ap.add_argument("--scopes", action="store_true", help="print the resolved scope map and exit")
    ap.add_argument("--files-only", action="store_true")
    ap.add_argument("--sessions-only", action="store_true")
    a = ap.parse_args()

    home = pathlib.Path(a.home).expanduser() if a.home else default_home()
    scopes_file = (pathlib.Path(a.scopes_file).expanduser() if a.scopes_file
                   else home / "notes" / "recall-scopes.json")
    scopes = load_scopes(scopes_file)

    if a.scopes:
        print_scopes(home, scopes, scopes_file)
        return 0
    if not a.query:
        print("give me a query (or --scopes to show the scope map)")
        return 2
    q = a.query.strip()
    if not q:
        print("give me a query")
        return 2
    keep = parse_scope_arg(a.scope, scopes)

    t0 = time.time()
    rg = find_rg(home)
    if keep:
        print(sanitize("── scopes: %s (entries/files owned by other scopes are invisible)"
                       % ", ".join(scopes[s]["label"] for s in keep)))

    if not a.files_only:
        rows, note = search_sessions(home / "state.db", q, a.limit, a.days)
        print(sanitize(f"── conversation history ({note})") + (f" → {len(rows)}" if rows else " → none"))
        for when, sid, role, body in rows:
            print(sanitize(f"   [{when}] {sid[:24]} ({role}) {body}"))
        if not rows:
            print("   (nothing found — do NOT turn this into 'it never happened')")

    if not a.sessions_only:
        if a.roots:
            targets = []
            for p in a.roots.split(","):
                p = p.strip()              # strip BEFORE building the path, not only to filter
                if p:
                    targets.append((p, pathlib.Path(p).expanduser()))
        else:
            targets = [(n, home / n) for n in ("notes", "wiki", "memories", "skills")]
        for label, d in targets:
            if label == "memories":
                hits, hidden = search_memories(home, q, a.limit, keep)
                extra = f" ({hidden} entries hidden by scope)" if keep and hidden else ""
                print(sanitize(f"── {label}") + (f" → {len(hits)}" if hits else " → none") + extra)
                for f, ln, text in hits:
                    print(f"   {f}:{ln}  {text}")
                continue
            # scoping costs hits: ask for more, then drop the foreign ones, then cap
            raw = search_dir(q, d, a.limit * (3 if keep else 1), rg)
            hits = scope_filter(raw, home, scopes, keep)[:a.limit]
            print(sanitize(f"── {label}") + (f" → {len(hits)}" if hits else " → none"))
            for f, ln, text in hits:
                print(f"   {f}:{ln}  {text}")

    print(f"\n({time.time() - t0:.1f}s · read-only; a hit is a lead, not a conclusion)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
