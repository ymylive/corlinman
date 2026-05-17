"""Pytest config — drop the package's src on sys.path so the suite runs
without requiring an editable install through the workspace.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
