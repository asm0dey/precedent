# /// script
# requires-python = ">=3.12"
# dependencies = ["grafeo", "filelock"]
# ///
"""precedent: durable, cross-project memory of the decisions you have made.

Two stores, on purpose:

  journal.jsonl  append-only, one line per decision, the source of truth
  graph.db       Grafeo graph, a queryable index rebuilt from the journal

The journal exists because the graph engine is young (v0.5.x). If it ever eats
itself, `precedent.py rebuild` replays the journal and nothing is lost. The journal is
also plain text, so it diffs and survives in git.

Every subcommand is a short-lived process holding a file lock: exclusive if
it writes, shared if it only reads. The lock is not optional for writers:
concurrent processes on one embedded database silently drop writes (measured:
120 writes across 6 processes -> 60 stored, zero errors raised). Readers share
because a full open/query/close cycle is about a millisecond and queueing them
behind a write buys nothing. Note that "reader" means reads no DECISION —
opening the graph at all appends to its write-ahead log, so the shared lock is
a bet that grafeo tolerates concurrent WAL appenders. `selftest` tests that bet
rather than assuming it.
"""
from __future__ import annotations

import argparse
import filelock
import json
import os
import pathlib
import re
import sys
import time
from datetime import date

DEFAULT_HOME = pathlib.Path.home() / ".local/share/precedent"
HOME = pathlib.Path(os.environ.get("PRECEDENT_HOME", DEFAULT_HOME))
SCOPES = ("architecture", "business", "process", "tooling", "product")


# --------------------------------------------------------------------------- store

class Store:
    def __init__(self, home: pathlib.Path = HOME, write: bool = True):
        self.home = home
        home.mkdir(parents=True, exist_ok=True)
        self.db_path = home / "graph.db"
        self.journal = home / "journal.jsonl"
        self.lock_path = home / ".lock"
        self.write = write
        # This library, not raw POSIX file locking: the POSIX module does
        # not exist on Windows, and it was imported at module scope, so the
        # whole CLI failed to start there. Hand-rolling a cross-platform
        # locking shim means owning a concurrency primitive on an OS this
        # project does not run and cannot test.
        # ReadWriteLock, not FileLock: `check`, `suggest` and `export`
        # change no decision, and queueing them behind a write serialises the
        # common case for nothing. Writers stay exclusive.
        # "Reader" is about decisions, not bytes: opening a grafeo db appends
        # ~10 bytes to its WAL even with no query run, so a shared lock lets
        # several processes append to that log at once. _check_grafeo_readers
        # is what says that is survivable.
        # 30s timeout, not the default unbounded wait, and it is on readers
        # too: the SessionStart hook runs `brief` on every session start, and
        # an unbounded lock would hang every new session silently if a slow
        # `rebuild` held it.
        self._lock = filelock.ReadWriteLock(str(self.lock_path), timeout=30)

    def __enter__(self):
        # The timeout is passed per call rather than left to the instance.
        # acquire_read/acquire_write default to -1, wait forever, and never
        # consult self.timeout — only the read_lock/write_lock context
        # managers do that, and they are context managers, so using them here
        # would mean holding the graph open inside a nested `with`. Relying on
        # the attribute would leave the 30s bound configured and unenforced.
        acquire = self._lock.acquire_write if self.write else self._lock.acquire_read
        try:
            acquire(self._lock.timeout)
        except filelock.Timeout:
            mode = "write" if self.write else "read"
            raise SystemExit(
                f"error: could not acquire a {mode} lock at {self.lock_path} "
                f"within {self._lock.timeout:g}s — another precedent process holds it")
        import grafeo

        self.db = grafeo.GrafeoDB(str(self.db_path))
        return self

    def __exit__(self, *exc):
        try:
            self.db.close()
        except Exception:
            pass
        self._lock.release()
        return False

    def q(self, cypher: str, params: dict | None = None) -> list[dict]:
        return list(self.db.execute(cypher, params) if params else self.db.execute(cypher))

    def log(self, op: str, payload: dict) -> None:
        """Journal first, then mutate. A crash between the two costs a replay, not data."""
        with open(self.journal, "a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "op": op, **payload}) + "\n")
            f.flush()
            os.fsync(f.fileno())


def slug(text: str, maxlen: int = 48) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:maxlen] or "untitled"


def today() -> str:
    return date.today().isoformat()


def csv(value: str | None) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


# ----------------------------------------------------------------- project tags

# A project is described by a SET of tags, not one type.
#
# One type string forces a false choice: a service is not "backend-api" OR
# "java" OR "distributed", it is all three, and which of them a past decision
# rhymes with depends on the decision. Tags also make the vocabulary converge on
# its own — small orthogonal words get reused far more often than compound names
# nobody spells the same way twice.
#
# Nothing is inferred from dependencies. Keyword matching cannot be made
# correct: a Rust CLI named "nextgen" matched the marker "next" because the word
# appears in its description, and "react-native" matched "react" before it could
# reach its own entry. The caller is an agent that can read the manifests, the
# layout and the README; it classifies better than any table, and it can
# recognise kinds nobody enumerated.

# Nothing is inferred from dependencies, and nothing is matched against a list
# of known build files either. A whitelist of manifests cannot be completed —
# Zig, Nim, Bazel, Nix, Julia, or whatever appears next year would all report
# "nothing found" — so this reports what is actually in the directory and lets
# the agent, which can read any of them, decide what it is looking at.
#
# The one list here is noise to omit, and getting it wrong is harmless: a
# missing entry shows clutter, never hides signal.
NOISE = {"node_modules", "__pycache__", "venv", "target", "build", "dist",
         "vendor", "Pods", "DerivedData", "bin", "obj", "out", "coverage"}

LISTING_CAP = 24


def detect_project(root: pathlib.Path) -> dict:
    """Identify a project and show what is in it, without guessing its kind.

    Files come before directories because build manifests are files and they
    carry the most signal per character.
    """
    root = root.resolve()
    files, dirs = [], []
    try:
        for f in sorted(root.iterdir()):
            if f.name.startswith(".") or f.name in NOISE:
                continue
            (dirs if f.is_dir() else files).append(
                f.name + ("/" if f.is_dir() else ""))
    except OSError:
        pass
    entries = files + dirs
    shown = entries[:LISTING_CAP]
    listing = ", ".join(shown)
    if len(entries) > LISTING_CAP:
        listing += f", … (+{len(entries) - LISTING_CAP} more)"
    return {"id": str(root), "name": root.name, "contents": listing}


def tags_of(s: "Store", project_id: str) -> list[str]:
    return [r["t"] for r in s.q(
        """MATCH (:Project {id:$id})-[:TAGGED]->(t:Tag) RETURN t.name AS t ORDER BY t""",
        {"id": project_id})]


def project_info(s: "Store", path: str, extra_tags: str = "") -> dict:
    info = detect_project(pathlib.Path(path))
    info["tags"] = sorted(set(tags_of(s, info["id"])) | set(csv(extra_tags)))
    return info


def vocabulary(s: "Store") -> list[dict]:
    """Tags already in use, commonest first — the menu the agent picks from."""
    return s.q("""MATCH (p:Project)-[:TAGGED]->(t:Tag)
                  RETURN t.name AS tag, count(DISTINCT p) AS projects
                  ORDER BY projects DESC, tag""")


def enclosing(s: "Store", project_id: str) -> list[dict]:
    """Projects that physically contain this one, outermost first.

    Containment needs no stored edge: `Project.id` is an absolute path, so a
    monorepo root is a path prefix of its modules. Deriving it from the key
    means it is always correct and never needs maintaining.
    """
    sep = os.sep
    rows = s.q("MATCH (p:Project) RETURN p.id AS id, p.name AS name")
    out = [r for r in rows if project_id.startswith(r["id"].rstrip(sep) + sep)]
    return sorted(out, key=lambda r: len(r["id"]))


def contained(s: "Store", project_id: str) -> list[dict]:
    """Projects physically inside this one — the modules of a monorepo."""
    sep = os.sep
    rows = s.q("MATCH (p:Project) RETURN p.id AS id, p.name AS name")
    return sorted((r for r in rows
                   if r["id"].startswith(project_id.rstrip(sep) + sep)),
                  key=lambda r: r["id"])


def same_tree(s: "Store", project_id: str) -> set[str]:
    """This project plus everything above and below it in the filesystem.

    These share a codebase, so they are structure rather than precedent: a
    module and its parent trivially share tags, and counting them as "closest
    projects" would crowd out genuinely comparable work elsewhere.
    """
    return ({project_id}
            | {r["id"] for r in enclosing(s, project_id)}
            | {r["id"] for r in contained(s, project_id)})


def effective_tags(s: "Store", info: dict) -> list[str]:
    """A module's own tags plus those of the projects containing it.

    A repo tagged `monorepo, internal` lends those to every module inside it —
    they are true of the module too, and they are how the module finds kin in
    other repos with the same shape.
    """
    tags = set(info["tags"])
    for anc in enclosing(s, info["id"]):
        tags |= set(tags_of(s, anc["id"]))
    return sorted(tags)


def neighbours(s: "Store", info: dict, min_shared: int = 1) -> list[dict]:
    """Other projects ranked by how many tags they share with this one.

    Overlap replaces exact type equality: a project sharing four tags is closer
    kin than one sharing a single generic tag, and the caller can see which.
    """
    tags = effective_tags(s, info)
    if not tags:
        return []
    skip = same_tree(s, info["id"])
    rows = s.q("""MATCH (p:Project)-[:TAGGED]->(t:Tag)
                  WHERE t.name IN $tags
                  RETURN p.id AS id, p.name AS name,
                         collect(DISTINCT t.name) AS shared,
                         count(DISTINCT t) AS n
                  ORDER BY n DESC, name""", {"tags": tags})
    return [r for r in rows if r["n"] >= min_shared and r["id"] not in skip]


