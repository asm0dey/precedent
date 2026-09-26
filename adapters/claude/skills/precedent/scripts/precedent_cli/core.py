from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time
from datetime import date



CLI = pathlib.Path(__file__).resolve().parent.parent / "precedent.py"
DEFAULT_HOME = pathlib.Path.home() / ".local/share/precedent"
HOME = pathlib.Path(os.environ.get("PRECEDENT_HOME", DEFAULT_HOME))
SCOPES = ("architecture", "business", "process", "tooling", "product")
# The fields an amendment may rewrite: how the decision was DESCRIBED.
# Everything else on a Decision — its topics, its options, its scope, the
# project it belongs to — is WHAT WAS DECIDED, and changing one of those is a
# different decision, which is `record --supersedes`. Also the whitelist that
# makes apply_amend's f-string SET clause safe.
AMENDABLE = ("title", "statement", "rationale")
POINTER = "location"
# The STANDING_ORDERS banner is this script's one copy; the session-start hook
# and SKILL.md both fetch it via `standing-orders` rather than holding their
# own text, so there is nothing else to keep in sync when this changes.
STANDING_ORDERS = """Standing orders for the rest of this session:
- Re-prime when you change project. The brief above covers the directory this
  session started in. After a `cd` into another project — or into a subproject of
  this one — run `precedent-prime` (plain: `uv run {cli} brief --project <dir>`)
  before your first substantive answer there. A brief from the wrong directory is
  worse than none, because it reads as this project's history.
- Before recommending a technology, framework, provider, or process choice, run
  `uv run {cli} check --topic <topic> --chose <option>` and lead with what it returns.
  Your opinion is worth less than what the user already chose and lived with.
- When a choice gets settled in conversation, offer to record it, then run
  `uv run {cli} record ...` with --rationale and --rejected. Ask first; a wrong
  entry is worse than a missing one because it gets quoted back as precedent.
- The user will not type a command. They say an ordinary sentence, and you notice:
  "let's go with X" -> `record`. "what did I use last time" -> `check`.
  "X was a mistake" / "X bit us" / "never again" -> `regret` (marks every project
  that chose it, so the graph stops arguing for it), NOT another `record`.
  "I usually do X, but here..." -> `record --despite`. Draft it, show one line,
  run it once they confirm.
- When a precedent here contradicts a ruling in another project, do not just note
  it — `check` reports that as DIVERGENCE, and leaving both live is the graph
  arguing with itself. Name both rulings and drive it to one of three ends: the
  pattern was wrong everywhere (`regret`), one side is simply out of date
  (`record --supersedes <id>`, in whichever project moved on), or the divergence
  is deliberate and gets written down as one (`record --despite`). The user picks;
  you make the fork visible and draft the command.
- Precedent is information, not a veto. Say when consistency is wrong here."""
SCHEMA = 1          # journal line format; bump only on a breaking change
GRAPH_FORMAT = 2    # graph layout; 2 = projects keyed by git remote (docs/adr/0009)


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
    # How long a command waits for another process's write before giving up.
    #
    # This was 30s, inherited from the file lock, where it was the right
    # number: that lock was held for a whole command, so a slow `rebuild`
    # really could make a `brief` wait that long. Nothing holds the database
    # across statements any more — each one is its own SQLite transaction —
    # so waits are per-statement and the inherited number was measuring a
    # world that no longer exists.
    #
    # Measured, 8 processes opening the store and writing back-to-back for 4
    # seconds, which is far harsher than N agent sessions recording:
    #
    #     timeout      writes   errors   slowest call
    #        10ms       9,425      618           38ms
    #        50ms       9,738       84           85ms
    #       250ms       9,806        2          260ms
    #      1,000ms      9,464        0          630ms
    #      5,000ms     10,646        0          832ms
    #     30,000ms     10,286        0          850ms
    #
    # Nothing ever waited past ~850ms even when allowed thirty seconds: the
    # queue drains, so the rest of the budget is unreachable. 5s is the first
    # round number with a comfortable margin over that, and it sits below the
    # SessionStart hook's own 20s timeout — so a command that really is stuck
    # reports for itself instead of being killed mid-write with no message.
    #
    # Pinned rather than left to the engine's default, which is 5s today: a
    # release that changed it would change this tool's behaviour under
    # contention, silently.
    BUSY_TIMEOUT_MS = 5_000

    def __init__(self, home: pathlib.Path = HOME, write: bool = True):
        home.mkdir(parents=True, exist_ok=True)
        self.home = home = resolve_home(home)
        home.mkdir(parents=True, exist_ok=True)
        self.db_path = home / "graph.db"
        self.journal = home / "journal.jsonl"
        # `write` no longer picks a lock mode — there is no lock. It still
        # says whether this command may change the store, and two things read
        # it: the grafeo-era migration below (only a writer may rebuild) and
        # `_check_lock_modes`, which pins the classification so a command
        # cannot start writing by accident.
        self.write = write
        # Set by _migrate_grafeo_store when it moves an old store aside, so
        # __enter__ knows to replay the journal into the fresh graph it then
        # opens. The replay cannot run before the db exists.
        self._pending_migration: pathlib.Path | None = None

    def __enter__(self):
        # No lock is acquired here. Writers are serialised by the engine:
        # concurrent CREATEs from separate processes either all land or raise
        # StorageError after BUSY_TIMEOUT_MS. Measured on Linux and macOS,
        # 8 processes x 200 writes with nothing coordinating them: 1600/1600
        # stored, zero errors. `_check_concurrent_writers` re-measures it in
        # CI on every OS this ships to, because the guarantee is the engine's
        # and not this file's to assert.
        import graphdblite

        self._migrate_grafeo_store()
        try:
            self.db = graphdblite.Database(str(self.db_path),
                                           busy_timeout_ms=self.BUSY_TIMEOUT_MS)
        except graphdblite.StorageError as exc:
            raise SystemExit(
                f"error: could not open the graph at {self.db_path}: {exc}\n"
                f"  the journal is the source of truth — `precedent.py rebuild` "
                f"replays it into a fresh graph")
        if self._pending_migration is not None:
            self._finish_migration()
        self._ensure_graph_format()
        return self

    def _migrate_grafeo_store(self) -> None:
        """Carry a store written by the previous engine across, once.

        grafeo kept `graph.db` as a DIRECTORY; graphdblite wants a file at
        that path and raises `unable to open database file` on the old one.
        Every store that predates this change is in that state, so leaving it
        to the user means the hook fails quietly on their next session — the
        one failure mode this project refuses. The journal is the source of
        truth and `rebuild` already replays it, so the migration is exactly
        that, run automatically.

        The old directory is renamed aside, never deleted: if the replay is
        wrong in some way nobody has thought of yet, the evidence is still on
        disk. A reader cannot do this — it is a write — so it says what to run
        instead of half-migrating under a command that promised not to change
        anything.
        """
        if not self.db_path.is_dir():
            return
        if not self.write:
            raise SystemExit(
                f"error: {self.db_path} was written by the previous graph engine.\n"
                f"  run `precedent.py rebuild` (or any command that records) to "
                f"replay the journal into the current one")
        # Renamed, not deleted, until the replay has proved itself — see
        # _finish_migration, which is what actually removes it.
        aside = self.db_path.with_name(f"graph.db.grafeo-{int(time.time())}")
        self.db_path.rename(aside)
        self._pending_migration = aside

    def _finish_migration(self) -> None:
        """Replay the journal into the new graph, then delete the old one.

        The old store is deleted rather than kept, and the reason is not
        tidiness. A precedent install that predates the engine swap still
        opens `graph.db` — a version of this plugin sitting in another
        agent's directory, an older ACR realisation, a checkout someone has
        not pulled. Left on disk, the old graph is a live store for those:
        they would read and write decisions the current engine never sees,
        and neither side would report anything wrong. Two stores that
        disagree is worse than one store that had to be rebuilt.

        What makes deleting safe is that the old graph was never the source
        of truth. `journal.jsonl` is, it is untouched by all of this, and
        `rebuild` reconstructs the graph from it at any time. Deleting a
        derived index whose source is intact loses nothing.

        A replay that could not read every entry is the one case where the
        old store may still hold something the journal does not, so it is
        kept and named. Everything else about the migration has already been
        proved by the replay itself.
        """
        aside = self._pending_migration
        self._pending_migration = None
        assert aside is not None
        from .replay import replay_journal  # lazy: replay imports core
        n, skipped = replay_journal(self)
        # Loud on stderr, not stdout: `brief`'s stdout is injected into a
        # model's context by the SessionStart hook, and a migration notice is
        # not precedent. It still has to be seen, so it is not silent.
        note = f"precedent: graph rebuilt from the journal for the current engine ({n} entries"
        if skipped:
            print(f"{note}; {len(skipped)} unreadable, so the previous store is kept "
                  f"at {aside.name})", file=sys.stderr)
            for msg in skipped:
                print(f"  skipped {msg}", file=sys.stderr)
            return
        import shutil

        # Sidecars of the old engine go with it, for the same reason: a
        # half-removed store is still something an old install can open.
        #
        # Removal is verified, not attempted. A store that has ever been
        # bind-mounted into a container holds root-owned files (see
        # _ensure_removable, which documents the same hazard for relocate),
        # and rmtree cannot delete those. Swallowing that error and printing
        # "removed" would leave the exact stale store this deletes to
        # prevent, while reporting that it was handled — measured on a real
        # store, where a root-owned default/data.grafeo survived.
        stuck = []
        for path in [aside] + sorted(self.home.glob("graph.db.spill*")):
            try:
                shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
            except OSError:
                pass
            if path.exists():
                stuck.append(path)
        if stuck:
            print(f"{note}; the previous store could NOT be removed)", file=sys.stderr)
            for path in stuck:
                print(f"  still present: {path}", file=sys.stderr)
            print("  an install predating this change can still open it, and would then "
                  "read and write decisions this one never sees. Delete it by hand — if "
                  "it was ever bind-mounted into a container, that needs root.",
                  file=sys.stderr)
            return
        print(f"{note}; the previous store has been removed — an install that predates "
              f"this change would otherwise keep writing to it unseen)", file=sys.stderr)

    CLAIM_STALE_S = 300   # ponytail: a crashed migrator delays everyone this long; a heartbeat if that ever bites

    def _claim_migration(self) -> bool:
        """One statement: claim the migration, or find someone already holds it.

        MERGE always finds-or-creates the Meta node; the WHERE after WITH is
        what makes the SET conditional on nobody holding a fresh claim, so two
        writers racing this cannot both see their own token come back.
        """
        tok = f"{os.getpid()}-{time.time()}"
        now = time.time()
        rows = self.q("""MERGE (m:Meta {id:'meta'})
                         WITH m WHERE coalesce(m.graph_format, 0) < $f
                           AND (m.claimed IS NULL OR m.claimed < $stale)
                         SET m.claim=$tok, m.claimed=$now
                         RETURN m.claim AS c""",
                      {"f": GRAPH_FORMAT, "stale": now - self.CLAIM_STALE_S,
                       "tok": tok, "now": now})
        return rows == [{"c": tok}]

    def _ensure_graph_format(self) -> None:
        """Re-key a graph laid out by an older precedent, once.

        Replaying the journal is the whole migration: upsert_project keys each
        project by its remote as the lines go in. Readers skip it — they must
        not write, and project_info resolves older keys for them. A 0.4.x
        `rebuild` wipes the marker along with everything else, so the next
        writer here simply migrates again; the journal is the same, so
        nothing is lost either way.
        """
        if not self.write:
            return
        row = self.q("MATCH (m:Meta {id:'meta'}) RETURN m.graph_format AS f")
        if row and (row[0]["f"] or 0) >= GRAPH_FORMAT:
            return
        if not self._claim_migration():
            # Someone else is migrating right now (or just finished and this
            # read raced it) — readers already resolve older keys, and a
            # second concurrent replay is exactly the interleaving this
            # guards against. ponytail: a write that races the claimant's
            # replay can be missing from the graph until the next `rebuild`
            # — the same window a manual `rebuild` already has.
            return
        if not self.journal.exists():
            from .replay import mark_graph_format  # lazy: replay imports core
            mark_graph_format(self)
            return
        from .replay import replay_journal  # lazy: replay imports core
        n, skipped = replay_journal(self)
        # stderr: brief's stdout is injected into a model's context.
        print(f"precedent: graph re-keyed by git remote ({n} journal entries)",
              file=sys.stderr)
        for msg in skipped:
            print(f"  skipped {msg}", file=sys.stderr)

    def __exit__(self, *exc):
        try:
            self.db.close()
        except Exception:
            pass
        return False

    def q(self, cypher: str, params: dict | None = None) -> list[dict]:
        """Run one statement. No explicit transaction, on purpose.

        The engine this replaced needed `begin_transaction()` around every
        write, because its WAL recorded ONE PROPERTY PER RECORD: `SET n.a=$i,
        n.b=$i` was one statement but two records with no commit boundary
        joining them, so a second process could read the new `a` beside the
        stale `b`. graphdblite runs a bare statement as its own SQLite
        transaction, which is the boundary that was missing. Measured rather
        than assumed, on macOS where the old tear reproduced: 4 readers
        against a live writer, 11,956 reads, zero torn — the same harness,
        same machine, showed 15 torn reads out of 873 on the old engine.

        `_check_reader_isolation` runs that measurement in CI, so a future
        engine or version that loses the property fails there rather than in
        somebody's verdict.
        """
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
