from __future__ import annotations

import argparse
import pathlib

from .admin import cmd_cypher, cmd_export, cmd_init, cmd_standing_orders
import precedent_cli
from .core import HOME, SCOPES, Store
from .maintain import cmd_maintain, cmd_merge_project, cmd_tag
from .reading import cmd_brief, cmd_check, cmd_suggest
from .replay import cmd_rebuild
from .writing import cmd_amend, cmd_principle, cmd_record, cmd_regret


# ---------------------------------------------------------------------------- main

def cmd_selftest(a, s: Store) -> None:
    # Imported here, not at the top: the checks import cli back, and ordinary
    # commands should not load ~1,900 lines of checks they never run.
    from .selftest import cmd_selftest as run
    run(a, s)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="precedent", description=precedent_cli.__doc__,
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

    am = sub.add_parser("amend",
                        help="reword a decision — same decision, better words")
    am.add_argument("--id", required=True,
                    help="the decision id, as printed by `check` and `brief` after the '#'")
    am.add_argument("--title", default="")
    am.add_argument("--statement", default="")
    am.add_argument("--rationale", default="")
    am.add_argument("--topic", default="",
                    help="comma-separated; REPLACES the whole topic set. For a decision "
                         "filed under the wrong question, e.g. a project tag used as a topic")
    am.set_defaults(writes=True, fn=cmd_amend)

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

    mp = sub.add_parser("merge-project",
                        help="fold one project node into another (repair a ghost or a split)")
    mp.add_argument("--from", dest="frm", required=True,
                    help="exact project id to fold away, as `maintain` prints it")
    mp.add_argument("--into", required=True,
                    help="project id, or a directory such as . for the project you are in")
    mp.set_defaults(writes=True, fn=cmd_merge_project)

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
    # Reports only, until --apply deletes orphan nodes; wants_write()
    # promotes it then.
    m.set_defaults(writes=False, fn=cmd_maintain)

    sub.add_parser("rebuild", help="replay journal.jsonl into a fresh graph"
                   ).set_defaults(writes=True, fn=cmd_rebuild)

    it = sub.add_parser("init", help="show where the store lives, or move it elsewhere")
    it.add_argument("location", nargs="?",
                    help="directory to keep the store in; a pointer file is left at the default path")
    it.set_defaults(writes=True, fn=cmd_init)

    so = sub.add_parser("standing-orders",
                        help="print the standing orders every adapter appends after a brief")
    so.set_defaults(writes=False, fn=cmd_standing_orders)

    cy = sub.add_parser("cypher", help="escape hatch")
    cy.add_argument("query")
    cy.add_argument("--params", default="")
    # A writer, though it usually reads: the query is arbitrary text, so
    # `cypher "CREATE (...)"` cannot be told from a MATCH before it runs.
    # Serialising a rare interactive escape hatch is the cheap side of that.
    cy.set_defaults(writes=True, fn=cmd_cypher)

    ex = sub.add_parser("export", help="snapshot the store (graph + journal) to a directory")
    ex.add_argument("--out", help="where to write the snapshot (default: <store>/export)")
    ex.set_defaults(writes=False, fn=cmd_export)

    sub.add_parser("selftest").set_defaults(writes=True, fn=cmd_selftest)
    return p


def wants_write(a) -> bool:
    """Whether a parsed command may change the store.

    Answered before the Store exists because Store takes it as an argument.
    It used to pick a lock mode, and had to be settled up front because
    ReadWriteLock refuses to promote a read lock to a write lock; with the
    lock gone it decides whether a command may migrate a store written by the
    previous engine — see Store._migrate_grafeo_store.
    """
    if a.cmd == "maintain":
        # --apply deletes orphan nodes; without it maintain only reports.
        return bool(a.apply)
    return a.writes


def main(argv=None) -> None:
    a = build_parser().parse_args(argv)
    if a.cmd in ("init", "standing-orders"):
        a.fn(a)
        return
    with Store(pathlib.Path(a.home), write=wants_write(a)) as s:
        a.fn(a, s)
