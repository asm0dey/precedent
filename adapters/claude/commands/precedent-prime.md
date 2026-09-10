---
description: Load this project's recorded decisions and precedent into context
argument-hint: "[path, defaults to cwd]"
---

Run:

```bash
uv run ~/.claude/skills/precedent/../../scripts/precedent.py brief --project ${1:-.}
```

Report what it returns, leading with whatever bears on the work at hand rather
than reciting the whole brief. If the project is untagged, say so and offer tags
based on what you can see in the directory — do not pin them without agreement.

Then hold these for the rest of the session: check the graph before recommending
a technology, and offer to record a decision in the message where it gets
settled. Guidance for both is in
`~/.claude/skills/precedent/SKILL.md`; read it if it is not already loaded.
