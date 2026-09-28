"""
Wspolna konfiguracja testow.

Testy opisuja domyslne wdrozenie (kontener Docker z hostem pod /hostfs),
wiec runtime jest ustawiony na `docker` niezaleznie od maszyny, na ktorej
uruchamiamy pytest. Testy trybu native/kubernetes nadpisuja to same.
"""

import pytest


@pytest.fixture(autouse=True)
def _docker_runtime(monkeypatch):
    monkeypatch.setenv("PIPE_RUNTIME", "docker")
    monkeypatch.delenv("HOST_ROOT", raising=False)
    monkeypatch.delenv("HOST_PROC", raising=False)
