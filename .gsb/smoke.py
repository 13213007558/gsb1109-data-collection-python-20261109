import os
import subprocess
import sys
import tempfile


ARGS = [
    sys.executable, "-m", "pytest", "-q", "-o", "addopts=",
    "tests/test_form.py", "-k", "not online and not submit_set",
]

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
result = subprocess.call(ARGS, cwd=root)
if result == 0:
    raise SystemExit(0)

# In restricted sandboxes pytest may fail during collection while probing
# parent directories for conftest.py (a PermissionError on an unreadable
# ancestor). Re-run from a writable directory that mirrors the workspace via
# symlinks, where no such unreadable parent is traversed.
probe = subprocess.run(
    ARGS, cwd=root, capture_output=True, text=True)
if "PermissionError" not in (probe.stdout + probe.stderr):
    raise SystemExit(result)

mirror = tempfile.mkdtemp(prefix="mechanicalsoup-smoke-")
for name in ("mechanicalsoup", "tests", "setup.cfg", ".coveragerc"):
    os.symlink(os.path.join(root, name), os.path.join(mirror, name))
raise SystemExit(subprocess.call(ARGS, cwd=mirror))
