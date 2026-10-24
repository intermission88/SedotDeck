# SedotDeck

AppImage/PC-port updater for Steam Deck. Core: `sedotdeck.py` (stdlib-only Python). Frontends: `decky-plugin/` (Decky Loader, TypeScript) and `desktop/` (Tauri). Tests: `python3 -I tests/test_sedotdeck.py sedotdeck.py`.

## Agent orchestration

The main session (Opus) plans, decides and reviews. Delegate the legwork to the agents in `.claude/agents/` without asking first:

| Agent | Model | Use for |
|---|---|---|
| `scout` | Haiku | Read-only lookups: "where is X", tracing a call path, reading logs, git history |
| `checker` | Haiku | Running tests/typecheck/cargo check after a change and reporting pass/fail |
| `builder` | Sonnet | Implementing a change once the approach is decided and can be briefed precisely |

Rules:
- Keep design decisions, ambiguous bugs, cross-cutting changes (CLI/JSON contract between `sedotdeck.py` and the frontends) and anything touching git in the main session.
- Small edits (a few lines, one file) are faster done directly; don't spin up `builder` for them.
- Brief `builder` with files, intended behavior and what done looks like. Independent changes in different files can go to parallel `builder` agents; changes to the same file never run in parallel.
- After `builder` finishes, review its diff yourself, then send `checker` to verify. Agent reports are claims, not proof.
- Use `scout` instead of reading many files yourself when you only need the conclusion.

<!-- antislop:start -->
## antislop
For UI, people, or code comments work, read `.claude/skills/antislop/SKILL.md` (core) and then the skill for the task:
- UI / visual: `.claude/skills/antislop-ui/SKILL.md`
- People: `.claude/skills/antislop-human/SKILL.md`
- Code comments: `.claude/skills/antislop-code/SKILL.md`
Copywriting and mobile layout skills are not installed here; skip any rule that points to them.
Before starting, follow the core's "Two Usage Modes" section in strict order: explicit session instruction first, then global preference, then ask. A session instruction always wins. For a resolved mode, say `antislop active: <mode> (session override).` or `antislop active: <mode> (global preference).` once before presenting findings or making edits, using the actual mode and source. Acknowledging the user's request without naming the source does not replace this notice.
Only an explicit choice of antislop during or after selects a session mode. A request to review, audit, or avoid file edits does not select a mode; read the global preference in that case. Another skill's mode does not select antislop's mode.
If the mode is unresolved, ask during/after and end the response; wait for the answer before any UI review, planning, or concept. For read-only tasks, put the active-mode notice only at the start of the final answer, never in progress messages. For editing tasks, announce before the first edit and omit it from the final answer.
To update antislop later: copy the same four folders from `skills/` in https://github.com/miqdadbadjuber/anti-slop over `.claude/skills/`, all from one release.
<!-- antislop:end -->
