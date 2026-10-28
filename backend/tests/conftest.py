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


@pytest.fixture(autouse=True)
def _isolated_env_file(tmp_path, monkeypatch):
    """Kreator (backend/configure.py) nigdy nie pisze do prawdziwego backend/.env z testow."""
    from backend import configure
    monkeypatch.setattr(configure, "ENV_PATH", tmp_path / "isolated.env")


@pytest.fixture(autouse=True)
def _ask_about_every_change(monkeypatch):
    """
    Testy potwierdzen (TAK/NIE, plan bezpiecznika, viewer, obserwacja) uzywaja rutynowych komend jak `mkdir` czy
    `systemctl restart`, ktore przy samodzielnosci (domyslnie wlaczonej) wykonalyby sie bez pytania. Tu AUTONOMY=0;
    samodzielnosc sprawdza test_autonomy.py, wlaczajac ja z powrotem.
    """
    from backend.config import settings
    monkeypatch.setattr(settings, "AUTONOMY", False)


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Domyslny DATA_DIR w katalogu tymczasowym (sesje, pamiec) — testy z wlasnym DATA_DIR go nadpisuja."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "default-data"))
