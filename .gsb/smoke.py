import subprocess,sys
raise SystemExit(subprocess.call([sys.executable,"-m","pytest","-q","-o","addopts=",'tests/tree','tests/cli','tests/test_helpers.py']))
