"""Shared test fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

# Allow `from app...` imports without installing the package
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
