from __future__ import annotations


from ..core import Store
from .checks_graph import _check_amend, _check_decisions, _check_drift, _check_maintain, _check_maintain_hints, _check_missing_project, _check_projects, _check_verdicts
from .checks_identity import _check_detect_project, _check_identity, _check_identity_gaps, _check_merge_legacy_clash, _check_merge_project, _check_merge_replay, _check_merged_into_portable, _check_portable_replay, _check_rekey, _check_remote_changed, _check_settle, _check_worktrees
from .checks_store import _check_concurrent_writers, _check_entry_point, _check_export, _check_graph_format, _check_helpers, _check_journal, _check_lock_modes, _check_missing_engine, _check_reader_isolation, _check_relocate, _check_standing_orders


def cmd_selftest(a, s: Store) -> None:
    """One runnable check over the paths that contain real logic.

    Each check cleans up after itself, so the node count is the invariant
    that catches a check which forgot to.
    """
    before = s.q("MATCH (n) RETURN count(n) AS n")[0]["n"]
    _check_standing_orders()
    _check_entry_point()
    _check_missing_engine()
    _check_helpers()
    _check_detect_project()
    _check_worktrees()
    _check_missing_project(s)
    _check_drift()
    _check_projects(s)
    _check_decisions(s)
    _check_relocate()
    _check_export(s)
    _check_concurrent_writers()
    _check_journal(s)
    _check_amend(s)
    _check_lock_modes()
    _check_reader_isolation()
    _check_verdicts(s)
    _check_maintain(s)
    _check_identity(s)
    _check_identity_gaps(s)
    _check_rekey(s)
    _check_settle()
    _check_merge_project()
    _check_merge_replay()
    _check_merged_into_portable()
    _check_remote_changed()
    _check_merge_legacy_clash()
    _check_graph_format()
    _check_maintain_hints()
    _check_portable_replay()
    after = s.q("MATCH (n) RETURN count(n) AS n")[0]["n"]
    assert after == before, f"selftest changed node count {before} -> {after}"
    print(f"selftest ok ({before} nodes, unchanged)")
