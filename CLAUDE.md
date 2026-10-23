# DeckPort

AppImage/PC-port updater for Steam Deck. Core: `deckport.py` (stdlib-only Python). Frontends: `decky-plugin/` (Decky Loader, TypeScript) and `desktop/` (Tauri). Tests: `python3 -I tests/test_deckport.py deckport.py`.

## Agent orchestration

The main session (Opus) plans, decides and reviews. Delegate the legwork to the agents in `.claude/agents/` without asking first:

| Agent | Model | Use for |
|---|---|---|
| `scout` | Haiku | Read-only lookups: "where is X", tracing a call path, reading logs, git history |
| `checker` | Haiku | Running tests/typecheck/cargo check after a change and reporting pass/fail |
| `builder` | Sonnet | Implementing a change once the approach is decided and can be briefed precisely |

Rules:
- Keep design decisions, ambiguous bugs, cross-cutting changes (CLI/JSON contract between `deckport.py` and the frontends) and anything touching git in the main session.
- Small edits (a few lines, one file) are faster done directly; don't spin up `builder` for them.
- Brief `builder` with files, intended behavior and what done looks like. Independent changes in different files can go to parallel `builder` agents; changes to the same file never run in parallel.
- After `builder` finishes, review its diff yourself, then send `checker` to verify. Agent reports are claims, not proof.
- Use `scout` instead of reading many files yourself when you only need the conclusion.
