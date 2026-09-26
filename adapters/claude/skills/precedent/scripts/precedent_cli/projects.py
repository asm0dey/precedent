from __future__ import annotations

import os
import pathlib
import re
import sys

from .core import Store, csv, today


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


def git_out(root: pathlib.Path, *args: str) -> str | None:
    """stdout of one git command run in `root`; None if git is missing, slow or fails.

    None is a fine answer everywhere this is used: no remote, no worktree
    mapping. A guessed answer is not — it merges two projects' histories.
    """
    import subprocess
    try:
        # cwd, not `-C root`: the directory never becomes an argument git
        # parses, so no path can be read as an option.
        r = subprocess.run(["git", *args], cwd=root,
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def remote_url(root: pathlib.Path) -> str:
    """origin, else the only remote there is, else nothing.

    Several remotes and no origin is not guessed at: picking one is how a
    fork and its upstream would end up sharing one identity.
    """
    url = git_out(root, "remote", "get-url", "origin")
    if url:
        return url
    names = (git_out(root, "remote") or "").split()
    return (git_out(root, "remote", "get-url", names[0]) or "") if len(names) == 1 else ""


def identity_path(root: pathlib.Path) -> pathlib.Path:
    """This directory as its main checkout sees it.

    A linked worktree is the same project as the checkout it was added from.
    A repo with a remote gets that through the portable id; one without would
    key a second project on the worktree's own path. Only a linked worktree
    is mapped — its git dir differs from the common dir — so a submodule,
    whose common dir is `.git/modules/<name>`, keeps its own path.

    ponytail: a worktree of a submodule maps onto .git/modules/<name>. Still
    one stable key per project, just not a pretty one.
    """
    root = root.resolve()
    # git < 2.31 has no --path-format and echoes "--path-format=absolute" back
    # as output. The paths below then fail to match, and the mapping falls back
    # to the directory itself — the safe answer, never a wrong project.
    top = git_out(root, "rev-parse", "--show-toplevel")
    git_dir = git_out(root, "rev-parse", "--path-format=absolute", "--git-dir")
    common = git_out(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not (top and git_dir and common):
        return root
    common_p = pathlib.Path(common).resolve()
    if pathlib.Path(git_dir).resolve() == common_p:
        return root
    main = common_p.parent if common_p.name == ".git" else common_p
    if not main.is_dir():
        return root
    try:
        return main / root.relative_to(pathlib.Path(top).resolve())
    except ValueError:
        return root


def portable_id(root: pathlib.Path) -> str | None:
    """The identity that survives a machine, a clone location and an OS.

    None is a fine answer — a project with no remote is correctly
    machine-local. A guessed id is not: it silently merges the histories of
    two unrelated projects, which is the failure a wrong tag causes.
    """
    remote = normalise_remote(remote_url(root))
    if not remote:
        return None
    top = git_out(root, "rev-parse", "--show-toplevel")
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


def refuse_missing_project(s: "Store", value: str) -> None:
    """Exit 2 unless `--project` names a directory.

    A name passed here resolves against the cwd into a path nothing lives at;
    that path has no remote, so it keyed a fresh, untagged, invisible project
    on every call (issue #13). Refusing is cheaper than any repair.
    """
    target = pathlib.Path(value)
    if target.is_dir():
        return
    here = pathlib.Path.cwd().resolve()
    msg = [f"--project {value!r} is not a directory (resolved to {target.resolve()}).",
           "Pass a path, not a name."]
    if value == here.name:
        msg.append(f"You are in {here.name!r} — did you mean --project . ?")
    elif s.q("MATCH (p:Project {name:$n}) RETURN p.id AS id LIMIT 1", {"n": value}):
        msg.append(f"A project named {value!r} exists — pass its directory,"
                   " or run from inside it with --project .")
    print("\n".join(msg), file=sys.stderr)
    raise SystemExit(2)


def project_info(s: "Store", path: str, extra_tags: str = "") -> dict:
    """Identify this directory: where it belongs, and which node to read.

    `key` is where the project belongs: its portable id, else the node
    already holding its path, else its main-checkout path (docs/adr/0009).
    `id` is the node to READ — the key's node when it exists, else a node an
    older key left behind, so `check` and `suggest`, which must not write,
    still see a project no writer has re-keyed yet. `legacy` lists those
    older nodes; `settle` folds them.

    `contents` comes from the directory actually open, not the main
    checkout, so the listing matches what is on disk here. The name is the
    stored one when the project exists: a worktree reads as its project.

    Reads only.
    """
    refuse_missing_project(s, path)
    here = pathlib.Path(path)
    info = detect_project(here)
    ident = identity_path(here)
    info["path"], info["name"] = str(ident), ident.name
    info["portable"] = portable_id(here)
    info["key"] = info["portable"] or path_holder(s, info["path"]) or info["path"]
    info["legacy"] = legacy_keys(s, info["key"], info["portable"],
                                 sorted({info["path"], str(here.resolve())}))
    read = lambda pid: s.q("""MATCH (p:Project {id:$id})
                              RETURN p.id AS id, p.name AS name, p.paths AS paths""",
                           {"id": pid})
    row = read(info["key"]) or (read(info["legacy"][0]) if info["legacy"] else [])
    if row:
        info["id"], info["paths"] = row[0]["id"], paths_of(row[0])
        info["name"] = row[0]["name"] or info["name"]
    else:
        info["id"], info["paths"] = info["key"], [info["path"]]
    info["tags"] = sorted(set(tags_of(s, info["id"])) | set(csv(extra_tags)))
    return info


def merge_target(info: dict) -> dict:
    """What a `project_merge` line records about the node it folds into.

    `from` and `into` alone are not enough to rebuild the target: when the
    fold is what created it (merge-project into a directory with no node, or
    a re-key whose key did not exist yet), replay would make it a rename of
    the source — the source's paths only, no portable id. With these fields
    replay upserts the target exactly as the writer did, then folds.
    """
    return {"portable": info.get("portable"), "project_path": info["path"],
            "name": info["name"]}


def settle(s: Store, info: dict) -> None:
    """Fold every older node this project left behind into its key.

    The lazy re-key: called by the commands that write — `record`, `tag`,
    `brief`, `merge-project` — never by readers. Each fold is journalled
    first, because a worktree keyed on its own path cannot be re-folded from
    journal data alone. `brief` still never creates a node from nothing:
    with no older node there is nothing to fold, and folding one is a rename.
    This subsumes the old standalone portable-id backfill: a project that
    gained a remote is exactly a portable-less node on one of our paths.
    """
    if not info["legacy"]:
        return
    for old in info["legacy"]:
        s.log("project_merge", {"from": old, "into": info["key"], **merge_target(info)})
        fold_project(s, old, info["key"])
    if info["portable"]:
        s.q("MATCH (p:Project {id:$id}) SET p.portable=$pp",
            {"id": info["key"], "pp": info["portable"]})
    row = s.q("MATCH (p:Project {id:$id}) RETURN p.name AS name, p.paths AS paths",
              {"id": info["key"]})[0]
    # The live path joins too — a SET on the node the folds just produced,
    # never a node from nothing. Without it a fold from a worktree leaves the
    # key holding only the worktree's path, and once that is removed
    # `maintain` reports the main checkout's own project as gone.
    paths = paths_of(row)
    if info["path"] not in paths:
        paths = sorted(paths + [info["path"]])
        s.q("MATCH (p:Project {id:$id}) SET p.paths=$paths",
            {"id": info["key"], "paths": "\n".join(paths)})
    info["id"], info["legacy"] = info["key"], []
    info["name"], info["paths"] = row["name"] or info["name"], paths
    info["tags"] = sorted(set(info["tags"]) | set(tags_of(s, info["id"])))


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
    return s.q("""MATCH (t:Topic)
                  WHERE EXISTS { MATCH (:Decision {status:'active'})-[:ABOUT]->(t) }
                     OR EXISTS { MATCH (:Principle)-[:ABOUT]->(t) }
                  OPTIONAL MATCH (d:Decision {status:'active'})-[:ABOUT]->(t)
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


def path_holder(s: Store, path: str) -> str | None:
    """The node already holding a remote-less `path`: its id, or None.

    Without a remote a path has no identity of its own to fold anything
    into — it belongs to whichever node already holds it (after a merge,
    a node keyed on another path, or a remote-keyed project a notes folder
    was merged into). An exact id match wins, so an unmerged project always
    resolves to itself; then a portable-less node listing the path; then a
    remote-keyed one. Resolving ONTO a remote-keyed node is not folding it:
    `legacy_keys` still never folds a node with a portable on a path match.
    """
    rows = s.q("MATCH (p:Project) RETURN p.id AS id, p.paths AS paths, p.portable AS pp")
    hits = [r for r in rows if path in (paths_of(r) or [r["id"]])]
    if any(r["id"] == path for r in hits):
        return path
    for want_portable in (False, True):
        ids = sorted(r["id"] for r in hits if bool(r["pp"]) == want_portable)
        if ids:
            return ids[0]
    return None


def legacy_keys(s: Store, key: str, portable: str | None,
                local_paths: list[str]) -> list[str]:
    """Nodes holding this project under a key other than `key`.

    With a remote: a node carrying this exact portable id under a path key
    (every node before docs/adr/0009, and what 0.4.x still creates), or a
    portable-less node whose paths include one of ours (a repo that has
    since gained a remote) — that fold is by path because the portable side
    has nothing yet to match on. A node with a DIFFERENT portable is never
    matched by path: its remote says it is another project, and a wrong
    merge is worse than a visible split.

    Without a remote: only a node keyed on one of our own paths (a worktree
    keyed on its own path) folds — a node that merely LISTS our path already
    owns it (see `path_holder`), so matching on membership here would drag
    that node back under whichever path last wrote, undoing an earlier merge.
    """
    rows = s.q("""MATCH (p:Project) WHERE p.id <> $key
                  RETURN p.id AS id, p.portable AS pp, p.paths AS paths""", {"key": key})
    mine = set(local_paths)
    return sorted(r["id"] for r in rows
                  if (portable and (r["pp"] == portable
                                    or (r["pp"] is None and mine & set(paths_of(r) or [r["id"]]))))
                  or (not portable and r["pp"] is None and r["id"] in mine))


def fold_project(s: Store, frm: str, into: str) -> int:
    """Move one project node into another: its decisions, tags and paths.

    The single operation behind every merge — `merge-project`, the re-key of
    a node an older key left behind, and the replay of both. Decision ids do
    not change, so every reference to one still resolves. The target keeps
    its own name and portable id; a missing target is created from the
    source, which makes the fold a rename. Returns the decisions moved.
    """
    if frm == into:
        return 0
    src = s.q("""MATCH (p:Project {id:$id})
                 RETURN p.name AS name, p.seen AS seen, p.paths AS paths""", {"id": frm})
    if not src:
        return 0
    src = src[0]
    s.q("MERGE (p:Project {id:$id}) ON CREATE SET p.name=$name, p.seen=$seen",
        {"id": into, "name": src["name"], "seen": src["seen"] or today()})
    dst = s.q("MATCH (p:Project {id:$id}) RETURN p.paths AS paths", {"id": into})[0]
    paths = sorted(set(paths_of(dst)) | set(paths_of(src)))
    s.q("MATCH (p:Project {id:$id}) SET p.paths=$paths",
        {"id": into, "paths": "\n".join(paths)})
    moved = s.q("""MATCH (d:Decision)-[:IN_PROJECT]->(:Project {id:$f})
                   RETURN count(d) AS n""", {"f": frm})[0]["n"]
    s.q("""MATCH (d:Decision)-[:IN_PROJECT]->(:Project {id:$f}), (p:Project {id:$t})
           MERGE (d)-[:IN_PROJECT]->(p)""", {"f": frm, "t": into})
    s.q("""MATCH (:Project {id:$f})-[:TAGGED]->(t:Tag), (p:Project {id:$t})
           MERGE (p)-[:TAGGED]->(t)""", {"f": frm, "t": into})
    s.q("MATCH (p:Project {id:$id}) DETACH DELETE p", {"id": frm})
    return moved


def upsert_project(s: Store, info: dict) -> dict:
    """Key the node by the remote when there is one, by the local path when not.

    `id` is the graph key. `path` stays this machine's, because containment
    is derived from paths and must be derived from local ones. Anything the
    project was keyed by before folds in here, on writes and on replay alike,
    so a store converges on one node per project with no migration step to
    remember. These folds need no journal line: replay reproduces them from
    the `portable` and `project_path` every line already carries. The folds
    `settle()` and `merge-project` perform at runtime are different — a
    worktree keyed on its own path, or a ghost, cannot be derived from any
    line's data — so those are journalled as `project_merge`.

    The name is set once, at creation: a write from a worktree or a clone
    under another directory name must not rename the project for everyone.
    """
    local, portable = info["path"], info.get("portable")
    key = portable or path_holder(s, local) or local
    for old in legacy_keys(s, key, portable, [local]):
        fold_project(s, old, key)
    s.q("MERGE (p:Project {id:$id}) ON CREATE SET p.name=$name",
        {"id": key, "name": info["name"]})
    s.q("MATCH (p:Project {id:$id}) SET p.seen=$seen", {"id": key, "seen": today()})
    if portable:
        # Kept although it equals `id`: 0.4.x finds nodes by this property.
        s.q("MATCH (p:Project {id:$id}) SET p.portable=$pp", {"id": key, "pp": portable})
    row = s.q("MATCH (p:Project {id:$id}) RETURN p.paths AS paths", {"id": key})[0]
    known = paths_of(row)
    if local not in known:
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
