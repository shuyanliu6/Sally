"""Compatibility helper for direct script execution.

Installed/package execution already has the project root on sys.path. Direct
commands like `python scripts/daily_pipeline.py` run with only the scripts
directory importable, so add the DataLayer root before importing quantamental.
"""

from pathlib import Path
import sys


def add_project_root(file: str) -> None:
    root = Path(file).resolve().parents[2]
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

