from __future__ import annotations

import os
import pathlib

from ..core import SCHEMA, Store, today
from ..maintain import cmd_maintain, contradictions_in, liveness
from ..projects import attach_tags, contained, effective_tags, enclosing, neighbours, normalised, project_info, tags_of, topic_vocabulary, upsert_project
from ..reading import cmd_check, worth_backfilling
from ..replay import replay_entry
from .checks_identity import _git_repo
from .checks_store import _expect_exit
from ..writing import apply_amend, apply_regret, cmd_principle, write_decision

DELETE_PROJECT = "MATCH (p:Project {id:$id}) DETACH DELETE p"
DELETE_ORPHAN_TOPIC_OPTION = """MATCH (n) WHERE (n:Topic OR n:Option) AND n.name = $name
                     AND NOT EXISTS { MATCH (n)<--() } DETACH DELETE n"""
WIDER_TITLE = "wider title"


def _check_missing_project(s: Store) -> None:
    """`--project calit` resolved to <cwd>/calit, a directory that does not
    exist, and keyed a fresh untagged project every time — issue #13. It must
    be refused, create nothing, and say what was probably meant.
    """
    import contextlib
    import io
    import tempfile

    count = lambda: s.q("MATCH (p:Project) RETURN count(p) AS n")[0]["n"]
    before = count()
    cwd = os.getcwd()
    named = "selftest-named-project"
    try:
        s.q("CREATE (:Project {id:'selftest-named-id', name:$n})", {"n": named})
        with tempfile.TemporaryDirectory() as tmp:
            here = pathlib.Path(tmp) / "calit"
            here.mkdir()
            (here / "notes.txt").write_text("x", encoding="utf-8")
            os.chdir(here)
            try:
                for value, hint in (("calit", "did you mean --project . ?"),
                                    ("notes.txt", None),   # a file is not a project
                                    (named, f"A project named {named!r} exists"),
                                    ("nope-selftest", None)):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        exc = _expect_exit(project_info, s, value,
                                           message=f"--project {value!r} must be refused")
                    assert exc.code == 2, (value, exc.code)
                    assert "is not a directory" in err.getvalue(), err.getvalue()
                    if hint:
                        assert hint in err.getvalue(), err.getvalue()
            finally:
                os.chdir(cwd)   # before the directory is removed: Windows cannot delete a cwd
    finally:
        s.q("MATCH (p:Project {id:'selftest-named-id'}) DETACH DELETE p")
    assert count() == before, "a refused --project must create no node"


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
        for pid in (proj, other, root, mod, sibling, win, "github.com/asm0dey/selftest-c"):
            s.q(DELETE_PROJECT, {"id": pid})
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
        try:
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
        finally:
            s.q("MATCH (l:Lesson {id:'selftest-lesson'}) DETACH DELETE l")
            s.q("MATCH (n) WHERE n.id STARTS WITH 'selftest' DETACH DELETE n")
            s.q(DELETE_PROJECT, {"id": str(tmp)})
            for name in ("persistence", "postgres", "mongo", "sqlite",
                         "selftest-backend", "selftest-java"):
                s.q("""MATCH (n) WHERE (n:Topic OR n:Option OR n:Tag) AND n.name = $name
                         AND NOT EXISTS { MATCH (n)<--() } DETACH DELETE n""", {"name": name})


