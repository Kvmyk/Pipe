#!/usr/bin/env python3
"""
Piaskownica Pipe: serwer w pudelku (kontener z nginx i sklepem, w srodku Pipe) i scenariusze awarii.

    python3 sandbox/run.py try                       # wypróbuj Pipe w 2 minuty na swoim komputerze
    python3 sandbox/run.py try --scenario nginx-502  # od razu z awaria do naprawienia
    python3 sandbox/run.py self-test                 # scenariusze psuja i naprawiaja sie poprawnie (bez modelu)
    python3 sandbox/run.py eval                      # agent naprawia scenariusze; tabela wynikow + JSON
    python3 sandbox/run.py eval --scenario app-down --label gemini-flash

Model dla `try` i `eval`: zmienne LLM_PROVIDER, LLM_API_KEY, LLM_MODEL (jak w backend/.env) albo --env-file.
Wartosci kluczy ida do kontenera przez `docker run -e NAZWA` — nie pojawiaja sie w liscie procesow.
Bez modelu `try` dalej dziala (czuwanie, raporty, MCP), a `eval` konczy sie bledem.

Tylko biblioteka standardowa i docker.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = Path(__file__).resolve().parent / "scenarios"
RESULTS = Path(__file__).resolve().parent / "results"
IMAGE = "pipe-sandbox"
READ_LIMIT = 32 * 1024 * 1024
PASSED_ENV = ("LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL", "LLM_REASONING_EFFORT", "LLM_TIMEOUT",
              "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
              "GROQ_API_KEY", "DEEPSEEK_API_KEY", "MISTRAL_API_KEY", "XAI_API_KEY", "PIPE_LANG", "WORKER_MODEL")
MAX_CONFIRMATIONS = 12
# Po --env-file (np. backend/.env z serwera) — ustawienia, ktore musza zostac takie jak w piaskownicy.
SANDBOX_FIXED = ("PIPE_RUNTIME=native", "TCP_HOST=0.0.0.0", "TCP_PORT=7379", "AGENT_SOCKET=/run/pipe.sock",
                 "DATA_DIR=/var/lib/pipe", "AUDIT_LOG_PATH=/var/lib/pipe/audit.log", "AGENT_TOKEN=",
                 "AGENT_VIEWER_TOKEN=", "PIPE_OBSERVE=0", "WEBHOOK_PORT=", "MCP_PORT=", "HOST_ROOT=", "HOST_PROC=")


def scenarios() -> dict[str, dict]:
    return {path.name: json.loads((path / "scenario.json").read_text(encoding="utf-8"))
            for path in sorted(SCENARIOS.iterdir()) if (path / "scenario.json").exists()}


def docker(*args: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=check, text=True,
                          stdout=subprocess.PIPE if quiet else None, stderr=subprocess.PIPE if quiet else None)


def build() -> None:
    print("[sandbox] buduje obraz pipe-sandbox (pierwszy raz ok. minuty)...", flush=True)
    docker("build", "-q", "-f", "sandbox/Dockerfile", "-t", IMAGE, str(ROOT), quiet=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start(name: str, port: int, scenario: str | None, env_file: str | None, *, with_llm: bool) -> None:
    docker("rm", "-f", name, check=False, quiet=True)
    command = ["run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:7379"]
    if scenario:
        command += ["-e", f"SCENARIO={scenario}"]
    if with_llm:
        if env_file:
            command += ["--env-file", env_file]
        command += [arg for name_ in PASSED_ENV if os.environ.get(name_) for arg in ("-e", name_)]
        command += [arg for value in SANDBOX_FIXED for arg in ("-e", value)]
    docker(*command, IMAGE, quiet=True)


def wait_ready(name: str, port: int, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            frames = asyncio.run(request(port, {"command": "health", "session_id": "sandbox-probe"}))
            if frames and frames[-1].get("done"):
                return
        except OSError:
            pass
        state = docker("inspect", "-f", "{{.State.Running}}", name, check=False, quiet=True).stdout.strip()
        if state == "false":
            logs = docker("logs", "--tail", "30", name, check=False, quiet=True)
            raise SystemExit(f"[sandbox] kontener {name} zakonczyl sie:\n{logs.stdout}{logs.stderr}")
        time.sleep(1)
    raise SystemExit(f"[sandbox] Pipe w {name} nie odpowiada po {timeout:.0f} s")


async def request(port: int, payload: dict) -> list[dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=READ_LIMIT)
    try:
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()
        frames = []
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=600)
            if not line:
                break
            frame = json.loads(line)
            frames.append(frame)
            if frame.get("done"):
                break
        return frames
    finally:
        writer.close()


def run_check(name: str, scenario: str) -> bool:
    return docker("exec", name, f"/opt/sandbox/scenarios/{scenario}/check.sh", check=False, quiet=True).returncode == 0


def converse(port: int, prompt: str, lang: str) -> dict:
    """Jedna prosba + automatyczne TAK na kazde pytanie o potwierdzenie (jak uzytkownik, ktory sie zgadza)."""
    session = f"eval-{uuid.uuid4()}"
    texts, confirmations = [], 0
    frames = asyncio.run(request(port, {"message": prompt, "session_id": session, "interface": f"eval:{lang}"}))
    while True:
        texts += [f.get("response", "") for f in frames if f.get("response")]
        errors = [f.get("response", "") for f in frames if f.get("status") == "error"]
        if errors and any("LLM_PROVIDER=none" in e for e in errors):
            raise SystemExit("[sandbox] Pipe w kontenerze nie ma modelu — ustaw LLM_PROVIDER i LLM_API_KEY albo --env-file")
        if not any(f.get("status") == "confirm" for f in frames) or confirmations >= MAX_CONFIRMATIONS:
            break
        confirmations += 1
        frames = asyncio.run(request(port, {"confirm": True, "session_id": session}))
    return {"answer": texts[-1] if texts else "", "transcript": texts, "confirmations": confirmations}


# ─── Tryby ──────────────────────────────────────────────────────────────────

def cmd_try(args: argparse.Namespace) -> int:
    known = scenarios()
    if args.scenario and args.scenario not in known:
        raise SystemExit(f"Nieznany scenariusz {args.scenario!r}. Dostepne: {', '.join(known)}")
    build()
    start("pipe-sandbox", args.port, args.scenario, args.env_file, with_llm=True)
    wait_ready("pipe-sandbox", args.port)
    model = "bez modelu (dodaj providera w pipe web albo /providerzy)" \
        if not (os.environ.get("LLM_PROVIDER") or args.env_file) else os.environ.get("LLM_PROVIDER", "z --env-file")
    print(f"""
