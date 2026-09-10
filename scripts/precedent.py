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
POINTER = "location"
SCHEMA = 1          # journal line format; bump only on a breaking change


class JournalTooNew(Exception):
    """A journal line written by a newer precedent than this one."""


# --------------------------------------------------------------------------- store

def resolve_home(home: pathlib.Path) -> pathlib.Path:
    """Follow a relocation pointer, once.

    The default path is wired into the SessionStart hook and every slash
    command, and PRECEDENT_HOME is unset in the hook's environment, so an
    env var cannot move the store. A symlink can, but needs Developer Mode
    on Windows. A file holding a path needs neither.

    Exactly one hop: a pointer found inside the target is a stale file, not
    an instruction, and following it is how a relocation loop starts.

    A pointer that is empty, truncated, or otherwise not one absolute path is
    refused rather than followed: write_text is not atomic, so a process
    killed mid-write leaves exactly this on disk, and Path("").expanduser()
    is Path(".") — silently redirecting every later command to whatever the
    current directory happens to be is the wrong failure for the one function
    whose job is finding the only copy of the journal.
    """
    pointer = home / POINTER
    if not pointer.is_file():
        return home
    content = pointer.read_text().strip()
    if not content:
        raise SystemExit(
            f"error: {pointer} is empty (contents: {content!r}). "
            "It should contain exactly one absolute path, written by `init --location`. "
            "Fix or delete it by hand, then retry.")
    target = pathlib.Path(content).expanduser()
    if not target.is_absolute():
        raise SystemExit(
            f"error: {pointer} does not hold an absolute path (contents: {content!r}). "
            "It should contain exactly one absolute path, written by `init --location`. "
            "Fix or delete it by hand, then retry.")
    return target


class Store:
    def __init__(self, home: pathlib.Path = HOME, write: bool = True):
        home.mkdir(parents=True, exist_ok=True)
        self.home = home = resolve_home(home)
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
        # several processes append to that log at once. Measured: 6 concurrent
        # readers, 27662 opens, nothing lost — while 6 concurrent writers lost
        # 79% of their writes in silence (grafeo#405). Concurrent appends are
        # benign, concurrent data writes are not. _check_grafeo_readers keeps
        # that honest; its docstring carries the numbers.
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
            # "v" is spread AFTER **payload, not before: a payload key named
            # "v" must never silently override the schema stamp — that would
            # stop stamping without a visible error, and a later replay could
            # fail to refuse a line it should have refused.
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "op": op, **payload, "v": SCHEMA}) + "\n")
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