def _check_amend(s: Store) -> None:
    """An amendment must rewrite the words and nothing else.

    `check` prints the title first, so a misleading title is the expensive
    error — it is the part that gets quoted back as precedent. What makes
    amending safe to reach for instead of superseding is that it cannot
    touch what was decided: an amendment able to move a CHOSE edge would be
    a silent rewrite of history in the one tool whose value is that history
    is not rewritten.
    """
    d = {"id": "selftest-amend", "title": "narrow title", "statement": "s",
         "rationale": "r", "scope": "architecture", "created": today(),
         "project_id": "selftest-amend-proj", "project_name": "selftest",
         "tags": [], "topics": ["selftest-amend-topic"],
         "chose": ["selftest-amend-opt"], "rejected": [], "supersedes": []}
    try:
        write_decision(s, d)
        apply_amend(s, {"id": "selftest-amend", "title": WIDER_TITLE})
        assert s.q("""MATCH (d:Decision {id:'selftest-amend'})
                      RETURN d.title AS t, d.statement AS st,
                             d.rationale AS r""") \
            == [{"t": WIDER_TITLE, "st": "s", "r": "r"}], \
            "an absent field means unchanged, never cleared"
        assert s.q("""MATCH (:Decision {id:'selftest-amend'})-[:CHOSE]->(o:Option)
                      RETURN o.name AS n""") == [{"n": "selftest-amend-opt"}], \
            "an amendment must not touch what was decided"
        assert s.q("MATCH (d:Decision {id:'selftest-amend'}) RETURN d.status AS st") \
            == [{"st": "active"}], "an amendment is not a supersession"

        # Replayed from a journal line it must land identically: the graph is
        # rebuilt from these lines and from nothing else, so an op that works
        # only when called directly is an amendment that disappears on the
        # next `rebuild` — and the old wording comes back.
        replay_entry(s, {"op": "amend", "id": "selftest-amend",
                         "rationale": "replayed reason", "v": SCHEMA})
        assert s.q("""MATCH (d:Decision {id:'selftest-amend'})
                      RETURN d.rationale AS r""") == [{"r": "replayed reason"}]

        # An id that is not there must not be invented. A rewording is not a
        # decision, and a bare node carrying only a title would be precedent
        # nobody ever recorded — quoted back with full confidence.
        replay_entry(s, {"op": "amend", "id": "selftest-amend-ghost",
                         "title": "ghost", "v": SCHEMA})
        assert s.q("MATCH (d:Decision {id:'selftest-amend-ghost'}) RETURN d.id AS id") \
            == [], "amend must never create a Decision"

        # An empty amendment is a no-op, not a query with an empty SET clause
        # (which does not parse). cmd_amend refuses this case up front, but a
        # journal line is not under its control.
        apply_amend(s, {"id": "selftest-amend"})
        assert s.q("""MATCH (d:Decision {id:'selftest-amend'})
                      RETURN d.title AS t""") == [{"t": WIDER_TITLE}]
    finally:
        s.q("MATCH (n) WHERE n.id STARTS WITH 'selftest-amend' DETACH DELETE n")
        for name in ("selftest-amend-topic", "selftest-amend-opt"):
            s.q(DELETE_ORPHAN_TOPIC_OPTION,
                {"name": name})


