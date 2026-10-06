#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Functional tests for recall.py — stdlib only, no third-party deps.

Each test builds a throwaway agent home (notes/skills/memories + a small SQLite
conversation store) and drives the real script as a subprocess, so the tests
exercise the same code path a user does.
"""

import json
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
RECALL = ROOT / "recall.py"


def run(args, **kw):
    return subprocess.run(
        [sys.executable, str(RECALL)] + list(args),
        capture_output=True, text=True, timeout=120, **kw
    )


class RecallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self._tmp.name) / "agent-home"
        for sub in ("notes", "skills/demo", "memories"):
            (self.home / sub).mkdir(parents=True, exist_ok=True)

        (self.home / "notes" / "decisions.md").write_text(
            "# decisions\n\n- we chose ZEBRAQUARTZ for the parser\n", encoding="utf-8")
        (self.home / "skills" / "demo" / "SKILL.md").write_text(
            "# demo\n\nUses ZEBRAQUARTZ tokens when parsing.\n", encoding="utf-8")
        (self.home / "memories" / "MEMORY.md").write_text(
            "- env fact: ZEBRAQUARTZ is the parser\n", encoding="utf-8")

        con = sqlite3.connect(str(self.home / "state.db"))
        con.execute("create table messages "
                    "(session_id text, timestamp real, role text, content text)")
        con.execute("insert into messages values (?, ?, ?, ?)",
                    ("sess-1", time.time(), "user", "we should ZEBRAQUARTZ it"))
        con.commit()
        con.close()

    def tearDown(self):
        self._tmp.cleanup()

    # ── files ────────────────────────────────────────────────────────────
    def test_finds_query_in_files(self):
        r = run(["ZEBRAQUARTZ", "--home", str(self.home), "--files-only"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("decisions.md", r.stdout)
        self.assertIn("SKILL.md", r.stdout)
        self.assertIn("MEMORY.md", r.stdout)

    def test_files_only_skips_conversation_store(self):
        r = run(["ZEBRAQUARTZ", "--home", str(self.home), "--files-only"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("conversation history", r.stdout)

    def test_reports_no_match_instead_of_pretending(self):
        r = run(["NOSUCHTOKENHERE", "--home", str(self.home), "--files-only"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("none", r.stdout)

    # ── conversation store ───────────────────────────────────────────────
    def test_finds_query_in_conversation_store(self):
        r = run(["ZEBRAQUARTZ", "--home", str(self.home), "--sessions-only"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sess-1", r.stdout)

    def test_missing_conversation_store_is_not_an_error(self):
        r = run(["ZEBRAQUARTZ", "--home", str(self.home / "nowhere"), "--sessions-only"])
        self.assertEqual(r.returncode, 0, r.stderr)

    # ── the CJK trap the README calls out ────────────────────────────────
    def test_two_char_cjk_query_still_finds_the_file(self):
        (self.home / "notes" / "cn.md").write_text(
            "我们把换机这件事记在这里\n", encoding="utf-8")
        r = run(["换机", "--home", str(self.home), "--files-only"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cn.md", r.stdout)

    # ── contract: read-only, and a bad call fails loudly ─────────────────
    def test_does_not_modify_the_conversation_store(self):
        db = self.home / "state.db"
        before = db.stat().st_mtime_ns
        run(["ZEBRAQUARTZ", "--home", str(self.home)])
        self.assertEqual(before, db.stat().st_mtime_ns,
                         "recall.py must open the store read-only")

    def test_blank_query_exits_nonzero(self):
        r = run(["   ", "--home", str(self.home)])
        self.assertEqual(r.returncode, 2)

    def test_custom_roots_override_default_dirs(self):
        extra = pathlib.Path(self._tmp.name) / "elsewhere"
        extra.mkdir()
        (extra / "x.md").write_text("ZEBRAQUARTZ lives here\n", encoding="utf-8")
        r = run(["ZEBRAQUARTZ", "--home", str(self.home),
                 "--files-only", "--roots", str(extra)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("x.md", r.stdout)


    # ── regressions found by an external review, verified by hand first ───
    def test_strips_terminal_escape_sequences_from_output(self):
        (self.home / "notes" / "evil.md").write_text(
            "SAFE \x1b[31mRED\x1b[0m \x1b]8;;http://evil.example\x07link\x1b]8;;\x07 tail\n",
            encoding="utf-8")
        r = run(["SAFE", "--home", str(self.home), "--files-only"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("evil.md", r.stdout)
        self.assertNotIn("\x1b", r.stdout, "raw ESC must never reach the terminal")

    def test_query_is_matched_literally_not_as_regex_or_wildcard(self):
        (self.home / "notes" / "lit.md").write_text("aXb a.b\n", encoding="utf-8")
        # '.' must be literal: "a.b" hits, and it must not behave like a regex 'a.b'
        r = run(["a.b", "--home", str(self.home), "--files-only"])
        self.assertIn("lit.md", r.stdout)
        # a bare wildcard query must not match everything
        r2 = run(["%", "--home", str(self.home), "--files-only"])
        self.assertIn("none", r2.stdout, "'%' must be a literal percent, not a LIKE wildcard")

    def test_html_and_htm_files_are_searched(self):
        (self.home / "notes" / "page.html").write_text(
            "<p>ZEBRAQUARTZ in html</p>\n", encoding="utf-8")
        (self.home / "notes" / "old.htm").write_text(
            "ZEBRAQUARTZ in htm\n", encoding="utf-8")
        r = run(["ZEBRAQUARTZ", "--home", str(self.home), "--files-only"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("page.html", r.stdout)
        self.assertIn("old.htm", r.stdout)

    def test_skipped_dirs_stay_skipped(self):
        deep = self.home / "notes" / "node_modules" / "pkg"
        deep.mkdir(parents=True)
        (deep / "index.md").write_text("ZEBRAQUARTZ vendored\n", encoding="utf-8")
        r = run(["ZEBRAQUARTZ", "--home", str(self.home), "--files-only"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("node_modules", r.stdout)

    def test_non_positive_limit_is_rejected(self):
        for bad in ("-1", "0"):
            r = run(["ZEBRAQUARTZ", "--home", str(self.home), "-n", bad])
            self.assertEqual(r.returncode, 2, "-n %s must be rejected" % bad)

    def test_roots_are_stripped_before_use(self):
        second = pathlib.Path(self._tmp.name) / "second"
        second.mkdir()
        (second / "y.md").write_text("ZEBRAQUARTZ in second root\n", encoding="utf-8")
        spaced = "%s, %s" % (self.home / "notes", second)   # note the space after the comma
        r = run(["ZEBRAQUARTZ", "--home", str(self.home), "--files-only", "--roots", spaced])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("y.md", r.stdout, "roots must be stripped before building the path")


class ScopeTests(unittest.TestCase):
    """Scopes: an entry or file owned by another scope must be invisible, not ranked lower."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self._tmp.name) / "agent-home"
        for sub in ("notes", "memories", "notes/acme", "notes/other"):
            (self.home / sub).mkdir(parents=True, exist_ok=True)
        # memory: one per-scope entry (tagged) + one shared (untagged)
        (self.home / "memories" / "MEMORY.md").write_text(
            "[[acme]] ACMEONLY is the acme token\n"
            "\u00a7\n"
            "[[other]] OTHERONLY is the other token\n"
            "\u00a7\n"
            "SHAREDTOKEN applies to everything\n",
            encoding="utf-8")
        (self.home / "notes" / "acme" / "a.md").write_text("ACMEONLY zed\n", encoding="utf-8")
        (self.home / "notes" / "other" / "b.md").write_text("OTHERONLY zed\n", encoding="utf-8")
        (self.home / "notes" / "shared.md").write_text("SHAREDTOKEN zed\n", encoding="utf-8")
        (self.home / "notes" / "recall-scopes.json").write_text(json.dumps({
            "acme": {"label": "Acme", "dirs": ["notes/acme"], "hints": ["acme"]},
            "other": {"label": "Other", "dirs": ["notes/other"], "hints": ["other"]},
        }), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_scopes_configured_is_not_an_error(self):
        r = run(["--scopes", "--home", str(self.home / "nowhere")])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no scopes configured", r.stdout)

    def test_scopes_map_is_printed(self):
        r = run(["--scopes", "--home", str(self.home)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("acme", r.stdout)
        self.assertIn("notes/acme", r.stdout)

    def test_without_scope_everything_is_visible(self):
        r = run(["zed", "--home", str(self.home), "--files-only"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("a.md", r.stdout)
        self.assertIn("b.md", r.stdout)

    def test_scope_hides_files_owned_by_other_scopes(self):
        r = run(["zed", "--home", str(self.home), "--files-only", "--scope", "acme"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("a.md", r.stdout)
        self.assertIn("shared.md", r.stdout, "shared files stay visible in every scope")
        self.assertNotIn("b.md", r.stdout, "another scope's file must be invisible")

    def test_scope_hides_tagged_memory_entries_but_keeps_shared_ones(self):
        r = run(["ONLY", "--home", str(self.home), "--files-only", "--scope", "acme"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ACMEONLY", r.stdout)
        self.assertNotIn("OTHERONLY", r.stdout, "another scope's entry must be invisible")
        self.assertIn("hidden by scope", r.stdout, "the count of hidden entries is reported")

    def test_untagged_memory_entry_is_visible_in_every_scope(self):
        r = run(["SHAREDTOKEN", "--home", str(self.home), "--files-only", "--scope", "acme"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SHAREDTOKEN", r.stdout)

    def test_unknown_scope_fails_loudly(self):
        r = run(["zed", "--home", str(self.home), "--scope", "nosuch"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown scope", r.stdout)

    def test_conversation_history_is_never_scope_filtered(self):
        con = sqlite3.connect(str(self.home / "state.db"))
        con.execute("create table messages (session_id text, timestamp real, role text, content text)")
        con.execute("insert into messages values (?, ?, ?, ?)",
                    ("s", time.time(), "user", "OTHERONLY said in a session"))
        con.commit()
        con.close()
        r = run(["OTHERONLY", "--home", str(self.home), "--sessions-only", "--scope", "acme"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("said in a session", r.stdout,
                      "the raw record is never filtered — filtering it would hide evidence")

    def test_session_flag_excludes_that_session(self):
        con = sqlite3.connect(str(self.home / "state.db"))
        con.execute("create table messages (session_id text, timestamp real, role text, content text)")
        con.execute("insert into messages values (?, ?, ?, ?)",
                    ("mine", time.time(), "user", "ZEBRAQUARTZ in my own session"))
        con.execute("insert into messages values (?, ?, ?, ?)",
                    ("theirs", time.time(), "user", "ZEBRAQUARTZ in another session"))
        con.commit()
        con.close()
        r = run(["ZEBRAQUARTZ", "--home", str(self.home), "--sessions-only", "--session", "mine"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("another session", r.stdout)
        self.assertNotIn("my own session", r.stdout, "--session must drop that session's rows")

    def test_scopes_file_can_override_the_default_search_roots(self):
        extra = self.home / "delivered"
        extra.mkdir()
        (extra / "out.md").write_text("ZEBRAQUARTZ delivered\n", encoding="utf-8")
        (self.home / "notes" / "recall-scopes.json").write_text(json.dumps({
            "roots": ["notes", "delivered"],
            "scopes": {"acme": {"dirs": ["notes/acme"], "hints": ["acme"]}},
        }), encoding="utf-8")
        r = run(["ZEBRAQUARTZ", "--home", str(self.home), "--files-only"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("out.md", r.stdout, "roots from the scopes file must be searched")
        r2 = run(["--scopes", "--home", str(self.home)])
        self.assertIn("delivered", r2.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
