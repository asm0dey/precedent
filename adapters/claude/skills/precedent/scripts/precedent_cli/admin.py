from __future__ import annotations

import json
import os
import pathlib

from .core import CLI, DEFAULT_HOME, POINTER, STANDING_ORDERS, Store, resolve_home


def _ensure_removable(path: pathlib.Path) -> None:
    """Raise if this process cannot delete `path` (file or directory tree).

    Checked before relocate() moves anything, not caught mid-move: a real
    store here can hold root-owned directories (left by an earlier
    docker-mounted graph server run, store bind-mounted), and a
    PermissionError partway through the move loop would strand payload split
    across both directories with no pointer written — after which
    resolve_home(default) keeps quietly resolving to default, masking
    whatever already moved.

    TOCTOU: permissions can still change between this check and the actual
    move. Accepted as the cheap tradeoff for not owning a staging-directory
    and rollback file mover.
    """
    if path.is_dir() and not path.is_symlink():
        if not os.access(path, os.W_OK):
            raise SystemExit(
                f"cannot relocate: {path} is not writable by this process; "
                "fix its permissions or move it aside by hand, then retry.")
        for child in path.iterdir():
            _ensure_removable(child)


def relocate(target: pathlib.Path, default: pathlib.Path = DEFAULT_HOME) -> str:
    """Keep the store somewhere else, and leave a pointer at the default path."""
    import shutil

    target.mkdir(parents=True, exist_ok=True)
    if target == default:
        return f"store: {target}  (the default location)"

    if default.exists() and not default.is_dir():
        raise SystemExit(f"{default} exists and is not a directory; move it aside first")
    default.mkdir(parents=True, exist_ok=True)
    # `.lock*` is skipped for stores that predate the engine swap: the file
    # lock is gone, but a store written before it was removed still has one
    # sitting there, and a leftover from a tool nobody runs any more must not
    # be what makes relocate refuse to move a store.
    payload = [f for f in default.iterdir()
               if not f.name.startswith(".lock") and f.name != POINTER]
    occupied = [f for f in target.iterdir()
                if not f.name.startswith(".lock") and f.name != POINTER]
    if payload and occupied:
        raise SystemExit(
            f"both {default} and {target} hold a store; refusing to merge.\n"
            f"  merging is a journal concatenation, so do it deliberately:\n"
            f"    cat {default}/journal.jsonl >> {target}/journal.jsonl\n"
            f"    mv {default} {default}.bak\n"
            f"  then re-run this, and `precedent.py rebuild`.")
    if payload and not os.access(default, os.W_OK):
        raise SystemExit(f"cannot relocate: {default} is not writable by this process; "
                          "fix its permissions, then retry.")
    for f in payload:
        _ensure_removable(f)
    for f in payload:
        shutil.move(str(f), str(target / f.name))
    (default / POINTER).write_text(str(target) + "\n")
    moved = f"  moved {len(payload)} file(s) from the default location\n" if payload else ""
    return f"store: {target}\n{moved}  {default}/{POINTER} points here"


def cmd_init(a) -> None:
    """Runs without a Store: opening one would create the default directory
    before this command has decided where the store belongs."""
    if not a.location:
        d = DEFAULT_HOME
        real = resolve_home(d)
        if real != d:
            print(f"store: {real}  (via {d}/{POINTER})")
        elif d.exists():
            print(f"store: {d}  (the default location)")
        else:
            print(f"no store yet; it will be created at {d}")
        if os.environ.get("PRECEDENT_HOME"):
            print(f"  note: PRECEDENT_HOME is set to {os.environ['PRECEDENT_HOME']},"
                  " which overrides the above for this shell only —"
                  " the SessionStart hook will not see it.")
        return
    # relocate() always acts on DEFAULT_HOME, never on --home: the
    # SessionStart hook and every slash command are wired to the default
    # path with no flag in between, so a --home that points elsewhere would
    # otherwise relocate a store this invocation was never told about.
    home_arg = pathlib.Path(a.home).resolve()
    if home_arg != DEFAULT_HOME.resolve():
        raise SystemExit(
            f"error: --home {home_arg} was given, but `init --location` always "
            f"relocates the default location ({DEFAULT_HOME}), because that is "
            "the path the SessionStart hook and every slash command are wired "
            "to — not --home. Re-run without --home to relocate the store this "
            "machine actually uses by default.")
    print(relocate(pathlib.Path(a.location).expanduser().resolve()))


def cmd_standing_orders(a) -> None:
    """Print the banner every adapter appends after a brief.

    Runs without a Store: opening one would create the store directory as a
    side effect of printing text, and this command is called from hooks that
    may run in directories the user never records anything in.

    Prints `core.CLI`, which is derived from the entry script's own location
    rather than assumed, so a checkout installed anywhere — ~/.claude/skills,
    an ACR cache, a bare clone — prints a command line that actually runs.
    """
    print(STANDING_ORDERS.format(cli=CLI))


def cmd_cypher(a, s: Store) -> None:
    for row in s.q(a.query, json.loads(a.params) if a.params else None):
        print(row)


def export_to(s: Store, out: pathlib.Path) -> pathlib.Path:
    """Write a consistent, self-contained copy of the store.

    `snapshot_to` copies the graph as of now — taken while this command holds
    the db open, so it cannot catch a half-finished write from another
    process. The journal goes with it because the journal, not the graph, is
    the source of truth: a snapshot without it is an index nothing can rebuild.

    The layout is a store directory, so the copy is usable directly:
    `precedent.py --home <out> check ...`. It does NOT follow the original —
    re-run this after recording.

    What this used to produce was a grafeo-server data directory, for a
    read-only web UI on :7474. graphdblite has no server, so that UI is gone
    rather than ported.
    """
    import shutil

    out.mkdir(parents=True, exist_ok=True)
    s.db.snapshot_to(str(out / "graph.db"))
    if s.journal.exists():
        shutil.copy2(s.journal, out / "journal.jsonl")
    return out


def cmd_export(a, s: Store) -> None:
    out = export_to(s, pathlib.Path(a.out or pathlib.Path(a.home) / "export").expanduser().resolve())
    print(f"exported: {out}")
    print("it is a complete store — graph plus journal — so query it in place:")
    print(f"  precedent.py --home {out} check --topic <topic>")
    print("re-run this after recording, the snapshot is a copy and does not follow the store")
