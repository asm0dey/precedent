from __future__ import annotations

import pathlib

from .core import Store, slug
from .projects import classify_prompt, contained, effective_tags, enclosing, neighbours, project_info, settle, tags_of, topic_vocabulary


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


def _brief_silence(s: Store, info: dict) -> None:
    # Silence here would be self-defeating: a project with history and
    # nothing recorded is precisely the one worth backfilling, and it is
    # the only case where nobody is ever prompted to do it.
    if worth_backfilling(s, info):
        print(f"project: {info['name']}   nothing recorded here yet.")
        print(f"  contents: {info['contents']}")
        print("  it has history the graph cannot see. Offer /precedent-analyze"
              " — read the manifests, CI and docs, and record the decisions"
              " already visible in them. Offer once; do not push it.")


def _print_brief_header(s: Store, info: dict) -> list[str]:
    tags = effective_tags(s, info)
    own = set(info["tags"])
    shown = ", ".join(t if t in own else f"{t}*" for t in tags) or "none"
    print(f"project: {info['name']}   tags: {shown}")
    if len(tags) > len(own):
        print("  (* inherited from an enclosing project)")
    return tags


def _print_decided_here(here: list[dict]) -> None:
    print(f"\n== decided here ({len(here)}) ==")
    for r in here:
        print(f"  [{r['scope']}] {r['title']}"
              f"  ({','.join(filter(None, r['topics'])) or '-'})  #{r['id']}")
        if r.get("despite"):
            print(f"       knowing exception: {r['despite']}")


def _print_inherited(inherited: list[dict]) -> None:
    if not inherited:
        return
    # A monorepo's CI, release and licensing decisions are true of every
    # module in it. Recorded once at the root, they have to reach here.
    print(f"\n== inherited from enclosing projects ({len(inherited)}) ==")
    for r in inherited:
        print(f"  [{r['scope']}] {r['title']}  (from {r['project']})  #{r['id']}")


def _print_modules(s: Store, below: list[dict]) -> None:
    if not below:
        return
    print(f"\n== modules inside this project ({len(below)}) ==")
    for r in below:
        mt = ", ".join(tags_of(s, r["id"])) or "untagged"
        n = s.q("""MATCH (d:Decision)-[:IN_PROJECT]->(:Project {id:$id})
                   WHERE d.status='active' RETURN count(d) AS n""",
                {"id": r["id"]})[0]["n"]
        print(f"  {r['name']:<20} {mt}  ({n} decision(s))")
    print("  record against the module a decision is about, not the root")


def _print_peer_precedent(s: Store, kin: list[dict]) -> None:
    print(f"\n== closest projects ({len(kin)}) ==")
    for r in kin[:8]:
        print(f"  {r['name']:<20} {r['n']} shared: {', '.join(sorted(r['shared']))}")
    if not kin:
        print("  none share a tag with this project yet")

    ids = [r["id"] for r in kin]
    peers = s.q("""MATCH (d:Decision)-[:IN_PROJECT]->(p:Project),
                         (d)-[:ABOUT]->(tp:Topic)
                   WHERE p.id IN $ids AND d.status='active'
                   OPTIONAL MATCH (d)-[:CHOSE]->(o:Option)
                   RETURN tp.name AS topic, collect(DISTINCT o.name) AS opts,
                          collect(DISTINCT p.name) AS projects, count(DISTINCT p) AS n
                   ORDER BY n DESC LIMIT 20""", {"ids": ids}) if ids else []
    print(f"\n== precedent from those projects ({len(peers)}) ==")
    for r in peers:
        print(f"  {r['topic']}: {','.join(filter(None, r['opts'])) or '-'}"
              f"   [{r['n']}x: {','.join(r['projects'])}]")


def _print_lessons_and_principles(s: Store) -> None:
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


