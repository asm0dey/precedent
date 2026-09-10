---
description: Record that this project knowingly departs from precedent, and why
argument-hint: "<why this project is different>"
---

Use when the precedent is sound but this project is genuinely an exception —
not when the precedent is stale (that is `--supersedes`) and not when it was
wrong everywhere (that is `/precedent-regret`).

```bash
uv run ~/.claude/skills/precedent/../../scripts/precedent.py record --project . \
  --title "..." --topic "..." --chose "..." --rationale "..." \
  --despite "$ARGUMENTS"
```

Read `~/.claude/skills/precedent/SKILL.md` first if it is not already loaded.

`--despite` is what stops `check` repeating an unanswerable DIVERGENCE warning in
this project forever. State what is different about *this* project, not why the
option is good in general — the reason has to be checkable by a future session
deciding whether the exception still holds.