def classify_prompt(s: "Store", info: dict) -> str:
    """What to tell the agent when a project carries no tags yet."""
    known = vocabulary(s)
    lines = ["  no tags yet, so no precedent can be matched to this project.",
             f"  top-level contents: {info['contents'] or '(empty)'}"]
    if known:
        lines.append("  tags already in use: "
                     + ", ".join(f"{r['tag']} ({r['projects']})" for r in known))
        lines.append("  reuse these where they fit — precedent is ranked by how"
                     " many tags two projects share.")
    lines.append("  classify it from the project's contents, then tag it:")
    lines.append(f"    precedent.py tag --project {info['id']} --add backend,java,distributed")
    return "\n".join(lines)


def close_matches(known: list[str], name: str) -> list[str]:
    """Existing tags that probably mean the same thing as `name`.

    Two shapes of drift occur and neither is caught naively. Spelling variants
    ("data_pipeline" / "data-pipeline") need edit distance. Abbreviations
    ("tgbot" / "telegram-bot") share no word and score far below any sane
    threshold, but the short form is the long form with letters removed — so
    test for subsequence, anchored on a shared first letter and a length ratio
    that stops "api" matching "data-pipeline".

    Shared-token matching was tried and removed: "telegram-bot" and
    "discord-bot" share "bot" and mean entirely different things.
    """
    import difflib

    def flat(x: str) -> str:
        return re.sub(r"[-_ ]+", "", x)

    def subsequence(short: str, long: str) -> bool:
        it = iter(long)
        return all(c in it for c in short)

    out = []
    for k in known:
        a, b = flat(k), flat(name)
        short, long = (a, b) if len(a) <= len(b) else (b, a)
        abbreviation = (short[:1] == long[:1] and len(short) >= 3
                        and len(short) / len(long) >= 0.35
                        and subsequence(short, long))
        if abbreviation or difflib.SequenceMatcher(None, k, name).ratio() >= 0.6:
            out.append(k)
    return sorted(out)


def asserted_distinct(s: "Store") -> set[tuple[str, str]]:
    """Tag pairs a human already declared to mean different things."""
    out: set[tuple[str, str]] = set()
    if not s.journal.exists():
        return out
    for line in open(s.journal):
        if '"tags_distinct"' not in line:
            continue
        e = json.loads(line)
        if e.get("op") == "tags_distinct":
            out.add(tuple(sorted((e["a"], e["b"]))))
    return out


def refuse_drift(s: "Store", names: list[str], allow_new: bool) -> None:
    """Stop near-duplicate tags being minted.

    Precedent is computed on exact tag strings, so `tgbot` beside `telegram-bot`
    does not merely look untidy — it hides half the history from the other half,
    silently, with no error at the moment it happens. Different agents in
    different sessions reach for different words for the same thing, so this is
    the normal case rather than an edge case. Refusing costs one command.
    """
    known = [r["tag"] for r in vocabulary(s)]
    for name in names:
        if name in known:
            continue
        near = close_matches(known, name)
        if allow_new:
            for other in near:
                s.log("tags_distinct", {"a": name, "b": other})
            continue
        if not near:
            continue
        print(f"refusing to create the tag '{name}': it looks like"
              f" {', '.join(repr(n) for n in near)}, already in use.")
        print("  precedent matches tags exactly, so a near-duplicate hides half"
              " the history from the other half.")
        if known:
            print("  tags in use: " + ", ".join(known))
        print(f"  reuse one:   precedent.py tag --project . --add {near[0]}")
        print(f"  or if it really means something different:")
        print(f"               precedent.py tag --project . --add {name} --new-tag")
        raise SystemExit(2)


def upsert_project(s: Store, info: dict) -> dict:
    s.q("""MERGE (p:Project {id:$id}) SET p.name=$name, p.seen=$seen""",
        {"id": info["id"], "name": info["name"], "seen": today()})
    return info


def attach_tags(s: Store, project_id: str, tags: list[str]) -> None:
    for t in tags:
        s.q("MERGE (t:Tag {name:$n})", {"n": t})
        s.q("""MATCH (p:Project {id:$id}),(t:Tag {name:$n})
               MERGE (p)-[:TAGGED]->(t)""", {"id": project_id, "n": t})


# ------------------------------------------------------------------------- writing

def write_decision(s: Store, d: dict) -> str:
    upsert_project(s, {"id": d["project_id"], "name": d["project_name"]})
    attach_tags(s, d["project_id"], d.get("tags", []))
    s.q("""MERGE (n:Decision {id:$id})
           SET n.title=$title, n.statement=$statement, n.rationale=$rationale,
               n.scope=$scope, n.status='active', n.created=$created""",
        {"id": d["id"], "title": d.get("title", ""),
         "statement": d.get("statement", ""), "rationale": d.get("rationale", ""),
         "scope": d.get("scope", "architecture"),
         "created": d.get("created", today())})
    s.q("""MATCH (n:Decision {id:$id}), (p:Project {id:$project_id})
           MERGE (n)-[:IN_PROJECT]->(p)""", d)

    for topic in d.get("topics", []):
        s.q("MERGE (t:Topic {name:$n})", {"n": topic})
        s.q("""MATCH (n:Decision {id:$id}),(t:Topic {name:$n})
               MERGE (n)-[:ABOUT]->(t)""", {"id": d["id"], "n": topic})
    for rel, names in (("CHOSE", d.get("chose", [])),
                       ("REJECTED", d.get("rejected", []))):
        for name in names:
            s.q("MERGE (o:Option {name:$n})", {"n": name})
            s.q(f"""MATCH (n:Decision {{id:$id}}),(o:Option {{name:$n}})
                    MERGE (n)-[:{rel}]->(o)""", {"id": d["id"], "n": name})

    # A knowing exception. Without recording it, the graph reports DIVERGENCE
    # here forever and the user learns to ignore the warning — which costs more
    # than the warning was ever worth.
    if d.get("despite"):
        s.q("MATCH (n:Decision {id:$id}) SET n.despite=$despite",
            {"id": d["id"], "despite": d["despite"]})
        for other in d.get("diverges_from", []):
            s.q("""MATCH (new:Decision {id:$new}),(old:Decision {id:$old})
                   MERGE (new)-[:DIVERGES_FROM]->(old)""",
                {"new": d["id"], "old": other})

    # Supersede, never overwrite. When you revisit the same call in two years,
    # the old reasoning is the most valuable thing in the graph.
    for old in d.get("supersedes", []):
        s.q("""MATCH (new:Decision {id:$new}),(old:Decision {id:$old})
               SET old.status='superseded'
               MERGE (new)-[:SUPERSEDES]->(old)""", {"new": d["id"], "old": old})
    return d["id"]


def cmd_record(a, s: Store) -> None:
    info = project_info(s, a.project)
    d = {
        "id": a.id or f"{slug(a.title)}-{int(time.time())}",
        "title": a.title,
        "statement": a.statement or a.title,
        "rationale": a.rationale,
        "scope": a.scope,
        "created": today(),
        "project_id": info["id"], "project_name": info["name"],
        "tags": info["tags"],
        "topics": [t.lower() for t in csv(a.topic)],
        "chose": csv(a.chose), "rejected": csv(a.rejected),
        "supersedes": csv(a.supersedes),
        "despite": a.despite,
    }
    if a.despite:
        # Resolve what is being departed from, so the exception is anchored to
        # the actual prior decisions rather than to a topic name.
        d["diverges_from"] = csv(a.diverges_from) or [
            r["id"] for r in s.q(
                """MATCH (o:Decision)-[:ABOUT]->(t:Topic),
                         (o)-[:CHOSE]->(opt:Option),
                         (o)-[:IN_PROJECT]->(p:Project)
                   WHERE t.name IN $topics AND o.status='active'
                     AND p.id <> $pid AND NOT opt.name IN $chose
                   RETURN DISTINCT o.id AS id""",
                {"topics": d["topics"], "pid": info["id"], "chose": d["chose"]})]
    s.log("record", d)
    write_decision(s, d)
    print(f"recorded {d['id']}  [{a.scope}] in {info['name']}"
          f"  tags: {', '.join(info['tags']) or 'none'}")
    if not a.rationale:
        print("  note: no --rationale. Future you will want the why, not the what.")
    if not info["tags"]:
        print("  note: this project has no tags, so the decision will not surface"
              " as precedent anywhere. Tag it: precedent.py tag --project . --add <tags>")


def cmd_regret(a, s: Store) -> None:
    """Mark a choice you repeated as one you now consider a mistake.

    Repetition is what gives precedent its weight, which is exactly the problem
    when the repeated thing was wrong: without this, "you chose mongo in eight
    projects" reads as a strong norm and argues for a ninth. A regret inverts
    that — the eight become the evidence that the lesson is expensive and real.

    Nothing is deleted. The decisions stay, with their rationales, because the
    fact that a reason looked good eight times is the most useful thing here.
    """
    topic, option = a.topic.lower(), a.chose
    hits = s.q("""MATCH (d:Decision)-[:ABOUT]->(:Topic {name:$t}),
                        (d)-[:CHOSE]->(:Option {name:$o}),
                        (d)-[:IN_PROJECT]->(p:Project)
                  WHERE d.status='active'
                  RETURN d.id AS id, d.title AS title, p.name AS project""",
               {"t": topic, "o": option})
    if not hits:
        print(f"no active decision chose '{option}' for '{topic}' — nothing to regret")
        raise SystemExit(2)

    lid = a.id or slug(f"{topic}-{option}-regret")
    payload = {"id": lid, "topic": topic, "option": option,
               "because": a.because, "instead": a.instead,
               "decisions": [h["id"] for h in hits], "created": today()}
    s.log("regret", payload)
    apply_regret(s, payload)

    print(f"regretted '{option}' for {topic} across {len(hits)} decision(s):")
    for h in hits:
        print(f"  {h['project']}: {h['title']}  #{h['id']}")
    print(f"  lesson: {a.because}")
    if a.instead:
        print(f"  now prefer: {a.instead}")
    print("  the decisions are kept, marked 'regretted' — they are the evidence")


