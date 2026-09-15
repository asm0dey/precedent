# 0008 — An amendment keeps the id, stale slug and all

## Status

Accepted, 2026-09-15.

## Context

A decision id embeds a slug of the title it was minted from:

```
notify4j-core-for-multi-channel-booking-notifica-1789206712
                             ^^^^^^^
```

`amend` rewords a decision without superseding it (issue #10). The moment a
title can change, the slug inside the id is a description of a title that no
longer exists, and something has to be decided about it: leave it, or mint a
new id and point the old one at it.

## Decision

The id does not change. The slug inside it is allowed to rot.

## Consequences

The id is identity; the slug within it is an accident of how it was minted, in
the same way `Project.id` is only the native path a project was first seen at
while `Project.portable` is what the project *is* (`docs/adr/0002`). This
project has already answered the general form of this question once, and
answered it the same way: a human-readable string is a display fact, never the
identity.

This decision is also recorded in the graph precedent itself, as
`#an-amendment-keeps-the-decision-id-1789487212`.

Everything already holding an id keeps resolving — a `SUPERSEDES` edge, a
`DIVERGES_FROM` edge, a `Principle`'s `DERIVED_FROM`, a rule file, a commit
message, a GitHub issue. None of these are enumerable, which is the argument:
an id that can change is an id that can be dangling somewhere nobody will
check.

The rejected alternative was to regenerate the id and keep the old one as an
alias. It costs an alias lookup on every `MATCH (d:Decision {id:$x})` in the
file, a second journal op shape, and a mapping that can itself go stale — all
to keep a cosmetic substring honest in a string the user reads as opaque.
Regenerating with no alias was rejected outright: every existing reference
would silently stop matching, and a silent no-op is this tool's worst failure
mode.

The visible cost is that `#notify4j-core-for-multi-channel-booking-notifica-…`
can sit beside the title *"notify4j-core for all outbound third-party
delivery"* and look inconsistent. That is the intended reading — the id is a
handle, not a summary.
