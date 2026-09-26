from __future__ import annotations

import json

from .core import GRAPH_FORMAT, JournalTooNew, SCHEMA, Store
from .maintain import apply_tag_merge
from .projects import attach_tags, fold_project, upsert_project
from .writing import apply_amend, apply_regret, write_decision


def mark_graph_format(s: "Store") -> None:
    s.q("MERGE (m:Meta {id:'meta'}) SET m.graph_format=$f, m.claim=null, m.claimed=null",
        {"f": GRAPH_FORMAT})


def replay_journal(s: Store) -> tuple[int, list[str]]:
    """Replay every journal entry into the graph, reporting what would not go.

    Split out of `cmd_rebuild` because the grafeo-era migration in
    `Store._migrate_grafeo_store` needs the same replay without the command's
    printing: one implementation, so a store rebuilt by hand and a store
    carried across engines are built the same way.
    """
    # Refuse a journal from a newer precedent BEFORE wiping. Stopping at the
    # line itself, as replay_entry does, leaves a half-rebuilt graph.
    with open(s.journal, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            try:
                e = json.loads(line) if line.strip() else {}
            except json.JSONDecodeError:
                continue
            v = e.get("v", 1) if isinstance(e, dict) else 1
            if isinstance(v, int) and v > SCHEMA:
                raise SystemExit(f"stopping before touching the graph: journal line {lineno}"
                                 f" is schema v{v}, this precedent understands v{SCHEMA}"
                                 " — upgrade before replaying")
    # :Meta survives its own replay — it carries the migration claim this
    # very call may be running under, and wiping it would let another
    # concurrent writer see no claim and start a second, interleaved replay.
    s.q("MATCH (n) WHERE NOT n:Meta DETACH DELETE n")
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
    mark_graph_format(s)
    return n, skipped


def cmd_rebuild(a, s: Store) -> None:
    """Replay the journal into a fresh graph. The escape hatch that makes a
    young graph engine an acceptable dependency — and the thing that made
    swapping the engine underneath it a rebuild rather than a migration."""
    if not s.journal.exists():
        print("no journal — nothing to rebuild from")
        return
    n, skipped = replay_journal(s)
    print(f"rebuilt {n} journal entries into {s.db_path}")
    if skipped:
        print(f"skipped {len(skipped)} unusable entr"
              f"{'y' if len(skipped) == 1 else 'ies'}:")
        for msg in skipped:
            print(f"  {msg}")


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
        # Written by 0.4.x `brief` when a project gained a remote. The node it
        # names folds into its portable key — MATCH first, because `brief`
        # never creates a node and its journal line must not either.
        if s.q("MATCH (p:Project {id:$id}) RETURN p.id AS id", {"id": e["project_id"]}):
            fold_project(s, e["project_id"], e["portable"])
            s.q("MATCH (p:Project {id:$id}) SET p.portable=$pp",
                {"id": e["portable"], "pp": e["portable"]})
    elif e["op"] == "project_merge":
        # A missing source is a no-op: upsert_project's own fold on replay may
        # already have absorbed it. Otherwise the target is upserted from the
        # identity the line carries BEFORE the fold, as the writer did — else
        # a target the merge created comes back as a rename of the source.
        # Lines written before these fields existed fold exactly as before.
        src = s.q("MATCH (p:Project {id:$id}) RETURN p.id AS id", {"id": e["from"]})
        if src and e.get("project_path"):
            upsert_project(s, {"id": e["into"], "path": e["project_path"],
                               "name": e.get("name") or e["into"],
                               "portable": e.get("portable")})
        fold_project(s, e["from"], e["into"])
        if e.get("portable"):
            s.q("MATCH (p:Project {id:$id}) SET p.portable=$pp",
                {"id": e["into"], "pp": e["portable"]})
    elif e["op"] == "regret":
        apply_regret(s, e)
    elif e["op"] == "amend":
        apply_amend(s, e)
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
