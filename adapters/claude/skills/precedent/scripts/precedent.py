# /// script
# requires-python = ">=3.12"
# dependencies = ["graphdblite"]
# ///
"""precedent CLI entry point. The code lives in precedent_cli/ beside this file.

This script puts its own resolved directory on sys.path, so the package
imports with no install step: `uv run precedent.py`, or `python precedent.py`
once graphdblite is installed.
"""
import pathlib
import sys

# Python resolves a symlinked script's directory only on POSIX; resolving it
# here lets a symlinked entry script find its package on Windows too.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from precedent_cli.cli import main  # noqa: E402

if __name__ == "__main__":
    main()  # errors leave through SystemExit; returning means exit 0
