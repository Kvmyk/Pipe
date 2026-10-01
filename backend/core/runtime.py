"""
Runtime — gdzie dziala Pipe i jak widzi zarzadzana maszyne.

Pipe moze dzialac w trzech trybach (PIPE_RUNTIME):

  docker      kontener z hostem zamontowanym pod /hostfs i /proc hosta pod
              /hostproc (backend/docker-compose.yml) — domyslny tryb VPS
  native      proces bezposrednio na maszynie (systemd, bez Dockera);
              system plikow i /proc to juz host
  kubernetes  pod w klastrze; zarzadza klastrem przez kubectl (ServiceAccount),
              a jesli overlay zamontowal wezel pod /hostfs — takze wezlem

`auto` (domyslnie) wybiera tryb po srodowisku. Sciezki mozna nadpisac
zmiennymi HOST_ROOT i HOST_PROC.

Konwencja sciezek: `Session.cwd` i wszystko, co widzi model, to sciezki
HOSTA (`/var/www`). `to_local()` zamienia je na sciezki, pod ktorymi proces
Pipe faktycznie je widzi (`/hostfs/var/www` w Dockerze, bez zmian natywnie).

Modul czyta zmienne srodowiskowe przy kazdym wywolaniu i nie importuje
settings — dzieki temu testy moga przelaczac tryby przez monkeypatch.setenv.
"""

from __future__ import annotations

import os
from typing import Literal

from backend.core.i18n import tr

RuntimeKind = Literal["docker", "native", "kubernetes"]

DOCKER_HOST_ROOT = "/hostfs"
DOCKER_HOST_PROC = "/hostproc"


def kind() -> RuntimeKind:
    """Tryb dzialania: z PIPE_RUNTIME albo wykryty automatycznie."""
    configured = os.getenv("PIPE_RUNTIME", "auto").strip().lower()
    if configured in ("docker", "native", "kubernetes"):
        return configured  # type: ignore[return-value]
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        return "kubernetes"
    if os.path.isdir(DOCKER_HOST_ROOT):
        return "docker"
    return "native"


def host_root() -> str:
    """Prefiks, pod ktorym widac system plikow hosta ('' = jestesmy na hoscie)."""
    configured = os.getenv("HOST_ROOT")
    if configured is not None:
        return configured.rstrip("/")
    runtime = kind()
    if runtime == "docker":
        return DOCKER_HOST_ROOT
    if runtime == "kubernetes" and os.path.isdir(DOCKER_HOST_ROOT):
        return DOCKER_HOST_ROOT
    return ""


def host_proc() -> str:
    """Katalog z /proc hosta (statystyki, gniazda sieciowe)."""
    configured = os.getenv("HOST_PROC")
    if configured:
        return configured.rstrip("/")
    if kind() != "native" and os.path.isdir(DOCKER_HOST_PROC):
        return DOCKER_HOST_PROC
    return "/proc"


def workspace_root() -> str:
    """Najwyzszy katalog, do ktorego agent ma dostep plikowy."""
    return host_root() or "/"


def to_local(path: str) -> str:
    """Sciezka hosta (`/etc/nginx`) -> sciezka widziana przez proces Pipe."""
    path = "/" + path.lstrip("/")
    root = host_root()
    if not root:
        return path
    return root if path == "/" else root + path


def to_host(path: str, cwd: str = "/") -> str:
    """
    Zamienia sciezke podana przez model na sciezke po stronie hosta.

    Akceptuje sciezke hosta (`/var/www`), lokalna z prefiksem (`/hostfs/var/www`)
    i wzgledna (liczona od `cwd`). Wynik zawsze zaczyna sie od `/` i nigdy
    od prefiksu HOST_ROOT — tak jak `Session.cwd`.
    """
    joined = os.path.normpath(os.path.join(cwd, path))
    root = host_root()
    if root and (joined == root or joined.startswith(root + "/")):
        joined = joined[len(root):] or "/"
    return "/" + joined.lstrip("/")


def describe() -> str:
    """Opis trybu dla system promptu — model musi wiedziec, co opisuja komendy."""
    runtime = kind()
    root = host_root()
    proc = host_proc()
    if runtime == "native":
        return tr(
            "Tryb: native. Dzialasz bezposrednio na serwerze (bez kontenera): sciezki, /proc, "
            "`ss`, `ip`, `df` i `systemctl` opisuja ten serwer.",
            "Mode: native. You run directly on the server (no container): paths, /proc, "
            "`ss`, `ip`, `df` and `systemctl` describe this server."
        )
    if runtime == "kubernetes" and not root:
        return tr(
            "Tryb: kubernetes. Dzialasz jako pod w klastrze. Klastrem zarzadzasz przez kubectl "
            "(uprawnienia z ServiceAccount). System plikow poda to nie jest wezel — o wezlach "
            "dowiadujesz sie z `kubectl get nodes` / `kubectl describe node`.",
            "Mode: kubernetes. You run as a pod in the cluster. You manage the cluster through kubectl "
            "(permissions from the ServiceAccount). The pod's filesystem is not the node — you learn about "
            "nodes from `kubectl get nodes` / `kubectl describe node`."
        )
    where = "kontenerze Docker" if runtime == "docker" else "podzie Kubernetesa z zamontowanym wezlem"
    where_en = "a Docker container" if runtime == "docker" else "a Kubernetes pod with the node mounted"
    return tr(
        f"Tryb: {runtime}. Dzialasz w {where}. System plikow hosta jest pod `{root}` (tylko do odczytu, "
        f"poza `{root}/root`), /proc hosta pod `{proc}`. `hostname`, `cat /etc/...`, `ip addr`, `ss` "
        f"i `df /` opisuja kontener, nie serwer: konfiguracje hosta czytaj z `{root}/etc`, porty hosta "
        f"daje network_info, dysk `df -h {root}`. Uzytkownikowi zawsze podawaj sciezki hosta, bez `{root}`.",
        f"Mode: {runtime}. You run in {where_en}. The host filesystem is under `{root}` (read-only, "
        f"except `{root}/root`), the host /proc under `{proc}`. `hostname`, `cat /etc/...`, `ip addr`, `ss` "
        f"and `df /` describe the container, not the server: read host configs from `{root}/etc`, host ports "
        f"come from network_info, disk from `df -h {root}`. Always give the user host paths, without `{root}`."
    )