def normalise_remote(url: str) -> str | None:
    """Reduce any shape git hands back to `host/owner/repo`.

    Lowercased so two clones agree. That loses case on a local-path remote,
    which is an acceptable trade for an identity that has to match across
    machines.
    """
    url = url.strip()
    if not url:
        return None
    url, had_scheme = re.subn(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", url)  # https:// ssh:// git://
    url = re.sub(r"^[^/@]*@", "", url)                       # git@ or user:token@
    host, sep, path = url.partition("/")
    if ":" in host:
        host, _, extra = host.partition(":")
        if not had_scheme:  # scp-style has no port -- a scheme URL can carry one, scp-style can't
            path = f"{extra}/{path}" if sep else extra
    url = f"{host}/{path}".rstrip("/") if path else host
    if url.endswith(".git"):
        url = url[:-4]
    return url.lower() or None


def portable_id(root: pathlib.Path) -> str | None:
    """The identity that survives a machine, a clone location and an OS.

    None is a fine answer — a project with no remote is correctly
    machine-local. A guessed id is not: it silently merges the histories of
    two unrelated projects, which is the failure a wrong tag causes.
    """
    import subprocess

    def git(*args: str) -> str | None:
        try:
            r = subprocess.run(["git", "-C", str(root), *args],
                               capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    remote = normalise_remote(git("remote", "get-url", "origin") or "")
    if not remote:
        return None
    top = git("rev-parse", "--show-toplevel")
    if not top:
        return remote
    try:
        sub = root.resolve().relative_to(pathlib.Path(top).resolve())
    except ValueError:
        return remote
    # as_posix keeps the key identical on Windows; a backslash here would
    # split the graph exactly the way the native path already does.
    return remote if str(sub) == "." else f"{remote}#/{sub.as_posix()}"


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


def paths_of(row: dict) -> list[str]:
    """Project.paths is newline-delimited rather than a list property.

    Every consumer already filters in Python — containment reads all
    projects and compares there — so a string costs nothing and does not
    depend on the graph engine supporting list properties.
    """
    return [p for p in (row.get("paths") or "").split("\n") if p]


def tags_of(s: "Store", project_id: str) -> list[str]:
    return [r["t"] for r in s.q(
        """MATCH (:Project {id:$id})-[:TAGGED]->(t:Tag) RETURN t.name AS t ORDER BY t""",
        {"id": project_id})]


def project_info(s: "Store", path: str, extra_tags: str = "") -> dict:
    """Identify this directory, resolving it onto the node that already holds it.

    `id` is the graph key — the native path of first sighting, which may be
    another machine's. `path` is this machine's, because containment is
    derived from paths and must be derived from local ones.

    This reads and never writes: `check` and `suggest` hold a shared lock,
    which cannot be promoted. The portable id is written onto the node by
    `upsert_project`, on the commands that already take the write lock.
    """
    info = detect_project(pathlib.Path(path))
    info["path"] = info["id"]
    info["portable"] = portable_id(pathlib.Path(info["path"]))
    if info["portable"]:
        # ORDER BY for the same reason as upsert_project's lookup, and the
        # same one, so a read and the write beside it agree on which node.
        rows = s.q("""MATCH (p:Project {portable:$pp})
                      RETURN p.id AS id, p.paths AS paths ORDER BY p.id LIMIT 1""",
                   {"pp": info["portable"]})
        if rows:
            info["id"], info["paths"] = rows[0]["id"], paths_of(rows[0])
    info.setdefault("paths", [info["path"]])
    info["tags"] = sorted(set(tags_of(s, info["id"])) | set(csv(extra_tags)))
    return info


def vocabulary(s: "Store") -> list[dict]:
    """Tags already in use, commonest first — the menu the agent picks from."""
    return s.q("""MATCH (p:Project)-[:TAGGED]->(t:Tag)
                  RETURN t.name AS tag, count(DISTINCT p) AS projects
                  ORDER BY projects DESC, tag""")


def topic_vocabulary(s: "Store") -> list[dict]:
    """Topics already in use, commonest first — the menu `check` picks from.

    Topics are the key `check` matches on, and matching is exact. The
    caller invents the word fresh each session, so a synonym returns
    nothing; showing the vocabulary is how it corrects itself.
    """
    return s.q("""MATCH (d:Decision)-[:ABOUT]->(t:Topic)
                  WHERE d.status='active'
                  RETURN t.name AS topic, count(DISTINCT d) AS decisions
                  ORDER BY decisions DESC, topic""")


def enclosing(s: "Store", info: dict) -> list[dict]:
    """Projects that physically contain this one, outermost first.

    Containment needs no stored edge: a monorepo root is a path prefix of its
    modules. Deriving it means it is always correct and never needs
    maintaining. It is derived from `paths` rather than from `id`, because
    `id` may be a path recorded on another machine — comparing this machine's
    directory against a Windows key would silently report no containment.

    `os.sep` here is not new platform surgery: every path in `paths` was
    recorded natively, and comparing local paths against the local separator
    is the existing containment semantics.
    """
    sep = os.sep
    here = info["path"]
    rows = s.q("MATCH (p:Project) RETURN p.id AS id, p.name AS name, p.paths AS paths")
    out = []
    for r in rows:
        if r["id"] == info["id"]:
            continue
        # Only the paths that actually contain this one, and only those may
        # order the result. `len(id)` was a depth proxy while every id was a
        # local path; once one node's id is a Windows path it is not a depth
        # at all, and "outermost first" can come back inverted.
        outer = [p.rstrip(sep) for p in paths_of(r) or [r["id"]]
                 if here.startswith(p.rstrip(sep) + sep)]
        if outer:
            out.append((min(len(p) for p in outer), r))
    return [r for _, r in sorted(out, key=lambda pair: pair[0])]


def contained(s: "Store", info: dict) -> list[dict]:
    """Projects physically inside this one — the modules of a monorepo."""
    sep = os.sep
    here = info["path"].rstrip(sep) + sep
    rows = s.q("MATCH (p:Project) RETURN p.id AS id, p.name AS name, p.paths AS paths")
    out = []
    for r in rows:
        if r["id"] == info["id"]:
            continue
        # Same rule as enclosing: order by the local path that matched, never
        # by `id`. This list is printed, so a foreign id sorts the modules of
        # a monorepo into a visibly arbitrary order.
        inside = [p for p in paths_of(r) or [r["id"]] if p.startswith(here)]
        if inside:
            out.append((min(inside), r))
    return [r for _, r in sorted(out, key=lambda pair: pair[0])]


def same_tree(s: "Store", info: dict) -> set[str]:
    """This project plus everything above and below it in the filesystem.

    These share a codebase, so they are structure rather than precedent: a
    module and its parent trivially share tags, and counting them as "closest
    projects" would crowd out genuinely comparable work elsewhere.
    """
    return ({info["id"]}
            | {r["id"] for r in enclosing(s, info)}
            | {r["id"] for r in contained(s, info)})


def effective_tags(s: "Store", info: dict) -> list[str]:
    """A module's own tags plus those of the projects containing it.

    A repo tagged `monorepo, internal` lends those to every module inside it —
    they are true of the module too, and they are how the module finds kin in
    other repos with the same shape.
    """
    tags = set(info["tags"])
    for anc in enclosing(s, info):
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
    skip = same_tree(s, info)
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
    # info['path'], not info['id']: the id may be the path this project was
    # first seen at on another machine, and a command the user cannot run is
    # worse than no suggestion.
    lines.append(f"    precedent.py tag --project {info['path']} --add backend,java,distributed")
    return "\n".join(lines)


def normalised(tag: str) -> str:
    """Case, separators and a trailing plural are spelling, not meaning.

    This is the whole of the automated drift check. It reports identity
    after trivial normalisation, which is a fact; whether two genuinely
    different words mean the same thing is a judgment, and it belongs to
    the caller, which has the vocabulary, the repository and the
    conversation in front of it.
    """
    flat = re.sub(r"[-_ ]+", "", tag.lower())
    return flat[:-1] if len(flat) > 3 and flat.endswith("s") else flat


def upsert_project(s: Store, info: dict) -> dict:
    """Resolve by portable id first, then fall back to the native path.

    The returned dict's `id` is the graph key, which may be a path from
    another machine. `path` stays this machine's, because containment is
    still derived from paths and must be derived from local ones.

    Writes resolve as well as reads. Resolving only on read finds the
    existing node while the write beside it creates a second one, so the
    graph fragments a little more with every machine and every session,
    invisibly.
    """
    local, portable = info["path"], info.get("portable")
    key = local
    if portable:
        # ORDER BY, because LIMIT 1 without one picks arbitrarily. Two nodes
        # can share a portable id: a graph rebuilt from journal lines that
        # predate this task holds one path-keyed node per machine, and a later
        # write on each stamps the same portable onto both.
        rows = s.q("""MATCH (p:Project {portable:$pp})
                      RETURN p.id AS id ORDER BY p.id LIMIT 1""",
                   {"pp": portable})
        if rows:
            key = rows[0]["id"]
    s.q("""MERGE (p:Project {id:$id}) SET p.name=$name, p.seen=$seen""",
        {"id": key, "name": info["name"], "seen": today()})
    if portable:
        # Backfilled on every write, so a repo that acquires a remote later
        # gains its identity without a migration step to remember.
        s.q("MATCH (p:Project {id:$id}) SET p.portable=$pp", {"id": key, "pp": portable})
    row = s.q("MATCH (p:Project {id:$id}) RETURN p.paths AS paths", {"id": key})[0]
    known = paths_of(row)
    if local not in known:
        # Sorted before it is both stored and returned: two representations of
        # one fact is how the next reader gets it wrong.
        known = sorted(known + [local])
        s.q("MATCH (p:Project {id:$id}) SET p.paths=$paths",
            {"id": key, "paths": "\n".join(known)})
    info["id"], info["paths"] = key, known
    return info


def attach_tags(s: Store, project_id: str, tags: list[str]) -> None:
    for t in tags:
        s.q("MERGE (t:Tag {name:$n})", {"n": t})
        s.q("""MATCH (p:Project {id:$id}),(t:Tag {name:$n})
               MERGE (p)-[:TAGGED]->(t)""", {"id": project_id, "n": t})


# ------------------------------------------------------------------------- writing

def write_decision(s: Store, d: dict) -> str:
    # Everything below keys off the RESOLVED id, never d["project_id"]: on a
    # second machine the two differ, and a tag attached to the unresolved path
    # matches no node and vanishes without an error — invisible until
    # precedent goes quiet, because tag overlap is what ranks kin.
    # An old journal line carries neither project_path nor portable and falls
    # back to the native path, exactly as before.
    pid = upsert_project(s, {"id": d["project_id"],
                             "path": d.get("project_path", d["project_id"]),
                             "name": d["project_name"],
                             "portable": d.get("portable")})["id"]
    attach_tags(s, pid, d.get("tags", []))
    s.q("""MERGE (n:Decision {id:$id})
           SET n.title=$title, n.statement=$statement, n.rationale=$rationale,
               n.scope=$scope, n.status='active', n.created=$created""",
        {"id": d["id"], "title": d.get("title", ""),
         "statement": d.get("statement", ""), "rationale": d.get("rationale", ""),
         "scope": d.get("scope", "architecture"),
         "created": d.get("created", today())})
    s.q("""MATCH (n:Decision {id:$id}), (p:Project {id:$project_id})
           MERGE (n)-[:IN_PROJECT]->(p)""",
        {"id": d["id"], "project_id": pid})

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
        # The identity and this machine's path, so a replay on another machine
        # resolves the same project rather than inventing a second one.
        "portable": info["portable"], "project_path": info["path"],
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
    p = {"id": a.id, "statement": a.statement, "derived_from": csv(a.derived_from),
         "topics": [t.lower() for t in csv(a.topic)]}
    s.log("principle", p)
    s.q("""MERGE (pr:Principle {id:$id}) SET pr.statement=$statement, pr.created=$created""",
        {**p, "created": today()})
    for topic in p["topics"]:
        s.q("MERGE (t:Topic {name:$n})", {"n": topic})
        s.q("""MATCH (pr:Principle {id:$id}),(t:Topic {name:$n})
               MERGE (pr)-[:ABOUT]->(t)""", {"id": a.id, "n": topic})
    for did in p["derived_from"]:
        s.q("""MATCH (pr:Principle {id:$id}),(d:Decision {id:$did})
               MERGE (pr)-[:DERIVED_FROM]->(d)""", {"id": a.id, "did": did})
    print(f"principle {a.id}: {a.statement}")
    if not p["topics"]:
        print("  note: no --topic, so `check` will never surface this principle.")


# ------------------------------------------------------------------------ reading

def worth_backfilling(s: "Store", info: dict) -> bool:
    """Is this a real project whose decisions are simply not in the graph yet?

    Two gates, both cheap and both necessary. A version-controlled directory is
    the difference between a project and a download folder. A non-empty graph is
    the difference between a user who forgot this repo and a user who has never
    used the tool and is being sold it on their first session.
    """
    if not (pathlib.Path(info["path"]) / ".git").exists():
        return False
    return s.q("MATCH (d:Decision) RETURN count(d) AS n")[0]["n"] > 0


def backfill_portable(s: Store, info: dict) -> None:
    """Fill in a portable id that did not exist when the project was first
    recorded — a repo that gains a remote later would otherwise stay split
    forever. Called from `brief`, on every session start, which is the
    cheapest place to catch this.

    Writes to an EXISTING node only (`MATCH ... SET`, never `MERGE`): `brief`
    must never create one, or a SessionStart hook would litter the graph with
    an empty project for every directory anyone ever opens. The guard only
    fires when the node's own `portable` reads back exactly `[{"pp": None}]`
    — a node that is there but has never had one set; a nonexistent node
    reads back `[]` and the guard is false there too, so this never even
    reaches a MATCH that could theoretically race a create.

    Journalled BEFORE the mutation (`Store.log`'s own rule), because the
    graph is not the durable copy: `rebuild` replays the journal into a fresh
    graph, and a mutation with no journal entry is invisible to it — the
    backfill would be silently undone by the next rebuild, and a second
    machine resolving this project's portable id would then miss the lookup
    and fork a second node, reopening exactly the split Task 13 closed.
    """
    if not info["portable"]:
        return
    if s.q("MATCH (p:Project {id:$id}) RETURN p.portable AS pp",
           {"id": info["id"]}) != [{"pp": None}]:
        return
    s.log("project_portable", {"project_id": info["id"], "portable": info["portable"]})
    s.q("MATCH (p:Project {id:$id}) SET p.portable=$pp",
        {"id": info["id"], "pp": info["portable"]})


def cmd_brief(a, s: Store) -> None:
    # Writes to an existing Project node only, to backfill a portable id. Never
    # creates one: a SessionStart hook calls this in every directory the user
    # opens, and creating would litter the graph with empty projects.
    info = project_info(s, a.project)
    backfill_portable(s, info)

    here = s.q("""MATCH (d:Decision)-[:IN_PROJECT]->(:Project {id:$pid})
                  OPTIONAL MATCH (d)-[:ABOUT]->(t:Topic)
                  WHERE d.status='active'
                  RETURN d.id AS id, d.title AS title, d.scope AS scope,
                         d.created AS created, d.despite AS despite,
                         collect(DISTINCT t.name) AS topics
                  ORDER BY created DESC LIMIT 25""", {"pid": info["id"]})
    kin = neighbours(s, info, a.min_shared)
    above, below = enclosing(s, info), contained(s, info)

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
        print("  nothing recorded under this exact word.")
        known = topic_vocabulary(s)
        if known:
            print("\n== topics in use ==")
            for r in known:
                print(f"  {r['topic']:<24} {r['decisions']} decision(s)")
            print("\n  matching is exact, so a synonym finds nothing. If one of"
                  " these is the same question, re-run with it. If none is,"
                  " this is new ground.")
        else:
            print("  no decisions recorded anywhere yet — this is new ground.")
        return

    if not a.chose:
        return

    # Resolved once, here rather than in the loop below: the loop runs up to
    # three times and project_info shells out to git. A bare
    # Path(a.project).resolve() would miss the node whenever this checkout
    # was first seen on another machine, and every acknowledged divergence
    # would be reported as unacknowledged again.
    pid = project_info(s, a.project)["id"]

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
    # The mirror of REGRET. Without it, taking a lesson's own advice is met
    # with the rejections recorded by the decisions that lesson regrets.
    endorsed = s.q("""MATCH (l:Lesson {topic:$t, instead:$o})
                      OPTIONAL MATCH (l)-[:REGRETS]->(d:Decision)
                      RETURN l.option AS was, l.statement AS why, count(d) AS n""",
                   {"t": topic, "o": a.chose})
    endorsed = [r for r in endorsed if r["why"]]
    for r in endorsed:
        print(f"  LESSON: you chose '{r['was']}' for this in {r['n']} project(s),"
              f" concluded it was a mistake, and now prefer this —")
        print(f"     \"{r['why']}\"")
    revived = s.q("""MATCH (d:Decision)-[:ABOUT]->(:Topic {name:$t}),
                           (d)-[:REJECTED]->(:Option {name:$o}),
                           (d)-[:IN_PROJECT]->(p:Project)
                     WHERE d.status='active'
                     RETURN p.name AS pname, d.title AS title, d.rationale AS why""",
                  {"t": topic, "o": a.chose})
    for r in revived:
        print(f"  CONFLICT: rejected in {r['pname']} ({r['title']})"
              f" — {r['why'] or 'no rationale recorded'}")

    norm = s.q("""MATCH (d:Decision)-[:ABOUT]->(:Topic {name:$t}),
                        (d)-[:CHOSE]->(o:Option),
                        (d)-[:IN_PROJECT]->(p:Project)
                  WHERE d.status='active' AND o.name <> $o
                  RETURN o.name AS other, count(DISTINCT p) AS n,
                         collect(d.created) AS dates
                  ORDER BY n DESC LIMIT 3""", {"t": topic, "o": a.chose})
    diverged = [r for r in norm if r["n"] >= 2]
    for r in diverged:
        # Scoped to THIS norm: the exception was argued out against a specific
        # prior decision, and letting it answer every warning on the topic is
        # how one acknowledged divergence hides three unacknowledged ones.
        ack = s.q("""MATCH (d:Decision)-[:ABOUT]->(:Topic {name:$t}),
                           (d)-[:IN_PROJECT]->(:Project {id:$pid}),
                           (d)-[:DIVERGES_FROM]->(:Decision)-[:CHOSE]->(:Option {name:$other})
                     WHERE d.status='active' AND d.despite IS NOT NULL
                     RETURN d.despite AS why LIMIT 1""",
                  {"t": topic, "pid": pid, "other": r["other"]})
        # A count with no date weighs a choice from 2019 in a dead repo exactly
        # as heavily as one from last month. The reader can discount it; the
        # tool should not decide the history expired.
        last = max([d for d in r["dates"] if d], default="")
        age = f", last: {last[:7]}" if last else ""
        if ack:
            # Already argued out, in this project, on the record. Repeating the
            # warning here is how a useful signal becomes noise.
            print(f"  DIVERGENCE from '{r['other']}' ({r['n']} projects{age}) —"
                  f" acknowledged here: \"{ack[0]['why']}\"")
        else:
            print(f"  DIVERGENCE: you chose '{r['other']}' for this in"
                  f" {r['n']} other projects{age}")

    violated = s.q("""MATCH (pr:Principle)-[:ABOUT]->(:Topic {name:$t})
                      RETURN pr.id AS id, pr.statement AS stmt""", {"t": topic})
    for r in violated:
        print(f"  PRINCIPLE in play: {r['stmt']}  #{r['id']}")

    if not revived and not diverged and not regrets and not endorsed:
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
              f" --topic {r['topic']}"
              f" --statement \"For {r['topic']}, use {r['opt']}.\"")


# -------------------------------------------------------------------- maintenance

def cmd_tag(a, s: Store) -> None:
    """Show the tag vocabulary, or change this project's tags.

    Tags are free-form: no fixed list can anticipate every kind of thing someone
    builds. But precedent is ranked by exact tag overlap, so a near-duplicate
    hides history rather than merely looking untidy — hence the vocabulary is
    printed on every write, not only on request, so the caller can see a
    near-duplicate and merge it. Whether two words mean the same thing is a
    judgment call for the caller, not this function. See docs/adr/0001.
    """
    if a.merge:
        # Drift happens anyway — different agents in different sessions reach
        # for different words for the same thing. Repair has to be one
        # command, or the graph stays split.
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

    upsert_project(s, info)
    if add:
        # Journalled like a record: without the identity here, a rebuild that
        # replays a tag line before the first record line on that project
        # creates the node with no portable id, and the record line then
        # cannot resolve onto it.
        s.log("project_tags", {"project_id": info["id"], "name": info["name"],
                               "portable": info["portable"],
                               "project_path": info["path"],
                               "add": add})
        attach_tags(s, info["id"], add)
    for t in remove:
        s.log("project_tags", {"project_id": info["id"], "name": info["name"],
                               "portable": info["portable"],
                               "project_path": info["path"],
                               "remove": [t]})
        s.q("""MATCH (:Project {id:$id})-[r:TAGGED]->(:Tag {name:$n}) DELETE r""",
            {"id": info["id"], "n": t})
    print(f"{info['name']} tags: {', '.join(tags_of(s, info['id'])) or 'none'}")
    known = vocabulary(s)
    if known:
        # Printed on every write, not only on request: precedent is ranked by
        # exact tag overlap, so a near-duplicate hides half the history from
        # the other half. The caller can see it here and merge.
        print("\ntags in use across all projects:")
        for r in known:
            print(f"  {r['tag']:<20} {r['projects']} project(s)")
        print("  a near-duplicate above splits your history —"
              " merge with: precedent.py tag --merge <from> --into <to>")


def apply_tag_merge(s: Store, frm: str, to: str) -> None:
    s.q("MERGE (t:Tag {name:$n})", {"n": to})
    for r in s.q("""MATCH (p:Project)-[:TAGGED]->(:Tag {name:$f})
                    RETURN p.id AS id""", {"f": frm}):
        s.q("""MATCH (p:Project {id:$id}),(t:Tag {name:$to})
               MERGE (p)-[:TAGGED]->(t)""", {"id": r["id"], "to": to})
    s.q("""MATCH (:Project)-[r:TAGGED]->(:Tag {name:$f}) DELETE r""", {"f": frm})
    s.q("""MATCH (t:Tag {name:$f}) DETACH DELETE t""", {"f": frm})


def contradictions_in(s: "Store") -> list[dict]:
    """Two live decisions in one project answering one topic differently.

    Grouped by decision id, not by project+topic: `record --chose redis,memcached`
    is one decision picking a stack, and reporting it as a clash asks the user
    to supersede a decision that is correct.
    """
    rows = s.q("""MATCH (d:Decision)-[:ABOUT]->(t:Topic),
                        (d)-[:CHOSE]->(o:Option),
                        (d)-[:IN_PROJECT]->(p:Project)
                  WHERE d.status='active'
                  RETURN p.name AS pname, t.name AS topic, d.id AS did,
                         collect(DISTINCT o.name) AS opts""")
    grouped: dict[tuple[str, str], dict[str, list[str]]] = {}
    for r in rows:
        grouped.setdefault((r["pname"], r["topic"]), {})[r["did"]] = sorted(r["opts"])
    out = []
    for (pname, topic), decisions in sorted(grouped.items()):
        if len(decisions) < 2:
            continue
        if len({tuple(v) for v in decisions.values()}) < 2:
            continue        # two decisions, same answer: duplicated, not conflicting
        out.append({"project": pname, "topic": topic, "decisions": decisions})
    return out


def liveness(row: dict) -> str:
    """live | elsewhere | gone.

    A path shaped for another platform is unknown, not dead: on Linux every
    Windows-recorded project fails an exists() test, and feeding that into a
    discount rule writes off the other machine's whole history.
    """
    known = paths_of(row) or [row["id"]]
    if any(pathlib.Path(p).exists() for p in known):
        return "live"
    foreign = os.sep == "/" and any(re.match(r"^[A-Za-z]:\\", p) for p in known)
    foreign = foreign or (os.sep == "\\" and all(p.startswith("/") for p in known))
    return "elsewhere" if foreign else "gone"


def identity_gaps(s: "Store") -> list[dict]:
    """Projects carrying no portable id, and the node that already holds theirs.

    A project recorded before portable ids existed never acquires one once a
    second machine has claimed its remote: `project_info` resolves onto the
    node carrying the portable, so `backfill_portable` reads back a non-None
    value and returns without writing. The older node keeps its decisions,
    never gains an identity, and every later write lands on the newer one.
    Nothing heals that and nothing else reports it — so this does.

    Reports, never merges. Two decision histories are one repo's or two
    repos', and telling them apart is a judgment; merging them silently is the
    failure ADR 0002 exists to prevent.

    Detection is exact rather than a heuristic: `portable` is read from the
    local checkout's own git remote, and a twin is claimed only when another
    node already carries that exact id. A project whose paths are not on this
    machine has no remote to read here, so it is listed with no verdict — a
    name or path resemblance is a guess, and a guess here merges the histories
    of two unrelated projects.
    """
    rows = s.q("""MATCH (p:Project)
                  RETURN p.id AS id, p.name AS name, p.paths AS paths,
                         p.portable AS portable""")
    claimed = {r["portable"]: r for r in rows if r["portable"]}
    out = []
    for r in sorted((r for r in rows if not r["portable"]),
                    key=lambda r: (r["name"] or "", r["id"])):
        # Only a directory that is actually here can be asked what its remote
        # is, and only one git call per gap, so this stays cheap on a store
        # whose projects mostly predate portable ids.
        local = next((p for p in paths_of(r) or [r["id"]] if pathlib.Path(p).exists()), None)
        pp = portable_id(pathlib.Path(local)) if local else None
        out.append({"id": r["id"], "name": r["name"], "local": local,
                    "portable": pp, "twin": claimed.get(pp)})
    return out


def cmd_maintain(a, s: Store) -> None:
    clashes = contradictions_in(s)
    print(f"== contradictions: same project, same topic, two live answers ({len(clashes)}) ==")
    for r in clashes:
        print(f"  {r['project']}/{r['topic']} — supersede one:")
        for did, opts in sorted(r["decisions"].items()):
            print(f"     {','.join(opts)}  #{did}")

    tags = [r["tag"] for r in vocabulary(s)]
    groups: dict[str, list[str]] = {}
    for t in tags:
        groups.setdefault(normalised(t), []).append(t)
    dupes = [sorted(v) for v in groups.values() if len(v) > 1]
    print(f"\n== tags that differ only in spelling ({len(dupes)}) ==")
    if not dupes:
        print("  none")
    for names in sorted(dupes):
        print(f"  {' / '.join(repr(n) for n in names)}"
              f" — merge with: precedent.py tag --merge {names[1]} --into {names[0]}")
    print(f"\n== the whole tag vocabulary ({len(tags)}) ==")
    for r in vocabulary(s):
        print(f"  {r['tag']:<20} {r['projects']} project(s)")
    print("  two of these that mean the same thing split your history."
          " Spelling variants are listed above; the rest is a judgment call.")

    untagged = s.q("""MATCH (p:Project)
                      WHERE NOT EXISTS { MATCH (p)-[:TAGGED]->(:Tag) }
                      RETURN p.name AS name""")
    print(f"\n== untagged projects, invisible to precedent ({len(untagged)}) ==")
    for r in untagged:
        print(f"  {r['name']}")

    projects = s.q("MATCH (p:Project) RETURN p.id AS id, p.name AS name, p.paths AS paths")
    states = {"gone": [], "elsewhere": []}
    for r in projects:
        st = liveness(r)
        if st != "live":
            states[st].append(r)
    print(f"\n== projects whose path no longer exists ({len(states['gone'])}) ==")
    for r in states["gone"]:
        print(f"  {r['name']}  ({r['id']})")
    print(f"\n== projects recorded on another machine ({len(states['elsewhere'])}) ==")
    for r in states["elsewhere"]:
        print(f"  {r['name']}  ({r['id']})  — not gone, just not here")

    gaps = identity_gaps(s)
    splits = sum(1 for g in gaps if g["twin"])
    print(f"\n== projects with no portable identity ({len(gaps)},"
          f" {splits} split across two nodes) ==")
    if not gaps:
        print("  none — every project is keyed by its git remote")
    for g in gaps:
        if g["twin"]:
            print(f"  {g['name']}  ({g['id']})  — the same repo as"
                  f" {g['twin']['name']} ({g['twin']['id']}), which already holds"
                  f" {g['portable']}")
            print("     two nodes, two histories, one repo: every new decision lands on"
                  " the second, and this one is stranded.")
            print("     precedent will not merge them for you — that is a judgment."
                  " Read both sides first:")
            print("       precedent.py cypher --params "
                  f"{json.dumps({'ids': [g['id'], g['twin']['id']]})!r} \\")
            print('         "MATCH (d:Decision)-[:IN_PROJECT]->(p:Project)'
                  ' WHERE p.id IN $ids RETURN p.id AS project, d.title AS decision"')
        elif g["portable"]:
            print(f"  {g['name']}  ({g['id']})  — {g['portable']} is unclaimed,"
                  " so the next write here stamps it")
        elif g["local"]:
            print(f"  {g['name']}  ({g['id']})  — no git remote, so it is"
                  " machine-local by design")
        else:
            print(f"  {g['name']}  ({g['id']})  — not on this machine, so its"
                  " remote cannot be read here")

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
        except JournalTooNew as exc:
            raise SystemExit(f"stopping at line {lineno}: {exc}")
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
    # A line with no "v" predates versioning and is v1 by definition. A line
    # from the future cannot be interpreted by guessing which keys it has,
    # which is exactly what the rest of this function does.
    if e.get("v", 1) > SCHEMA:
        raise JournalTooNew(
            f"entry {e.get('id', e.get('op', '?'))!r} is schema v{e['v']},"
            f" this precedent understands v{SCHEMA} — upgrade before replaying")
    if e["op"] == "record":
        if "tags" not in e and e.get("project_type") not in (None, "", "unknown",
                                                             "unclassified"):
            # Pre-tag journal format carried one type string. Migrate it to a
            # single tag — but never turn the old "we don't know" placeholders
            # into a real tag that projects could then match on.
            e["tags"] = [e["project_type"]]
        write_decision(s, e)
    elif e["op"] == "tags_distinct":
        pass  # legacy: written by the removed drift guard, kept so old
              # journals still replay. Never emitted now. See docs/adr/0001.
    elif e["op"] == "tag_merge":
        apply_tag_merge(s, e["from"], e["to"])
    elif e["op"] in ("project_tags", "project_type"):
        # project_type is the pre-tag journal format: one type string per
        # project. Replaying it as a single tag migrates old graphs on the
        # first rebuild, with no separate migration step to forget.
        # Same rule as write_decision: everything keys off the resolved id, so
        # a tag never lands on a node this machine does not have.
        pid = upsert_project(s, {"id": e["project_id"],
                                 "path": e.get("project_path", e["project_id"]),
                                 "name": e["name"],
                                 "portable": e.get("portable")})["id"]
        add = e.get("add") or ([e["type"]] if e.get("type") else [])
        attach_tags(s, pid, add)
        for t in e.get("remove", []):
            s.q("""MATCH (:Project {id:$id})-[r:TAGGED]->(:Tag {name:$n}) DELETE r""",
                {"id": pid, "n": t})
    elif e["op"] == "project_portable":
        # MATCH, not upsert_project's MERGE: `brief` (the only writer of this
        # op) never creates a node, and replay must preserve that — a node
        # this line's project_id does not resolve to on this replay (an
        # unlikely reordering, or a line replayed in isolation) is silently
        # skipped rather than fabricated. Journal-order replay always plays
        # the line that first created the node before this one.
        s.q("MATCH (p:Project {id:$id}) SET p.portable=$pp",
            {"id": e["project_id"], "pp": e["portable"]})
    elif e["op"] == "regret":
        apply_regret(s, e)
    elif e["op"] == "principle":
        s.q("""MERGE (pr:Principle {id:$id})
               SET pr.statement=$statement, pr.created=$ts""", e)
        for topic in e.get("topics", []):
            s.q("MERGE (t:Topic {name:$n})", {"n": topic})
            s.q("""MATCH (pr:Principle {id:$id}),(t:Topic {name:$n})
                   MERGE (pr)-[:ABOUT]->(t)""", {"id": e["id"], "n": topic})
        for did in e.get("derived_from", []):
            s.q("""MATCH (pr:Principle {id:$id}),(d:Decision {id:$did})
                   MERGE (pr)-[:DERIVED_FROM]->(d)""", {"id": e["id"], "did": did})
    else:
        # An op this version does not know: report it rather than counting a
        # silent no-op as a successful replay.
        raise ValueError(f"unknown journal op {e.get('op')!r}")


def _ensure_removable(path: pathlib.Path) -> None:
    """Raise if this process cannot delete `path` (file or directory tree).

    Checked before relocate() moves anything, not caught mid-move: a real
    store here can hold root-owned directories (left by an earlier
    docker-mounted grafeo-server run, store bind-mounted), and a
    PermissionError partway through the move loop would strand payload split
    across both directories with no pointer written — after which
    resolve_home(default) keeps quietly resolving to default, masking
    whatever already moved.

    TOCTOU: permissions can still change between this check and the actual
    move. Accepted as the cheap tradeoff for not owning a staging-directory
    and rollback file mover.
    """
    if path.is_dir() and not path.is_symlink():
        if not os.access(path, os.W_OK):
            raise SystemExit(
                f"cannot relocate: {path} is not writable by this process; "
                "fix its permissions or move it aside by hand, then retry.")
        for child in path.iterdir():
            _ensure_removable(child)


def relocate(target: pathlib.Path, default: pathlib.Path = DEFAULT_HOME) -> str:
    """Keep the store somewhere else, and leave a pointer at the default path."""
    import shutil

    target.mkdir(parents=True, exist_ok=True)
    if target == default:
        return f"store: {target}  (the default location)"

    if default.exists() and not default.is_dir():
        raise SystemExit(f"{default} exists and is not a directory; move it aside first")
    default.mkdir(parents=True, exist_ok=True)
    # Prefix match, not exact equality: filelock's SQLite backend produces no
    # sidecars today (checked under a held write lock, a held read lock, and
    # after close), but a future filelock that switches to WAL would, and
    # this survives that without another look here.
    payload = [f for f in default.iterdir()
               if not f.name.startswith(".lock") and f.name != POINTER]
    occupied = [f for f in target.iterdir()
                if not f.name.startswith(".lock") and f.name != POINTER]
    if payload and occupied:
        raise SystemExit(
            f"both {default} and {target} hold a store; refusing to merge.\n"
            f"  merging is a journal concatenation, so do it deliberately:\n"
            f"    cat {default}/journal.jsonl >> {target}/journal.jsonl\n"
            f"    mv {default} {default}.bak\n"
            f"  then re-run this, and `precedent.py rebuild`.")
    if payload and not os.access(default, os.W_OK):
        raise SystemExit(f"cannot relocate: {default} is not writable by this process; "
                          "fix its permissions, then retry.")
    for f in payload:
        _ensure_removable(f)
    for f in payload:
        shutil.move(str(f), str(target / f.name))
    (default / POINTER).write_text(str(target) + "\n")
    moved = f"  moved {len(payload)} file(s) from the default location\n" if payload else ""
    return f"store: {target}\n{moved}  {default}/{POINTER} points here"


def cmd_init(a) -> None:
    """Runs without a Store: opening one would create the default directory
    before this command has decided where the store belongs."""
    if not a.location:
        d = DEFAULT_HOME
        real = resolve_home(d)
        if real != d:
            print(f"store: {real}  (via {d}/{POINTER})")
        elif d.exists():
            print(f"store: {d}  (the default location)")
        else:
            print(f"no store yet; it will be created at {d}")
        if os.environ.get("PRECEDENT_HOME"):
            print(f"  note: PRECEDENT_HOME is set to {os.environ['PRECEDENT_HOME']},"
                  " which overrides the above for this shell only —"
                  " the SessionStart hook will not see it.")
        return
    # relocate() always acts on DEFAULT_HOME, never on --home: the
    # SessionStart hook and every slash command are wired to the default
    # path with no flag in between, so a --home that points elsewhere would
    # otherwise relocate a store this invocation was never told about.
    home_arg = pathlib.Path(a.home).resolve()
    if home_arg != DEFAULT_HOME.resolve():
        raise SystemExit(
            f"error: --home {home_arg} was given, but `init --location` always "
            f"relocates the default location ({DEFAULT_HOME}), because that is "
            "the path the SessionStart hook and every slash command are wired "
            "to — not --home. Re-run without --home to relocate the store this "
            "machine actually uses by default.")
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

    # The POSIX-only locking module this CLI used to import at module scope,
    # which made `--help` itself fail on Windows (spec A4). The name is
    # assembled rather than written out because it must not appear in this
    # file at all — spelled the obvious way, the assertion would match itself
    # and could never fail. This is the durable form of what was a one-off
    # grep during the port.
    banned = "f" + "cntl"
    # encoding pinned: this file is full of non-ASCII prose and read_text()
    # defaults to the locale codepage, which raises on a Windows console.
    assert banned not in pathlib.Path(__file__).read_text(encoding="utf-8"), (
        f"{banned} does not exist on Windows and this CLI must start there;"
        " filelock is already a dependency and is the cross-platform answer.")


def _check_detect_project() -> None:
    assert detect_project(pathlib.Path("/nonexistent"))["contents"] == ""

    # tempfile, not a literal /tmp path: pathlib.Path("/tmp") is C:\tmp on
    # Windows, which need not exist, and mkdir(parents=False) would raise
    # FileNotFoundError before a single assertion ran. Nothing below needs a
    # stable path, and TemporaryDirectory removes the fixture on the way out
    # however this check ends.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        # Listing, not whitelisting: an unheard-of build file must still show up.
        (tmp / "build.zig").write_text("// zig")
        (tmp / "shard.yml").write_text("# crystal")
        (tmp / "src").mkdir()
        got = detect_project(tmp)
        assert "build.zig" in got["contents"], got
        assert "shard.yml" in got["contents"], got
        assert "src/" in got["contents"], got
        (tmp / "node_modules").mkdir()
        assert "node_modules" not in detect_project(tmp)["contents"]


def _check_drift() -> None:
    """Identity after trivial normalisation is a fact. Similarity is a
    judgment, and it moved to the model — difflib scored a shared prefix
    and called web-frontend and web-backend the same thing, 7 of 11
    realistic pairs wrong. See docs/adr/0001.
    """
    for a_, b_ in [("data_pipeline", "data-pipeline"), ("Data-Pipeline", "data-pipeline"),
                   ("backend-apis", "backend-api"), ("telegram bot", "telegram-bot")]:
        assert normalised(a_) == normalised(b_), f"spelling variant missed: {a_} ~ {b_}"
    for a_, b_ in [("web-frontend", "web-backend"), ("mobile-ios", "mobile-android"),
                   ("telegram-bot", "telegram-api"), ("python", "python3"),
                   ("backend", "backend-api"), ("ml-training", "ml-serving"),
                   ("cli", "clip")]:
        assert normalised(a_) != normalised(b_), f"false match: {a_} ~ {b_}"


def _check_projects(s: Store) -> None:
    # Every path below is a graph key and a prefix-comparison operand; none of
    # them is ever touched on disk, so none of them needs to exist. They are
    # built with os.sep rather than a literal "/" because that is what
    # enclosing/contained compare with: on Windows `here.startswith(p + "\\")`
    # is never true of a POSIX-shaped fixture, so every containment assertion
    # below would degrade to [] and pass or fail for the wrong reason.
    base = os.sep + os.path.join("tmp", "precedent-selftest")

    # Tags are a set, and overlap is what ranks precedent.
    proj = base + "-tags-proj"
    other = base + "-other"
    root, mod = base + "-root", os.path.join(base + "-root", "mod")
    sibling = base + "-root-elsewhere"   # prefix-similar but NOT inside
    # A project first seen on another machine keeps that machine's path as its
    # id, so `id` is not a depth and cannot order containment: len("C:\\m")
    # would sort this module ahead of the root that contains it. Ordering has
    # to come from the local paths that actually matched. On Windows this is a
    # short local path rather than a foreign one, which tests the same thing —
    # a four-character id must not sort ahead of the root that contains the
    # module.
    win, mid = "C:\\m", os.path.join(root, "mid")
    try:
        upsert_project(s, {"id": proj, "path": proj, "name": "selftest"})
        attach_tags(s, proj, ["selftest-backend", "selftest-java"])
        assert set(tags_of(s, proj)) == {"selftest-backend", "selftest-java"}
        upsert_project(s, {"id": other, "path": other, "name": "other"})
        attach_tags(s, other, ["selftest-java"])
        kin = neighbours(s, {"id": other, "path": other, "tags": ["selftest-java"]})
        assert any(k["id"] == proj and k["n"] == 1 for k in kin), kin
        kin2 = neighbours(s, {"id": other, "path": other, "tags": ["selftest-java"]},
                          min_shared=2)
        assert kin2 == [], "min_shared must exclude weakly-related projects"

        # Containment is derived from the paths, so a monorepo needs no schema.
        for pid, name in ((root, "root"), (mod, "mod"), (sibling, "elsewhere")):
            upsert_project(s, {"id": pid, "path": pid, "name": name})
        attach_tags(s, root, ["selftest-monorepo"])
        attach_tags(s, mod, ["selftest-java"])
        attach_tags(s, sibling, ["selftest-java"])
        mod_info = {"id": mod, "path": mod}
        root_info = {"id": root, "path": root}
        assert [r["id"] for r in enclosing(s, mod_info)] == [root], enclosing(s, mod_info)
        assert [r["id"] for r in contained(s, root_info)] == [mod], contained(s, root_info)
        assert enclosing(s, {"id": sibling, "path": sibling}) == [], \
            "a shared name prefix is not containment"

        for path in (win, mid):    # first seen on Windows, then seen here
            upsert_project(s, {"id": win, "path": path, "name": "mid",
                               "portable": "github.com/asm0dey/selftest-c"})
        deep = os.path.join(mid, "deep")
        assert [r["name"] for r in enclosing(s, {"id": deep, "path": deep})] \
            == ["root", "mid"], "outermost first, ordered by the containing local path"
        assert effective_tags(s, {**mod_info, "tags": ["selftest-java"]}) \
            == ["selftest-java", "selftest-monorepo"], "a module inherits enclosing tags"
        kin3 = {r["id"] for r in neighbours(s, {**mod_info, "tags": ["selftest-java"]})}
        assert sibling in kin3, "comparable work outside the tree must count as kin"
        assert root not in kin3, "the enclosing project is structure, not precedent"
        assert mod not in kin3, "a project is not its own kin"
    finally:
        # Every other check in this file cleans up in a finally, and this one
        # must too: a failing assertion above used to leak six Project nodes,
        # and the NEXT run then tripped the node-count invariant in
        # cmd_selftest — an error naming a check that was never at fault.
        for pid in (proj, other, root, mod, sibling, win):
            s.q("MATCH (p:Project {id:$id}) DETACH DELETE p", {"id": pid})
        for t in ("selftest-monorepo", "selftest-backend", "selftest-java"):
            s.q("""MATCH (n:Tag {name:$n}) WHERE NOT EXISTS { MATCH (n)<--() }
                     DETACH DELETE n""", {"n": t})


def _check_decisions(s: Store) -> None:
    # A real directory, from tempfile rather than a literal /tmp path, because
    # worth_backfilling stats it for a .git and Windows has no /tmp to mkdir
    # into. The graph fixtures key off whatever path it lands on.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
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
        # Shaped like a real info dict, id and all: worth_backfilling reads only
        # `path`, but a fixture missing `id` is a template for a KeyError the next
        # time one of these is passed to a containment helper.
        proj_info = {"id": str(tmp), "path": str(tmp)}
        assert not worth_backfilling(s, proj_info), "a non-repo must not be nudged"
        (tmp / ".git").mkdir(exist_ok=True)
        assert worth_backfilling(s, proj_info), "a repo with a populated graph must be"
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


def _check_relocate() -> None:
    # Relocation moves the only copy of the journal, so every branch is checked.
    # tempfile, not a literal /tmp path, so this runs where there is no /tmp.
    # `base` is a directory INSIDE the temporary one because the blocks below
    # rmtree it between cases, and removing the directory TemporaryDirectory
    # is holding would make its own cleanup raise.
    import shutil as _sh
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        base = pathlib.Path(td) / "home"
        dflt, tgt = base / "default", base / "elsewhere"
        relocate(tgt, dflt)
        assert (dflt / POINTER).read_text().strip() == str(tgt), \
            "fresh default must get a pointer"

        _sh.rmtree(base); dflt.mkdir(parents=True)
        (dflt / "journal.jsonl").write_text("x\n"); (dflt / ".lock").write_text("")
        relocate(tgt, dflt)
        assert (tgt / "journal.jsonl").read_text() == "x\n", \
            "an existing store must move, not vanish"
        # The .lock prefix filter exists so the lock stays with the path
        # processes still queue on. A moved lock file is how relocation
        # strands a store, so assert it stayed rather than only exercising it.
        assert not (tgt / ".lock").exists(), \
            "the lock file must not travel with the payload"
        assert (dflt / ".lock").exists(), "the lock file must stay at the old path"

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
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        exp = pathlib.Path(td) / "export"
        export_to(s, exp)
        assert (exp / "default" / "data.grafeo").stat().st_size > 0, "export wrote nothing"


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

    # A pointer file, not a symlink: os.symlink raises WinError 1314 without
    # Developer Mode, which made `init` unavailable on Windows for its whole
    # purpose. Resolution follows the pointer exactly once — a pointer inside
    # the target is data, not a second hop.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        default, target = pathlib.Path(tmp) / "default", pathlib.Path(tmp) / "target"
        default.mkdir()
        (default / "journal.jsonl").write_text('{"op":"noop"}\n')
        relocate(target, default=default)
        assert not (default / "journal.jsonl").exists(), "payload must move"
        assert (target / "journal.jsonl").exists(), "payload must arrive"
        assert (default / POINTER).read_text().strip() == str(target)
        assert resolve_home(default) == target, "pointer must resolve"
        (target / POINTER).write_text(str(default))
        assert resolve_home(default) == target, "resolution must not loop"

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

    Deliberately unlocked, and deliberately harsher than production.
    ReadWriteLock grants N readers OR one exclusive writer, so under the lock
    a reader and a writer are never concurrent; this runs them concurrently
    anyway. That is conservative on purpose — do not delete the check as
    unrealistic, it tests something strictly harder than the lock permits.

    It has to, because a "reader" here reads no decision but is not passive at
    the file level: opening a grafeo db appends ~10 bytes to its write-ahead
    log before any query runs, and closing appends more. Which sounds like it
    sinks the whole split — N processes writing at once is what the lock exists
    to stop — so it was measured rather than argued, unlocked, 3s each:

        6 concurrent writers:  attempted 6329, stored 1311, seed intact
        6 concurrent readers:  27662 opens, seed intact, WAL grew to 290KB

    The first row is grafeo#405 reproducing exactly: 79% of writes gone, none
    raised. That is what makes the second row mean anything — the harness
    demonstrably detects loss, and found none in 27,662 concurrent opens.
    Concurrent WAL appends by readers are benign; concurrent data writes are
    not, and the read lock rests entirely on that difference.

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

    WRITE_BUDGET = 10.0  # wall clock, not iterations, so the run is bounded
    READ_BUDGET = 1.2    # ends inside the writer's window, from both sides
    WARMUP = 0.4         # let the node exist before readers look for it
    READERS = 4          # several appenders on one WAL, not just a pair
    STUCK = 5.0          # a reader past this is wedged, not slow
    # WRITE_BUDGET is an upper bound nothing waits on: the writer is killed as
    # soon as the readers are reaped, so raising it costs no wall clock and
    # only widens the margin behind the `writer.poll()` assertion below. At
    # 3.0 the margin behind that assertion was ~1.4s, which four cold
    # Python+grafeo process starts can eat on a loaded machine and Windows
    # process spawn eats comfortably. Since the budget is free, it is 10.

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


def _check_journal(s: Store) -> None:
    """A journal written by a newer precedent must stop the replay, not
    half-succeed. `rebuild` is the escape hatch that makes a v0.5 graph
    engine an acceptable dependency; an escape hatch that silently
    half-works is not one.

    This exercises replay_entry directly rather than cmd_rebuild, because
    rebuild starts by deleting every node and selftest must leave the
    graph untouched.
    """
    assert SCHEMA == 1
    try:
        replay_entry(s, {"op": "record", "v": SCHEMA + 1, "id": "selftest-future"})
        raise AssertionError("a future schema version must not replay")
    except JournalTooNew as exc:
        assert "selftest-future" in str(exc), exc
    # The refused attempt above must leave no trace — a caught exception is
    # not proof by itself that nothing was written first.
    assert s.q("MATCH (d:Decision {id:'selftest-future'}) RETURN d.id AS id") == []

    # An entry with no "v" key is pre-versioning and must still replay as v1 —
    # this is the property that keeps every line already in the user's real
    # journal (none of which carry a "v" key) readable.
    replay_entry(s, {"op": "record", "id": "selftest-noversion",
                     "project_id": "selftest-noversion-proj", "project_name": "selftest"})
    assert s.q("MATCH (d:Decision {id:'selftest-noversion'}) RETURN d.id AS id") \
        == [{"id": "selftest-noversion"}], "a v-less entry must still replay as v1"
    s.q("MATCH (n) WHERE n.id STARTS WITH 'selftest-noversion' DETACH DELETE n")


def _check_verdicts(s: Store) -> None:
    """`clear` must mean 'I searched and your history is silent', never
    'you typed a word I have never seen'. Reproduced before the fix:
    `check --topic database` against a graph holding the same decision
    under `persistence` printed `new ground` and then `clear`.
    """
    d = {"id": "selftest-v1", "title": "T", "statement": "T",
         "rationale": "concurrent writers", "scope": "architecture",
         "created": today(), "project_id": "/tmp/precedent-selftest-v",
         "project_name": "selftest-v", "tags": ["selftest-v-tag"],
         "topics": ["selftest-persistence"], "chose": ["postgres"],
         "rejected": ["sqlite"], "supersedes": []}
    write_decision(s, d)
    try:
        topics = [r["topic"] for r in topic_vocabulary(s)]
        assert "selftest-persistence" in topics, topics
    finally:
        s.q("MATCH (n) WHERE n.id STARTS WITH 'selftest-v' DETACH DELETE n")
        s.q("""MATCH (p:Project {id:'/tmp/precedent-selftest-v'}) DETACH DELETE p""")
        for name in ("selftest-persistence", "postgres", "sqlite", "selftest-v-tag"):
            s.q("""MATCH (n) WHERE (n:Topic OR n:Option OR n:Tag) AND n.name = $name
                     AND NOT EXISTS { MATCH (n)<--() } DETACH DELETE n""", {"name": name})

    import argparse as _argparse
    import contextlib
    import io

    # Resolve once and reuse for both the stored project id and every CLI
    # argument in this block. On macOS /tmp is a symlink to /private/tmp, so
    # a literal "/tmp" node id would never match pathlib.Path("/tmp").resolve()
    # inside cmd_check's ack lookup — resolving here keeps both sides in
    # agreement on whatever path the platform's real temp dir resolves to.
    here = str(pathlib.Path("/tmp").resolve())

    base = {"title": "T", "statement": "T", "rationale": "flexible schema",
            "scope": "architecture", "created": today(),
            "tags": ["selftest-v-tag"], "topics": ["selftest-persistence"],
            "chose": ["mongo"], "rejected": ["postgres"], "supersedes": []}
    write_decision(s, {**base, "id": "selftest-v2",
                       "project_id": "/tmp/precedent-selftest-v2", "project_name": "vA"})
    write_decision(s, {**base, "id": "selftest-v3",
                       "project_id": "/tmp/precedent-selftest-v3", "project_name": "vB"})
    apply_regret(s, {"id": "selftest-v-lesson", "topic": "selftest-persistence",
                     "option": "mongo", "because": "schema drift",
                     "instead": "postgres",
                     "decisions": ["selftest-v2", "selftest-v3"], "created": today()})
    # A second lesson on a different topic. Ruling 5's bug bound the WHERE to
    # the OPTIONAL MATCH's leg, not the Lesson, so every lesson with a
    # statement came back regardless of topic — this must stay silent below.
    apply_regret(s, {"id": "selftest-v-lesson-other", "topic": "selftest-v-other-topic",
                     "option": "sqlite", "because": "unrelated mistake",
                     "instead": "mysql", "decisions": [], "created": today()})
    try:
        out = io.StringIO()
        args = _argparse.Namespace(topic="selftest-persistence",
                                   chose="postgres", project=here)
        with contextlib.redirect_stdout(out):
            cmd_check(args, s)
        text = out.getvalue()
        assert "CONFLICT" not in text, f"a regretted rejection must not fire CONFLICT:\n{text}"
        assert "LESSON" in text, f"the lesson that recommends this must fire:\n{text}"
        assert "schema drift" in text, text
        assert "clear —" not in text, text
        assert "unrelated mistake" not in text, \
            f"a lesson on a different topic must not fire here:\n{text}"

        out2 = io.StringIO()
        args2 = _argparse.Namespace(topic="selftest-persistence",
                                    chose="sqlite", project=here)
        s.q("MATCH (d:Decision) WHERE d.id STARTS WITH 'selftest-v' SET d.status='active'")
        with contextlib.redirect_stdout(out2):
            cmd_check(args2, s)
        div = out2.getvalue()
        assert "DIVERGENCE" in div, div
        assert f"last: {today()[:7]}" in div, f"the norm must carry its age:\n{div}"

        # Two norms on one topic: mongo (2 projects) and cassandra (2 projects).
        # The exception is recorded against the mongo one only, so the
        # cassandra warning must still fire unacknowledged.
        for n, pid in (("selftest-v4", "/tmp/precedent-selftest-v4"),
                       ("selftest-v5", "/tmp/precedent-selftest-v5")):
            write_decision(s, {**base, "id": n, "chose": ["cassandra"],
                               "rejected": [], "supersedes": [],
                               "project_id": pid, "project_name": n})
        write_decision(s, {**base, "id": "selftest-v6", "chose": ["sqlite"],
                           "rejected": [], "supersedes": [],
                           "despite": "single user, no concurrency",
                           "diverges_from": ["selftest-v2"],
                           "project_id": here, "project_name": "here"})
        out3 = io.StringIO()
        with contextlib.redirect_stdout(out3):
            cmd_check(_argparse.Namespace(topic="selftest-persistence",
                                          chose="sqlite", project=here), s)
        scoped = out3.getvalue()
        warnings = [l for l in scoped.splitlines() if "DIVERGENCE" in l]
        mongo = [l for l in warnings if "mongo" in l]
        cass = [l for l in warnings if "cassandra" in l]
        assert mongo and "acknowledged here" in mongo[0], f"{warnings}"
        assert cass and "acknowledged here" not in cass[0], \
            f"an unrelated norm must not be reported as acknowledged: {cass}"
    finally:
        s.q("MATCH (l:Lesson {id:'selftest-v-lesson'}) DETACH DELETE l")
        s.q("MATCH (l:Lesson {id:'selftest-v-lesson-other'}) DETACH DELETE l")
        s.q("MATCH (n) WHERE n.id STARTS WITH 'selftest-v' DETACH DELETE n")
        for pid in ("/tmp/precedent-selftest-v2", "/tmp/precedent-selftest-v3",
                    "/tmp/precedent-selftest-v4", "/tmp/precedent-selftest-v5", here):
            s.q("MATCH (p:Project {id:$id}) DETACH DELETE p", {"id": pid})
        for name in ("selftest-persistence", "selftest-v-other-topic",
                      "mongo", "postgres", "sqlite", "mysql", "cassandra",
                      "selftest-v-tag"):
            s.q("""MATCH (n) WHERE (n:Topic OR n:Option OR n:Tag) AND n.name = $name
                     AND NOT EXISTS { MATCH (n)<--() } DETACH DELETE n""", {"name": name})

    # A principle's prose can contain a topic word as a mere substring
    # ("selftest-author" contains "selftest-auth") without being ABOUT it.
    # Only the edge may decide — this is Task 5's exact-match rule applied
    # to Principle, which previously had no edge to match on at all.
    # project_id/project use `here`, not a literal "/tmp" — Task 10 fixed the
    # exact macOS mismatch a literal produces (/tmp resolves to /private/tmp),
    # and cmd_check's `diverged` branch is the part of this query that
    # resolves a.project through pathlib. It only runs when 2+ projects chose
    # a different option than $chose for the topic, which is not true for the
    # single-option fixture below — but a later block on this same topic
    # would make it true, so this stays consistent with Task 7's fixture
    # rather than relying on that being dead code forever.
    pd = {"id": "selftest-p1", "title": "T", "statement": "T", "rationale": "r",
          "scope": "architecture", "created": today(),
          "project_id": here, "project_name": "p",
          "tags": [], "topics": ["selftest-auth"], "chose": ["oidc"],
          "rejected": [], "supersedes": []}
    try:
        write_decision(s, pd)
        # The prose contains the topic word as a substring. Only the edge
        # should decide, so this principle must NOT surface for selftest-auth.
        s.q("""MERGE (pr:Principle {id:'selftest-p'})
               SET pr.statement='Prefer one selftest-author per module.',
                   pr.created=$c""", {"c": today()})
        s.q("MERGE (t:Topic {name:'selftest-p-other'})")
        s.q("""MATCH (pr:Principle {id:'selftest-p'}),(t:Topic {name:'selftest-p-other'})
               MERGE (pr)-[:ABOUT]->(t)""")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd_check(_argparse.Namespace(topic="selftest-auth",
                                          chose="oidc", project=here), s)
        text = out.getvalue()
        assert "PRINCIPLE" not in text, \
            f"'selftest-author' in the prose must not match topic 'selftest-auth':\n{text}"

        s.q("""MATCH (pr:Principle {id:'selftest-p'}),(t:Topic {name:'selftest-auth'})
               MERGE (pr)-[:ABOUT]->(t)""")
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            cmd_check(_argparse.Namespace(topic="selftest-auth",
                                          chose="oidc", project=here), s)
        assert "PRINCIPLE" in out2.getvalue(), \
            f"a principle with the topic edge must surface:\n{out2.getvalue()}"
    finally:
        s.q("MATCH (pr:Principle {id:'selftest-p'}) DETACH DELETE pr")
        s.q("MATCH (n) WHERE n.id STARTS WITH 'selftest-p' DETACH DELETE n")
        s.q("MATCH (p:Project {id:$id}) DETACH DELETE p", {"id": here})
        for name in ("selftest-auth", "selftest-p-other", "oidc"):
            s.q("""MATCH (n) WHERE (n:Topic OR n:Option) AND n.name = $name
                     AND NOT EXISTS { MATCH (n)<--() } DETACH DELETE n""", {"name": name})

    # cmd_principle and replay_entry both mutate the graph the same way the
    # existing tests above never touch: through s.log, which appends a real
    # line to the journal. Exercising that against the running selftest
    # store would grow /tmp/precedent-plan/journal.jsonl on every run and
    # break "leave the filesystem exactly as it found it" — so this runs
    # against its own throwaway Store in its own throwaway directory, which
    # tempfile.TemporaryDirectory removes completely on exit. Without this,
    # the query fix above is tested but the feature that populates the edge
    # (--topic on `principle`, and its replay) is not — the same shape as
    # Task 2b's lock-mode check once proving the library rather than the
    # code's use of it.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp_home:
        with Store(pathlib.Path(tmp_home), write=True) as ps:
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_principle(_argparse.Namespace(
                    id="selftest-p2", statement="selftest-p2 statement",
                    derived_from="", topic="selftest-p2-topic"), ps)
            written = ps.q("""MATCH (pr:Principle {id:'selftest-p2'})
                                    -[:ABOUT]->(t:Topic {name:'selftest-p2-topic'})
                              RETURN pr.id AS id""")
            assert written, "cmd_principle --topic must create the ABOUT edge"

            # Drop the edge to simulate a graph rebuilt from the journal
            # alone, then replay the exact payload cmd_principle wrote
            # (topics included) and confirm replay recreates it. This is the
            # silent-data-loss path: without it, a principle recorded today
            # would carry its topics in the journal but lose them on the
            # next rebuild, and `check` would stop surfacing it with no
            # error anywhere.
            ps.q("""MATCH (:Principle {id:'selftest-p2'})-[r:ABOUT]->(:Topic)
                    DELETE r""")
            gone = ps.q("""MATCH (pr:Principle {id:'selftest-p2'})-[:ABOUT]->(:Topic)
                           RETURN pr.id AS id""")
            assert not gone, "edge must be gone before replay is exercised"
            replay_entry(ps, {"op": "principle", "id": "selftest-p2",
                              "statement": "selftest-p2 statement",
                              "derived_from": [], "topics": ["selftest-p2-topic"],
                              "ts": today()})
            replayed = ps.q("""MATCH (pr:Principle {id:'selftest-p2'})
                                     -[:ABOUT]->(t:Topic {name:'selftest-p2-topic'})
                               RETURN pr.id AS id""")
            assert replayed, "replay_entry must recreate the ABOUT edge from topics"


def _check_maintain(s: Store) -> None:
    """One decision choosing redis AND memcached is a stack, not a clash.
    Two decisions in one project choosing differently is the real thing.
    """
    base = {"title": "T", "statement": "T", "rationale": "r",
            "scope": "architecture", "created": today(),
            "project_id": "/tmp/precedent-selftest-m", "project_name": "m",
            "tags": [], "topics": ["selftest-caching"],
            "rejected": [], "supersedes": []}
    try:
        write_decision(s, {**base, "id": "selftest-m1", "chose": ["redis", "memcached"]})
        assert contradictions_in(s) == [], "a multi-option decision is not a clash"
        write_decision(s, {**base, "id": "selftest-m2", "chose": ["hazelcast"]})
        clash = contradictions_in(s)
        assert len(clash) == 1 and clash[0]["topic"] == "selftest-caching", clash
        assert set(clash[0]["decisions"]) == {"selftest-m1", "selftest-m2"}, clash

        # "The directory is not here" was never evidence a project is dead. It is
        # equally consistent with an unmounted drive, another checkout, or a
        # machine you are not sitting at — and dead projects get discounted.
        #
        # Each fixture is shaped for the platform running the check, because
        # liveness answers relative to os.sep and a literal would encode one
        # platform's answer as if it were the rule. `foreign` is a path shaped
        # for the OTHER platform whichever one this is — on POSIX a Windows
        # drive path, on Windows a POSIX absolute path — and `dead` is a
        # native-shaped path that does not exist, which is the only shape that
        # can legitimately read as `gone`.
        import tempfile
        absent = "nonexistent-precedent-selftest"
        foreign = f"C:\\{absent}\\x" if os.sep == "/" else f"/{absent}/x"
        dead = os.path.join(os.sep + absent, "x")
        with tempfile.TemporaryDirectory() as live:
            assert liveness({"id": live, "paths": live}) == "live"
            assert liveness({"id": live, "paths": f"{dead}\n{live}"}) == "live", \
                "live if ANY known path exists"
        assert liveness({"id": foreign, "paths": foreign}) == "elsewhere", \
            "a path shaped for another platform is unknown, not dead"
        assert liveness({"id": dead, "paths": dead}) == "gone"
    finally:
        s.q("MATCH (n) WHERE n.id STARTS WITH 'selftest-m' DETACH DELETE n")
        s.q("MATCH (p:Project {id:'/tmp/precedent-selftest-m'}) DETACH DELETE p")
        for name in ("selftest-caching", "redis", "memcached", "hazelcast"):
            s.q("""MATCH (n) WHERE (n:Topic OR n:Option) AND n.name = $name
                     AND NOT EXISTS { MATCH (n)<--() } DETACH DELETE n""", {"name": name})


def _check_identity(s: Store) -> None:
    """A home-relative id was rejected: /home/u/src/api and C:\\dev\\api are the
    same repo at different relative paths, and two different projects can sit
    at the same relative path on two machines. The remote is the identity.
    """
    for raw, want in [
        ("https://github.com/asm0dey/precedent.git", "github.com/asm0dey/precedent"),
        ("git@github.com:asm0dey/precedent.git", "github.com/asm0dey/precedent"),
        ("ssh://git@github.com/asm0dey/precedent.git", "github.com/asm0dey/precedent"),
        ("https://user:token@github.com/o/r.git", "github.com/o/r"),
        ("ssh://git@host:2222/o/r.git", "host/o/r"),
        ("https://GitHub.com/Asm0dey/Precedent", "github.com/asm0dey/precedent"),
        ("", None),
        # A numeric first path segment must not be mistaken for a port: scp-style
        # syntax has no port, only a scheme URL can carry one (Ruling 24).
        ("git@host:12345/repo.git", "host/12345/repo"),
        ("git@host:67890/repo.git", "host/67890/repo"),
    ]:
        assert normalise_remote(raw) == want, f"{raw!r} -> {normalise_remote(raw)!r}"

    # …and the direction that cannot be undone. Convergence that goes too far
    # merges two unrelated histories onto one node, and the journal is
    # append-only, so a wrong merge cannot be edited back out. Same shape as
    # _check_drift's false-match list.
    for a_, b_ in [("https://github.com/o/r.git", "https://gitlab.com/o/r.git"),
                   ("git@github.com:o/r.git", "git@github.com:o/r2.git"),
                   ("git@github.com:o/r.git", "git@github.com:o2/r.git")]:
        assert normalise_remote(a_) != normalise_remote(b_), \
            f"distinct remotes collapsed: {a_} ~ {b_} -> {normalise_remote(a_)!r}"

    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "repo"
        (root / "mod").mkdir(parents=True)
        run = lambda *a: subprocess.run(["git", "-C", str(root), *a],
                                        capture_output=True, check=True)
        assert portable_id(root) is None, "no git dir yet"
        run("init", "-q")
        assert portable_id(root) is None, "a repo with no origin has no portable id"
        run("remote", "add", "origin", "git@github.com:asm0dey/precedent.git")
        assert portable_id(root) == "github.com/asm0dey/precedent"
        assert portable_id(root / "mod") == "github.com/asm0dey/precedent#/mod"

    # Two machines, one repo: the second must resolve onto the first node,
    # for reads AND writes. Read-only resolution finds the existing node
    # while writes create a second one, fragmenting the graph a little more
    # with every machine and every session, invisibly.
    linux, windows = "/tmp/precedent-selftest-i", "C:\\dev\\precedent-selftest-i"
    unrelated = "/tmp/precedent-selftest-i-other"
    pp = "github.com/asm0dey/selftest-i"
    pp2 = "github.com/asm0dey/selftest-i-other"
    try:
        a_info = upsert_project(s, {"id": linux, "path": linux, "name": "i",
                                    "portable": pp})
        b_info = upsert_project(s, {"id": windows, "path": windows, "name": "i",
                                    "portable": pp})
        assert a_info["id"] == linux, a_info
        assert b_info["id"] == linux, "the second sighting must resolve onto the first"
        assert s.q("MATCH (p:Project {portable:$pp}) RETURN count(p) AS n",
                   {"pp": pp}) == [{"n": 1}], "one repo, one node"
        row = s.q("MATCH (p:Project {id:$id}) RETURN p.paths AS paths", {"id": linux})[0]
        assert set(paths_of(row)) == {linux, windows}, row

        # The other direction, and the one that cannot be undone: convergence
        # is worth nine assertions above, but a resolver that over-converged
        # would fold two unrelated repos into one node and merge their
        # histories in an append-only store. A different remote must key a
        # different node even when everything else about the sighting matches
        # — same name, sibling path, same session.
        c_info = upsert_project(s, {"id": unrelated, "path": unrelated, "name": "i",
                                    "portable": pp2})
        assert c_info["id"] == unrelated, \
            "a different remote must not resolve onto another repo's node"
        assert set(paths_of(s.q("MATCH (p:Project {id:$id}) RETURN p.paths AS paths",
                                {"id": unrelated})[0])) == {unrelated}, \
            "a distinct repo must not inherit the other one's paths"
        assert s.q("MATCH (p:Project {portable:$pp}) RETURN count(p) AS n",
                   {"pp": pp}) == [{"n": 1}], "two repos, two nodes"
        assert s.q("MATCH (p:Project) WHERE p.portable IN $pps RETURN count(p) AS n",
                   {"pps": [pp, pp2]}) == [{"n": 2}], "two repos, two nodes"
    finally:
        for pid in (linux, windows, unrelated):
            s.q("MATCH (p:Project {id:$id}) DETACH DELETE p", {"id": pid})


def _check_identity_gaps(s: Store) -> None:
    """The split a pre-portable project falls into, and what may be claimed
    about it.

    Both directions matter. Missing the split leaves a stranded history
    nothing reports; claiming one that is not there points a user at a merge
    that would fold two unrelated projects together in an append-only store.
    So the twin is asserted present where the local checkout's own remote
    proves it, and asserted ABSENT where nothing but a resemblance would
    supply it.
    """
    import subprocess
    import tempfile

    pp = "github.com/asm0dey/selftest-gap"
    holder = "/tmp/precedent-selftest-gap-holder"
    ids = [holder]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            split = pathlib.Path(tmp) / "split"   # a real checkout of that repo
            bare = pathlib.Path(tmp) / "bare"     # a repo that has no origin
            for d in (split, bare):
                d.mkdir()
                subprocess.run(["git", "-C", str(d), "init", "-q"],
                               check=True, capture_output=True)
            subprocess.run(["git", "-C", str(split), "remote", "add", "origin",
                            "git@github.com:asm0dey/selftest-gap.git"],
                           check=True, capture_output=True)
            ids += [str(split), str(bare)]

            # The second machine, which claimed the remote first…
            upsert_project(s, {"id": holder, "path": holder, "name": "gap",
                               "portable": pp})
            # …and this machine's older node for the same repo, recorded before
            # portable ids existed. brief cannot heal it: project_info resolves
            # onto the holder, so backfill_portable sees a non-None portable.
            upsert_project(s, {"id": str(split), "path": str(split),
                               "name": "gap", "portable": None})
            upsert_project(s, {"id": str(bare), "path": str(bare),
                               "name": "gap", "portable": None})

            gaps = {g["id"]: g for g in identity_gaps(s)}
            assert holder not in gaps, "a project that HAS an identity is not a gap"
            assert str(split) in gaps and str(bare) in gaps, gaps
            twin = gaps[str(split)]["twin"]
            assert twin and twin["id"] == holder, \
                f"the split must be reported against the node holding the remote: {gaps}"
            assert gaps[str(split)]["portable"] == pp, gaps[str(split)]
            # Same name, sibling path, same session — everything a heuristic
            # would match on. It has no remote, so nothing may be claimed.
            assert gaps[str(bare)]["portable"] is None, gaps[str(bare)]
            assert gaps[str(bare)]["twin"] is None, \
                "a repo with no remote must not be paired by resemblance"
    finally:
        for pid in ids:
            s.q("MATCH (p:Project {id:$id}) DETACH DELETE p", {"id": pid})


def _check_backfill_replay() -> None:
    """A backfilled portable id must survive a rebuild — Ruling 26.

    graph.db is the index; journal.jsonl is the source of truth, and
    `rebuild` replays it into a fresh graph. A mutation with no journal
    entry is invisible to that replay and is lost the moment someone runs
    `rebuild` — silently, since nothing about `brief` or `rebuild` fails.

    This exercises replay_entry directly rather than cmd_rebuild, the way
    _check_journal does, because rebuild deletes every node first.

    Takes no Store: backfill_portable calls Store.log, so running it against
    the selftest's own store appends a real line to a real append-only
    journal on every run — permanently, and to the user's own journal when
    `selftest` runs without --home. The node-count invariant cannot see it,
    because the node is deleted and only the journal grows; a later `rebuild`
    then counts those lines as legitimate history. Same rule and same remedy
    as _check_verdicts' cmd_principle block: a throwaway Store in a
    throwaway directory, which TemporaryDirectory removes on the way out.
    """
    import tempfile

    pid = "/tmp/precedent-selftest-bp"
    portable = "github.com/asm0dey/selftest-bp"
    # A throwaway store starts with no journal at all, so every read of it
    # here tolerates that: a bare open() would raise FileNotFoundError and
    # hide the assertion that is the point of the check.
    def journal_lines(st: Store) -> list[str]:
        return (st.journal.read_text(encoding="utf-8").splitlines()
                if st.journal.exists() else [])

    with tempfile.TemporaryDirectory() as tmp_home:
        with Store(pathlib.Path(tmp_home), write=True) as s:
            # An existing, portable-less node — the exact shape backfill_portable
            # requires before it will write anything.
            upsert_project(s, {"id": pid, "path": pid, "name": "bp", "portable": None})
            before = len(journal_lines(s))

            backfill_portable(s, {"id": pid, "portable": portable})
            assert s.q("MATCH (p:Project {id:$id}) RETURN p.portable AS pp",
                       {"id": pid}) == [{"pp": portable}]
            lines = journal_lines(s)
            assert len(lines) == before + 1, \
                "backfill_portable must journal exactly one line, or rebuild forgets it"
            entry = json.loads(lines[-1])
            assert entry["op"] == "project_portable", entry
            assert entry["project_id"] == pid and entry["portable"] == portable, entry
            # Spec A4: every journal line carries its schema version. Without
            # the stamp, replay_entry's refusal of a future version has
            # nothing to read and a v2 line replays as v1 — the exact
            # half-succeeding rebuild the version exists to prevent.
            assert entry.get("v") == SCHEMA, \
                f"Store.log must stamp the schema version on every line: {entry}"

            # And the stamp must win over the payload. It is spread AFTER
            # **payload for this reason, so a payload key called "v" cannot
            # shadow it; written the other way round the stamping stops with
            # no visible error anywhere.
            s.log("project_portable", {"project_id": pid, "portable": portable, "v": 99})
            shadowed = json.loads(journal_lines(s)[-1])
            assert shadowed.get("v") == SCHEMA, \
                f"a payload key named 'v' must not shadow the schema stamp: {shadowed}"

            # Simulate the loss `rebuild` would cause without it: what a graph
            # freshly rebuilt up to (but not including) this journal line would
            # look like, then replay just this line.
            s.q("MATCH (p:Project {id:$id}) SET p.portable=null", {"id": pid})
            assert s.q("MATCH (p:Project {id:$id}) RETURN p.portable AS pp",
                       {"id": pid}) == [{"pp": None}]
            replay_entry(s, entry)
            assert s.q("MATCH (p:Project {id:$id}) RETURN p.portable AS pp",
                       {"id": pid}) == [{"pp": portable}], \
                "a backfilled portable id must survive a replay"

            # Idempotent: the node carries a portable id again, so a further
            # call must neither write nor journal.
            journalled = len(journal_lines(s))
            backfill_portable(s, {"id": pid, "portable": portable})
            assert len(journal_lines(s)) == journalled, \
                "backfill_portable must not re-journal once the node carries a portable id"

            # Replay must never create the node this line refers to — brief's own
            # never-create rule extends to the journal line it writes.
            s.q("MATCH (p:Project {id:$id}) DETACH DELETE p", {"id": pid})
            replay_entry(s, entry)
            assert s.q("MATCH (p:Project {id:$id}) RETURN p.id AS id", {"id": pid}) == [], \
                "replaying a project_portable line must not create a node"


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
    _check_journal(s)
    _check_lock_modes()
    _check_grafeo_readers()
    _check_verdicts(s)
    _check_maintain(s)
    _check_identity(s)
    _check_identity_gaps(s)
    _check_backfill_replay()
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
    pr.add_argument("--topic", default="", help="comma-separated topics this governs")
    pr.set_defaults(writes=True, fn=cmd_principle)

    tg = sub.add_parser("tag", help="list the tag vocabulary, or change this project's tags")
    tg.add_argument("--project", default=".")
    tg.add_argument("--add", default="", help="comma-separated tags to add")
    tg.add_argument("--remove", default="", help="comma-separated tags to remove")
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
                    help="directory to keep the store in; a pointer file is left at the default path")
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