def apply_regret(s: Store, e: dict) -> None:
    s.q("""MERGE (l:Lesson {id:$id})
           SET l.statement=$because, l.topic=$topic, l.option=$option,
               l.instead=$instead, l.created=$created""", e)
    for did in e["decisions"]:
        s.q("MATCH (d:Decision {id:$id}) SET d.status='regretted'", {"id": did})
        s.q("""MATCH (l:Lesson {id:$lid}),(d:Decision {id:$did})
               MERGE (l)-[:REGRETS]->(d)""", {"lid": e["id"], "did": did})


def cmd_principle(a, s: Store) -> None:
    p = {"id": a.id, "statement": a.statement, "derived_from": csv(a.derived_from)}
    s.log("principle", p)
    s.q("""MERGE (pr:Principle {id:$id}) SET pr.statement=$statement, pr.created=$created""",
        {**p, "created": today()})
    for did in p["derived_from"]:
        s.q("""MATCH (pr:Principle {id:$id}),(d:Decision {id:$did})
               MERGE (pr)-[:DERIVED_FROM]->(d)""", {"id": a.id, "did": did})
    print(f"principle {a.id}: {a.statement}")


# ------------------------------------------------------------------------ reading

def worth_backfilling(s: "Store", info: dict) -> bool:
    """Is this a real project whose decisions are simply not in the graph yet?

    Two gates, both cheap and both necessary. A version-controlled directory is
    the difference between a project and a download folder. A non-empty graph is
    the difference between a user who forgot this repo and a user who has never
    used the tool and is being sold it on their first session.
    """
    if not (pathlib.Path(info["id"]) / ".git").exists():
        return False
    return s.q("MATCH (d:Decision) RETURN count(d) AS n")[0]["n"] > 0


def cmd_brief(a, s: Store) -> None:
    # Read-only: never writes. A SessionStart hook calls this in every directory
    # the user opens, and writing would litter the graph with empty projects.
    info = project_info(s, a.project)

    here = s.q("""MATCH (d:Decision)-[:IN_PROJECT]->(:Project {id:$pid})
                  OPTIONAL MATCH (d)-[:ABOUT]->(t:Topic)
                  WHERE d.status='active'
                  RETURN d.id AS id, d.title AS title, d.scope AS scope,
                         d.created AS created, d.despite AS despite,
                         collect(DISTINCT t.name) AS topics
                  ORDER BY created DESC LIMIT 25""", {"pid": info["id"]})
    kin = neighbours(s, info, a.min_shared)
    above, below = enclosing(s, info["id"]), contained(s, info["id"])

    inherited = s.q("""MATCH (d:Decision)-[:IN_PROJECT]->(p:Project)
                       OPTIONAL MATCH (d)-[:ABOUT]->(t:Topic)
                       WHERE p.id IN $ids AND d.status='active'
                       RETURN d.id AS id, d.title AS title, d.scope AS scope,
                              p.name AS project, collect(DISTINCT t.name) AS topics
                       ORDER BY project""",
                    {"ids": [r["id"] for r in above]}) if above else []

    # --only-if-relevant lets a SessionStart hook call this unconditionally in
    # every directory: staying silent is decided here, on the data, rather than
    # by the caller grepping this output for phrases that later change.
    if a.only_if_relevant and not here and not kin and not inherited and not below:
        # Silence here would be self-defeating: a project with history and
        # nothing recorded is precisely the one worth backfilling, and it is
        # the only case where nobody is ever prompted to do it.
        if worth_backfilling(s, info):
            print(f"project: {info['name']}   nothing recorded here yet.")
            print(f"  contents: {info['contents']}")
            print("  it has history the graph cannot see. Offer /precedent-analyze"
                  " — read the manifests, CI and docs, and record the decisions"
                  " already visible in them. Offer once; do not push it.")
        return

    tags = effective_tags(s, info)
    own = set(info["tags"])
    shown = ", ".join(t if t in own else f"{t}*" for t in tags) or "none"
    print(f"project: {info['name']}   tags: {shown}")
    if len(tags) > len(own):
        print("  (* inherited from an enclosing project)")

    print(f"\n== decided here ({len(here)}) ==")
    for r in here:
        print(f"  [{r['scope']}] {r['title']}"
              f"  ({','.join(filter(None, r['topics'])) or '-'})  #{r['id']}")
        if r.get("despite"):
            print(f"       knowing exception: {r['despite']}")

    if inherited:
        # A monorepo's CI, release and licensing decisions are true of every
        # module in it. Recorded once at the root, they have to reach here.
        print(f"\n== inherited from enclosing projects ({len(inherited)}) ==")
        for r in inherited:
            print(f"  [{r['scope']}] {r['title']}  (from {r['project']})  #{r['id']}")

    if below:
        print(f"\n== modules inside this project ({len(below)}) ==")
        for r in below:
            mt = ", ".join(tags_of(s, r["id"])) or "untagged"
            n = s.q("""MATCH (d:Decision)-[:IN_PROJECT]->(:Project {id:$id})
                       WHERE d.status='active' RETURN count(d) AS n""",
                    {"id": r["id"]})[0]["n"]
            print(f"  {r['name']:<20} {mt}  ({n} decision(s))")
        print("  record against the module a decision is about, not the root")

    if not tags:
        print("\n== precedent ==")
        print(classify_prompt(s, info))
        return

    print(f"\n== closest projects ({len(kin)}) ==")
    for r in kin[:8]:
        print(f"  {r['name']:<20} {r['n']} shared: {', '.join(sorted(r['shared']))}")
    if not kin:
        print("  none share a tag with this project yet")

    ids = [r["id"] for r in kin]
    peers = s.q("""MATCH (d:Decision)-[:IN_PROJECT]->(p:Project),
                         (d)-[:ABOUT]->(tp:Topic)
                   OPTIONAL MATCH (d)-[:CHOSE]->(o:Option)
                   WHERE p.id IN $ids AND d.status='active'
                   RETURN tp.name AS topic, collect(DISTINCT o.name) AS opts,
                          collect(DISTINCT p.name) AS projects, count(DISTINCT p) AS n
                   ORDER BY n DESC LIMIT 20""", {"ids": ids}) if ids else []
    print(f"\n== precedent from those projects ({len(peers)}) ==")
    for r in peers:
        print(f"  {r['topic']}: {','.join(filter(None, r['opts'])) or '-'}"
              f"   [{r['n']}x: {','.join(r['projects'])}]")

    lessons = s.q("""MATCH (l:Lesson)
                     RETURN l.id AS id, l.topic AS topic, l.option AS option,
                            l.statement AS stmt, l.instead AS instead
                     ORDER BY l.created""")
    if lessons:
        print("\n== lessons learned the hard way ==")
        for r in lessons:
            tail = f"  -> now prefer {r['instead']}" if r["instead"] else ""
            print(f"  {r['topic']}: not {r['option']} — {r['stmt']}{tail}  #{r['id']}")

    prins = s.q("MATCH (pr:Principle) RETURN pr.id AS id, pr.statement AS stmt ORDER BY pr.created")
    if prins:
        print("\n== standing principles ==")
        for r in prins:
            print(f"  {r['stmt']}  #{r['id']}")


