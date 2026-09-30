# recall — search everywhere your agent's knowledge lives

[![ci](https://github.com/gujing-hub/agent-memory-recall/actions/workflows/ci.yml/badge.svg)](https://github.com/gujing-hub/agent-memory-recall/actions/workflows/ci.yml)

A single read-only command that searches **all five places** a long-running agent's knowledge ends up, so that "what did we do last time?" (and "we don't have X") is answered by looking it up instead of remembering.

```bash
python3 recall.py "some query" -n 5 --days 90
```

```
── conversation history (FTS(messages_fts)) → 2
   [2026-09-29 22:33] 20260929_135457_ed474d46 (assistant) ...
── notes → 2
   /home/me/.hermes/notes/decisions.md:229  - **Context**：...
── wiki → none
── memories → 1
   /home/me/.hermes/memories/USER.md:91  【...】
── skills → 2
   /home/me/.hermes/skills/.../SKILL.md:19  - ...
```

## The problem it solves

Knowledge in a long-running agent lives in at least five places:

| # | Where | Typical path |
|---|---|---|
| 1 | Conversation history | SQLite store (FTS5) |
| 2 | Notes / knowledge base | `notes/`, `wiki/` |
| 3 | Hot memory (injected every turn) | `memories/*.md` |
| 4 | Procedures / skills | `skills/**/SKILL.md` |
| 5 | Delivered artifacts | output directories |

People (and agents) routinely search **one** of them and then speak as if they had checked everything. This script makes checking everything cheap enough that there's no excuse — 0.5s, no dependencies.

## Install

No dependencies beyond the Python standard library (3.9+). Either:

```bash
# one-liner: drop it somewhere on your PATH
curl -fsSL https://raw.githubusercontent.com/gujing-hub/agent-memory-recall/main/recall.py \
  -o ~/.local/bin/recall.py && chmod +x ~/.local/bin/recall.py

# or from a clone
git clone https://github.com/gujing-hub/agent-memory-recall && cd agent-memory-recall
cp recall.py ~/.local/bin/recall.py && chmod +x ~/.local/bin/recall.py
```

Tests: `python -m unittest discover -s tests -v` (no deps, runs in under a second).

It uses [ripgrep](https://github.com/BurntSushi/ripgrep) when it can find one (including the copy Hermes ships under `~/.hermes/tools/ripgrep-*/rg`), and falls back to a pure-Python search otherwise.

## Usage

```bash
recall.py "query"                       # default home: $HERMES_HOME or ~/.hermes
recall.py "query" -n 8                  # up to 8 hits per location
recall.py "query" --days 30             # only the last 30 days of conversation history
recall.py "query" --files-only          # skip the conversation store
recall.py "query" --home /path/to/.hermes
recall.py "query" --roots ~/notes,~/wiki   # search your own directories instead
```

## Two implementation details worth knowing

**1. CJK short queries silently fail on FTS5.** A trigram FTS5 index cannot match strings shorter than 3 characters, so two-character Chinese queries (e.g. `换机`) return zero results from the index while the content is right there in the database. The script detects this and falls back to `LIKE` — a full scan of a 600 MB store takes ~0.2s, which is fine interactively. If you only support English you will never notice this, which is exactly why it's worth handling.

**2. It is strictly read-only.** The SQLite store is opened with `mode=ro`; nothing is ever written. A search tool that can mutate memory is a liability.

## The three rules that make it useful

A search tool alone doesn't fix anything — the discipline does:

1. **A hit is a lead, not a conclusion.** The output gives you `file:line`; open it and check before you state anything as fact.
2. **"Nothing found" ≠ "it never happened."** Say *"I searched these five places and found nothing"* — that is checkable. *"We don't have X"* is an assertion, and it's the one that gets you burned.
3. **Before concluding "this is a gap," search specifically for counter-evidence.** Looking for support after you've decided something is a good insight is confirmation bias; a command that costs 0.5s removes the excuse.

These fall out of a longer write-up on memory governance (hot-layer authoring rules, verifying that "moved to file X" claims are actually true, and treating memory changes as measurable): see `ARTICLE.md` in this repo, or the same content under the agent's own notes.

## License

MIT — see `LICENSE`.
