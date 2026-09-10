---
description: Show or set this project's tags, which is how precedent finds it
argument-hint: "[comma-separated tags to add]"
---

With no arguments, show the vocabulary and this project's current tags:

```bash
uv run ~/.claude/skills/precedent/scripts/precedent.py tag --project .
```

With arguments, add them:

```bash
uv run ~/.claude/skills/precedent/scripts/precedent.py tag --project . --add "$ARGUMENTS"
```

Always look at the vocabulary before coining anything. Matching is exact, so a
near-duplicate hides half the history from the other half; the command refuses
obvious collisions but cannot know whether you meant something genuinely
different. Prefer several small orthogonal tags (`backend`, `java`,
`distributed`) over one compound name — each part is reused by other projects,
which is what makes overlap meaningful.

In a monorepo, tag each module separately and tag the root with what is true of
the whole repository.