def cmd_check(a, s: Store) -> None:
    topic = a.topic.lower()
    rows = s.q("""MATCH (d:Decision)-[:ABOUT]->(:Topic {name:$t}),
                        (d)-[:IN_PROJECT]->(p:Project)
                  OPTIONAL MATCH (d)-[:CHOSE]->(c:Option)
                  OPTIONAL MATCH (d)-[:REJECTED]->(r:Option)
                  OPTIONAL MATCH (p)-[:TAGGED]->(pt:Tag)
                  RETURN d.id AS id, d.title AS title, d.rationale AS why,
                         d.status AS status, d.created AS created,
                         p.name AS pname, collect(DISTINCT pt.name) AS ptags,
                         collect(DISTINCT c.name) AS chose,
                         collect(DISTINCT r.name) AS rejected
                  ORDER BY created DESC LIMIT 20""", {"t": topic})
    print(f"== prior decisions about '{topic}' ({len(rows)}) ==")
    for r in rows:
        mark = {"active": "", "superseded": "  (superseded)",
                "regretted": "  (REGRETTED — see verdict below)"}.get(r["status"], "")
        tags = ",".join(sorted(filter(None, r["ptags"]))) or "untagged"
        print(f"  {r['pname']} [{tags}]{mark}: {r['title']}")
        print(f"     chose: {','.join(filter(None, r['chose'])) or '-'}"
              f"   rejected: {','.join(filter(None, r['rejected'])) or '-'}   #{r['id']}")
        if r["why"]:
            print(f"     why: {r['why']}")
    if not rows:
        print("  nothing recorded — this is new ground")

    if not a.chose:
        return

    # Two things are worth interrupting a human for: reviving something they
    # already rejected, and quietly diverging from their own settled norm.
    print(f"\n== verdict for choosing '{a.chose}' ==")
    regrets = s.q("""MATCH (l:Lesson {topic:$t, option:$o})
                     OPTIONAL MATCH (l)-[:REGRETS]->(d:Decision)
                     RETURN l.statement AS why, l.instead AS instead,
                            count(d) AS n""", {"t": topic, "o": a.chose})
    regrets = [r for r in regrets if r["why"]]
    for r in regrets:
        # Loudest verdict there is: not "you usually do otherwise" but "you did
        # exactly this, repeatedly, and concluded it was a mistake".
        print(f"  REGRET: you chose this in {r['n']} project(s) and later"
              f" concluded it was a mistake —")
        print(f"     \"{r['why']}\"")
        if r["instead"]:
            print(f"     you now prefer: {r['instead']}")
    revived = s.q("""MATCH (d:Decision)-[:ABOUT]->(:Topic {name:$t}),
                           (d)-[:REJECTED]->(:Option {name:$o}),
                           (d)-[:IN_PROJECT]->(p:Project)
                     RETURN p.name AS pname, d.title AS title, d.rationale AS why""",
                  {"t": topic, "o": a.chose})
    for r in revived:
        print(f"  CONFLICT: rejected in {r['pname']} ({r['title']})"
              f" — {r['why'] or 'no rationale recorded'}")

    norm = s.q("""MATCH (d:Decision)-[:ABOUT]->(:Topic {name:$t}),
                        (d)-[:CHOSE]->(o:Option),
                        (d)-[:IN_PROJECT]->(p:Project)
                  WHERE d.status='active' AND o.name <> $o
                  RETURN o.name AS other, count(DISTINCT p) AS n
                  ORDER BY n DESC LIMIT 3""", {"t": topic, "o": a.chose})
    ack = s.q("""MATCH (d:Decision)-[:ABOUT]->(:Topic {name:$t}),
                       (d)-[:IN_PROJECT]->(:Project {id:$pid})
                 WHERE d.status='active' AND d.despite IS NOT NULL
                 RETURN d.despite AS why, d.title AS title""",
              {"t": topic, "pid": str(pathlib.Path(a.project).resolve())})
    diverged = [r for r in norm if r["n"] >= 2]
    for r in diverged:
        if ack:
            # Already argued out, in this project, on the record. Repeating the
            # warning here is how a useful signal becomes noise.
            print(f"  DIVERGENCE from '{r['other']}' ({r['n']} projects) —"
                  f" acknowledged here: \"{ack[0]['why']}\"")
        else:
            print(f"  DIVERGENCE: you chose '{r['other']}' for this in {r['n']} other projects")

    violated = s.q("""MATCH (pr:Principle) WHERE pr.statement CONTAINS $t
                      RETURN pr.id AS id, pr.statement AS stmt""", {"t": topic})
    for r in violated:
        print(f"  PRINCIPLE in play: {r['stmt']}  #{r['id']}")

    if not revived and not diverged and not regrets:
        print("  clear — no rejection history, no regret, no divergence from your norm")


def cmd_suggest(a, s: Store) -> None:
    info = project_info(s, a.project)
    print(f"project: {info['name']}   tags: {', '.join(info['tags']) or 'none'}")

    if not info["tags"]:
        print()
        print(classify_prompt(s, info))
        return

    kin = neighbours(s, info, a.min_shared)
    ids = [r["id"] for r in kin]
    peers = s.q("""MATCH (d:Decision)-[:IN_PROJECT]->(p:Project),
                         (d)-[:ABOUT]->(tp:Topic)
                   OPTIONAL MATCH (d)-[:CHOSE]->(o:Option)
                   WHERE p.id IN $ids AND d.status='active'
                   RETURN tp.name AS topic, count(DISTINCT p) AS n,
                          collect(DISTINCT o.name) AS opts
                   ORDER BY n DESC""", {"ids": ids}) if ids else []
    settled = {r["topic"] for r in s.q(
        """MATCH (d:Decision)-[:IN_PROJECT]->(:Project {id:$pid}),
                 (d)-[:ABOUT]->(t:Topic) RETURN DISTINCT t.name AS topic""",
        {"pid": info["id"]})}
    gaps = [r for r in peers if r["topic"] not in settled and r["n"] >= a.min_projects]
    print(f"\n== undecided here, settled in {a.min_projects}+ comparable projects ({len(gaps)}) ==")
    for r in gaps:
        print(f"  {r['topic']}: you usually pick"
              f" {','.join(filter(None, r['opts'])) or '-'}  ({r['n']} projects)")
    if not gaps:
        print("  none — either nothing comparable is recorded, or this project is covered")

    reps = s.q("""MATCH (d:Decision)-[:ABOUT]->(t:Topic), (d)-[:CHOSE]->(o:Option),
                        (d)-[:IN_PROJECT]->(p:Project)
                  WHERE d.status='active'
                  RETURN t.name AS topic, o.name AS opt, count(DISTINCT p) AS n
                  ORDER BY n DESC LIMIT 10""")
    known = {r["id"] for r in s.q("MATCH (pr:Principle) RETURN pr.id AS id")}
    cands = [r for r in reps if r["n"] >= a.min_principle
             and slug(f"{r['topic']}-{r['opt']}") not in known]
    print(f"\n== principle candidates ({len(cands)}) ==")
    for r in cands:
        pid = slug(f"{r['topic']}-{r['opt']}")
        print(f"  '{r['topic']} -> {r['opt']}' holds in {r['n']} projects")
        print(f"     promote: precedent.py principle --id {pid}"
              f" --statement \"For {r['topic']}, use {r['opt']}.\"")


# -------------------------------------------------------------------- maintenance

def cmd_tag(a, s: Store) -> None:
    """Show the tag vocabulary, or change this project's tags.

    Tags are free-form: no fixed list can anticipate every kind of thing someone
    builds. But precedent is ranked by exact tag overlap, so a near-duplicate
    hides history rather than merely looking untidy — hence the vocabulary is
    printed before anything is coined, and `refuse_drift` blocks the obvious
    collisions.
    """
    if a.merge:
        # Drift happens anyway — through --new-tag, through tags minted before
        # this guard existed, through two agents tagging at the same moment.
        # Repair has to be one command, or the graph stays split.
        if not a.into:
            print("--merge needs --into <existing tag>"); raise SystemExit(2)
        moved = s.q("""MATCH (p:Project)-[:TAGGED]->(:Tag {name:$f})
                       RETURN count(DISTINCT p) AS n""", {"f": a.merge})[0]["n"]
        if not moved:
            print(f"no projects carry the tag '{a.merge}'"); raise SystemExit(2)
        s.log("tag_merge", {"from": a.merge, "to": a.into})
        apply_tag_merge(s, a.merge, a.into)
        print(f"merged '{a.merge}' into '{a.into}' — {moved} project(s) retagged")
        return

    info = project_info(s, a.project)
    add, remove = csv(a.add), csv(a.remove)

    if not add and not remove:
        known = vocabulary(s)
        print("== tags already in use ==")
        for r in known:
            print(f"  {r['tag']:<20} {r['projects']} project(s)")
        if not known:
            print("  (none yet — these will be the first)")
        print(f"\nthis project: {info['name']}")
        print(f"tags:         {', '.join(info['tags']) or 'none'}")
        print(f"contents:     {info['contents'] or '(empty)'}")
        print("add some with: precedent.py tag --project . --add backend,java,distributed")
        return

    refuse_drift(s, add, a.new_tag)     # before any write, exits non-zero
    upsert_project(s, info)
    if add:
        s.log("project_tags", {"project_id": info["id"], "name": info["name"],
                               "add": add})
        attach_tags(s, info["id"], add)
    for t in remove:
        s.log("project_tags", {"project_id": info["id"], "name": info["name"],
                               "remove": [t]})
        s.q("""MATCH (:Project {id:$id})-[r:TAGGED]->(:Tag {name:$n}) DELETE r""",
            {"id": info["id"], "n": t})
    print(f"{info['name']} tags: {', '.join(tags_of(s, info['id'])) or 'none'}")


def apply_tag_merge(s: Store, frm: str, to: str) -> None:
    s.q("MERGE (t:Tag {name:$n})", {"n": to})
    s.q("""MATCH (p:Project)-[r:TAGGED]->(:Tag {name:$f})
           DELETE r""", {"f": frm}) if False else None
    for r in s.q("""MATCH (p:Project)-[:TAGGED]->(:Tag {name:$f})
                    RETURN p.id AS id""", {"f": frm}):
        s.q("""MATCH (p:Project {id:$id}),(t:Tag {name:$to})
               MERGE (p)-[:TAGGED]->(t)""", {"id": r["id"], "to": to})
    s.q("""MATCH (:Project)-[r:TAGGED]->(:Tag {name:$f}) DELETE r""", {"f": frm})
    s.q("""MATCH (t:Tag {name:$f}) DETACH DELETE t""", {"f": frm})


def cmd_maintain(a, s: Store) -> None:
    contradictions = s.q("""MATCH (d:Decision)-[:ABOUT]->(t:Topic),
                                  (d)-[:CHOSE]->(o:Option),
                                  (d)-[:IN_PROJECT]->(p:Project)
                            WHERE d.status='active'
                            RETURN p.name AS pname, t.name AS topic,
                                   collect(DISTINCT o.name) AS opts""")
    clashes = [r for r in contradictions if len(r["opts"]) > 1]
    print(f"== contradictions: same project, same topic, two live answers ({len(clashes)}) ==")
    for r in clashes:
        print(f"  {r['pname']}/{r['topic']}: {','.join(r['opts'])} — supersede one")

    tags = [r["tag"] for r in vocabulary(s)]
    pairs = {tuple(sorted((t, n))) for t in tags for n in close_matches(tags, t) if n != t}
    pairs -= asserted_distinct(s)
    print(f"\n== tags that may mean the same thing ({len(pairs)}) ==")
    if not pairs:
        print("  none")
    for a_, b_ in sorted(pairs):
        print(f"  '{a_}' / '{b_}' — merge with: precedent.py tag --merge {b_} --into {a_}")

    untagged = s.q("""MATCH (p:Project)
                      WHERE NOT EXISTS { MATCH (p)-[:TAGGED]->(:Tag) }
                      RETURN p.name AS name""")
    print(f"\n== untagged projects, invisible to precedent ({len(untagged)}) ==")
    for r in untagged:
        print(f"  {r['name']}")

    projects = s.q("MATCH (p:Project) RETURN DISTINCT p.id AS id, p.name AS name")
    gone = [r for r in projects if not pathlib.Path(r["id"]).exists()]
    print(f"\n== projects whose path no longer exists ({len(gone)}) ==")
    for r in gone:
        print(f"  {r['name']}  ({r['id']})")

    orphans = s.q("""MATCH (n) WHERE (n:Topic OR n:Option)
                       AND NOT EXISTS { MATCH (n)<--() }
                     RETURN count(n) AS n""")[0]["n"]
    print(f"\n== orphan topic/option nodes: {orphans} ==")
    if a.apply and orphans:
        s.q("""MATCH (n) WHERE (n:Topic OR n:Option)
                 AND NOT EXISTS { MATCH (n)<--() } DETACH DELETE n""")
        print("  deleted")

    d = s.q("MATCH (d:Decision) RETURN count(d) AS n")[0]["n"]
    p = s.q("MATCH (p:Project) RETURN count(p) AS n")[0]["n"]
    lines = sum(1 for _ in open(s.journal)) if s.journal.exists() else 0
    print(f"\ngraph: {d} decisions across {p} projects | journal: {lines} entries")
    print(f"home:  {s.home}")


