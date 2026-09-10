---
description: Record a decision that was just settled, with its rationale and rejected alternatives
argument-hint: "[what was decided, or leave blank to use the conversation]"
---

Record the decision: $ARGUMENTS

If that is blank, use the decision settled in this conversation. Read
`~/.claude/skills/precedent/SKILL.md` first if it is not already loaded —
it covers what is worth recording and what is not.

Draft the command, show the user the one-line summary, and run it only once they
confirm. A wrong entry is worse than a missing one, because it gets quoted back
later as their own precedent.

```bash
uv run ~/.claude/skills/precedent/../../scripts/precedent.py record --project . \
  --title "..." --rationale "..." --scope architecture \
  --topic "..." --chose "..." --rejected "..."
```

`--rationale` is the field that matters: in two years the *what* is recoverable
from the code and the *why* is not. `--rejected` is what lets a future session
warn about reviving something already turned down. In a monorepo, record against
the module the decision is about, not the repository root.
