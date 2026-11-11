#!/usr/bin/env python3
"""The repository-local spelling of ``diset-capture``: the command lives in the package.

``pip install -e .`` gives the real command (``diset-capture``); this shim exists
so a checkout that has not been installed can still record evidence with
``python tools/diset_capture.py ...``.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from xrddirac.capture import main

if __name__ == "__main__":
    raise SystemExit(main())
