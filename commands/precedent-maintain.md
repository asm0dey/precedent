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

Projects with no portable identity are reported too. A `split across two nodes`
line means one repo has two Project nodes holding two separate decision
histories — surface it, run the `cypher` command it prints to show both sides,
and let the user decide. Never merge them yourself: precedent has no merge
command precisely because merging two histories is a judgment.

Run `maintain --apply` only to delete orphan nodes; everything else is a
proposal that needs the user's judgment.
