Python 3.12.13
Setup from repository root:
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r .gsb/requirements.lock -e .
Run offline baseline:
.venv/bin/python .gsb/smoke.py
Scope: full tests directory, excluding optional comparison benchmarks
