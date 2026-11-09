import subprocess,sys
raise SystemExit(subprocess.call([sys.executable,"-m","pytest","-q","-o","addopts=",'tests/test_form.py','-k','not online and not submit_set']))
