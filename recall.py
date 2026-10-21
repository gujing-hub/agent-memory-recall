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
  python3 recall.py "some query" --session <id>       # drop one session from the results
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
def load_config(path: pathlib.Path) -> dict:
    """Read the scope map and the optional default search roots.

    A missing file is normal (no scopes configured, default roots). The file is either
    `{"roots": [...], "scopes": {name: {...}}}` or a bare `{name: {...}}`.
    """
    empty = {"scopes": {}, "roots": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty
    except (OSError, ValueError) as e:
        print(sanitize(f"warning: ignoring unreadable scopes file {path}: {e}"))
        return empty
    if not isinstance(raw, dict):
        return empty
    body = raw.get("scopes")
    if not isinstance(body, dict):
        body = dict((k, v) for k, v in raw.items() if k != "roots")
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
    roots = [str(x).strip() for x in (raw.get("roots") or []) if isinstance(x, (str, bytes))]
    return {"scopes": out, "roots": [r for r in roots if r]}


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
    """Which scope owns this path — by directory prefix, else by filename hint.

    `dirs` may be written home-relative (`notes/acme`), `~`-relative or absolute
    (`~/Projects/acme`) — a machine keeping its projects outside the agent home needs
    the latter.
    """
    abs_p = path_str if path_str.startswith("/") else str(home / path_str)
    try:
        rel = str(pathlib.Path(abs_p).relative_to(home))
    except ValueError:
        rel = None
    name = abs_p.rsplit("/", 1)[-1].lower()
    for scope, conf in scopes.items():
        for d in conf["dirs"]:
            dd = str(pathlib.Path(d).expanduser()) if d[:1] in ("~", "/") else d
            if dd.startswith("/"):
                if abs_p == dd or abs_p.startswith(dd.rstrip("/") + "/"):
                    return scope
            else:
                for cand in (rel, path_str):
                    if cand and (cand == dd or cand.startswith(dd.rstrip("/") + "/")):
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


def print_scopes(home: pathlib.Path, scopes: dict, path: pathlib.Path, roots=()) -> None:
    print(sanitize(f"scopes file: {path}"))
    print(sanitize(f"search roots: {', '.join(roots) if roots else 'notes, wiki, memories, skills (default)'}"))
    if not scopes:
        print("  (no scopes configured — every path and entry is shared)")
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


def search_sessions(db: pathlib.Path, q: str, limit: int, days: int | None, exclude: str | None = None):
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
        if exclude and sid == exclude:
            continue
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


def tokenize(q: str) -> list[str]:
    """Split a query into search terms.

    A single-term query behaves exactly as before (one exact substring match).
    Multi-term queries used to be matched as one literal string — so
    "battery buyback port" found nothing at all, because that sentence never
    appears verbatim. Each term is now searched on its own and hits are ranked
    by how many terms they cover.
    """
    toks = [t for t in re.split(r"\s+", q.strip()) if t]
    return toks or [q]


# Names of archive/index-like files: they match almost anything, so a hit there
# carries little information and must not crowd out the real answer.
NOISY_NAME = ("记忆归档", "memory-index", "index.md", "aa-snapshots", ".bak-")

_size_cache: dict[str, int] = {}


def _size(path: str) -> int:
    v = _size_cache.get(path)
    if v is None:
        try:
            v = os.path.getsize(path)
        except OSError:
            v = 0
        _size_cache[path] = v
    return v


def _score_hit(path: str, text: str, tokens: list[str], q: str) -> tuple[float, int]:
    """Rank a hit; higher wins. Returns (score, terms_covered).

    Terms covered dominates (a file matching every term is the likely answer),
    then a term appearing in the file name, then a verbatim-phrase bonus.
    Huge files and archive/index names are pushed down — they match everything.
    """
    low, plow = text.lower(), path.lower()
    base = os.path.basename(plow)
    covered = sum(1 for t in tokens if t.lower() in low)
    score = 100.0 * covered
    if len(tokens) == 1:
        return score, covered
    if q.lower() in low:
        score += 30.0
    score += 60.0 * sum(1 for t in tokens if t.lower() in plow)
    score += 20.0 * sum(1 for t in tokens if t.lower() in base)
    score -= min(40.0, _size(path) / 20480.0)          # −1 per 20 KB, capped at −40
    if any(n in base for n in NOISY_NAME):
        score -= 30.0
    return score, covered


def search_dir(q: str, root: pathlib.Path, limit: int, rg: str | None):
    if not root.exists():
        return []
    tokens = tokenize(q)
    best: dict[str, tuple[float, int, str, str]] = {}  # path -> (score, covered, line, snippet)

    def consider(f: str, ln: str, text: str) -> None:
        f = sanitize(f)
        snip = sanitize(re.sub(r"\s+", " ", text))[:SNIPPET]
        sc, cov = _score_hit(f, snip, tokens, q)
        if len(tokens) > 1:
            # show how much of the query this hit covers — a 1/3 hit is a weak
            # lead, and saying so beats letting it look like an answer
            snip = "[%d/%d] %s" % (cov, len(tokens), snip)
        cur = best.get(f)
        if cur is None or cov > cur[1] or (cov == cur[1] and sc > cur[0]):
            best[f] = (sc, cov, ln, snip)

    if rg:
        globs: list[str] = []
        for s in TEXT_SUFFIXES:
            globs += ["--glob", "*" + s]
        for d in sorted(SKIP_DIRS):
            # '!**/name/**' — a bare '!name/**' only matches at the search root, so a
            # nested node_modules/ would still be searched (verified against rg 15.2).
            globs += ["--glob", "!**/" + d + "/**"]
        for tok in tokens:
            try:
                r = subprocess.run(
                    [rg, "--no-heading", "--line-number", "--color", "never",
                     "--fixed-strings", "--max-count", str(max(2, limit)),
                     "--max-columns", "400", "--max-columns-preview", "-i"]
                    + globs + ["--", tok, str(root)],
                    capture_output=True, text=True, timeout=60,
                )
            except Exception:
                continue
            for line in r.stdout.splitlines():
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    consider(parts[0], parts[1], parts[2])
    else:
        lows = [t.lower() for t in tokens]
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            try:
                for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if any(t in line.lower() for t in lows):
                        consider(str(p), str(i), line)
                        break
            except Exception:
                continue
    # best hit per file, then rank — one file must not fill the whole list
    ranked = sorted(best.items(), key=lambda kv: (kv[1][1], kv[1][0]), reverse=True)
    if len(tokens) > 1:
        # A multi-term query must not degrade into "some single common word matched"
        # — that floods the answer with noise ("部署"/"流程" match everywhere).
        # If files covering ≥2 terms exist, show only those; OR is the fallback
        # for when nothing stronger exists at all.
        strong = [kv for kv in ranked if kv[1][1] >= 2]
        if strong:
            ranked = strong
    return [(f, v[2], v[3]) for f, v in ranked[:limit]]


def collect_targets(home: pathlib.Path, roots, cfg_roots, keep, scopes: dict):
    """解析要搜的目录：显式 --roots 优先，否则用配置里的 roots（再叠加 scope 自己的目录）。"""
    if roots:
        targets = []
        for p in roots.split(","):
            p = p.strip()             # strip BEFORE building the path, not only to filter
            if p:
                targets.append((p, pathlib.Path(p).expanduser()))
        return targets
    names = cfg_roots or ["notes", "wiki", "memories", "skills"]
    targets = []
    for n in names:
        p = pathlib.Path(n).expanduser()
        targets.append((n, p if p.is_absolute() else home / n))
    # `--scope X` also searches X's own directories. A scope may own a tree the
    # default roots never touch (a books/ folder outside the agent home, say).
    for s in keep:
        for d in scopes.get(s, {}).get("dirs", []):
            dp = pathlib.Path(d).expanduser()
            if not dp.is_absolute():
                dp = home / dp
            if not any(str(dp) == str(t[1]) for t in targets):
                targets.append((d, dp))
    return targets


def run_tiered(home: pathlib.Path, q: str, limit: int, stop: int, rg, keep,
               scopes: dict, targets: list) -> int:
    """三级检索：Skill → trace → world model，命中够就停。

    借 MemOS 的顺序而不是它那套架构：先找「已经会做的事」，再找「做过/交付过
    的东西」，最后才翻知识库。目的是省 token —— 多数接活问题第一级就有答案，
    不必把五处全扫一遍。`stop` 是每级的短路阈值（默认 3）。

    T2 用的是配置里除 notes/wiki/memories/skills 之外的目录（本机是 ~/hermes产出），
    没有配就只搜会话历史 —— 不硬编码任何绝对路径，测试环境才不会去翻真实产出。
    """
    world_names = {"notes", "wiki", "memories"}
    t1 = [(n, p) for n, p in targets if n == "skills"]
    t3 = [(n, p) for n, p in targets if n in world_names]
    t2_dirs = [(n, p) for n, p in targets if n not in world_names and n != "skills"]
    tiers = [
        ("T1 skill 现成能力", t1, False),
        ("T2 trace 相似经历", t2_dirs, True),
        ("T3 world 知识", t3, False),
    ]
    for name, dirs, with_sessions in tiers:
        print(sanitize(f"━━ {name}"))
        found = 0
        if with_sessions:
            rows, note = search_sessions(home / "state.db", q, limit, None, None)
            print(sanitize(f"   会话历史 ({note})") + (f" → {len(rows)}" if rows else " → none"))
            for when, sid, role, body in rows:
                # 会话原文常常很长（含系统提示），截断显示，完整内容按 session id 回查
                short = body if len(body) <= 140 else body[:140] + "…"
                print(sanitize(f"   [{when}] {sid[:24]} ({role}) {short}"))
            found += len(rows)
        for label, d in dirs:
            if not d.exists():
                continue
            if label == "memories":
                hits, _hidden = search_memories(home, q, limit, keep)
            else:
                raw = search_dir(q, d, limit * (3 if keep else 1), rg)
                hits = scope_filter(raw, home, scopes, keep)[:limit]
            print(sanitize(f"   {label}") + (f" → {len(hits)}" if hits else " → none"))
            for f, ln, text in hits:
                print(f"     {f}:{ln}  {text}")
            found += len(hits)
        if found >= stop:
            print(sanitize(f"   ✓ 命中 {found} 条，后面的层级跳过"
                           f"（要看全用 --flat 或 -n 调大）"))
            break
    return 0


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
    ap.add_argument("--session", default=None,
                    help="exclude one session id from the conversation results")
    ap.add_argument("--files-only", action="store_true")
    ap.add_argument("--sessions-only", action="store_true")
    ap.add_argument("--tiered", action="store_true",
                    help="三级检索 Skill → trace → world model，命中够就停（省 token）")
    ap.add_argument("--tier-stop", type=pos_int, default=3,
                    help="每级命中多少条就短路（默认 3，仅 --tiered）")
    a = ap.parse_args()

    home = pathlib.Path(a.home).expanduser() if a.home else default_home()
    scopes_file = (pathlib.Path(a.scopes_file).expanduser() if a.scopes_file
                   else home / "notes" / "recall-scopes.json")
    config = load_config(scopes_file)
    scopes, cfg_roots = config["scopes"], config["roots"]

    if a.scopes:
        print_scopes(home, scopes, scopes_file, cfg_roots)
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

    if a.tiered:
        tg = collect_targets(home, a.roots, cfg_roots, keep, scopes)
        run_tiered(home, q, a.limit, a.tier_stop, rg, keep, scopes, tg)
        print(f"\n({time.time() - t0:.1f}s · read-only; a hit is a lead, not a conclusion)")
        return 0

    if not a.files_only:
        rows, note = search_sessions(home / "state.db", q, a.limit, a.days, a.session)
        print(sanitize(f"── conversation history ({note})") + (f" → {len(rows)}" if rows else " → none"))
        for when, sid, role, body in rows:
            print(sanitize(f"   [{when}] {sid[:24]} ({role}) {body}"))
        if not rows:
            print("   (nothing found — do NOT turn this into 'it never happened')")

    if not a.sessions_only:
        targets = collect_targets(home, a.roots, cfg_roots, keep, scopes)
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
