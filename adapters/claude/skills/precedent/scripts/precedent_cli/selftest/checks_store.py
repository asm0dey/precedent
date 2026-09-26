from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

from ..admin import cmd_standing_orders, export_to, relocate
from ..cli import build_parser, wants_write
from ..core import CLI, AMENDABLE, GRAPH_FORMAT, JournalTooNew, POINTER, SCHEMA, Store, csv, slug, today
from ..replay import replay_entry, replay_journal


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
    offenders = [str(f) for f in [CLI, *CLI.parent.joinpath("precedent_cli").rglob("*.py")]
                 if banned in f.read_text(encoding="utf-8")]
    assert not offenders, (
        f"{banned} does not exist on Windows and this CLI must start there."
        " Nothing in this file locks any more — the engine serialises writers"
        " itself; see _check_concurrent_writers.", offenders)


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
    # An export that writes a file nothing can open is indistinguishable from
    # a good one until someone needs it, so this opens the copy and reads it
    # back through a real Store rather than stat-ing the bytes.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        exp = pathlib.Path(td) / "export"
        export_to(s, exp)
        assert (exp / "graph.db").stat().st_size > 0, "export wrote no graph"
        # Only when there is one to copy: a store that has recorded nothing
        # has no journal, and an export of it is legitimately graph-only.
        assert (exp / "journal.jsonl").exists() == s.journal.exists(), (
            "export wrote a graph with no journal — the copy cannot be rebuilt")
        with Store(exp, write=False) as copy:
            n = copy.q("MATCH (n) RETURN count(n) AS n")[0]["n"]
        live = s.q("MATCH (n) RETURN count(n) AS n")[0]["n"]
        assert n == live, f"export holds {n} nodes, the store has {live}"


