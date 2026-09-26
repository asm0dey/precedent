# /// script
# requires-python = ">=3.12"
# dependencies = ["graphdblite"]
# ///
"""precedent CLI entry point. The code lives in precedent_cli/ beside this file.

Python puts this file's directory on sys.path, so the package imports with no
install step: `uv run precedent.py`, or `python precedent.py` once graphdblite
is installed.
"""
import sys

from precedent_cli.cli import main

if __name__ == "__main__":
    sys.exit(main())