def cmd_brief(a, s: Store) -> None:
    # Writes only to fold an older key into this project's (settle). Never
    # creates a node: a SessionStart hook calls this in every directory the
    # user opens, and creating would litter the graph with empty projects.
    info = project_info(s, a.project)
    settle(s, info)

    # WHERE goes with the MATCH it filters, BEFORE the OPTIONAL MATCH. After
    # it, openCypher binds it to the optional pattern, which filters nothing:
    # superseded decisions come back with an empty topic list instead of being
    # excluded. The previous engine applied it to the whole query, so this read
    # correctly there by accident; graphdblite follows the spec and showed the
    # bug (10 "decided here" instead of 8). Three siblings below had it too.
    here = s.q("""MATCH (d:Decision)-[:IN_PROJECT]->(:Project {id:$pid})
                  WHERE d.status='active'
                  OPTIONAL MATCH (d)-[:ABOUT]->(t:Topic)
                  RETURN d.id AS id, d.title AS title, d.scope AS scope,
                         d.created AS created, d.despite AS despite,
                         collect(DISTINCT t.name) AS topics
                  ORDER BY created DESC LIMIT 25""", {"pid": info["id"]})
    kin = neighbours(s, info, a.min_shared)
    above, below = enclosing(s, info), contained(s, info)

    inherited = s.q("""MATCH (d:Decision)-[:IN_PROJECT]->(p:Project)
                       WHERE p.id IN $ids AND d.status='active'
                       OPTIONAL MATCH (d)-[:ABOUT]->(t:Topic)
                       RETURN d.id AS id, d.title AS title, d.scope AS scope,
                              p.name AS project, collect(DISTINCT t.name) AS topics
                       ORDER BY project""",
                    {"ids": [r["id"] for r in above]}) if above else []

    # --only-if-relevant lets a SessionStart hook call this unconditionally in
    # every directory: staying silent is decided here, on the data, rather than
    # by the caller grepping this output for phrases that later change.
    if a.only_if_relevant and not here and not kin and not inherited and not below:
        _brief_silence(s, info)
        return

    tags = _print_brief_header(s, info)
    _print_decided_here(here)
    _print_inherited(inherited)
    _print_modules(s, below)

    if not tags:
        print("\n== precedent ==")
        print(classify_prompt(s, info))
        return

    _print_peer_precedent(s, kin)
    _print_lessons_and_principles(s)


def _print_prior(rows: list[dict]) -> None:
    for r in rows:
        mark = {"active": "", "superseded": "  (superseded)",
                "regretted": "  (REGRETTED — see verdict below)"}.get(r["status"], "")
        tags = ",".join(sorted(filter(None, r["ptags"]))) or "untagged"
        print(f"  {r['pname']} [{tags}]{mark}: {r['title']}")
        print(f"     chose: {','.join(filter(None, r['chose'])) or '-'}"
              f"   rejected: {','.join(filter(None, r['rejected'])) or '-'}   #{r['id']}")
        if r["why"]:
            print(f"     why: {r['why']}")


def _print_topics_in_use(s: Store, governing: list[dict]) -> None:
    known = topic_vocabulary(s)
    # A governed topic is settled ground, not new ground — saying
    # otherwise invites the caller to decide it again from scratch.
    ground = "it is already governed above." if governing else "this is new ground."
    if known:
        print("\n== topics in use ==")
        for r in known:
            print(f"  {r['topic']:<24} {r['decisions']} decision(s)")
        print("\n  matching is exact, so a synonym finds nothing. If one of"
              " these is the same question, re-run with it. If none is,"
              f" {ground}")
    else:
        print(f"  no decisions recorded anywhere yet — {ground}")


def _print_regrets(s: Store, topic: str, chose: str) -> list[dict]:
    regrets = s.q("""MATCH (l:Lesson {topic:$t, option:$o})
                     OPTIONAL MATCH (l)-[:REGRETS]->(d:Decision)
                     RETURN l.statement AS why, l.instead AS instead,
                            count(d) AS n""", {"t": topic, "o": chose})
    regrets = [r for r in regrets if r["why"]]
    for r in regrets:
        # Loudest verdict there is: not "you usually do otherwise" but "you did
        # exactly this, repeatedly, and concluded it was a mistake".
        print(f"  REGRET: you chose this in {r['n']} project(s) and later"
              f" concluded it was a mistake —")
        print(f"     \"{r['why']}\"")
        if r["instead"]:
            print(f"     you now prefer: {r['instead']}")
    return regrets


