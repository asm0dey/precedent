from __future__ import annotations

import os
import pathlib
import re
import sys

from .core import Store, csv
from .projects import attach_tags, fold_project, merge_target, normalised, paths_of, portable_id, project_info, settle, tags_of, upsert_project, vocabulary


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

    settle(s, info)
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


def cmd_merge_project(a, s: Store) -> None:
    """Fold one project node into another — the repair for a ghost (#13).

    Built for an agent: `--from` is the exact id `maintain` prints (a ghost
    has no directory to name it by), `--into` is an id or a live path, so `.`
    works from inside the real project. A wrong id lists near-misses instead
    of guessing, so the next call can be right. Whether two histories are one
    project is the caller's judgment; this carries it out, journalled.
    """
    exists = lambda pid: bool(s.q("MATCH (p:Project {id:$id}) RETURN p.id AS id",
                                  {"id": pid}))
    if not exists(a.frm):
        v = a.frm.lower()
        near = [r for r in s.q("MATCH (p:Project) RETURN p.id AS id, p.name AS name ORDER BY p.id")
                if v in r["id"].lower() or v in (r["name"] or "").lower()][:5]
        print(f"no project has the id {a.frm!r}.", file=sys.stderr)
        for r in near:
            print(f"  {r['name']}  {r['id']}", file=sys.stderr)
        if not near:
            print("  `precedent.py maintain` lists project ids", file=sys.stderr)
        raise SystemExit(2)
    if exists(a.into):
        into = a.into
        # An existing node has no local path to speak for; its portable id
        # and name are all the line needs, and replay finds the node itself.
        row = s.q("MATCH (p:Project {id:$id}) RETURN p.portable AS pp, p.name AS name",
                  {"id": into})[0]
        target = {"portable": row["pp"], "name": row["name"]}
    else:
        info = project_info(s, a.into)            # exits 2 unless a directory
        if a.frm in info["legacy"]:
            settle(s, info)
            print(f"{a.frm} was an older key of {info['key']} — folded into it")
            print_new_clashes(s, info["key"])
            return
        if info["key"] == a.frm:
            into = a.frm
        else:
            settle(s, info)
            into = upsert_project(s, info)["id"]
        target = merge_target(info)
    if into == a.frm:
        print(f"{a.frm!r} and {a.into!r} are the same project.", file=sys.stderr)
        raise SystemExit(2)
    s.log("project_merge", {"from": a.frm, "into": into, **target})
    moved = fold_project(s, a.frm, into)
    print(f"merged {a.frm} into {into} — {moved} decision(s) moved")
    print_new_clashes(s, into)


def print_new_clashes(s: Store, into: str) -> None:
    """The contradictions a merge just put in one project, in maintain's
    format. Shown, never resolved: which answer stands is the user's call."""
    for r in contradictions_in(s, into):
        print(f"  now contradicting: {r['project']}/{r['topic']} — supersede one:")
        for did, opts in sorted(r["decisions"].items()):
            print(f"     {','.join(opts)}  #{did}")


def contradictions_in(s: "Store", project_id: str | None = None) -> list[dict]:
    """Two live decisions in one project answering one topic differently.

    Grouped by decision id, not by project+topic: `record --chose redis,memcached`
    is one decision picking a stack, and reporting it as a clash asks the user
    to supersede a decision that is correct.

    `project_id` scopes the report to one project's own node — used right
    after a merge, where only the clashes the merge itself just created
    matter, not everything else already live in the graph.
    """
    rows = s.q("""MATCH (d:Decision)-[:ABOUT]->(t:Topic),
                        (d)-[:CHOSE]->(o:Option),
                        (d)-[:IN_PROJECT]->(p:Project)
                  WHERE d.status='active'
                  RETURN p.id AS pid, p.name AS pname, t.name AS topic, d.id AS did,
                         collect(DISTINCT o.name) AS opts""")
    grouped: dict[tuple[str, str], dict[str, list[str]]] = {}
    for r in rows:
        if project_id and r["pid"] != project_id:
            continue
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
    node carrying the portable, so nothing stamps it. The older node keeps
    its decisions, never gains an identity, and every later write lands on
    the newer one. Nothing heals that and nothing else reports it — so this
    does.

    Reports, never merges: `merge-project` does, when the caller decides. Two
    decision histories are one repo's or two repos', and telling them apart is
    a judgment; merging them silently is the failure ADR 0002 exists to
    prevent.

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