def _check_concurrent_writers() -> None:
    """Concurrent writers must all land, or fail loudly. Never silently drop.

    This is the check that replaced a file lock. precedent is N short-lived
    processes on one store — a SessionStart hook running `brief`, a model
    running `check`, a user running `record`, times however many sessions are
    open — and nothing coordinates them. The previous engine could not take
    that: 6 processes x 20 writes stored 60 of 120 on macOS and 40 of 120 on
    Linux, every process exiting 0, nothing raised. 8 x 200 stored 569 of
    1600. Silent loss in a store whose whole job is remembering is the worst
    failure this project has.

    graphdblite serialises writers through SQLite and waits up to
    BUSY_TIMEOUT_MS before raising StorageError. Measured unlocked, 8
    processes x 200 writes: 1600/1600 on Linux and on macOS, no errors. That
    is a property of someone else's engine on someone else's OS, which is
    exactly the kind of claim this project does not take on trust — Windows
    is where the old engine's worst behaviour showed up, and CI runs this
    there on every push.

    Each writer is its own `Store(home, write=True)` per write — acquire
    nothing, open, write, close — because that is the real shape of a
    precedent command. A single Store held across a loop would test one
    process's connection, not two processes' contention.

    A write that RAISES is not a failure of this check as long as nothing is
    lost: loud contention is a supported outcome (the caller sees a non-zero
    exit and a message), silent loss is not. So the assertion is on the sum:
    every write either stored a row or reported itself.
    """
    import subprocess
    import tempfile

    WRITERS = 6
    PER_WRITER = 20

    scripts_dir = str(CLI.parent)
    # A subprocess cannot `import precedent` — this file is a script, not an
    # installed package — so it loads this exact module by path.
    writer_src = (
        "import sys, json, pathlib\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import precedent_cli.core as precedent\n"
        "home = pathlib.Path(sys.argv[2])\n"
        "wid, n = sys.argv[3], int(sys.argv[4])\n"
        "stored = raised = 0\n"
        "for i in range(n):\n"
        "    try:\n"
        "        with precedent.Store(home, write=True) as s:\n"
        "            s.q('CREATE (:WriteProbe {k:$k})', {'k': wid + '-' + str(i)})\n"
        "        stored += 1\n"
        "    except BaseException:\n"
        "        raised += 1\n"
        "print(json.dumps({'stored': stored, 'raised': raised}), flush=True)\n")

    # ignore_cleanup_errors: these checks fork processes that hold the store
    # open and then kill them, and Windows refuses to delete a file whose
    # handle has not drained yet — observed as WinError 32 on windows-latest
    # after a passing check. A few KB left in the temp directory is not worth
    # a red build that says nothing about the property under test.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        home = pathlib.Path(tmp) / "home"
        with Store(home, write=True):
            pass                      # create the store before racing on it
        procs = [subprocess.Popen(
            [sys.executable, "-c", writer_src, scripts_dir, str(home), f"w{w}", str(PER_WRITER)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for w in range(WRITERS)]
        claimed = raised = 0
        for i, pr in enumerate(procs):
            try:
                out, err = pr.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                pr.kill()
                raise AssertionError(
                    f"writer {i} never finished {PER_WRITER} writes — a writer that "
                    "blocks forever is as bad as one that loses data")
            assert pr.returncode == 0, f"writer {i} died: {err.strip()[-500:]}"
            res = json.loads(out)
            claimed += res["stored"]
            raised += res["raised"]
        with Store(home, write=False) as s:
            actual = s.q("MATCH (n:WriteProbe) RETURN count(n) AS n")[0]["n"]

        expected = WRITERS * PER_WRITER
        assert claimed + raised == expected, (
            f"{expected - claimed - raised} of {expected} writes neither stored nor "
            "raised — they vanished between the two")
        assert actual == claimed, (
            f"{claimed} writes reported success but {actual} are in the graph: "
            f"{claimed - actual} were lost in silence, which is the exact failure "
            "the removed file lock used to prevent. Do not paper over this by "
            "reintroducing a lock without re-reading the engine's guarantees.")
        assert raised == 0, (
            f"{raised} of {expected} writes raised under {WRITERS}-way contention "
            f"within {Store.BUSY_TIMEOUT_MS}ms. Nothing was lost, but a command "
            "failing under ordinary concurrency is a regression worth seeing.")


def _check_reader_isolation() -> None:
    """A reader must never observe a torn write, with nothing but the engine.

    This is the second property the file lock used to provide, and the one
    that is easiest to lose quietly. Torn means a reader sees `[[N, N-1]]` —
    the new value of one property beside the stale value of the other — from
    a statement that set both. A verdict computed from a torn read is wrong
    and says nothing about being wrong.

    The previous engine tore because its WAL recorded ONE PROPERTY PER RECORD
    (`WalRecord::SetNodeProperty`): `SET n.a=$i, n.b=$i` was one statement but
    two records with no cross-process commit boundary between them. Wrapping
    the WRITER in a transaction was tried and did not fix it — a transaction
    bounds what the writer's own process sees, not what a second process sees
    mid-append — so the lock had to keep readers and writers apart entirely.

    graphdblite runs each statement as its own SQLite transaction, which is
    the boundary that was missing, so readers and writers may now overlap.
    Measured before it was believed, on macOS, where the old tear reproduced
    and Linux never did: same harness, same machine, 4 readers against a live
    writer — the old engine gave 15 torn reads out of 873, graphdblite gave 0
    out of 11,956. Linux showing nothing proves nothing here; that is why this
    runs on every OS in CI.

    Each reader reopens the store every round rather than opening once and
    looping. A reader that opens once sees one frozen value for as long as the
    writer runs, can never tear, and would pass vacuously — and reopening is
    the true shape of every precedent command anyway: open, query, close.

    Two honesty guards, kept from the locked version because this needs them
    just as much: readers must have watched the writer's value ADVANCE (more
    than one distinct value seen — a reader stuck on one value could never
    tear and would pass for free), and the writer must still be alive when the
    readers are reaped (a writer that quit early means nothing was read under
    contention). Both are asserted below.

    If a future engine, version, or "optimisation" loses single-statement
    atomicity across processes, the tear comes back and this assertion is what
    catches it — before it reaches a verdict somebody acts on.
    """
    import subprocess
    import tempfile

    WRITE_BUDGET = 10.0  # wall clock, not iterations, so the run is bounded
    READ_BUDGET = 1.5    # ends inside the writer's window, from both sides
    WARMUP = 0.4         # let the node exist before readers look for it
    READERS = 4          # several Store(write=False) lifecycles, not just a pair
    STUCK = 5.0          # a reader past this is wedged, not slow
    WRITE_PACE = 0.005   # a realistic gap between commands; see the note below
    # WRITE_BUDGET is an upper bound nothing waits on: the writer is killed as
    # soon as the readers are reaped, so raising it costs no wall clock and
    # only widens the margin behind the `writer.poll()` assertion below.
    # Since the budget is free, it is 10 — comfortably above the ~2s this
    # check actually takes.
    #
    # WRITE_PACE: nothing issues precedent commands back-to-back at loop
    # speed, so an unpaced writer is the unrealistic case, not the paced one.
    # It also keeps a reader from spending its whole budget waiting on
    # BUSY_TIMEOUT_MS behind a writer that never pauses, which would starve
    # the distinct-values guard below and fail the check for the wrong reason.

    scripts_dir = str(CLI.parent)

    # A subprocess can't `import precedent` — this file is a script, not an
    # installed package — so it loads this exact module by path instead.
    # Both loops use real `Store`, so they open the graph exactly the way
    # every command does — same engine, same busy timeout, no lock.
    writer_src = (
        "import sys, time, pathlib\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import precedent_cli.core as precedent\n"
        "home = pathlib.Path(sys.argv[2])\n"
        "end = time.monotonic() + float(sys.argv[3])\n"
        "pace = float(sys.argv[4])\n"
        "i = 0\n"
        "while time.monotonic() < end:\n"
        "    i += 1\n"
        "    with precedent.Store(home, write=True) as s:\n"
        "        s.q(\"MERGE (n:RWProbe {id:'p'}) SET n.a=$i, n.b=$i\", {'i': i})\n"
        "    time.sleep(pace)\n"
        "print(i, flush=True)\n")

    reader_src = (
        "import sys, time, json, pathlib\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import precedent_cli.core as precedent\n"
        "home = pathlib.Path(sys.argv[2])\n"
        "end = time.monotonic() + float(sys.argv[3])\n"
        "seen, torn = set(), []\n"
        "while time.monotonic() < end:\n"
        "    with precedent.Store(home, write=False) as s:\n"
        "        for r in s.q(\"MATCH (n:RWProbe {id:'p'}) RETURN n.a AS a, n.b AS b\"):\n"
        "            seen.add(r['a'])\n"
        "            if r['a'] != r['b']:\n"
        "                torn.append([r['a'], r['b']])\n"
        "print(json.dumps({'distinct': len(seen), 'torn': torn[:5]}), flush=True)\n")

    # ignore_cleanup_errors: these checks fork processes that hold the store
    # open and then kill them, and Windows refuses to delete a file whose
    # handle has not drained yet — observed as WinError 32 on windows-latest
    # after a passing check. A few KB left in the temp directory is not worth
    # a red build that says nothing about the property under test.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        # A `home` for Store to build graph.db/journal.jsonl under —
        # never the real store; each run gets a fresh, disposable one.
        home = pathlib.Path(tmp) / "home"
        writer = subprocess.Popen(
            [sys.executable, "-c", writer_src, scripts_dir, str(home),
             str(WRITE_BUDGET), str(WRITE_PACE)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        readers: list[subprocess.Popen] = []
        try:
            time.sleep(WARMUP)
            for _ in range(READERS):
                readers.append(subprocess.Popen(
                    [sys.executable, "-c", reader_src, scripts_dir, str(home), str(READ_BUDGET)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            results = []
            for i, r in enumerate(readers):
                # Bounded like every other wait here. A reader wedged behind
                # a writer — BUSY_TIMEOUT_MS is 30s, twenty times this
                # budget — is the very thing this check probes, and it must
                # fail loudly rather than hang the selftest; the finally
                # below reaps it either way.
                try:
                    out, err = r.communicate(timeout=READ_BUDGET + STUCK)
                except subprocess.TimeoutExpired:
                    raise AssertionError(
                        f"reader {i} outlived a {READ_BUDGET:g}s budget by {STUCK:g}s — "
                        "it is wedged inside the engine, not merely slow")
                assert r.returncode == 0, (
                    f"reader {i} crashed while the writer was running: {err.strip()[-500:]}")
                results.append(json.loads(out))
            if writer.poll() is not None:
                raise AssertionError(
                    "the writer stopped before the readers did, so nothing was read "
                    f"under contention: {writer.communicate()[1].strip()[-500:]}")
            for i, res in enumerate(results):
                assert not res["torn"], (
                    f"reader {i} observed a torn write {res['torn']}: one statement set "
                    "both properties, so a reader that sees one new and one stale value "
                    "means the engine is not giving a bare statement a cross-process "
                    "commit boundary. Verdicts computed from this are wrong and look "
                    "right. This is what the removed file lock used to prevent.")
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
    """Every command's write classification, against a table written by hand.

    build_parser's `writes=True` fallback covers a subparser that forgets the
    flag. Nothing covers one that sets it wrongly. With the file lock gone the
    flag no longer picks a lock mode, but it is not decorative: a command
    marked False may not migrate a grafeo-era store (see
    `Store._migrate_grafeo_store`), so a writer misfiled as a reader dies on
    the first store that needs carrying across instead of carrying it.

    The expected mapping is literal, not derived from the parser. Deriving it
    would track whatever the table says and never fail; written out, changing
    a command's mode means changing it here too, on purpose.
    """
    import argparse

    expected = {
        # read-only in the sense that matters: they settle no decision
        "check": False, "suggest": False, "export": False,
        "maintain": False,      # until --apply; see wants_write()
        "standing-orders": False,  # returns before any Store is opened
        # writers
        "record": True, "tag": True, "regret": True, "principle": True,
        "rebuild": True, "selftest": True,
        "amend": True,          # rewords a decision in place
        "brief": True,          # settle() folds older keys
        "cypher": True,         # arbitrary query text; CREATE is unknowable up front
        "init": True,           # returns before any Store is opened
        "merge-project": True,  # folds one project into another
    }
    p = build_parser()
    subparsers = [x for x in p._actions if isinstance(x, argparse._SubParsersAction)]
    assert len(subparsers) == 1, "expected exactly one subparser group"
    actual = {name: sp.get_default("writes") for name, sp in subparsers[0].choices.items()}
    assert actual == expected, (
        "command lock modes changed — a writer classified as a reader corrupts "
        f"the graph, so confirm this on purpose:\n  got      {actual}\n  expected {expected}")

    # maintain is the derived case, and the derivation is the thing that can rot.
    assert wants_write(p.parse_args(["maintain"])) is False, "maintain must read"
    assert wants_write(p.parse_args(["maintain", "--apply"])) is True, (
        "maintain --apply deletes nodes and must take the write lock")
    # and the flag still reaches the mode for an ordinary reader and writer
    assert wants_write(p.parse_args(["check", "--topic", "t"])) is False
    assert wants_write(p.parse_args(["record", "--title", "t"])) is True

    # AMENDABLE is documented as the single whitelist of fields `amend` can
    # reword, but it is not the only place that knows the three names:
    # cmd_amend's `was` query and this `amend` subparser's --flags each spell
    # them out by hand. Binding AMENDABLE to the subparser's actual options
    # here means a fourth member added to AMENDABLE without updating both
    # other sites fails the suite instead of raising AttributeError on the
    # first `amend --<new-field>` a user tries.
    amend_dests = {act.dest for act in subparsers[0].choices["amend"]._actions
                   if act.dest not in ("help", "id")}
    assert amend_dests == set(AMENDABLE), (
        "AMENDABLE and the `amend` subparser's --flags have drifted apart — "
        f"got {amend_dests}, expected {set(AMENDABLE)}. AMENDABLE is meant to "
        "be the single whitelist: update the matching add_argument calls in "
        "build_parser's `amend` subparser and the was-query in cmd_amend "
        "alongside it.")


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


def _check_graph_format() -> None:
    """An older store is re-keyed once, by a writer, and never again; a
    too-new journal is refused before anything is wiped.
    """
    import contextlib
    import io
    import tempfile

    pp, old = "github.com/asm0dey/selftest-gf", "/tmp/precedent-selftest-gf"
    meta = lambda s: s.q("MATCH (m:Meta) RETURN m.graph_format AS f")
    with tempfile.TemporaryDirectory() as tmp_home:
        home = pathlib.Path(tmp_home)
        with Store(home, write=True) as s:
            assert meta(s) == [{"f": GRAPH_FORMAT}], "a new store is marked at once"
            # What 0.4.x leaves: path-keyed node with portable, its journal line, no marker.
            s.log("record", {"id": "selftest-gf1", "title": "t", "statement": "t",
                             "rationale": "", "scope": "tooling", "created": today(),
                             "project_id": old, "project_name": "gf", "project_path": old,
                             "portable": pp, "tags": [], "topics": [], "chose": [],
                             "rejected": [], "supersedes": []})
            s.q("CREATE (:Project {id:$id, name:'gf', portable:$pp, paths:$id})",
                {"id": old, "pp": pp})
            s.q("CREATE (:Decision {id:'selftest-gf1', status:'active'})")
            s.q("""MATCH (d:Decision {id:'selftest-gf1'}), (p:Project {id:$id})
                   MERGE (d)-[:IN_PROJECT]->(p)""", {"id": old})
            s.q("MATCH (m:Meta) DETACH DELETE m")

        with Store(home, write=False) as s:
            assert meta(s) == [], "a reader must not migrate"

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with Store(home, write=True) as s:
                assert s.q("MATCH (p:Project {id:$id}) RETURN p.id AS id", {"id": old}) == []
                assert s.q("""MATCH (:Decision {id:'selftest-gf1'})-[:IN_PROJECT]->(p:Project)
                              RETURN p.id AS id""") == [{"id": pp}]
                assert meta(s) == [{"f": GRAPH_FORMAT}]
                s.q("CREATE (:Project {id:'selftest-gf-sentinel'})")
        assert out.getvalue() == "", "the migration notice must never reach stdout"
        assert "re-keyed" in err.getvalue(), err.getvalue()

        # A fresh claim already held by another writer (a second SessionStart
        # `brief` racing this one) must block a second, interleaved replay.
        with tempfile.TemporaryDirectory() as tmp_home2:
            home2 = pathlib.Path(tmp_home2)
            with Store(home2, write=True) as s:
                s.log("record", {"id": "selftest-gf2", "title": "t", "statement": "t",
                                 "rationale": "", "scope": "tooling", "created": today(),
                                 "project_id": old, "project_name": "gf", "project_path": old,
                                 "portable": pp, "tags": [], "topics": [], "chose": [],
                                 "rejected": [], "supersedes": []})
                s.q("CREATE (:Project {id:$id, name:'gf', portable:$pp, paths:$id})",
                    {"id": old, "pp": pp})
                s.q("MATCH (m:Meta) SET m.graph_format=null, m.claim='other', m.claimed=$c",
                    {"c": time.time()})

            with Store(home2, write=True) as s:
                assert s.q("MATCH (p:Project {id:$id}) RETURN p.id AS id", {"id": old}) \
                    == [{"id": old}], "a fresh claim held elsewhere must block replay"
                row = s.q("MATCH (m:Meta) RETURN m.graph_format AS f")[0]["f"]
                assert (row or 0) < GRAPH_FORMAT, "an unclaimed replay must leave the format unmarked"

        # A stale claim (the claimant crashed or was killed) must not wedge
        # every future writer — someone else may migrate instead.
        with tempfile.TemporaryDirectory() as tmp_home3:
            home3 = pathlib.Path(tmp_home3)
            with Store(home3, write=True) as s:
                s.log("record", {"id": "selftest-gf3", "title": "t", "statement": "t",
                                 "rationale": "", "scope": "tooling", "created": today(),
                                 "project_id": old, "project_name": "gf", "project_path": old,
                                 "portable": pp, "tags": [], "topics": [], "chose": [],
                                 "rejected": [], "supersedes": []})
                s.q("CREATE (:Project {id:$id, name:'gf', portable:$pp, paths:$id})",
                    {"id": old, "pp": pp})
                s.q("MATCH (m:Meta) SET m.graph_format=null, m.claim='other', m.claimed=$c",
                    {"c": time.time() - 10_000})

            with contextlib.redirect_stderr(io.StringIO()):
                with Store(home3, write=True) as s:
                    assert s.q("MATCH (p:Project {id:$id}) RETURN p.id AS id", {"id": old}) \
                        == [], "a stale claim must not block replay"
                    assert s.q("MATCH (p:Project {portable:$pp}) RETURN p.id AS id", {"pp": pp}) \
                        == [{"id": pp}]
                    row = s.q("MATCH (m:Meta) RETURN m.graph_format AS f, m.claim AS c")[0]
                    assert row == {"f": GRAPH_FORMAT, "c": None}, row

        with Store(home, write=True) as s:
            assert s.q("MATCH (p:Project {id:'selftest-gf-sentinel'}) RETURN p.id AS id"), \
                "a marked graph must not be replayed again"
            # 0.4.x finds nodes by the portable property: it must still be there.
            assert s.q("MATCH (p:Project {portable:$pp}) RETURN p.id AS id", {"pp": pp}) \
                == [{"id": pp}]

            with open(s.journal, "a", encoding="utf-8") as f:
                f.write(json.dumps({"op": "record", "id": "selftest-gf-future",
                                    "v": SCHEMA + 1}) + "\n")
            n = s.q("MATCH (n) RETURN count(n) AS n")[0]["n"]
            try:
                replay_journal(s)
                raise AssertionError("a too-new journal must be refused")
            except SystemExit as exc:
                assert "upgrade" in str(exc), exc
            assert s.q("MATCH (n) RETURN count(n) AS n")[0]["n"] == n, \
                "a refused replay must not have wiped the graph"


def _check_standing_orders() -> None:
    """One source for the banner, and it must name a runnable CLI path.

    The banner lived in two places — the session-start hook and SKILL.md —
    and this plan adds two more adapters. Four copies drift, and a drifted
    standing order is a model told to run a command that no longer exists.
    """
    import io
    import contextlib

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cmd_standing_orders(None)
    text = out.getvalue()

    assert "{cli}" not in text, "the placeholder must be substituted, not printed"
    assert CLI.is_file() and str(CLI) in text, \
        "the banner must name this script's real path, so a copied install still works"
    # A distinguishing phrase from each bullet's prose, not the code-example
    # invocation line — `f" {verb}"` used to pass on `uv run {cli} check`/
    # `record` alone, so it could not detect the prose bullets going missing.
    distinguishing_phrases = {
        "check": "lead with what it returns",
        "record": "offer to record it",
        "regret": "stops arguing for it",
        "prime": "Re-prime when you change project",
        "diverge": "arguing with itself",
    }
    for verb, phrase in distinguishing_phrases.items():
        assert phrase in text, f"the banner must keep the bullet that covers `{verb}`"
    assert "not a veto" in text, "the banner must keep the 'precedent is information' line"


def _check_entry_point() -> None:
    """The entry script imports its package from any cwd and through a symlink.

    Python puts the *resolved* script directory on sys.path, which is the whole
    reason the split needs no install step. Manual installs symlink the skill
    or the script, and agents run it from the project directory, so both must
    work. standing-orders needs no Store, so this costs no engine start.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        targets = [CLI]
        link = pathlib.Path(tmp) / "precedent-link.py"
        try:
            link.symlink_to(CLI)
            targets.append(link)
        except OSError:
            pass  # ponytail: Windows without Developer Mode cannot symlink; the cwd case still runs
        for target in targets:
            r = subprocess.run([sys.executable, str(target), "standing-orders"],
                               cwd=tmp, capture_output=True, text=True, encoding="utf-8",
                               env={**os.environ, "PYTHONUTF8": "1"})
            assert r.returncode == 0, f"{target} failed from {tmp}: {r.stderr}"
            assert str(CLI) in r.stdout, f"{target} must name the entry script {CLI}"


def _check_missing_engine() -> None:
    """Without graphdblite, a store command says how to install it.

    The README invites plain-python users; their first run is the one most
    likely to lack the engine, and a ModuleNotFoundError traceback does not
    say which package, or that uv would have fetched it.
    """
    import tempfile

    probe = ("import sys; sys.path.insert(0, sys.argv[1]); sys.modules['graphdblite'] = None\n"
             "from precedent_cli.cli import main\n"
             "sys.exit(main(['--home', sys.argv[2], 'check', '--topic', 't']))\n")
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run([sys.executable, "-c", probe, str(CLI.parent), tmp],
                           capture_output=True, text=True, encoding="utf-8",
                           env={**os.environ, "PYTHONUTF8": "1"})
    assert r.returncode != 0, "a store command must fail without the engine"
    assert "pip install graphdblite" in r.stderr, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