def cmd_rebuild(a, s: Store) -> None:
    """Replay the journal into a fresh graph. The escape hatch that makes a
    young graph engine an acceptable dependency."""
    if not s.journal.exists():
        print("no journal — nothing to rebuild from")
        return
    s.q("MATCH (n) DETACH DELETE n")
    n, skipped = 0, []
    for lineno, line in enumerate(open(s.journal), 1):
        line = line.strip()
        if not line:
            continue
        # One unreadable line must not cost the whole graph: the journal is the
        # source of truth and it is append-only, so a bad entry is history, not
        # something to repair by hand mid-replay.
        try:
            e = json.loads(line)
        except json.JSONDecodeError as exc:
            skipped.append(f"line {lineno}: unparseable ({exc.msg})")
            continue
        try:
            replay_entry(s, e)
        except Exception as exc:
            skipped.append(f"line {lineno}: {e.get('op', '?')} — {type(exc).__name__}: {exc}")
            continue
        n += 1
    print(f"rebuilt {n} journal entries into {s.db_path}")
    if skipped:
        print(f"skipped {len(skipped)} unusable entr"
              f"{'y' if len(skipped) == 1 else 'ies'}:")
        for msg in skipped:
            print(f"  {msg}")
    return


def replay_entry(s: Store, e: dict) -> None:
    if e["op"] == "record":
        if "tags" not in e and e.get("project_type") not in (None, "", "unknown",
                                                             "unclassified"):
            # Pre-tag journal format carried one type string. Migrate it to a
            # single tag — but never turn the old "we don't know" placeholders
            # into a real tag that projects could then match on.
            e["tags"] = [e["project_type"]]
        write_decision(s, e)
    elif e["op"] == "tags_distinct":
        pass  # advisory bookkeeping, read straight from the journal
    elif e["op"] == "tag_merge":
        apply_tag_merge(s, e["from"], e["to"])
    elif e["op"] in ("project_tags", "project_type"):
        # project_type is the pre-tag journal format: one type string per
        # project. Replaying it as a single tag migrates old graphs on the
        # first rebuild, with no separate migration step to forget.
        upsert_project(s, {"id": e["project_id"], "name": e["name"]})
        add = e.get("add") or ([e["type"]] if e.get("type") else [])
        attach_tags(s, e["project_id"], add)
        for t in e.get("remove", []):
            s.q("""MATCH (:Project {id:$id})-[r:TAGGED]->(:Tag {name:$n}) DELETE r""",
                {"id": e["project_id"], "n": t})
    elif e["op"] == "regret":
        apply_regret(s, e)
    elif e["op"] == "principle":
        s.q("""MERGE (pr:Principle {id:$id})
               SET pr.statement=$statement, pr.created=$ts""", e)
        for did in e.get("derived_from", []):
            s.q("""MATCH (pr:Principle {id:$id}),(d:Decision {id:$did})
                   MERGE (pr)-[:DERIVED_FROM]->(d)""", {"id": e["id"], "did": did})
    else:
        # An op this version does not know: report it rather than counting a
        # silent no-op as a successful replay.
        raise ValueError(f"unknown journal op {e.get('op')!r}")


def relocate(target: pathlib.Path, default: pathlib.Path = DEFAULT_HOME) -> str:
    """Keep the store somewhere else, and symlink the default path at it.

    The default path is wired into a dozen places that never see a flag — the
    SessionStart hook, every slash command, every `uv run precedent.py` typed by
    hand. A symlink moves the bytes without touching any of them, which an
    env var cannot do: `PRECEDENT_HOME` is unset in the hook's environment and
    in every shell the user did not export it from.
    """
    import shutil

    target.mkdir(parents=True, exist_ok=True)
    if target == default:
        return f"store: {target}  (the default location)"

    if default.is_symlink():
        # A symlink carries no data, so repointing loses nothing — the old
        # store stays where it is, intact, in case it was the wrong move.
        old = os.readlink(default)
        default.unlink()
        default.symlink_to(target, target_is_directory=True)
        return f"store: {target}\n  {default} now points here (was {old}, left untouched)"

    if default.exists():
        if not default.is_dir():
            raise SystemExit(f"{default} exists and is not a directory; move it aside first")
        # `.lock` is recreated on every run and never carries state.
        payload = [f for f in default.iterdir() if f.name != ".lock"]
        occupied = [f for f in target.iterdir() if f.name != ".lock"]
        if payload and occupied:
            raise SystemExit(
                f"both {default} and {target} hold a store; refusing to merge.\n"
                f"  merging is a journal concatenation, so do it deliberately:\n"
                f"    cat {default}/journal.jsonl >> {target}/journal.jsonl\n"
                f"    mv {default} {default}.bak\n"
                f"  then re-run this, and `precedent.py rebuild`.")
        for f in payload:
            shutil.move(str(f), str(target / f.name))
        shutil.rmtree(default)
        moved = f"  moved {len(payload)} file(s) from the default location\n" if payload else ""
    else:
        moved = ""

    default.parent.mkdir(parents=True, exist_ok=True)
    default.symlink_to(target, target_is_directory=True)
    return f"store: {target}\n{moved}  {default} -> {target}"


def cmd_init(a) -> None:
    """Runs without a Store: opening one would create the default directory
    that this command may be about to replace with a symlink."""
    if not a.location:
        d = DEFAULT_HOME
        if d.is_symlink():
            print(f"store: {d.resolve()}  (via symlink at {d})")
        elif d.exists():
            print(f"store: {d}  (the default location)")
        else:
            print(f"no store yet; it will be created at {d}")
        if os.environ.get("PRECEDENT_HOME"):
            print(f"  note: PRECEDENT_HOME is set to {os.environ['PRECEDENT_HOME']},"
                  " which overrides the above for this shell only —"
                  " the SessionStart hook will not see it.")
        return
    print(relocate(pathlib.Path(a.location).expanduser().resolve()))


def cmd_cypher(a, s: Store) -> None:
    for row in s.q(a.query, json.loads(a.params) if a.params else None):
        print(row)


def export_to(s: Store, out: pathlib.Path) -> pathlib.Path:
    """Write the graph where grafeo-server can read it.

    The live store is WAL-only: graph.db holds a wal/ directory and no
    data.grafeo, so a server pointed straight at it reports an empty database
    and helpfully creates a second, real one next to yours. save() materialises
    the file, under the <data-dir>/<name>/ layout the server looks for.
    """
    out.mkdir(parents=True, exist_ok=True)
    (out / "default").mkdir(exist_ok=True)
    s.db.save(str(out / "default" / "data.grafeo"))
    return out


def cmd_export(a, s: Store) -> None:
    out = export_to(s, pathlib.Path(a.out or pathlib.Path(a.home) / "export").expanduser().resolve())
    user = "" if os.name == "nt" else " --user $(id -u):$(id -g)"
    print(f"exported: {out}")
    print("serve it read-only — two writers on one embedded db silently drop writes:")
    print(f"  docker run --rm -p 7474:7474{user} \\")
    print(f"    -v {out}:/data grafeo/grafeo-server:latest --data-dir /data --read-only")
    print("  then open http://localhost:7474")
    print("re-run this after recording, the snapshot is a copy and does not follow the store")


def _check_helpers() -> None:
    assert slug("Use Postgres, not Mongo!") == "use-postgres-not-mongo"
    assert csv(" a, b ,,c ") == ["a", "b", "c"]


def _check_detect_project() -> None:
    assert detect_project(pathlib.Path("/nonexistent"))["contents"] == ""

    tmp = pathlib.Path("/tmp/precedent-selftest-proj")
    tmp.mkdir(exist_ok=True)
    # Listing, not whitelisting: an unheard-of build file must still show up.
    (tmp / "build.zig").write_text("// zig")
    (tmp / "shard.yml").write_text("# crystal")
    (tmp / "src").mkdir(exist_ok=True)
    got = detect_project(tmp)
    assert "build.zig" in got["contents"], got
    assert "shard.yml" in got["contents"], got
    assert "src/" in got["contents"], got
    (tmp / "node_modules").mkdir(exist_ok=True)
    assert "node_modules" not in detect_project(tmp)["contents"]
    (tmp / "node_modules").rmdir()
    (tmp / "build.zig").unlink(); (tmp / "shard.yml").unlink(); (tmp / "src").rmdir()
    tmp.rmdir()


