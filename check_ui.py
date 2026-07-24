#!/usr/bin/env python3
"""
check_ui.py — parse the dashboard's JavaScript before shipping it.

WHY THIS EXISTS. A single bad escape (`channel\\'s` — an escaped backslash, then a quote that closed
the string) made the ENTIRE dashboard script fail to parse. Every panel went blank. It shipped because
the change was verified server-side only: the API was checked, the numbers were right, and the half
that actually renders them was never loaded. Python compiles fine; the browser never ran a line.

A blank desk during market hours is the most expensive failure mode here, and it is trivially
preventable: extract the <script> and hand it to node. Run this after ANY dashboard.html edit.

    python3 check_ui.py        -> exit 0 if the JS parses, 1 (with the error) if not
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HTML = Path(__file__).parent / "dashboard.html"


def main():
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", HTML.read_text(), re.S)
    if not blocks:
        print("no <script> block found in dashboard.html")
        return 1
    if not subprocess.run(["which", "node"], capture_output=True).returncode == 0:
        print("node not found — cannot syntax-check. INSTALL NODE or check manually.")
        return 1
    bad = 0
    for i, b in enumerate(blocks):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(b)
            path = f.name
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        if r.returncode:
            bad += 1
            print(f"✕ script block {i} FAILED to parse:\n{r.stderr}")
        else:
            print(f"✓ script block {i} parses ({len(b):,} chars)")
        Path(path).unlink(missing_ok=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
