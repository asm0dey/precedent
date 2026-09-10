---
description: Check the decision graph for contradictions, tag drift and dead projects
---

```bash
uv run ~/.claude/skills/precedent/scripts/precedent.py maintain
```

Report what it finds. Contradictions (one project, one topic, two live answers)
are for the user to resolve — record a new decision with `--supersedes <id>`
rather than editing history. Suspected duplicate tags are repaired with
`tag --merge <old> --into <new>`.

Run `maintain --apply` only to delete orphan nodes; everything else is a
proposal that needs the user's judgment.
