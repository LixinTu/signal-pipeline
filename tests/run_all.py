"""Run every offline check in tests/ and print one line per file.

The checks use simulated data in temporary folders and never call an API or start Docker.
At the end the number of checks that ran is compared with the number the two READMEs quote,
so that the pages cannot fall behind the checks without a failure.
check_digest_n8n.py also needs Node.js, to run the JavaScript inside the n8n workflow.
Run: python tests/run_all.py   (exit code 1 if any check fails)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Where each README quotes the total number of offline checks
QUOTED = {"README.md": r"([\d,]+) offline checks in", "README.zh-CN.md": r"([\d,]+) 条离线检查"}


def main() -> int:
    """Run each tests/check_*.py in its own Python process; show the failures."""
    scripts = sorted(Path(__file__).resolve().parent.glob("check_*.py"))
    failed = counted = 0
    for script in scripts:
        result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
        lines = result.stdout.strip().splitlines()
        print(f"{'ok  ' if result.returncode == 0 else 'FAIL'} {script.name}: "
              f"{lines[-1] if lines else '(no output)'}")
        ran = re.search(r"/(\d+) checks passed", lines[-1] if lines else "")
        counted += int(ran.group(1)) if ran else 0
        if result.returncode != 0:
            failed += 1
            for line in lines:
                if line.startswith("FAIL"):
                    print("     ", line)
            if result.stderr.strip():
                print("      stderr:", result.stderr.strip()[-2000:])
    print(f"\n{len(scripts) - failed} of {len(scripts)} check files passed, {counted} checks in all")
    stale = False
    for name, pattern in QUOTED.items():
        page = scripts[0].parent.parent / name if scripts else None
        quoted = set(re.findall(pattern, page.read_text(encoding="utf-8"))) if page and page.exists() else set()
        if quoted - {str(counted), f"{counted:,}"}:
            stale = True
            print(f"FAIL {name} quotes {', '.join(sorted(quoted))} offline checks; this run counted {counted}")
    return 1 if failed or stale else 0


if __name__ == "__main__":
    sys.exit(main())