def _check_drift() -> None:
    # Type-drift detection is the one piece of real heuristic logic here, and a
    # miss silently partitions the graph. Both shapes of drift, and the
    # near-misses that must NOT trip it.
    for a_, b_ in [("tg-bot", "telegram-bot"), ("tgbot", "telegram-bot"),
                   ("tg_bot", "telegram-bot"), ("telegrambot", "telegram-bot"),
                   ("backend-apis", "backend-api"), ("webfrontend", "web-frontend"),
                   ("data_pipeline", "data-pipeline"),
                   ("kotlin-telegram-bot", "telegram-bot")]:
        assert close_matches([b_], a_) == [b_], f"drift not caught: {a_} ~ {b_}"
    for a_, b_ in [("api", "data-pipeline"), ("cli", "library"), ("ml", "mobile"),
                   ("telegram-bot", "discord-bot"), ("backend-api", "web-frontend"),
                   ("cli", "desktop"), ("game", "gradle-plugin")]:
        assert close_matches([b_], a_) == [], f"false drift: {a_} ~ {b_}"


def _check_projects(s: Store) -> None:
    # Tags are a set, and overlap is what ranks precedent.
    proj = "/tmp/precedent-selftest-tags-proj"
    upsert_project(s, {"id": proj, "name": "selftest"})
    attach_tags(s, proj, ["selftest-backend", "selftest-java"])
    assert set(tags_of(s, proj)) == {"selftest-backend", "selftest-java"}
    other = "/tmp/precedent-selftest-other"
    upsert_project(s, {"id": other, "name": "other"})
    attach_tags(s, other, ["selftest-java"])
    kin = neighbours(s, {"id": other, "tags": ["selftest-java"]})
    assert any(k["id"] == proj and k["n"] == 1 for k in kin), kin
    kin2 = neighbours(s, {"id": other, "tags": ["selftest-java"]}, min_shared=2)
    assert kin2 == [], "min_shared must exclude weakly-related projects"

    # Containment is derived from the path key, so a monorepo needs no schema.
    root, mod = "/tmp/precedent-selftest-root", "/tmp/precedent-selftest-root/mod"
    sibling = "/tmp/precedent-selftest-root-elsewhere"   # prefix-similar but NOT inside
    for pid, name in ((root, "root"), (mod, "mod"), (sibling, "elsewhere")):
        upsert_project(s, {"id": pid, "name": name})
    attach_tags(s, root, ["selftest-monorepo"])
    attach_tags(s, mod, ["selftest-java"])
    attach_tags(s, sibling, ["selftest-java"])
    assert [r["id"] for r in enclosing(s, mod)] == [root], enclosing(s, mod)
    assert [r["id"] for r in contained(s, root)] == [mod], contained(s, root)
    assert enclosing(s, sibling) == [], "a shared name prefix is not containment"
    assert effective_tags(s, {"id": mod, "tags": ["selftest-java"]}) \
        == ["selftest-java", "selftest-monorepo"], "a module inherits enclosing tags"
    kin3 = {r["id"] for r in neighbours(s, {"id": mod, "tags": ["selftest-java"]})}
    assert sibling in kin3, "comparable work outside the tree must count as kin"
    assert root not in kin3, "the enclosing project is structure, not precedent"
    assert mod not in kin3, "a project is not its own kin"
    for pid in (proj, other, root, mod, sibling):
        s.q("MATCH (p:Project {id:$id}) DETACH DELETE p", {"id": pid})
    for t in ("selftest-monorepo", "selftest-backend", "selftest-java"):
        s.q("""MATCH (n:Tag {name:$n}) WHERE NOT EXISTS { MATCH (n)<--() }
                 DETACH DELETE n""", {"n": t})


def _check_decisions(s: Store) -> None:
    tmp = pathlib.Path("/tmp/precedent-selftest-proj")
    tmp.mkdir(exist_ok=True)

    s.q("MATCH (n) WHERE n.id STARTS WITH 'selftest' DETACH DELETE n")
    d = {"id": "selftest-1", "title": "T", "statement": "T", "rationale": "r",
         "scope": "architecture", "created": today(),
         "project_id": str(tmp), "project_name": "selftest",
         "tags": ["selftest-backend", "selftest-java"],
         "topics": ["persistence"], "chose": ["postgres"],
         "rejected": ["mongo"], "supersedes": []}
    write_decision(s, d)
    assert s.q("""MATCH (:Decision {id:'selftest-1'})-[:CHOSE]->(o:Option)
                  RETURN o.name AS n""") == [{"n": "postgres"}]
    assert s.q("""MATCH (:Decision {id:'selftest-1'})-[:REJECTED]->(o:Option)
                  RETURN o.name AS n""") == [{"n": "mongo"}]

    # The backfill nudge fires in a repo the graph has never seen, and nowhere
    # else — a bare directory is not a project worth prompting about.
    assert not worth_backfilling(s, {"id": str(tmp)}), "a non-repo must not be nudged"
    (tmp / ".git").mkdir(exist_ok=True)
    assert worth_backfilling(s, {"id": str(tmp)}), "a repo with a populated graph must be"
    (tmp / ".git").rmdir()

    d2 = {**d, "id": "selftest-2", "chose": ["sqlite"], "supersedes": ["selftest-1"]}
    write_decision(s, d2)
    assert s.q("MATCH (d:Decision {id:'selftest-1'}) RETURN d.status AS s") \
        == [{"s": "superseded"}], "supersede must flip the old decision's status"

    # A knowing exception is recorded on the decision, so the warning can be
    # answered once rather than repeated forever.
    d3 = {**d, "id": "selftest-3", "chose": ["sqlite"], "supersedes": [],
          "despite": "selftest reason", "diverges_from": ["selftest-1"]}
    write_decision(s, d3)
    assert s.q("MATCH (d:Decision {id:'selftest-3'}) RETURN d.despite AS w") \
        == [{"w": "selftest reason"}]
    assert s.q("""MATCH (:Decision {id:'selftest-3'})-[:DIVERGES_FROM]->(o:Decision)
                  RETURN o.id AS id""") == [{"id": "selftest-1"}]

    # A regret must invert precedent, not erase it: the decision survives with
    # its rationale, but stops counting as a norm.
    apply_regret(s, {"id": "selftest-lesson", "topic": "persistence",
                     "option": "postgres", "because": "selftest lesson",
                     "instead": "sqlite", "decisions": ["selftest-2"],
                     "created": today()})
    assert s.q("MATCH (d:Decision {id:'selftest-2'}) RETURN d.status AS s") \
        == [{"s": "regretted"}], "regret must mark the decision, not delete it"
    assert s.q("""MATCH (:Lesson {id:'selftest-lesson'})-[:REGRETS]->(d:Decision)
                  RETURN d.id AS id""") == [{"id": "selftest-2"}]
    assert s.q("""MATCH (d:Decision {id:'selftest-2'})-[:CHOSE]->(o:Option)
                  RETURN o.name AS n""") == [{"n": "sqlite"}], \
        "the original choice and its rationale must survive a regret"
    s.q("MATCH (l:Lesson {id:'selftest-lesson'}) DETACH DELETE l")

    s.q("MATCH (n) WHERE n.id STARTS WITH 'selftest' DETACH DELETE n")
    s.q("MATCH (p:Project {id:$id}) DETACH DELETE p", {"id": str(tmp)})
    for name in ("persistence", "postgres", "mongo", "sqlite",
                 "selftest-backend", "selftest-java"):
        s.q("""MATCH (n) WHERE (n:Topic OR n:Option OR n:Tag) AND n.name = $name
                 AND NOT EXISTS { MATCH (n)<--() } DETACH DELETE n""", {"name": name})
    tmp.rmdir()


def _check_relocate() -> None:
    # Relocation moves the only copy of the journal, so every branch is checked.
    import shutil as _sh
    base = pathlib.Path("/tmp/precedent-selftest-home")
    _sh.rmtree(base, ignore_errors=True)
    dflt, tgt = base / "default", base / "elsewhere"
    relocate(tgt, dflt)
    assert dflt.is_symlink() and dflt.resolve() == tgt.resolve(), "fresh default must be linked"

    _sh.rmtree(base); dflt.mkdir(parents=True)
    (dflt / "journal.jsonl").write_text("x\n"); (dflt / ".lock").write_text("")
    relocate(tgt, dflt)
    assert (tgt / "journal.jsonl").read_text() == "x\n", "an existing store must move, not vanish"
    assert dflt.is_symlink(), "and the default path must end up pointing at it"

    (dflt / "journal.jsonl").write_text("y\n")     # writes through the link
    second = base / "second"; second.mkdir()
    relocate(second, dflt)
    assert dflt.resolve() == second.resolve() and (tgt / "journal.jsonl").exists(), \
        "repointing a symlink must leave the old store intact"

    _sh.rmtree(base); dflt.mkdir(parents=True); tgt.mkdir(parents=True)
    (dflt / "journal.jsonl").write_text("a\n"); (tgt / "journal.jsonl").write_text("b\n")
    try:
        relocate(tgt, dflt)
        raise AssertionError("two populated stores must not be merged silently")
    except SystemExit:
        pass
    assert (dflt / "journal.jsonl").read_text() == "a\n"
    _sh.rmtree(base)


def _check_export(s: Store) -> None:
    # The export layout is what the server reads; if save() or the directory
    # shape changes, the UI shows an empty graph and says nothing.
    import shutil as _sh
    exp = pathlib.Path("/tmp/precedent-selftest-export")
    _sh.rmtree(exp, ignore_errors=True)
    export_to(s, exp)
    assert (exp / "default" / "data.grafeo").stat().st_size > 0, "export wrote nothing"
    _sh.rmtree(exp)


