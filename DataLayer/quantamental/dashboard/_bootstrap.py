"""Compatibility helper for `streamlit run dashboard/app.py`."""

from pathlib import Path
import sys


def add_project_root(file: str) -> None:
    root = Path(file).resolve().parents[2]
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

