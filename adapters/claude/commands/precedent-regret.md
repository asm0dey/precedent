---
description: Mark a choice you repeated across projects as a mistake, inverting its precedent
argument-hint: "<topic> <option that was wrong>"
---

```bash
uv run ~/.claude/skills/precedent/scripts/precedent.py regret \
  --topic "$1" --chose "$2" --because "..." --instead "..."
```

Read `~/.claude/skills/precedent/SKILL.md` first if it is not already loaded.

`--because` is the whole point: without the reason, a future session sees only
that the option is disfavoured and cannot tell whether the condition that made it
wrong applies to the case in front of it.

Show the user which decisions this will mark before running it — the scope is
every project at once. Nothing is deleted: the decisions keep their original
rationales, because a reason that looked good several times is what tells you the
lesson was expensive.
