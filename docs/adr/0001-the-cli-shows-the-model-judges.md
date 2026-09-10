# The CLI shows, the model judges

`precedent` had two string-similarity heuristics deciding when two words meant
the same thing: `close_matches` (difflib ratio plus an abbreviation test) behind
`refuse_drift`, which refused to mint a tag resembling an existing one. Measured
against realistic tag pairs it was wrong 7 times in 11 — `web-frontend` vs
`web-backend`, `mobile-ios` vs `mobile-android`, `telegram-bot` vs
`telegram-api` — because `SequenceMatcher` scores a shared prefix and the
thresholds had only ever been tuned against suffix-sharing pairs
(`telegram-bot` / `discord-bot`). We deleted the heuristics. The CLI now prints
the vocabulary and the model, which has the vocabulary, the repository and the
conversation in front of it, decides whether two words mean the same thing.

This is the argument the project already made for tags and did not carry through:
keyword classification was tried and removed because "the caller is an agent that
can read the manifests, the layout and the README; it classifies better than any
table." `close_matches` was a table by another name.

## The line this draws

**Matching** — resolving `--topic persistence` to stored decisions — stays exact
string equality. Fuzzy matching here would let the tool assert that precedent
applies when it does not, unreproducibly, and that is the one thing determinism
is buying.

**Detecting** — "is this new word the same as one I already have?" — is judgment
and moves to the model.

## Consequences

- A refusal that is wrong 7 times in 11 trains the model to reflex-pass
  `--new-tag`, which is strictly worse than no guard. Removing it removes that.
- `maintain` keeps a *strict* check — normalise case, `-`, `_` and plurals, then
  compare exactly — because that is identity after trivial normalisation, a fact
  rather than a similarity guess.
- `replay_entry` must keep its `tags_distinct` no-op branch so journals written
  before this change still replay.
