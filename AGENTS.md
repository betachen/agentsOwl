# AGENTS.md

AgentsOwl is a dependency-free Python 3.10+ CLI for optional worker/peer agent
collaboration.

- Keep repository policy authoritative; never turn peer feedback into an approval gate.
- Preserve compatibility aliases unless a migration document explains their removal.
- Runtime state belongs outside target repositories by default.
- Use only the Python standard library unless a new dependency is clearly justified.
- For code changes, run:
  - `PYTHONPATH=src python3 -m unittest discover -s tests -v`
  - `python3 -m compileall -q src tests`
  - `bash -n bin/agents-owl`
