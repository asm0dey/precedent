from __future__ import annotations

import time

from .core import AMENDABLE, Store, csv, slug, today
from .projects import attach_tags, project_info, settle, upsert_project


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
    settle(s, info)
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


def cmd_amend(a, s: Store) -> None:
    """Same decision, better words.

    Distinct from supersede on purpose, and the distinction is the whole
    point. `--supersedes` models a decision that CHANGED: it writes a second
    decision, marks the first superseded, and `check` carries both, because
    when you revisit the call in two years the old reasoning is the most
    valuable thing in the graph. A decision that was merely DESCRIBED badly
    has no such history, and superseding one asserts a change that never
    happened — in the field `check` prints first and quotes back.

    Only the words change; see AMENDABLE for why that is the line.

    The id never changes, including the stale title slug inside it. The id is
    identity and the slug within it is an accident of how it was minted —
    the same split as Project.portable against Project.id (docs/adr/0002).
    Anything already holding the old id keeps resolving: a SUPERSEDES edge,
    a rule file, a commit message.

    Journal first, then mutate, exactly as `record` does: a crash between the
    two costs a replay, not the amendment.
    """
    rows = s.q("""MATCH (d:Decision {id:$id})
                  RETURN d.title AS title, d.statement AS statement,
                         d.rationale AS rationale""", {"id": a.id})
    if not rows:
        # Exact matching, like everywhere else (docs/adr/0001). Guessing at a
        # near id here would amend the wrong decision, which is the one
        # outcome worse than amending none.
        print(f"no decision with id {a.id!r} — ids are printed by `check --topic <topic>`"
              f" and by `brief`, after the '#'")
        raise SystemExit(2)
    was = rows[0]
    fields = {k: getattr(a, k) for k in AMENDABLE if getattr(a, k)}
    if not fields:
        print("nothing to amend — pass at least one of "
              + ", ".join(f"--{k}" for k in AMENDABLE))
        raise SystemExit(2)

    payload = {"id": a.id, **fields}
    s.log("amend", payload)
    apply_amend(s, payload)

    print(f"amended {a.id}")
    # Both halves, because the user cannot see the graph and this is a write
    # to the text that gets quoted as their own words. A wrong entry is worse
    # than a missing one, so the confirmation shows what it replaced.
    for k, new in fields.items():
        print(f"  {k}")
        print(f"    was: {was[k] or '-'}")
        print(f"    now: {new}")
    # cmd_record sets statement to `a.statement or a.title`, so a decision
    # recorded without an explicit --statement carries the title verbatim in
    # both fields. When --title alone is amended, that old statement survives
    # untouched — and references/schema.md documents statement as a queryable
    # Decision property, so a `cypher` escape hatch would resurface exactly
    # the wording the amendment was meant to retire. Fixing this by silently
    # also rewriting statement would be worse: it changes a field the user
    # never named, which is the kind of magic this tool exists to not do. A
    # note is the whole fix.
    if "title" in fields and "statement" not in fields and was["statement"] == was["title"]:
        print(f'  note: statement still reads "{was["statement"]}" — it was a copy of the old')
        print("        title. Pass --statement to reword it too.")


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


def apply_amend(s: Store, e: dict) -> None:
    """Fold an amendment onto the decision it rewords.

    MATCH, never MERGE. An amendment describes a decision that already
    exists; a MERGE would fabricate a bare node carrying nothing but a new
    title whenever the `record` line above it was unreadable — inventing
    precedent out of a rewording, which `check` would then quote with no
    rationale and no options beside it. `project_portable` refuses to create
    a node for the same reason. Journal order guarantees the `record` line
    is replayed first, so a miss here means that line was skipped, and
    `replay_journal` has already reported it.

    Only the keys present are written: an absent key means "unchanged", so
    `amend --title` cannot silently blank a rationale that took a year to
    earn. An amendment with no fields is a no-op rather than an empty SET
    clause, which does not parse.

    The SET clause is interpolated, its values are not: field names come
    from AMENDABLE and nowhere else, every value goes through a parameter.
    """
    fields = {k: e[k] for k in AMENDABLE if k in e}
    if not fields:
        return
    clause = ", ".join(f"d.{k}=${k}" for k in fields)
    s.q(f"MATCH (d:Decision {{id:$id}}) SET {clause}", {"id": e["id"], **fields})


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