def _print_endorsed(s: Store, topic: str, chose: str) -> list[dict]:
    # The mirror of REGRET. Without it, taking a lesson's own advice is met
    # with the rejections recorded by the decisions that lesson regrets.
    endorsed = s.q("""MATCH (l:Lesson {topic:$t, instead:$o})
                      OPTIONAL MATCH (l)-[:REGRETS]->(d:Decision)
                      RETURN l.option AS was, l.statement AS why, count(d) AS n""",
                   {"t": topic, "o": chose})
    endorsed = [r for r in endorsed if r["why"]]
    for r in endorsed:
        print(f"  LESSON: you chose '{r['was']}' for this in {r['n']} project(s),"
              f" concluded it was a mistake, and now prefer this —")
        print(f"     \"{r['why']}\"")
    return endorsed


def _print_revived(s: Store, topic: str, chose: str) -> list[dict]:
    revived = s.q("""MATCH (d:Decision)-[:ABOUT]->(:Topic {name:$t}),
                           (d)-[:REJECTED]->(:Option {name:$o}),
                           (d)-[:IN_PROJECT]->(p:Project)
                     WHERE d.status='active'
                     RETURN p.name AS pname, d.title AS title, d.rationale AS why""",
                  {"t": topic, "o": chose})
    for r in revived:
        print(f"  CONFLICT: rejected in {r['pname']} ({r['title']})"
              f" — {r['why'] or 'no rationale recorded'}")
    return revived


def _print_divergence(s: Store, topic: str, pid: str, r: dict) -> None:
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


def _print_divergences(s: Store, topic: str, chose: str, pid: str) -> list[dict]:
    norm = s.q("""MATCH (d:Decision)-[:ABOUT]->(:Topic {name:$t}),
                        (d)-[:CHOSE]->(o:Option),
                        (d)-[:IN_PROJECT]->(p:Project)
                  WHERE d.status='active' AND o.name <> $o
                  RETURN o.name AS other, count(DISTINCT p) AS n,
                         collect(d.created) AS dates
                  ORDER BY n DESC LIMIT 3""", {"t": topic, "o": chose})
    diverged = [r for r in norm if r["n"] >= 2]
    for r in diverged:
        _print_divergence(s, topic, pid, r)
    return diverged


def _print_verdict(a, s: Store, topic: str) -> None:
    # Resolved once, here rather than in the loop below: the loop runs up to
    # three times and project_info shells out to git. A bare
    # Path(a.project).resolve() would miss the node whenever this checkout
    # was first seen on another machine, and every acknowledged divergence
    # would be reported as unacknowledged again.
    pid = project_info(s, a.project)["id"]

    # Two things are worth interrupting a human for: reviving something they
    # already rejected, and quietly diverging from their own settled norm.
    print(f"\n== verdict for choosing '{a.chose}' ==")
    regrets = _print_regrets(s, topic, a.chose)
    endorsed = _print_endorsed(s, topic, a.chose)
    revived = _print_revived(s, topic, a.chose)
    diverged = _print_divergences(s, topic, a.chose, pid)

    if not revived and not diverged and not regrets and not endorsed:
        print("  clear — no rejection history, no regret, no divergence from your norm")


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
    _print_prior(rows)
    # A Principle governs its topic through an ABOUT edge, whether or not a
    # Decision was ever recorded under that topic — and the topic with no
    # decisions is precisely the one a standing rule exists to answer. Asked
    # after the empty-topic return below, it answered only the topics that
    # least needed it.
    governing = s.q("""MATCH (pr:Principle)-[:ABOUT]->(:Topic {name:$t})
                       RETURN pr.id AS id, pr.statement AS stmt""", {"t": topic})
    if not rows:
        # "nothing recorded" is false when a principle is recorded under it.
        print("  no decisions recorded under this exact word."
              if governing else "  nothing recorded under this exact word.")
    for r in governing:
        print(f"  PRINCIPLE in play: {r['stmt']}  #{r['id']}")

    if not rows:
        _print_topics_in_use(s, governing)
        return

    if a.chose:
        _print_verdict(a, s, topic)


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
                   WHERE p.id IN $ids AND d.status='active'
                   OPTIONAL MATCH (d)-[:CHOSE]->(o:Option)
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
