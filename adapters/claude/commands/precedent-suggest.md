---
description: List decisions this project still owes, based on comparable projects
argument-hint: "[path, defaults to cwd]"
---

```bash
uv run ~/.claude/skills/precedent/../../scripts/precedent.py suggest --project ${1:-.}
```

Two kinds of output. Coverage gaps are topics settled in comparable projects but
open here — decisions owed, not yet made. Principle candidates are the same
answer reached in three or more projects, which is no longer a per-project
decision.

Present the gaps that matter for the current work rather than the whole list,
and ask before promoting anything to a principle: repetition is evidence, not
consent.
