"""Run every offline check in tests/ and print one line per file.

The checks use simulated data in temporary folders and never call an API or start Docker.
check_digest_n8n.py also needs Node.js, to run the JavaScript inside the n8n workflow.
Run: python tests/run_all.py   (exit code 1 if any check fails)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    """Run each tests/check_*.py in its own Python process; show the failures."""
    scripts = sorted(Path(__file__).resolve().parent.glob("check_*.py"))
    failed = 0
    for script in scripts:
        result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
        lines = result.stdout.strip().splitlines()
        print(f"{'ok  ' if result.returncode == 0 else 'FAIL'} {script.name}: "
              f"{lines[-1] if lines else '(no output)'}")
        if result.returncode != 0:
            failed += 1
            for line in lines:
                if line.startswith("FAIL"):
                    print("     ", line)
            if result.stderr.strip():
                print("      stderr:", result.stderr.strip()[-2000:])
    print(f"\n{len(scripts) - failed} of {len(scripts)} check files passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
