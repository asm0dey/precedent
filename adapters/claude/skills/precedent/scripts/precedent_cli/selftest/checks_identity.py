from __future__ import annotations

import json
import os
import pathlib

from ..cli import build_parser
from ..core import SCHEMA, Store, today
from ..maintain import cmd_maintain, cmd_merge_project, identity_gaps
from ..projects import attach_tags, detect_project, fold_project, identity_path, normalise_remote, paths_of, portable_id, project_info, settle, tags_of, upsert_project
from ..reading import cmd_brief
from ..replay import replay_entry, replay_journal
from ..writing import cmd_record, write_decision
from .checks_store import _expect_exit

PRECEDENT_REMOTE = "github.com/asm0dey/precedent"
PROJECT_PATHS = "MATCH (p:Project {id:$id}) RETURN p.paths AS paths"
COUNT_PROJECTS = "MATCH (p:Project) RETURN count(p) AS n"
PROJECT_BY_ID = "MATCH (p:Project {id:$id}) RETURN p.id AS id"


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
        (tmp / "build.zig").write_text("// zig", encoding="utf-8")
        (tmp / "shard.yml").write_text("# crystal", encoding="utf-8")
        (tmp / "src").mkdir()
        got = detect_project(tmp)
        assert "build.zig" in got["contents"], got
        assert "shard.yml" in got["contents"], got
        assert "src/" in got["contents"], got
        (tmp / "node_modules").mkdir()
        assert "node_modules" not in detect_project(tmp)["contents"]


def _git(root: pathlib.Path, *args: str) -> None:
    import subprocess
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)


