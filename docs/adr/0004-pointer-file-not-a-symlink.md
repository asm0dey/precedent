# A pointer file, not a symlink, relocates the store

`init` moved the store elsewhere and symlinked the default path at it. The
reasoning was right — the default path is wired into the SessionStart hook and
every slash command, and `PRECEDENT_HOME` is unset in the hook's environment, so
an environment variable cannot do this job. But `os.symlink` on Windows raises
`WinError 1314` without Developer Mode or an elevated shell, which makes `init`
unavailable to a default Windows user for its primary purpose.

The store's location now lives in a pointer file that `Store` reads. It
satisfies the same requirement — the default path stays the entry point, nothing
needs configuring — with no privilege, no platform branch, and less code than
the symlink handling it replaces.

## Consequences

A single mechanism on every platform rather than one that works and one that
does not. `selftest` asserts on the pointer file instead of `is_symlink()`.