def _check_verdicts(s: Store) -> None:
    """`clear` must mean 'I searched and your history is silent', never
    'you typed a word I have never seen'. Reproduced before the fix:
    `check --topic database` against a graph holding the same decision
    under `persistence` printed `new ground` and then `clear`.
    """
    d = {"id": "selftest-v1", "title": "T", "statement": "T",
         "rationale": "concurrent writers", "scope": "architecture",
         "created": today(), "project_id": "/nonexistent/precedent-selftest-v",
         "project_name": "selftest-v", "tags": ["selftest-v-tag"],
         "topics": ["selftest-persistence"], "chose": ["postgres"],
         "rejected": ["sqlite"], "supersedes": []}
    write_decision(s, d)
    try:
        topics = [r["topic"] for r in topic_vocabulary(s)]
        assert "selftest-persistence" in topics, topics
    finally:
        s.q("MATCH (n) WHERE n.id STARTS WITH 'selftest-v' DETACH DELETE n")
        s.q("""MATCH (p:Project {id:'/nonexistent/precedent-selftest-v'}) DETACH DELETE p""")
        for name in ("selftest-persistence", "postgres", "sqlite", "selftest-v-tag"):
            s.q("""MATCH (n) WHERE (n:Topic OR n:Option OR n:Tag) AND n.name = $name
                     AND NOT EXISTS { MATCH (n)<--() } DETACH DELETE n""", {"name": name})

    import argparse as _argparse
    import contextlib
    import io

    # A real directory (every --project must be one), resolved so the stored
    # id and cmd_check's lookup agree — on macOS /tmp is a symlink to
    # /private/tmp.
    import tempfile
    here = str(pathlib.Path(tempfile.gettempdir()).resolve())

    base = {"title": "T", "statement": "T", "rationale": "flexible schema",
            "scope": "architecture", "created": today(),
            "tags": ["selftest-v-tag"], "topics": ["selftest-persistence"],
            "chose": ["mongo"], "rejected": ["postgres"], "supersedes": []}
    write_decision(s, {**base, "id": "selftest-v2",
                       "project_id": "/nonexistent/precedent-selftest-v2", "project_name": "vA"})
    write_decision(s, {**base, "id": "selftest-v3",
                       "project_id": "/nonexistent/precedent-selftest-v3", "project_name": "vB"})
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
        for n, pid in (("selftest-v4", "/nonexistent/precedent-selftest-v4"),
                       ("selftest-v5", "/nonexistent/precedent-selftest-v5")):
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
        for pid in ("/nonexistent/precedent-selftest-v2", "/nonexistent/precedent-selftest-v3",
                    "/nonexistent/precedent-selftest-v4", "/nonexistent/precedent-selftest-v5", here):
            s.q(DELETE_PROJECT, {"id": pid})
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

        # Ruling: the topic a principle governs but no decision mentions is
        # the one a standing rule exists for, and it was the one case that
        # never reached the principle query — cmd_check returned at the
        # empty-decision branch first. 'selftest-p-other' has the ABOUT edge
        # and no Decision, which is exactly that shape.
        out3 = io.StringIO()
        with contextlib.redirect_stdout(out3):
            cmd_check(_argparse.Namespace(topic="selftest-p-other",
                                          chose="oidc", project=here), s)
        bare = out3.getvalue()
        assert "PRINCIPLE" in bare, \
            f"a principle must surface on a topic with no decisions:\n{bare}"
        assert "new ground" not in bare, \
            f"a governed topic is not new ground:\n{bare}"
        assert "selftest-p-other" in [r["topic"] for r in topic_vocabulary(s)], \
            "a topic known only to a principle must appear in the vocabulary"

        # The other early return: no --chose means no verdict, but the
        # principle is context, not verdict, and must survive it.
        out4 = io.StringIO()
        with contextlib.redirect_stdout(out4):
            cmd_check(_argparse.Namespace(topic="selftest-auth",
                                          chose="", project=here), s)
        assert "PRINCIPLE" in out4.getvalue(), \
            f"a principle must surface without --chose:\n{out4.getvalue()}"
    finally:
        s.q("MATCH (pr:Principle {id:'selftest-p'}) DETACH DELETE pr")
        s.q("MATCH (n) WHERE n.id STARTS WITH 'selftest-p' DETACH DELETE n")
        s.q(DELETE_PROJECT, {"id": here})
        for name in ("selftest-auth", "selftest-p-other", "oidc"):
            s.q(DELETE_ORPHAN_TOPIC_OPTION, {"name": name})

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
            "project_id": "/nonexistent/precedent-selftest-m", "project_name": "m",
            "tags": [], "topics": ["selftest-caching"],
            "rejected": [], "supersedes": []}
    # Scoped to this check's own project. contradictions_in() reports the
    # whole store, and asserting on that made the selftest assert a property
    # of the USER'S data: any real clash they have recorded — two live
    # decisions answering one topic differently, which `maintain` exists to
    # report — failed the check. It passed in CI only because CI's store is
    # empty. Confirmed against the previous engine too, so this is a
    # pre-existing isolation bug, not a consequence of the engine swap.
    mine = lambda: [c for c in contradictions_in(s) if c["project"] == "m"]
    try:
        write_decision(s, {**base, "id": "selftest-m1", "chose": ["redis", "memcached"]})
        assert mine() == [], "a multi-option decision is not a clash"
        write_decision(s, {**base, "id": "selftest-m2", "chose": ["hazelcast"]})
        clash = mine()
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
        s.q("MATCH (p:Project {id:'/nonexistent/precedent-selftest-m'}) DETACH DELETE p")
        for name in ("selftest-caching", "redis", "memcached", "hazelcast"):
            s.q(DELETE_ORPHAN_TOPIC_OPTION, {"name": name})


def _check_maintain_hints() -> None:
    """maintain names the repair, not a hand-written query."""
    import argparse
    import contextlib
    import io
    import tempfile

    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tmp_home:
        base = pathlib.Path(tmp).resolve()
        with Store(pathlib.Path(tmp_home), write=True) as s:
            live = base / "calit"
            live.mkdir()
            ghost = os.path.join(os.sep + "nonexistent-precedent-selftest", "calit")
            upsert_project(s, {"id": ghost, "path": ghost, "name": "calit"})
            upsert_project(s, {"id": str(live), "path": str(live), "name": "calit"})

            pp = "github.com/asm0dey/selftest-mh"
            split = base / "split"
            _git_repo(split, "git@github.com:asm0dey/selftest-mh.git")
            upsert_project(s, {"id": "/nonexistent/precedent-selftest-mh", "name": "mh",
                               "path": "/nonexistent/precedent-selftest-mh", "portable": pp})
            upsert_project(s, {"id": str(split), "path": str(split), "name": "mh"})

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cmd_maintain(argparse.Namespace(apply=False), s)
            text = out.getvalue()
            assert f'merge-project --from "{ghost}" --into "{live}"' in text, text
            assert f'merge-project --from "{split}" --into "{pp}"' in text, text
            assert "cypher --params" not in text, "the hand-written query is replaced"