def _git_repo(root: pathlib.Path, remote: str | None = None) -> None:
    """A repo at `root` with one commit — `git worktree add` needs a HEAD.

    Identity and signing come from `-c` so the check runs on any machine,
    including one that signs every commit.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "README").write_text("selftest", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=selftest", "-c", "user.email=selftest@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")
    if remote:
        _git(root, "remote", "add", "origin", remote)


def _check_worktrees() -> None:
    """A worktree is its main checkout; a submodule is not.

    Only a repo with a remote got this for free, through the portable id.
    Without one, the worktree's own path keyed a second project — issue #13.
    """
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        main, wt = base / "main", base / "wt"
        (main / "mod").mkdir(parents=True)
        (main / "mod" / "f").write_text("x", encoding="utf-8")
        _git_repo(main)
        _git(main, "worktree", "add", "-q", str(wt))

        assert identity_path(main) == main
        assert identity_path(main / "mod") == main / "mod"
        assert identity_path(wt) == main, "a remote-less worktree is its main checkout"
        assert identity_path(wt / "mod") == main / "mod", "its subdirectories map with it"
        assert portable_id(wt) is None

        _git(main, "remote", "add", "upstream", "git@github.com:asm0dey/selftest-wt.git")
        assert portable_id(main) == "github.com/asm0dey/selftest-wt", \
            "the only remote counts, whatever it is called"
        assert portable_id(wt / "mod") == "github.com/asm0dey/selftest-wt#/mod"
        _git(main, "remote", "add", "fork", "git@github.com:someone/selftest-wt.git")
        assert portable_id(main) is None, "two remotes and no origin is not guessed at"
        _git(main, "remote", "add", "origin", "git@github.com:asm0dey/selftest-wt.git")
        assert portable_id(main) == "github.com/asm0dey/selftest-wt"

        # A submodule's common dir is <super>/.git/modules/<name>, and it is
        # not a linked worktree: it keeps its own path.
        src = base / "subsrc"
        _git_repo(src)
        # as_uri(): a file:// URL is accepted by git on every OS; a bare
        # Windows path with backslashes is not reliably.
        _git(main, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
             src.as_uri(), "vendored")
        assert identity_path(main / "vendored") == main / "vendored", \
            "a submodule must not be mapped onto .git/modules"

        # The main checkout moved away: git cannot answer, so no mapping.
        shutil.move(str(main), str(base / "moved"))
        assert identity_path(wt) == wt, "a worktree whose main checkout is gone keys itself"


def _check_identity(s: Store) -> None:
    """A home-relative id was rejected: /home/u/src/api and C:\\dev\\api are the
    same repo at different relative paths, and two different projects can sit
    at the same relative path on two machines. The remote is the identity.
    """
    for raw, want in [
        ("https://github.com/asm0dey/precedent.git", PRECEDENT_REMOTE),
        ("git@github.com:asm0dey/precedent.git", PRECEDENT_REMOTE),
        ("ssh://git@github.com/asm0dey/precedent.git", PRECEDENT_REMOTE),
        ("https://user:token@github.com/o/r.git", "github.com/o/r"),
        ("ssh://git@host:2222/o/r.git", "host/o/r"),
        ("https://GitHub.com/Asm0dey/Precedent", PRECEDENT_REMOTE),
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
        assert portable_id(root) == PRECEDENT_REMOTE
        assert portable_id(root / "mod") == "github.com/asm0dey/precedent#/mod"

    # Two machines, one repo: the second must resolve onto the first node,
    # for reads AND writes. Read-only resolution finds the existing node
    # while writes create a second one, fragmenting the graph a little more
    # with every machine and every session, invisibly.
    linux, windows = "/nonexistent/precedent-selftest-i", "C:\\dev\\precedent-selftest-i"
    unrelated = "/nonexistent/precedent-selftest-i-other"
    pp = "github.com/asm0dey/selftest-i"
    pp2 = "github.com/asm0dey/selftest-i-other"
    try:
        a_info = upsert_project(s, {"id": linux, "path": linux, "name": "i",
                                    "portable": pp})
        b_info = upsert_project(s, {"id": windows, "path": windows, "name": "i",
                                    "portable": pp})
        assert a_info["id"] == pp, a_info
        assert b_info["id"] == pp, "the second sighting must resolve onto the same node"
        assert s.q("MATCH (p:Project {portable:$pp}) RETURN count(p) AS n",
                   {"pp": pp}) == [{"n": 1}], "one repo, one node"
        row = s.q(PROJECT_PATHS, {"id": pp})[0]
        assert set(paths_of(row)) == {linux, windows}, row

        # The other direction, and the one that cannot be undone: convergence
        # is worth nine assertions above, but a resolver that over-converged
        # would fold two unrelated repos into one node and merge their
        # histories in an append-only store. A different remote must key a
        # different node even when everything else about the sighting matches
        # — same name, sibling path, same session.
        c_info = upsert_project(s, {"id": unrelated, "path": unrelated, "name": "i",
                                    "portable": pp2})
        assert c_info["id"] == pp2, "a different remote must key a different node"
        assert set(paths_of(s.q(PROJECT_PATHS,
                                {"id": pp2})[0])) == {unrelated}, \
            "a distinct repo must not inherit the other one's paths"
        assert s.q("MATCH (p:Project {portable:$pp}) RETURN count(p) AS n",
                   {"pp": pp}) == [{"n": 1}], "two repos, two nodes"
        assert s.q("MATCH (p:Project) WHERE p.portable IN $pps RETURN count(p) AS n",
                   {"pps": [pp, pp2]}) == [{"n": 2}], "two repos, two nodes"
    finally:
        for pid in (linux, windows, unrelated, pp, pp2):
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
    holder = "/nonexistent/precedent-selftest-gap-holder"
    ids = [holder, pp]
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
            # portable ids existed. project_info resolves onto the holder, so
            # nothing stamps it.
            upsert_project(s, {"id": str(split), "path": str(split),
                               "name": "gap", "portable": None})
            upsert_project(s, {"id": str(bare), "path": str(bare),
                               "name": "gap", "portable": None})

            gaps = {g["id"]: g for g in identity_gaps(s)}
            assert pp not in gaps, "a project that HAS an identity is not a gap"
            assert str(split) in gaps and str(bare) in gaps, gaps
            twin = gaps[str(split)]["twin"]
            assert twin and twin["id"] == pp, \
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


def _check_rekey(s: Store) -> None:
    """One node per project, keyed by its remote — and every older key folds in.

    Older keys are history, not choice: every node before this change was
    path-keyed, and 0.4.x still creates path-keyed nodes carrying `portable`.
    The one direction that must never happen is a path match folding a node
    whose remote says it is a different project.
    """
    P = "/nonexistent/precedent-selftest-rk"
    pp, other = "github.com/asm0dey/selftest-rk", "github.com/asm0dey/selftest-rk-other"
    dec = lambda did, pid, path, portable: {
        "id": did, "title": "t", "statement": "t", "rationale": "", "scope": "tooling",
        "created": today(), "project_id": pid, "project_name": "rk",
        "project_path": path, "portable": portable,
        "tags": [], "topics": [], "chose": [], "rejected": [], "supersedes": []}
    in_project = lambda did: s.q(
        "MATCH (:Decision {id:$d})-[:IN_PROJECT]->(p:Project) RETURN p.id AS id", {"d": did})
    node = lambda pid: s.q("MATCH (p:Project {id:$id}) RETURN p.name AS name, p.paths AS paths,"
                           " p.portable AS pp", {"id": pid})
    wipe = lambda: (s.q("MATCH (n) WHERE n.id STARTS WITH 'selftest-rk' DETACH DELETE n"),
                    s.q("MATCH (p:Project) WHERE p.id IN $ids DETACH DELETE p",
                        {"ids": [P, P + "-b", pp, other]}),
                    s.q("""MATCH (t:Tag {name:'selftest-rk-tag'})
                           WHERE NOT EXISTS { MATCH (t)<--() } DETACH DELETE t"""))
    try:
        # fold_project: decisions, tags and paths move; the source goes; the target keeps its name.
        upsert_project(s, {"id": P, "path": P, "name": "src"})
        upsert_project(s, {"id": P + "-b", "path": P + "-b", "name": "dst"})
        attach_tags(s, P, ["selftest-rk-tag"])
        write_decision(s, dec("selftest-rk1", P, P, None))
        assert fold_project(s, P, P + "-b") == 1
        assert node(P) == [], "the source node must be gone"
        assert in_project("selftest-rk1") == [{"id": P + "-b"}]
        assert "selftest-rk-tag" in tags_of(s, P + "-b")
        assert set(paths_of(node(P + "-b")[0])) == {P, P + "-b"}
        assert node(P + "-b")[0]["name"] == "dst", "the target keeps its name"
        assert fold_project(s, "selftest-rk-missing", P + "-b") == 0, "a missing source is a no-op"
        wipe()

        # 0.4.x shape: path-keyed node carrying the portable id. The next write re-keys it.
        s.q("CREATE (:Project {id:$id, name:'rk', portable:$pp, paths:$id})", {"id": P, "pp": pp})
        s.q("CREATE (:Decision {id:'selftest-rk2', status:'active'})")
        s.q("MATCH (d:Decision {id:'selftest-rk2'}), (p:Project {id:$id}) MERGE (d)-[:IN_PROJECT]->(p)",
            {"id": P})
        got = upsert_project(s, {"id": P, "path": P, "name": "rk", "portable": pp})
        assert got["id"] == pp, got
        assert node(P) == [] and in_project("selftest-rk2") == [{"id": pp}]
        wipe()

        # A repo that gained a remote: its portable-less path node folds in.
        write_decision(s, dec("selftest-rk3", P, P, None))
        upsert_project(s, {"id": P, "path": P, "name": "rk", "portable": pp})
        assert in_project("selftest-rk3") == [{"id": pp}] and node(P) == []
        wipe()

        # A DIFFERENT remote at the same path is another project: never folded.
        upsert_project(s, {"id": P, "path": P, "name": "rk", "portable": other})
        upsert_project(s, {"id": P, "path": P, "name": "rk", "portable": pp})
        assert node(other) and node(pp), "a different portable must not fold on a path match"
        wipe()

        # The name is fixed when the node is created.
        upsert_project(s, {"id": P, "path": P, "name": "first", "portable": pp})
        upsert_project(s, {"id": P, "path": P + "-b", "name": "second", "portable": pp})
        assert node(pp)[0]["name"] == "first", node(pp)
        wipe()

        # Regression: a path already folded into another node must resolve
        # onto that node, not re-key it back under the writing path — a
        # remote-less write has no identity of its own to fold anything into.
        write_decision(s, dec("selftest-rk6", P, P, None))
        write_decision(s, dec("selftest-rk7", P + "-b", P + "-b", None))
        fold_project(s, P + "-b", P)
        write_decision(s, dec("selftest-rk8", P + "-b", P + "-b", None))
        assert (in_project("selftest-rk6") == in_project("selftest-rk7")
                == in_project("selftest-rk8") == [{"id": P}])
        assert node(P + "-b") == [], "the merged path must not resurrect its old node"
        write_decision(s, dec("selftest-rk9", P, P, None))
        assert in_project("selftest-rk9") == [{"id": P}], \
            "a write from the surviving id must stay put"
        wipe()

        # Replay: an old path-keyed record line, then one carrying the portable id.
        replay_entry(s, {"op": "record", **dec("selftest-rk4", P, P, None)})
        replay_entry(s, {"op": "record", **dec("selftest-rk5", pp, P, pp)})
        assert in_project("selftest-rk4") == [{"id": pp}] == in_project("selftest-rk5")
        assert node(P) == [], "replay must converge on one node per repo"
    finally:
        wipe()


def _check_settle() -> None:
    """Lazy re-key: readers still find a node no writer has re-keyed; the
    next writer folds it, journalled, so `rebuild` lands in the same place.

    Throwaway Store: settle() journals.
    """
    import argparse
    import contextlib
    import io
    import tempfile

    pp = "github.com/asm0dey/selftest-st"
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tmp_home:
        base = pathlib.Path(tmp).resolve()
        with Store(pathlib.Path(tmp_home), write=True) as s:
            in_project = lambda did: s.q(
                "MATCH (:Decision {id:$d})-[:IN_PROJECT]->(p:Project) RETURN p.id AS id",
                {"d": did})

            # 1. The 0.4.x shape in a repo with a remote.
            R = base / "remote-repo"
            _git_repo(R, "git@github.com:asm0dey/selftest-st.git")
            s.q("CREATE (:Project {id:$id, name:'remote-repo', portable:$pp, paths:$id})",
                {"id": str(R), "pp": pp})
            s.q("CREATE (:Decision {id:'selftest-st1', status:'active'})")
            s.q("""MATCH (d:Decision {id:'selftest-st1'}), (p:Project {id:$id})
                   MERGE (d)-[:IN_PROJECT]->(p)""", {"id": str(R)})
            info = project_info(s, str(R))
            assert info["key"] == pp and info["id"] == str(R), \
                f"a reader must see the node no writer has re-keyed yet: {info}"
            settle(s, info)
            assert info["id"] == pp and info["legacy"] == []
            assert in_project("selftest-st1") == [{"id": pp}]
            last = json.loads(s.journal.read_text(encoding="utf-8").splitlines()[-1])
            assert (last["op"], last["from"], last["into"], last["v"]) \
                == ("project_merge", str(R), pp, 1), last

            # 2. brief never creates a node from nothing.
            E = base / "empty-repo"
            _git_repo(E, "git@github.com:asm0dey/selftest-st-empty.git")
            n = s.q(COUNT_PROJECTS)[0]["n"]
            settle(s, project_info(s, str(E)))
            assert s.q(COUNT_PROJECTS)[0]["n"] == n

            # 3. A remote-less worktree that 0.4.x keyed on its own path.
            M, W = base / "main", base / "wt"
            _git_repo(M)
            _git(M, "worktree", "add", "-q", str(W))
            d = {"id": "selftest-st3", "title": "t", "statement": "t", "rationale": "",
                 "scope": "tooling", "created": today(), "project_id": str(W),
                 "project_name": "wt", "project_path": str(W), "portable": None,
                 "tags": [], "topics": [], "chose": [], "rejected": [], "supersedes": []}
            s.log("record", d)
            write_decision(s, d)
            info = project_info(s, str(W))
            assert info["key"] == str(M) and info["legacy"] == [str(W)], info
            settle(s, info)
            assert in_project("selftest-st3") == [{"id": str(M)}]
            m_paths = lambda: paths_of(s.q(PROJECT_PATHS,
                                           {"id": str(M)})[0])
            # Without the live path, removing the worktree leaves the main
            # checkout's own node reported as gone by `maintain`.
            assert str(M) in m_paths(), m_paths()
            # Only the journalled fold can reproduce this on rebuild.
            replay_journal(s)
            assert in_project("selftest-st3") == [{"id": str(M)}], \
                "rebuild must land the worktree's decisions on the main checkout"
            assert str(M) in m_paths(), f"rebuild must keep the live path: {m_paths()}"
            assert s.q(PROJECT_BY_ID, {"id": str(W)}) == []

            # 4. The header names the project, not the worktree directory.
            s.q("MATCH (p:Project {id:$id}) SET p.name='main'", {"id": str(M)})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cmd_brief(argparse.Namespace(project=str(W), min_shared=1,
                                             only_if_relevant=False), s)
            assert "project: main" in out.getvalue(), out.getvalue()

            # 5. A worktree of a repo WITH a remote: portable key, main-checkout path.
            M2, W2 = base / "main2", base / "wt2"
            _git_repo(M2, "git@github.com:asm0dey/selftest-st2.git")
            _git(M2, "worktree", "add", "-q", str(W2))
            info = project_info(s, str(W2))
            assert info["key"] == "github.com/asm0dey/selftest-st2", info
            assert info["path"] == str(M2) and info["name"] == "main2", info


def _check_merge_project() -> None:
    """The #13 repair, as one journalled command an agent can run.

    Throwaway Store: merge-project journals.
    """
    import argparse
    import contextlib
    import io
    import tempfile

    ghost = os.path.join(os.sep + "nonexistent-precedent-selftest", "calit", "calit")
    run = lambda s, frm, into: cmd_merge_project(argparse.Namespace(frm=frm, into=into), s)

    def refused(s, frm, into) -> str:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            exc = _expect_exit(run, s, frm, into,
                               message=f"merge-project {frm!r} -> {into!r} must be refused")
        assert exc.code == 2, exc.code
        return err.getvalue()

    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tmp_home:
        real = pathlib.Path(tmp).resolve() / "calit"
        real.mkdir()
        with Store(pathlib.Path(tmp_home), write=True) as s:
            dec = lambda did, pid, chose: {
                "id": did, "title": did, "statement": did, "rationale": "", "scope": "tooling",
                "created": today(), "project_id": pid, "project_name": "calit",
                "project_path": pid, "portable": None, "tags": [], "topics": ["cache"],
                "chose": [chose], "rejected": [], "supersedes": []}
            for d in (dec("selftest-mp1", ghost, "redis"), dec("selftest-mp2", str(real), "memcached")):
                s.log("record", d)
                write_decision(s, d)
            attach_tags(s, ghost, ["selftest-ghost"])

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                run(s, ghost, str(real))
            assert s.q(PROJECT_BY_ID, {"id": ghost}) == []
            assert {r["d"] for r in s.q("""MATCH (d:Decision)-[:IN_PROJECT]->(:Project {id:$id})
                                            RETURN d.id AS d""", {"id": str(real)})} \
                == {"selftest-mp1", "selftest-mp2"}
            assert "selftest-ghost" in tags_of(s, str(real))
            assert "cache" in out.getvalue(), "contradictions it creates must be shown"
            last = json.loads(s.journal.read_text(encoding="utf-8").splitlines()[-1])
            assert (last["op"], last["from"], last["into"], last["v"]) \
                == ("project_merge", ghost, str(real), 1), last

            replay_journal(s)
            assert {r["d"] for r in s.q("""MATCH (d:Decision)-[:IN_PROJECT]->(:Project {id:$id})
                                            RETURN d.id AS d""", {"id": str(real)})} \
                == {"selftest-mp1", "selftest-mp2"}, "rebuild must reproduce the merge"

            assert "calit" in refused(s, "calit", str(real)), "near-matches must be listed"
            assert "same project" in refused(s, str(real), str(real))
            refused(s, str(real), os.path.join(tmp, "missing-dir"))

            # --from is this project's own older key: folded, not an error.
            # `--into` is a path spelled unlike the node id ("<real>/."), so it
            # resolves through project_info — the way `--into .` does.
            older = os.path.join(os.sep + "nonexistent-precedent-selftest", "older")
            s.q("CREATE (:Project {id:$id, name:'calit', paths:$p})",
                {"id": older, "p": str(real)})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                run(s, older, os.path.join(str(real), "."))
            assert "merged" in out.getvalue(), out.getvalue()
            assert s.q(PROJECT_BY_ID, {"id": older}) == []

            n = s.q(COUNT_PROJECTS)[0]["n"]
            replay_entry(s, {"op": "project_merge", "from": "selftest-nope", "into": str(real)})
            assert s.q(COUNT_PROJECTS)[0]["n"] == n, \
                "replaying a merge whose source is gone is a no-op"


def _check_merge_replay() -> None:
    """A merge into a directory that had no node survives `rebuild`.

    merge-project creates that node on the spot. If the journal line named
    only `from` and `into`, replay made the target a rename of the ghost: the
    ghost's dead path only, no portable id — `maintain` then called the
    repaired project gone and 0.4.x could not find it by its remote.

    Throwaway Store: merge-project journals.
    """
    import argparse
    import contextlib
    import io
    import tempfile

    pp = "github.com/asm0dey/selftest-mr"
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tmp_home:
        base = pathlib.Path(tmp).resolve()
        plain, repo = base / "plain", base / "repo"
        plain.mkdir()
        _git_repo(repo, "git@github.com:asm0dey/selftest-mr.git")
        with Store(pathlib.Path(tmp_home), write=True) as s:
            for did, into in (("selftest-mr1", plain), ("selftest-mr2", repo)):
                ghost = os.path.join(os.sep + "nonexistent-precedent-selftest", did)
                d = {"id": did, "title": "t", "statement": "t", "rationale": "",
                     "scope": "tooling", "created": today(), "project_id": ghost,
                     "project_name": into.name, "project_path": ghost, "portable": None,
                     "tags": [], "topics": [], "chose": [], "rejected": [], "supersedes": []}
                s.log("record", d)
                write_decision(s, d)
                with contextlib.redirect_stdout(io.StringIO()):
                    cmd_merge_project(argparse.Namespace(frm=ghost, into=str(into)), s)

            def check(when: str) -> None:
                node = lambda pid: s.q("""MATCH (p:Project {id:$id})
                                          RETURN p.paths AS paths, p.portable AS pp""",
                                       {"id": pid})
                on = lambda did: s.q("""MATCH (:Decision {id:$d})-[:IN_PROJECT]->(p:Project)
                                        RETURN p.id AS id""", {"d": did})
                assert str(plain) in paths_of(node(str(plain))[0]), f"{when}: {node(str(plain))}"
                assert on("selftest-mr1") == [{"id": str(plain)}], when
                got = node(pp)
                assert got and got[0]["pp"] == pp and str(repo) in paths_of(got[0]), \
                    f"{when}: {got}"
                assert on("selftest-mr2") == [{"id": pp}], when

            check("after merge")
            replay_journal(s)
            check("after rebuild")


def _check_merged_into_portable() -> None:
    """A remote-less directory merged into a remote-keyed project stays there.

    `merge-project --from <notes> --into <repo>` worked, but the next record
    from notes/ keyed a fresh notes node: the lookup for a remote-less path
    only asked portable-less nodes, so the portable node now listing that
    path was never found.

    Throwaway Store: record and merge-project journal.
    """
    import argparse
    import contextlib
    import io
    import tempfile

    pp = "github.com/asm0dey/selftest-mip"
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tmp_home:
        base = pathlib.Path(tmp).resolve()
        notes, repo = base / "notes", base / "repo"
        notes.mkdir()
        _git_repo(repo, "git@github.com:asm0dey/selftest-mip.git")
        with Store(pathlib.Path(tmp_home), write=True) as s:
            record = lambda where, did: cmd_record(build_parser().parse_args(
                ["record", "--project", str(where), "--title", did, "--id", did,
                 "--rationale", "r"]), s)
            with contextlib.redirect_stdout(io.StringIO()):
                record(notes, "selftest-mip1")
                record(repo, "selftest-mip2")
                cmd_merge_project(argparse.Namespace(frm=str(notes), into=str(repo)), s)
                n = s.q(COUNT_PROJECTS)[0]["n"]
                record(notes, "selftest-mip3")
            assert s.q(COUNT_PROJECTS)[0]["n"] == n, \
                "a merged remote-less path must not key a new node"
            assert s.q("""MATCH (:Decision {id:'selftest-mip3'})-[:IN_PROJECT]->(p:Project)
                          RETURN p.id AS id""") == [{"id": pp}]
            assert project_info(s, str(notes))["id"] == pp, "readers resolve it the same way"
            replay_journal(s)
            assert s.q(COUNT_PROJECTS)[0]["n"] == n, "rebuild too"


def _check_remote_changed() -> None:
    """A checkout whose remote changed keys a new node; maintain must say so.

    The conservative rule (a different portable is never folded on a path
    match) makes `git remote set-url` a silent split: the history drops out
    of `brief`, and all maintain showed was two projects with one name.

    Throwaway Store: record journals.
    """
    import argparse
    import contextlib
    import io
    import tempfile

    old, new = "github.com/asm0dey/selftest-rc", "github.com/asm0dey/selftest-rc-renamed"
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tmp_home:
        repo = pathlib.Path(tmp).resolve() / "rc"
        _git_repo(repo, "git@github.com:asm0dey/selftest-rc.git")
        with Store(pathlib.Path(tmp_home), write=True) as s:
            record = lambda did: cmd_record(build_parser().parse_args(
                ["record", "--project", str(repo), "--title", did, "--id", did,
                 "--rationale", "r"]), s)
            with contextlib.redirect_stdout(io.StringIO()):
                record("selftest-rc1")
                _git(repo, "remote", "set-url", "origin",
                     "git@github.com:asm0dey/selftest-rc-renamed.git")
                record("selftest-rc2")
            n = s.q(COUNT_PROJECTS)[0]["n"]
            assert n == 2, "the conservative rule splits; maintain reports, never folds"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cmd_maintain(argparse.Namespace(apply=False), s)
            text = out.getvalue()
            assert f'merge-project --from "{old}" --into "{new}"' in text, text
            assert f'--from "{new}"' not in text, "the checkout's current remote is the target"
            assert s.q(COUNT_PROJECTS)[0]["n"] == n


def _check_merge_legacy_clash() -> None:
    """merge-project shows the clashes it creates on every path, including
    the one where `--from` turns out to be the target's own older key.

    Throwaway Store: merge-project and record journal.
    """
    import argparse
    import contextlib
    import io
    import tempfile

    pp = "github.com/asm0dey/selftest-mlc"
    old = os.path.join(os.sep + "nonexistent-precedent-selftest", "mlc")
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tmp_home:
        repo = pathlib.Path(tmp).resolve() / "mlc"
        _git_repo(repo, "git@github.com:asm0dey/selftest-mlc.git")
        with Store(pathlib.Path(tmp_home), write=True) as s:
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_record(build_parser().parse_args(
                    ["record", "--project", str(repo), "--title", "selftest-mlc1",
                     "--id", "selftest-mlc1", "--rationale", "r", "--topic", "selftest-mlc-cache",
                     "--chose", "memcached"]), s)
            # What 0.4.x leaves: a path-keyed node carrying the same portable id.
            s.q("CREATE (:Project {id:$id, name:'mlc', portable:$pp, paths:$id})",
                {"id": old, "pp": pp})
            s.q("CREATE (:Decision {id:'selftest-mlc2', status:'active'})")
            s.q("MERGE (:Topic {name:'selftest-mlc-cache'})")
            s.q("MERGE (:Option {name:'redis'})")
            s.q("""MATCH (d:Decision {id:'selftest-mlc2'}), (p:Project {id:$id}),
                         (t:Topic {name:'selftest-mlc-cache'}), (o:Option {name:'redis'})
                   MERGE (d)-[:IN_PROJECT]->(p) MERGE (d)-[:ABOUT]->(t)
                   MERGE (d)-[:CHOSE]->(o)""", {"id": old})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cmd_merge_project(argparse.Namespace(frm=old, into=str(repo)), s)
            assert "older key" in out.getvalue(), out.getvalue()
            assert "selftest-mlc-cache" in out.getvalue(), \
                f"the clash the fold created must be shown: {out.getvalue()}"


def _check_portable_replay() -> None:
    """0.4.x `brief` journalled `project_portable` when a project gained a
    remote. Replayed now, it folds the node it names into the portable key —
    and, as before, never creates a node.

    Throwaway Store: `Store.log` appends to a real append-only journal.
    """
    import tempfile

    pid = "/nonexistent/precedent-selftest-bp"
    portable = "github.com/asm0dey/selftest-bp"
    entry = {"op": "project_portable", "project_id": pid, "portable": portable, "v": 1}
    with tempfile.TemporaryDirectory() as tmp_home:
        with Store(pathlib.Path(tmp_home), write=True) as s:
            upsert_project(s, {"id": pid, "path": pid, "name": "bp", "portable": None})
            replay_entry(s, entry)
            assert s.q(PROJECT_BY_ID, {"id": pid}) == []
            assert s.q("MATCH (p:Project {id:$id}) RETURN p.portable AS pp, p.paths AS paths",
                       {"id": portable}) == [{"pp": portable, "paths": pid}]

            s.q("MATCH (p:Project) DETACH DELETE p")
            replay_entry(s, entry)
            assert s.q(COUNT_PROJECTS) == [{"n": 0}], \
                "replaying a project_portable line must not create a node"

            # Spec A4: the schema stamp is written after the payload, so a
            # payload key named "v" cannot shadow it.
            s.log("project_portable", {"project_id": pid, "portable": portable, "v": 99})
            last = json.loads(s.journal.read_text(encoding="utf-8").splitlines()[-1])
            assert last["v"] == SCHEMA, last