Piaskownica dziala: kontener pipe-sandbox, Pipe na 127.0.0.1:{args.port}, model: {model}.
{f"Scenariusz: {known[args.scenario]['title']['pl']}. Popros Pipe o pomoc:" if args.scenario else "Serwer jest sprawny. Przyklad:"}
  {known[args.scenario]['prompt']['pl'] if args.scenario else "Pokaz architekture tego serwera"}

Polacz sie:
  pipe --no-tunnel --local-port {args.port}          # terminal
  pipe web --no-tunnel --local-port {args.port}      # przegladarka

Koniec: docker rm -f pipe-sandbox
""")
    return 0


def cmd_self_test(args: argparse.Namespace) -> int:
    build()
    failures = 0
    for scenario, spec in scenarios().items():
        if args.scenario and scenario not in args.scenario:
            continue
        name, port = f"pipe-sandbox-test-{scenario}", free_port()
        start(name, port, scenario, None, with_llm=False)
        try:
            wait_ready(name, port)
            broken_ok = run_check(name, scenario)
            reply = asyncio.run(request(port, {"message": "czesc", "session_id": "self-test", "interface": "eval"}))
            answers = " ".join(f.get("response", "") for f in reply)
            docker("exec", name, f"/opt/sandbox/scenarios/{scenario}/fix.sh", check=False, quiet=True)
            fixed_ok = run_check(name, scenario)
            expected_broken = spec["kind"] == "fix"
            good = (broken_ok != expected_broken) and fixed_ok and "LLM_PROVIDER=none" in answers
            failures += not good
            print(f"{'OK ' if good else 'BLAD'} {scenario:<12} po zepsuciu check={'ok' if broken_ok else 'nie'}"
                  f", po naprawie check={'ok' if fixed_ok else 'nie'}, Pipe odpowiada={'tak' if answers else 'nie'}")
        finally:
            docker("rm", "-f", name, check=False, quiet=True)
    return 1 if failures else 0


def cmd_eval(args: argparse.Namespace) -> int:
    if not (os.environ.get("LLM_PROVIDER") or args.env_file):
        raise SystemExit("eval potrzebuje modelu: ustaw LLM_PROVIDER i LLM_API_KEY (albo --env-file backend/.env)")
    build()
    lang = os.environ.get("PIPE_LANG", "pl")
    results = []
    for scenario, spec in scenarios().items():
        if args.scenario and scenario not in args.scenario:
            continue
        for attempt in range(1, args.repeat + 1):
            name, port = f"pipe-sandbox-eval-{scenario}", free_port()
            start(name, port, scenario, args.env_file, with_llm=True)
            started = time.time()
            try:
                wait_ready(name, port)
                outcome = converse(port, spec["prompt"][lang], lang)
                check = run_check(name, scenario)
                found = all(word.lower() in " ".join(outcome["transcript"]).lower() for word in spec.get("expect", []))
                passed = check and found
            except SystemExit:
                raise
            except Exception as exc:     # blad polaczenia albo protokolu liczy sie jako porazka scenariusza
                outcome, check, found, passed = {"answer": f"{type(exc).__name__}: {exc}", "transcript": [],
                                                 "confirmations": 0}, False, False, False
            finally:
                if not args.keep:
                    docker("rm", "-f", name, check=False, quiet=True)
            seconds = round(time.time() - started, 1)
            results.append({"scenario": scenario, "attempt": attempt, "passed": passed, "check": check,
                            "expected_words": found, "confirmations": outcome["confirmations"], "seconds": seconds,
                            "answer": outcome["answer"][:2000]})
            print(f"{'ZDANY ' if passed else 'OBLANY'} {scenario:<12} #{attempt}  potwierdzen: {outcome['confirmations']:>2}"
                  f"  czas: {seconds:>5.1f} s", flush=True)
    passed = sum(r["passed"] for r in results)
    label = args.label or os.environ.get("LLM_MODEL") or os.environ.get("LLM_PROVIDER") or "model"
    print(f"\n{label}: {passed}/{len(results)} scenariuszy zdanych")
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{time.strftime('%Y%m%d-%H%M%S')}-{''.join(c if c.isalnum() or c in '.-_' else '_' for c in label)}.json"
    out.write_text(json.dumps({"label": label, "lang": lang, "passed": passed, "total": len(results),
                               "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Szczegoly: {out.relative_to(ROOT)}")
    return 0 if passed == len(results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    p_try = sub.add_parser("try", help="uruchom piaskownice do wyprobowania")
    p_try.add_argument("--scenario", help="zacznij od awarii: " + ", ".join(scenarios()))
    p_try.add_argument("--port", type=int, default=7390)
    p_try.add_argument("--env-file", help="plik z LLM_PROVIDER, LLM_API_KEY... (np. backend/.env)")
    p_self = sub.add_parser("self-test", help="sprawdz scenariusze bez modelu")
    p_self.add_argument("--scenario", nargs="*")
    p_eval = sub.add_parser("eval", help="agent naprawia scenariusze, wynik w tabeli")
    p_eval.add_argument("--scenario", nargs="*")
    p_eval.add_argument("--repeat", type=int, default=1, help="ile razy kazdy scenariusz (modele sa niedeterministyczne)")
    p_eval.add_argument("--label", help="nazwa w wynikach (domyslnie LLM_MODEL)")
    p_eval.add_argument("--env-file")
    p_eval.add_argument("--keep", action="store_true", help="nie usuwaj kontenerow po scenariuszu")
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    return {"try": cmd_try, "self-test": cmd_self_test, "eval": cmd_eval}[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
