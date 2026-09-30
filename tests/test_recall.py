#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Functional tests for recall.py — stdlib only, no third-party deps.

Each test builds a throwaway agent home (notes/skills/memories + a small SQLite
conversation store) and drives the real script as a subprocess, so the tests
exercise the same code path a user does.
"""

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
