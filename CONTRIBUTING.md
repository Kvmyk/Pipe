# Contributing to Pipe

Thanks for looking. Pipe is a one-person project, so small, focused contributions are the easiest to accept.
Code comments, docstrings and `docs/` are in Polish; issues, pull requests and discussions in English are welcome.

## What helps most right now

New features are frozen until Pipe has users beyond its author (see `docs/roadmap.md`). The most useful
contributions are:

- **Bug reports from real servers.** What you asked, what Pipe did, the version (the `VERSION` file or the CLI banner),
  the mode (docker / native / kubernetes) and the model.
- **Model results.** Run `python3 sandbox/run.py eval --repeat 3 --label <model>` and open an issue with the summary
  line and the JSON from `sandbox/results/`.
- **New failure scenarios** in `sandbox/scenarios/` (see `sandbox/README.md`): a realistic breakage, a `check.sh`
  that proves it's fixed, and a reference `fix.sh`.
- **Classifier bypasses.** A command classified as `safe` that changes state or leaks a secret. Report security
  issues privately (see `SECURITY.md`), not in a public issue.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt -r clients/telegram/requirements.txt
.venv/bin/python -m pytest backend/tests/ -q          # everything must pass
python3 sandbox/run.py self-test                      # if you touched sandbox/ (needs Docker)
```

CI runs the tests on Python 3.11 and 3.13, builds both images, renders the Kubernetes overlays, checks the shell
scripts and runs the sandbox self-test.

Before you change something, read `CLAUDE.md`: it describes the architecture, the handler contract, the security
model and the rules for user-visible text. The short version:

- Every user- or model-visible string exists in Polish and English: `tr("polski", "english")` next to its use.
  Prompts and tool descriptions are whole-file translations (`prompts.py` / `prompts_en.py`, `tools.py` /
  `tools_en.py`). A change to `docs/*.md` needs the same change in `docs/en/`.
- Protocol tags (`[POTWIERDZ]`, `[BLAD]`, `[ODMOWA]`, ...) never change with the language.
- A new tool is three edits: a schema in `core/tools.py`, a `handle_<name>` async generator in `core/handlers/`, and
  its export in `core/handlers/__init__.py`.
- Anything that runs a shell command goes through `common.run_classified()`. Never weaken the classifier without a
  test in `backend/tests/test_security*.py`.
- Never commit secrets: `backend/.env`, `clients/telegram/.env`, `backend/data/`.

## Pull requests

- One topic per pull request, with tests.
- Don't bump the version or edit `docs/changelog.md`; that happens at release.
- Describe what changes for the user and how you checked it (tests, sandbox, a real server).

By contributing you agree that your contribution is licensed under the MIT license of this repository.