def remote_changes(s: "Store") -> list[dict]:
    """Pairs of remote-keyed nodes sharing a checkout that is on this machine.

    `git remote set-url` (a rename, a move to another host) gives the same
    checkout a new portable id, and the conservative rule — a different
    portable is never folded on a path match — keys a fresh node for it. That
    split is deliberate and silent; this is where it stops being silent.

    `current` is what the checkout reports now, when it is one of the pair:
    the node the next write lands on, so the natural merge target. Otherwise
    neither side is the live one and no direction is offered.
    Reports, never merges: two remotes can also be a fork and its upstream.
    """
    rows = s.q("""MATCH (p:Project) WHERE p.portable IS NOT NULL
                  RETURN p.id AS id, p.name AS name, p.paths AS paths,
                         p.portable AS portable""")
    holders: dict[str, list[dict]] = {}
    for r in rows:
        for p in paths_of(r):
            holders.setdefault(p, []).append(r)
    out, seen = [], set()
    for path, rs in sorted(holders.items()):
        if len({r["portable"] for r in rs}) < 2 or not pathlib.Path(path).exists():
            continue
        current = portable_id(pathlib.Path(path))
        for i, a_ in enumerate(rs):
            for b_ in rs[i + 1:]:
                pair = tuple(sorted((a_["id"], b_["id"])))
                if a_["portable"] == b_["portable"] or pair in seen:
                    continue
                seen.add(pair)
                new = next((x for x in (a_, b_) if x["portable"] == current), None)
                old = None
                if new:
                    old = b_ if new is a_ else a_
                out.append({"path": path, "nodes": sorted((a_, b_), key=lambda x: x["id"]),
                            "old": old, "new": new})
    return out


def superseded_engine_leftovers(s: "Store") -> list[pathlib.Path]:
    """Store files no current code reads: the pre-swap graph and its sidecars.

    A clean migration deletes the old graph itself (see
    `Store._finish_migration`), so this is normally empty. It is not always:
    a replay that could not read every journal entry keeps the old store on
    purpose, and a migration interrupted partway can leave a sidecar behind.
    Those survive precisely because something might still be wrong, which is
    also why they are reported and never deleted here — a report command that
    removes a user's last copy of anything is the wrong kind of helpful.
    """
    return sorted(f for f in s.home.iterdir()
                  if f.name.startswith("graph.db.") and f.name != "graph.db")


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

    leftovers = superseded_engine_leftovers(s)
    print(f"\n== leftovers from the previous graph engine ({len(leftovers)}) ==")
    if not leftovers:
        print("  none")
    else:
        print("  the journal is the source of truth and the graph was rebuilt from it,")
        print("  so these are readable by nothing that is still installed:")
        for f in leftovers:
            print(f"    {f}")
        print("  delete them yourself when you are satisfied the history survived —"
              " `check` and `brief` read the graph, so exercise those first")

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
    gone = {r["id"] for r in states["gone"]}
    for r in states["gone"]:
        print(f"  {r['name']}  ({r['id']})")
        # A ghost (#13) usually shares its name with the project it belongs to.
        # Offered, never run: which one it is remains a judgment.
        # ponytail: plain quotes, no escaping — right in bash, zsh, PowerShell
        # and cmd for any path; an id containing " would break it, and
        # Windows forbids that character.
        for c in projects:
            if c["name"] == r["name"] and c["id"] not in gone and c["id"] != r["id"]:
                print(f'    merge with: precedent.py merge-project --from "{r["id"]}"'
                      f' --into "{c["id"]}"')
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
            print("     one repo, so this is almost certainly one project — but check"
                  " both sides' decisions first, then:")
            print(f'       precedent.py merge-project --from "{g["id"]}"'
                  f' --into "{g["twin"]["id"]}"')
        elif g["portable"]:
            print(f"  {g['name']}  ({g['id']})  — {g['portable']} is unclaimed,"
                  " so the next write here stamps it")
        elif g["local"]:
            print(f"  {g['name']}  ({g['id']})  — no git remote, so it is"
                  " machine-local by design")
        else:
            print(f"  {g['name']}  ({g['id']})  — not on this machine, so its"
                  " remote cannot be read here")

    moved = remote_changes(s)
    print(f"\n== one checkout, two remotes: a remote changed ({len(moved)}) ==")
    if not moved:
        print("  none")
    for m in moved:
        a_, b_ = m["nodes"]
        print(f"  {a_['name']} ({a_['id']}) and {b_['name']} ({b_['id']})"
              f" — both hold {m['path']}")
        if m["new"]:
            print(f"     the checkout now reports {m['new']['id']}: new decisions land there,")
            print("     the other history is stranded. A renamed or moved repo is one project,")
            print("     a fork and its upstream are two — check both sides, then:")
            print(f'       precedent.py merge-project --from "{m["old"]["id"]}"'
                  f' --into "{m["new"]["id"]}"')
        else:
            print("     the checkout reports neither remote now; check both before"
                  " merging with precedent.py merge-project")

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
