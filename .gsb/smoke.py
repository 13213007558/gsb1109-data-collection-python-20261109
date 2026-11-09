import subprocess,sys
raise SystemExit(subprocess.call([sys.executable,"-m","pytest","-q","-o","addopts=",'tests']))
