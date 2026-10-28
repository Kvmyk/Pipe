# Sandbox and failure scenarios

A container posing as a small VPS: Debian, nginx on port 80, a "shop" app on 8080 (`shopctl start|stop|restart`),
static files under `/static/`, and Pipe in native mode inside. Pipe manages the container like a real server.
Nothing from your computer is mounted.

You need Docker and Python 3. Give the model with the same variables as in `backend/.env` (`LLM_PROVIDER`,
`LLM_API_KEY`, `LLM_MODEL`) or with `--env-file backend/.env`. The key reaches the container through
`docker run -e NAME`, so it doesn't show up in the process list.

## Try it

```bash
python3 sandbox/run.py try                        # a healthy server
python3 sandbox/run.py try --scenario app-down    # broken from the start
pipe --no-tunnel --local-port 7390                # or: pipe web --no-tunnel --local-port 7390
docker rm -f pipe-sandbox                         # done
```

## Scenarios

| Scenario | What is broken | When it passes |
|---|---|---|
| `nginx-502` | `proxy_pass` points at port 8081 instead of 8080 | `curl localhost/` returns "Sklep dziala" |
| `app-down` | a typo in the port in `/etc/shop/app.conf`, the app doesn't start | as above |
| `static-403` | `/var/www/shop/static/logo.txt` has mode 600 | `/static/logo.txt` returns the file |
| `disk-hog` | a 64 MB `debug.log.1` in `/var/log/shop` | the answer points at that file, and the file still exists |

Every directory in `scenarios/` has `scenario.json` (title and prompt in English and Polish, kind `fix` or
`diagnose`, words the answer must contain), `break.sh` (breaks things at start), `check.sh` (exit 0 = passed) and
`fix.sh` (the reference fix, used only by the self-test).

## Evaluating models

```bash
LLM_PROVIDER=gemini LLM_API_KEY=... python3 sandbox/run.py eval
python3 sandbox/run.py eval --env-file backend/.env --repeat 3 --label "gemini-3.8-flash"
python3 sandbox/run.py eval --scenario nginx-502 app-down --keep    # containers stay for inspection
PIPE_LANG=pl python3 sandbox/run.py eval                           # prompts in Polish
```

For every scenario a fresh container starts, Pipe gets the prompt, and the script approves every proposed change (up
to 12 times), like a user who says "yes". Then it runs `check.sh`. The result shows the number of passed scenarios,
the number of confirmations and the time. Details with the answers go to `sandbox/results/*.json` (outside git).
Models are not deterministic, so use `--repeat` for comparisons.

Note: every scenario is a few to a dozen requests to the model, which is a real cost with a paid provider.

## Self-test

```bash
python3 sandbox/run.py self-test
```

Without a model it checks that every scenario fails `check.sh` after breaking, passes after `fix.sh`, and that Pipe
in the container answers. CI runs it on every push.

A new scenario is a new directory in `scenarios/` with four files; `backend/tests/test_sandbox.py` makes sure none is
missing.
