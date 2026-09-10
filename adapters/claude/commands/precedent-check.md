---
description: Show what was decided before on a topic, and flag conflicts with a proposed choice
argument-hint: "<topic> [option you are leaning toward]"
---

```bash
uv run ~/.claude/skills/precedent/../../scripts/precedent.py check --topic "$1" --chose "$2"
```

Lead with what it returns before offering your own opinion.

`CONFLICT` means this option was explicitly rejected before — quote the recorded
reason. `DIVERGENCE` means the user chose differently in comparable projects.

Both are information, not vetoes. Test whether the recorded reason still applies
to *this* case: a rationale about concurrent writers says nothing about a
single-user tool. Say which way you read it and leave the decision with them.
