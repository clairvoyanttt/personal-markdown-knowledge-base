"""Run from any directory with the project's virtual-environment Python."""
from pathlib import Path
import os
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
env = os.environ.copy()
env["PYTHONPATH"] = str(root / "src")
env["QT_QPA_PLATFORM"] = "offscreen"
commands = [
    [sys.executable, "-m", "compileall", "-q", "src", "tests"],
    [sys.executable, "-c", "import pmkb.contracts.common, pmkb.contracts.collections, pmkb.contracts.notes, pmkb.contracts.backup, pmkb.contracts.commit_resolution"],
    [sys.executable, "-m", "pip", "check"],
    [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
]
for command in commands:
    result = subprocess.run(command, cwd=root, env=env, check=False)
    if result.returncode:
        raise SystemExit(result.returncode)
print("Python baseline checks completed; V01-V16 full product acceptance remains unexecuted.")
