"""`osctl` as a command in the repository's venv (DESIGN.md, Registration (operator)).

A console script needs a module in the package, but the tools must stay out of
it: the image copies src/ whole, and osctl_command.py must never reach the
image. So this module holds no tool code. It runs scripts/osctl.py
from the checkout the package is installed from in editable mode
(`pip install -e .`), where this file sits in that checkout's src/.

It imports nothing from the server, so `osctl command` (the bot token) shares
its process with no server code; a test checks that. The server never imports
this module. In the image, the Dockerfile's later COPY replaces the
/usr/local/bin/osctl that `pip install .` makes from this entry point.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# src/outer_steppes/osctl_launcher.py -> the checkout's scripts/osctl.py
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "osctl.py"


def main() -> int:
    if not SCRIPT.is_file():
        print("osctl: no scripts/osctl.py beside this install. The osctl command works from a repository "
              "checkout installed with `pip install -e .`", file=sys.stderr)
        return 2
    spec = importlib.util.spec_from_file_location("osctl", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main(sys.argv[1:])
