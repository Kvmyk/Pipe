"""Piaskownica (sandbox/): kazdy scenariusz jest kompletny i opisany w obu jezykach. Dzialanie w kontenerze
sprawdza CI (`python3 sandbox/run.py self-test`)."""

import json
import os
from pathlib import Path

import pytest

SANDBOX = Path(__file__).resolve().parents[2] / "sandbox"
SCENARIOS = sorted(p for p in (SANDBOX / "scenarios").iterdir() if p.is_dir())


def test_there_are_scenarios():
    assert {p.name for p in SCENARIOS} >= {"nginx-502", "app-down", "static-403", "disk-hog"}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda p: p.name)
def test_scenario_is_complete(scenario):
    spec = json.loads((scenario / "scenario.json").read_text(encoding="utf-8"))
    assert spec["kind"] in ("fix", "diagnose")
    assert set(spec["title"]) == set(spec["prompt"]) == {"pl", "en"}
    if spec["kind"] == "diagnose":
        assert spec.get("expect"), "scenariusz diagnostyczny ocenia odpowiedz po slowach z `expect`"
    for script in ("break.sh", "fix.sh", "check.sh"):
        path = scenario / script
        assert path.read_text(encoding="utf-8").startswith("#!/bin/sh") and os.access(path, os.X_OK), path


def test_image_never_gets_secrets():
    ignore = (SANDBOX.parent / ".dockerignore").read_text(encoding="utf-8")
    assert "**/.env" in ignore and "backend/data/" in ignore and "!**/.env.example" in ignore
    dockerfile = (SANDBOX / "Dockerfile").read_text(encoding="utf-8")
    assert "LLM_PROVIDER=none" in dockerfile          # bez klucza piaskownica startuje bez modelu