def _check_store(s: Store) -> None:
    """The lock must exclude a second process in the modes it claims, and it
    must give up rather than wait forever.

    A lock that silently does not lock is the failure mode grafeo#405
    documents: 120 writes across 6 processes, 60 stored, no errors raised.
    Two lock objects for one path in one process would not prove exclusion —
    filelock returns the same instance for a given path — so every
    proposition here is proved against a forked holder process.

    The contention runs against throwaway paths in a temp directory, not
    s.lock_path: s's own lock is held for the whole selftest command (main()
    enters the Store before dispatching to cmd_selftest), and contending on
    the real path here would mean releasing the lock guarding the open graph
    db for the duration of the check — exactly the hazard the lock exists to
    prevent. A throwaway path proves the same filelock mechanics without
    mutating the store under test. Wiring — that Store built its lock against
    lock_path, in the mode it was asked for — is checked against s._lock's own
    attributes instead, and Store's own acquire path is exercised by two
    throwaway Stores on throwaway homes at the end.

    The timeout is proved behaviourally, not by reading the attribute back.
    acquire_read/acquire_write default to waiting forever and never consult
    the instance timeout — only the read_lock/write_lock context managers do
    — so a Store that configured 30s and then acquired without passing it
    would satisfy an attribute assert while hanging every session start. The
    holder below outlives the contender's timeout twentyfold, so an unenforced
    timeout cannot pass quietly: it either waits out the holder and then
    acquires, which the `else` branch fails on, or it raises Timeout long
    after it was asked to, which LATE fails on.
    """
    assert s.write is True, "selftest mutates the graph; Store must be in write mode"
    assert s._lock.lock_file == str(s.lock_path), "Store did not lock its own path"
    assert s._lock._current_mode == "write", "a writer command must hold a write lock"
    # Documentation of the configured value; the contention below is what
    # proves the mechanism.
    assert s._lock.timeout == 30, "Store lock must have a bounded timeout"

    import subprocess
    import tempfile
    import filelock

    # Holds `mode` on argv[1] until killed, announcing when it actually has it.
    holder_src = (
        "import sys, time, filelock;"
        "l = filelock.ReadWriteLock(sys.argv[1]);"
        "getattr(l, 'acquire_' + sys.argv[2])(10);"
        "print('held', flush=True);"
        "time.sleep(10)")

    def fork_holder(path: pathlib.Path, mode: str) -> subprocess.Popen:
        h = subprocess.Popen([sys.executable, "-c", holder_src, str(path), mode],
                             stdout=subprocess.PIPE, text=True)
        assert h.stdout is not None
        assert h.stdout.readline().strip() == "held", f"{mode} holder never acquired"
        return h

    # Long enough that an unenforced timeout is unmistakable, short enough to
    # keep the selftest quick.
    WAIT = 0.5
    LATE = 5.0

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)

        # A held write lock excludes both a second writer and a reader.
        write_held = tmpdir / "write-held.lock"
        # Contender first: fork_holder asserts, and a holder forked before it
        # would be left sleeping on the temp lock file if that assert fired.
        contender = filelock.ReadWriteLock(str(write_held))
        holder = fork_holder(write_held, "write")
        try:
            for mode in ("write", "read"):
                started = time.monotonic()
                try:
                    getattr(contender, "acquire_" + mode)(WAIT)
                except filelock.Timeout:
                    waited = time.monotonic() - started
                    assert waited < LATE, (
                        f"a {mode} lock waited {waited:.1f}s for a {WAIT}s timeout — "
                        "the timeout is configured but not enforced")
                else:
                    contender.release()
                    raise AssertionError(f"a held write lock let a {mode} lock in")
        finally:
            contender.close()
            holder.kill()
            holder.wait()

        # A held read lock does NOT exclude a second reader. This is the split.
        read_held = tmpdir / "read-held.lock"
        contender = filelock.ReadWriteLock(str(read_held))
        holder = fork_holder(read_held, "read")
        try:
            try:
                contender.acquire_read(WAIT)
            except filelock.Timeout:
                raise AssertionError("readers were serialised against each other")
            contender.release()
        finally:
            contender.close()
            holder.kill()
            holder.wait()

        # Store.__enter__ must PASS the timeout, not merely configure it. The
        # asserts above read the configured value back, which a Store that
        # acquired with no argument would still satisfy while waiting forever.
        # A throwaway Store on a throwaway home, its timeout turned down to
        # WAIT, is entered against a lock a second process already holds for
        # writing: it has to give up, in about its own timeout rather than
        # whenever the holder happens to exit.
        probe = Store(tmpdir / "store", write=False)
        probe._lock.timeout = WAIT
        holder = fork_holder(probe.lock_path, "write")
        try:
            started = time.monotonic()
            try:
                probe.__enter__()
            except SystemExit as exc:
                waited = time.monotonic() - started
                assert waited < LATE, (
                    f"Store waited {waited:.1f}s to fail a {WAIT}s lock — __enter__ "
                    "configures the timeout without passing it to acquire")
                assert "read lock" in str(exc), (
                    f"the lock error must name the mode it wanted, got: {exc}")
            else:
                probe.__exit__()
                raise AssertionError(
                    "Store opened a graph another process holds for writing")
        finally:
            probe._lock.close()
            holder.kill()
            holder.wait()

        # ...and a reader Store must really take a READ lock. Nothing above
        # would notice a __enter__ that ignored self.write and locked
        # exclusively every time: the raw locks prove filelock lets readers
        # share, not that Store asks it to. So enter a reader against a lock
        # another process holds for READING — which only a read lock can join
        # — and confirm the mode it ended up in.
        probe = Store(tmpdir / "shared-store", write=False)
        probe._lock.timeout = WAIT
        holder = fork_holder(probe.lock_path, "read")
        entered = False
        try:
            try:
                probe.__enter__()
                entered = True
            except SystemExit as exc:
                raise AssertionError(
                    f"a reader Store was locked out by another reader: {exc}")
            assert probe._lock._current_mode == "read", (
                "a reader command took a "
                f"{probe._lock._current_mode} lock — the split is not wired up")
        finally:
            if entered:
                probe.__exit__()
            probe._lock.close()
            holder.kill()
            holder.wait()


def _check_grafeo_readers() -> None:
    """Readers must not observe a torn write.

    The read-write split lets readers run while a writer holds the graph open.
    grafeo is v0.5 and its issue #405 documents writers silently dropping
    writes (120 across 6 processes, 60 stored, nothing raised); nothing
    documented reader isolation. So this proves it rather than assuming it:
    a writer process loops writes whose two properties must always agree, and
    readers assert they never see a row where they disagree.

    Deliberately unlocked. The point is what grafeo does when the lock lets
    several processes in, which is exactly what the read lock now permits.

    And they really do collide. A "reader" here reads no decision, but it is
    not passive at the file level: opening a grafeo db appends ~10 bytes to
    its write-ahead log before any query runs, and closing appends more. So
    the shared read lock is a bet that grafeo tolerates concurrent WAL
    appenders against a live writer, and this is where that bet is tested.

    Each reader reopens the db every round. That is not a detail — a reader
    that opens once and loops sees a single frozen value however long the
    writer runs, so it could never observe a tear and the check would pass
    vacuously. Reopening is also the true shape of every precedent command:
    open, query, close. Two independent guards keep the check honest: readers
    must have seen the writer's value advance (more than one distinct value),
    and the writer must still be running when they finish.
    """
    import subprocess
    import tempfile

    WRITE_BUDGET = 3.0   # wall clock, not iterations, so the run is bounded
    READ_BUDGET = 1.2    # ends inside the writer's window, from both sides
    WARMUP = 0.4         # let the node exist before readers look for it
    READERS = 4          # several appenders on one WAL, not just a pair
    STUCK = 5.0          # a reader past this is wedged, not slow
    # WRITE_BUDGET is an upper bound nothing waits on: the writer is killed as
    # soon as the readers are reaped, so raising it costs no wall clock and
    # only widens the margin behind the `writer.poll()` assertion below.

    writer_src = (
        "import sys, time, grafeo\n"
        "g = grafeo.GrafeoDB(sys.argv[1])\n"
        "end = time.monotonic() + float(sys.argv[2])\n"
        "i = 0\n"
        "while time.monotonic() < end:\n"
        "    i += 1\n"
        "    list(g.execute(\"MERGE (n:RWProbe {id:'p'}) SET n.a=$i, n.b=$i "
        "RETURN n.a AS a\", {\"i\": i}))\n"
        "g.close()\n")

    reader_src = (
        "import json, sys, time, grafeo\n"
        "end = time.monotonic() + float(sys.argv[2])\n"
        "seen, torn = set(), []\n"
        "while time.monotonic() < end:\n"
        "    g = grafeo.GrafeoDB(sys.argv[1])\n"
        "    for r in g.execute(\"MATCH (n:RWProbe {id:'p'}) "
        "RETURN n.a AS a, n.b AS b\"):\n"
        "        seen.add(r['a'])\n"
        "        if r['a'] != r['b']:\n"
        "            torn.append([r['a'], r['b']])\n"
        "    g.close()\n"
        "print(json.dumps({'distinct': len(seen), 'torn': torn[:5]}), flush=True)\n")

    with tempfile.TemporaryDirectory() as tmp:
        db = str(pathlib.Path(tmp) / "probe.db")
        writer = subprocess.Popen(
            [sys.executable, "-c", writer_src, db, str(WRITE_BUDGET)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        readers: list[subprocess.Popen] = []
        try:
            time.sleep(WARMUP)
            for _ in range(READERS):
                readers.append(subprocess.Popen(
                    [sys.executable, "-c", reader_src, db, str(READ_BUDGET)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            results = []
            for i, r in enumerate(readers):
                # Bounded like every other wait here. A reader wedged inside
                # g.execute under contention is the very thing this check
                # probes, and it must fail loudly rather than hang the
                # selftest; the finally below reaps it either way.
                try:
                    out, err = r.communicate(timeout=READ_BUDGET + STUCK)
                except subprocess.TimeoutExpired:
                    raise AssertionError(
                        f"reader {i} outlived a {READ_BUDGET:g}s budget by {STUCK:g}s — "
                        "it is stuck in grafeo while a writer holds the graph open")
                assert r.returncode == 0, (
                    f"reader {i} crashed while a writer held the graph open: "
                    f"{err.strip()[-500:]}")
                results.append(json.loads(out))
            if writer.poll() is not None:
                raise AssertionError(
                    "the writer stopped before the readers did, so nothing was read "
                    f"under contention: {writer.communicate()[1].strip()[-500:]}")
            for i, res in enumerate(results):
                assert not res["torn"], (
                    f"reader {i} observed a torn write {res['torn']}: grafeo does not "
                    "isolate a reader from a concurrent writer, so the read lock is unsafe")
                assert res["distinct"] > 1, (
                    f"reader {i} saw {res['distinct']} distinct value(s) — it never watched "
                    "the writer advance, so this check proved nothing")
        finally:
            for r in readers:
                r.kill()
                r.wait()
            writer.kill()
            writer.wait()


def _check_lock_modes() -> None:
    """Every command's lock mode, against a table written out by hand.

    build_parser's `writes=True` fallback covers a subparser that forgets the
    flag. Nothing covers one that sets it wrongly, and the two mistakes are not
    symmetric: a reader marked True is merely slow, a writer marked False
    corrupts the graph the lock exists to protect. This classification is the
    whole risk surface of the read-write split, so it gets an assertion.

    The expected mapping is literal, not derived from the parser. Deriving it
    would track whatever the table says and never fail; written out, changing
    a command's mode means changing it here too, on purpose.
    """
    import argparse

    expected = {
        # read-only in the sense that matters: they settle no decision
        "check": False, "suggest": False, "export": False,
        "maintain": False,      # until --apply; see wants_write_lock
        # writers
        "record": True, "tag": True, "regret": True, "principle": True,
        "rebuild": True, "selftest": True,
        "brief": True,          # the portable-id backfill mutates
        "cypher": True,         # arbitrary query text; CREATE is unknowable up front
        "init": True,           # returns before any Store is opened
    }
    p = build_parser()
    subparsers = [x for x in p._actions if isinstance(x, argparse._SubParsersAction)]
    assert len(subparsers) == 1, "expected exactly one subparser group"
    actual = {name: sp.get_default("writes") for name, sp in subparsers[0].choices.items()}
    assert actual == expected, (
        "command lock modes changed — a writer classified as a reader corrupts "
        f"the graph, so confirm this on purpose:\n  got      {actual}\n  expected {expected}")

    # maintain is the derived case, and the derivation is the thing that can rot.
    assert wants_write_lock(p.parse_args(["maintain"])) is False, "maintain must read"
    assert wants_write_lock(p.parse_args(["maintain", "--apply"])) is True, (
        "maintain --apply deletes nodes and must take the write lock")
    # and the flag still reaches the mode for an ordinary reader and writer
    assert wants_write_lock(p.parse_args(["check", "--topic", "t"])) is False
    assert wants_write_lock(p.parse_args(["record", "--title", "t"])) is True


def cmd_selftest(a, s: Store) -> None:
    """One runnable check over the paths that contain real logic.

    Each check cleans up after itself, so the node count is the invariant
    that catches a check which forgot to.
    """
    before = s.q("MATCH (n) RETURN count(n) AS n")[0]["n"]
    _check_helpers()
    _check_detect_project()
    _check_drift()
    _check_projects(s)
    _check_decisions(s)
    _check_relocate()
    _check_export(s)
    _check_store(s)
    _check_lock_modes()
    _check_grafeo_readers()
    after = s.q("MATCH (n) RETURN count(n) AS n")[0]["n"]
    assert after == before, f"selftest changed node count {before} -> {after}"
    print(f"selftest ok ({before} nodes, unchanged)")


# ---------------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="precedent", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--home", default=str(HOME), help="graph + journal directory")
    # Every subparser declares whether it writes. The fallback is deliberate:
    # a command wrongly treated as a writer is merely slow, one wrongly
    # treated as a reader corrupts the graph.
    p.set_defaults(writes=True)
    sub = p.add_subparsers(dest="cmd", required=True)

    def proj(sp):
        sp.add_argument("--project", default=".")

    r = sub.add_parser("record", help="write a decision")
    r.add_argument("--title", required=True)
    r.add_argument("--statement", default="", help="the decision in one sentence")
    r.add_argument("--rationale", default="", help="why — the part you will want later")
    r.add_argument("--scope", choices=SCOPES, default="architecture")
    r.add_argument("--topic", default="", help="comma-separated, e.g. persistence,auth")
    r.add_argument("--chose", default="", help="comma-separated")
    r.add_argument("--rejected", default="", help="comma-separated; alternatives you turned down")
    r.add_argument("--supersedes", default="", help="comma-separated decision ids")
    r.add_argument("--despite", default="",
                   help="why this project knowingly departs from the precedent")
    r.add_argument("--diverges-from", default="",
                   help="decision ids departed from (inferred from --topic if omitted)")
    r.add_argument("--id", default="")
    proj(r); r.set_defaults(writes=True, fn=cmd_record)

    b = sub.add_parser("brief", help="project type, decisions here, precedent from similar projects")
    b.add_argument("--min-shared", type=int, default=1,
                   help="how many tags a project must share to count as comparable")
    b.add_argument("--only-if-relevant", action="store_true",
                   help="print nothing when this project has no decisions and no comparable projects")
    # A writer despite being the hook-invoked, most-run command: the portable-id
    # backfill mutates, and a read lock can never be promoted mid-run.
    proj(b); b.set_defaults(writes=True, fn=cmd_brief)

    c = sub.add_parser("check", help="precedent for a topic, plus a conflict verdict")
    c.add_argument("--topic", required=True)
    c.add_argument("--chose", default="", help="the option you are leaning toward")
    c.add_argument("--project", default=".",
                   help="used to spot a divergence already acknowledged here")
    c.set_defaults(writes=False, fn=cmd_check)

    g = sub.add_parser("suggest", help="decisions not yet made here, and principle candidates")
    g.add_argument("--min-shared", type=int, default=1,
                   help="how many tags a project must share to count as comparable")
    g.add_argument("--min-projects", type=int, default=2)
    g.add_argument("--min-principle", type=int, default=3)
    proj(g); g.set_defaults(writes=False, fn=cmd_suggest)

    pr = sub.add_parser("principle", help="promote a repeated choice to a standing principle")
    pr.add_argument("--id", required=True)
    pr.add_argument("--statement", required=True)
    pr.add_argument("--derived-from", default="")
    pr.set_defaults(writes=True, fn=cmd_principle)

    tg = sub.add_parser("tag", help="list the tag vocabulary, or change this project's tags")
    tg.add_argument("--project", default=".")
    tg.add_argument("--add", default="", help="comma-separated tags to add")
    tg.add_argument("--remove", default="", help="comma-separated tags to remove")
    tg.add_argument("--new-tag", action="store_true",
                    help="confirm a tag resembling an existing one really means something else")
    tg.add_argument("--merge", default="", help="retag every project carrying this tag")
    tg.add_argument("--into", default="", help="the tag --merge should fold into")
    tg.set_defaults(writes=True, fn=cmd_tag)

    rg = sub.add_parser("regret",
                        help="mark a repeated choice as a mistake, inverting its precedent")
    rg.add_argument("--topic", required=True)
    rg.add_argument("--chose", required=True, help="the option you now consider wrong")
    rg.add_argument("--because", required=True, help="what went wrong — the lesson")
    rg.add_argument("--instead", default="", help="what you would choose now")
    rg.add_argument("--id", default="")
    rg.set_defaults(writes=True, fn=cmd_regret)

    m = sub.add_parser("maintain", help="contradictions, dead projects, orphans, counts")
    m.add_argument("--apply", action="store_true", help="perform the safe cleanups")
    # Reports only, until --apply deletes orphan nodes; wants_write_lock()
    # promotes it then.
    m.set_defaults(writes=False, fn=cmd_maintain)

    sub.add_parser("rebuild", help="replay journal.jsonl into a fresh graph"
                   ).set_defaults(writes=True, fn=cmd_rebuild)

    it = sub.add_parser("init", help="show where the store lives, or move it elsewhere")
    it.add_argument("location", nargs="?",
                    help="directory to keep the store in; the default path is symlinked at it")
    it.set_defaults(writes=True, fn=cmd_init)

    cy = sub.add_parser("cypher", help="escape hatch")
    cy.add_argument("query")
    cy.add_argument("--params", default="")
    # A writer, though it usually reads: the query is arbitrary text, so
    # `cypher "CREATE (...)"` cannot be told from a MATCH before it runs.
    # Serialising a rare interactive escape hatch is the cheap side of that.
    cy.set_defaults(writes=True, fn=cmd_cypher)

    ex = sub.add_parser("export", help="snapshot the graph for grafeo-server and its web UI")
    ex.add_argument("--out", help="where to write the snapshot (default: <store>/export)")
    ex.set_defaults(writes=False, fn=cmd_export)

    sub.add_parser("selftest").set_defaults(writes=True, fn=cmd_selftest)
    return p


def wants_write_lock(a) -> bool:
    """Whether a parsed command needs the exclusive lock.

    Answered before the Store exists, because it has to be: ReadWriteLock
    refuses to promote a read lock to a write lock, deliberately, so a command
    settles which one it wants before it takes either.
    """
    if a.cmd == "maintain":
        # --apply deletes orphan nodes; without it maintain only reports.
        return bool(a.apply)
    return a.writes


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd == "init":
        cmd_init(a)
        return 0
    with Store(pathlib.Path(a.home), write=wants_write_lock(a)) as s:
        a.fn(a, s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
