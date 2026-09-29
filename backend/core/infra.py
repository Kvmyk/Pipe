"""
Odkrywanie infrastruktury hosta i rysowanie jej mapy (Mermaid).

Zrodla (wszystko odczyty, bez potwierdzen):
  - kontenery Docker: `docker ps -aq` + `docker inspect` (porty, sieci, compose, healthcheck, Traefik)
  - reverse proxy: nginx (sites-enabled, conf.d) i Caddyfile z systemu plikow hosta
  - porty hosta: /proc/1/net (hostinfo.sockets)
  - uslugi systemd wlaczone na hoscie: /etc/systemd/system/*.wants
  - repozytoria git: przejscie po /root, /home, /srv, /opt, /var/www (bez uruchamiania git)
  - Kubernetes (gdy kubectl ma dostep): ingressy, serwisy, deploymenty

`to_mermaid()` sklada z tego diagram: Internet -> proxy -> aplikacje ->
bazy/uslugi, pogrupowane per projekt compose. Etykiety sa escapowane —
nazwy kontenerow i domeny nie moga zlamac skladni Mermaid.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from backend.core import executor, hostinfo, runtime

WELL_KNOWN_PORTS: dict[int, str] = {
    22: "ssh", 25: "smtp", 53: "dns", 80: "http", 443: "https", 465: "smtps", 587: "smtp", 993: "imaps",
    1883: "mqtt", 2375: "docker-api", 2376: "docker-api", 3000: "app/grafana", 3306: "mysql", 5432: "postgres",
    5672: "rabbitmq", 6379: "redis", 6443: "kube-api", 7379: "pipe", 8080: "http-alt", 8443: "https-alt",
    9000: "app", 9090: "prometheus", 9100: "node-exporter", 11434: "ollama", 27017: "mongodb",
}
DATABASE_HINTS = ("postgres", "mysql", "mariadb", "mongo", "redis", "valkey", "memcached", "elasticsearch",
                  "opensearch", "clickhouse", "influxdb", "rabbitmq", "kafka", "minio", "timescale")
PROXY_HINTS = ("nginx", "caddy", "traefik", "haproxy", "envoy", "nginx-proxy-manager", "swag")

REPO_ROOTS = ("/root", "/home", "/srv", "/opt", "/var/www", "/data")
REPO_MAX_DEPTH = 4
_SKIP_DIRS = {"node_modules", ".cache", ".npm", ".venv", "venv", "__pycache__", ".git", "vendor", ".local",
              "site-packages", ".cargo", ".rustup", "go", ".docker", "snap", "overlay2", "proc", ".ssh"}


# ─── Model ──────────────────────────────────────────────────────────────────

@dataclass
class PortMap:
    host_ip: str
    host_port: int
    container_port: int
    proto: str = "tcp"

    @property
    def public(self) -> bool:
        return self.host_ip in ("0.0.0.0", "::", "")


@dataclass
class Container:
    name: str
    image: str
    state: str
    health: str = ""
    ports: list[PortMap] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)
    project: str = ""
    service: str = ""
    workdir: str = ""
    restart_count: int = 0
    domains: list[str] = field(default_factory=list)   # z etykiet Traefika / VIRTUAL_HOST
    image_id: str = ""                                  # sha256:... — zmienia sie po pull + recreate
    mounts: list[tuple[str, str]] = field(default_factory=list)   # (sciezka hosta, sciezka w kontenerze)

    @property
    def kind(self) -> str:
        low = f"{self.image} {self.name}".lower()
        if any(h in low for h in PROXY_HINTS):
            return "proxy"
        if any(h in low for h in DATABASE_HINTS):
            return "db"
        return "app"


@dataclass
class Route:
    """Trasa reverse proxy: domeny -> upstream."""
    proxy: str            # nginx | caddy | traefik
    domains: list[str]
    upstream: str         # np. 127.0.0.1:8080, http://web:3000, unix:/run/x.sock
    source: str = ""


@dataclass
class Repo:
    path: str             # sciezka hosta
    remote: str = ""
    branch: str = ""


@dataclass
class KubeApp:
    namespace: str
    name: str
    kind: str             # Deployment | StatefulSet | DaemonSet
    replicas: str = ""
    services: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)


@dataclass
class Infra:
    hostname: str = ""
    os_name: str = ""
    containers: list[Container] | None = None     # None = Docker niedostepny
    routes: list[Route] = field(default_factory=list)
    listeners: list[hostinfo.Socket] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    kube: list[KubeApp] | None = None
    targets: list[tuple[str, str]] = field(default_factory=list)   # (nazwa, rodzaj)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """Tekstowe podsumowanie dla modelu."""
        lines = [f"Host: {self.hostname or '?'} ({self.os_name or 'system nieznany'})"]
        if self.containers is None:
            lines.append("Docker: niedostepny")
        else:
            lines.append(f"Kontenery ({len(self.containers)}):")
            for c in self.containers:
                ports = ", ".join(f"{p.host_ip or '*'}:{p.host_port}->{p.container_port}" for p in c.ports) or "-"
                extra = f" projekt={c.project}" if c.project else ""
                health = f" ({c.health})" if c.health else ""
                lines.append(f"  - {c.name} [{c.image}] {c.state}{health} porty: {ports}{extra}")
        if self.routes:
            lines.append("Trasy reverse proxy:")
            lines += [f"  - {r.proxy}: {', '.join(r.domains) or '(domyslny)'} -> {r.upstream}" for r in self.routes]
        # jeden wpis na port/protokol (IPv4 i IPv6 razem), bez efemerycznych portow UDP
        public = {(s.port, s.proto.rstrip("6")) for s in self.listeners
                  if s.public and not (s.proto.startswith("udp") and s.port >= 32768)}
        if public:
            lines.append("Porty publiczne hosta: " + ", ".join(f"{port}/{proto}" for port, proto in sorted(public)))
        if self.services:
            lines.append("Uslugi systemd: " + ", ".join(self.services))
        if self.kube:
            lines.append("Kubernetes:")
            lines += [f"  - {a.namespace}/{a.name} ({a.kind} {a.replicas}) hosty: {', '.join(a.hosts) or '-'}"
                      for a in self.kube]
        if self.targets:
            lines.append("Zdalne cele: " + ", ".join(f"{n} ({k})" for n, k in self.targets))
        lines += [f"Uwaga: {n}" for n in self.notes]
        return "\n".join(lines)


# ─── Docker ─────────────────────────────────────────────────────────────────

def parse_inspect(items: list[dict[str, Any]]) -> list[Container]:
    """Kontenery z wyniku `docker inspect` (lista obiektow JSON)."""
    result = []
    for item in items:
        config = item.get("Config") or {}
        state = item.get("State") or {}
        labels = config.get("Labels") or {}
        net = item.get("NetworkSettings") or {}
        ports = []
        for spec, bindings in (net.get("Ports") or {}).items():
            cport, _, proto = spec.partition("/")
            for binding in bindings or []:
                try:
                    ports.append(PortMap(binding.get("HostIp", ""), int(binding.get("HostPort", 0)),
                                         int(cport), proto or "tcp"))
                except (TypeError, ValueError):
                    continue
        # IPv4 i IPv6 tego samego portu to jedno mapowanie na diagramie
        unique: dict[tuple[int, int, str], PortMap] = {}
        for p in ports:
            key = (p.host_port, p.container_port, p.proto)
            known = unique.get(key)
            # publiczne przed lokalnym, IPv4 (0.0.0.0) przed IPv6 (::) — czytelniej na diagramie
            if known is None or (p.public and not known.public) or (p.host_ip == "0.0.0.0" and known.host_ip == "::"):
                unique[key] = p
        domains = []
        for key, value in labels.items():
            if key.startswith("traefik.http.routers.") and key.endswith(".rule"):
                domains += re.findall(r"Host\(`([^`]+)`\)", str(value))
        env = config.get("Env") or []
        for entry in env:
            if entry.startswith(("VIRTUAL_HOST=", "LETSENCRYPT_HOST=")):
                domains += [d.strip() for d in entry.split("=", 1)[1].split(",") if d.strip()]
        result.append(Container(
            name=str(item.get("Name", "")).lstrip("/") or str(item.get("Id", ""))[:12],
            image=str(config.get("Image", "")),
            state=str(state.get("Status", "")),
            health=str((state.get("Health") or {}).get("Status", "")),
            ports=sorted(unique.values(), key=lambda p: p.host_port),
            networks=sorted((net.get("Networks") or {}).keys()),
            project=str(labels.get("com.docker.compose.project", "")),
            service=str(labels.get("com.docker.compose.service", "")),
            workdir=str(labels.get("com.docker.compose.project.working_dir", "")),
            restart_count=int(item.get("RestartCount", 0) or 0),
            domains=sorted(set(domains)),
            image_id=str(item.get("Image", ""))[:19],
            mounts=[(str(m.get("Source", "")), str(m.get("Destination", ""))) for m in item.get("Mounts") or []
                    if m.get("Source") and m.get("Destination")],
        ))
    return sorted(result, key=lambda c: (c.project, c.name))


async def docker_containers() -> list[Container] | None:
    stdout, stderr, code = await executor.execute("docker ps -aq --no-trunc", timeout=20)
    if code != 0:
        return None
    ids = [line.strip() for line in stdout.splitlines() if re.fullmatch(r"[0-9a-f]{12,64}", line.strip())]
    if not ids:
        return []
    stdout, stderr, code = await executor.execute("docker inspect " + " ".join(ids[:200]), timeout=30)
    if code != 0:
        return None
    try:
        return parse_inspect(json.loads(stdout))
    except (json.JSONDecodeError, TypeError):
        return None


# ─── Reverse proxy ──────────────────────────────────────────────────────────

def _strip_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _blocks(text: str, keyword: str) -> list[str]:
    """Zawartosc blokow `keyword ... { ... }` z dopasowaniem nawiasow."""
    blocks = []
    for match in re.finditer(rf"(?m)^\s*{keyword}\b[^{{;]*\{{", text):
        depth, start = 1, match.end()
        i = start
        while i < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        blocks.append(text[start:i - 1])
    return blocks


def parse_nginx(text: str, source: str = "") -> list[Route]:
    text = _strip_comments(text)
    upstreams: dict[str, str] = {}
    for match in re.finditer(r"upstream\s+([\w.-]+)\s*\{([^}]*)\}", text):
        servers = re.findall(r"\bserver\s+([^\s;]+)", match.group(2))
        if servers:
            upstreams[match.group(1)] = servers[0]
    routes = []
    for block in _blocks(text, "server"):
        names = [n for line in re.findall(r"\bserver_name\s+([^;]+);", block) for n in line.split()
                 if n not in ("_", '""', "localhost")]
        for target in re.findall(r"\b(?:proxy_pass|grpc_pass|fastcgi_pass|uwsgi_pass)\s+([^;\s]+)\s*;", block):
            upstream = target
            host = re.sub(r"^\w+://", "", target).split("/", 1)[0]
            if host in upstreams:
                upstream = upstreams[host]
            routes.append(Route("nginx", sorted(set(names)), upstream, source))
    return routes


def parse_caddyfile(text: str, source: str = "") -> list[Route]:
    text = _strip_comments(text)
    routes = []
    depth = 0
    site: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if depth == 0 and stripped.endswith("{") and not stripped.startswith("("):
            head = stripped[:-1].strip()
            site = [re.sub(r"^https?://", "", s.strip().rstrip(",")) for s in head.split() if s.strip()]
            site = [s for s in site if s and not s.startswith(":")] or []
        if depth >= 1:
            match = re.match(r"reverse_proxy\s+(?:[^\s{]*\s+)?([^\s{]+:\d+|[^\s{]+)", stripped)
            if match and not match.group(1).startswith("/"):
                routes.append(Route("caddy", sorted(set(site)), match.group(1), source))
        depth += stripped.count("{") - stripped.count("}")
        if depth <= 0:
            depth = 0
    return routes


def proxy_routes() -> list[Route]:
    routes: list[Route] = []
    for directory in ("/etc/nginx/sites-enabled", "/etc/nginx/conf.d"):
        local = runtime.to_local(directory)
        try:
            names = sorted(os.listdir(local))
        except OSError:
            continue
        for name in names:
            path = os.path.join(local, name)
            if os.path.isfile(path):
                routes += parse_nginx(hostinfo._read(path), f"{directory}/{name}")
    caddyfile = runtime.to_local("/etc/caddy/Caddyfile")
    if os.path.isfile(caddyfile):
        routes += parse_caddyfile(hostinfo._read(caddyfile), "/etc/caddy/Caddyfile")
    return routes


# ─── Uslugi, system, repozytoria ────────────────────────────────────────────

_BORING_SERVICES = {"getty", "serial-getty", "systemd-", "dbus", "polkit", "cron", "rsyslog", "networkd",
                    "resolved", "timesyncd", "unattended-upgrades", "ModemManager", "snapd", "multipathd",
                    "irqbalance", "qemu-guest-agent", "cloud-", "ssh", "sshd", "open-vm-tools", "udisks2",
                    "networking", "e2scrub", "apparmor", "ufw", "remote-fs", "console-setup", "keyboard-setup",
                    "setvtrgb", "grub", "lvm2", "blk-availability", "pollinate", "secureboot", "vgauth",
                    "hibinit", "lxd", "finalrd", "apport", "wpa_supplicant", "chrony", "atd", "containerd",
                    "docker", "fwupd", "rpcbind", "motd", "ua-", "ubuntu-advantage", "iscsi", "open-iscsi"}


def enabled_services() -> list[str]:
    """Wlaczone uslugi hosta (bez systemowych) — to, co uzytkownik sam postawil."""
    names: set[str] = set()
    for wants in ("/etc/systemd/system/multi-user.target.wants", "/etc/systemd/system/default.target.wants"):
        try:
            entries = os.listdir(runtime.to_local(wants))
        except OSError:
            continue
        for entry in entries:
            if entry.endswith(".service"):
                name = entry[:-len(".service")]
                if not any(name == b or name.startswith(b) for b in _BORING_SERVICES):
                    names.add(name)
    return sorted(names)


def host_identity() -> tuple[str, str]:
    hostname = hostinfo._read(runtime.to_local("/etc/hostname")).strip()
    os_release = hostinfo._read(runtime.to_local("/etc/os-release"))
    match = re.search(r'^PRETTY_NAME="?([^"\n]+)"?', os_release, re.MULTILINE)
    return hostname, match.group(1) if match else ""


def _git_remote(git_dir: str) -> str:
    config = hostinfo._read(os.path.join(git_dir, "config"))
    match = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', config, re.DOTALL)
    if not match:
        match = re.search(r'\[remote "[^"]+"\][^\[]*?url\s*=\s*(\S+)', config, re.DOTALL)
    from backend.core.memory import strip_url_credentials
    return strip_url_credentials(match.group(1)) if match else ""


def _git_branch(git_dir: str) -> str:
    head = hostinfo._read(os.path.join(git_dir, "HEAD")).strip()
    return head.rsplit("/", 1)[-1] if head.startswith("ref:") else head[:12]


def git_repos(roots: tuple[str, ...] = REPO_ROOTS, max_depth: int = REPO_MAX_DEPTH, limit: int = 150) -> list[Repo]:
    """Repozytoria git pod typowymi katalogami (sciezki hosta). Bez uruchamiania gita."""
    repos: list[Repo] = []
    for root in roots:
        local_root = runtime.to_local(root)
        if not os.path.isdir(local_root):
            continue
        base_depth = local_root.rstrip("/").count("/")
        for current, dirs, _files in os.walk(local_root, followlinks=False):
            if ".git" in dirs or os.path.isfile(os.path.join(current, ".git")):
                git_dir = os.path.join(current, ".git")
                if os.path.isdir(git_dir):
                    repos.append(Repo(runtime.to_host(current), _git_remote(git_dir), _git_branch(git_dir)))
                dirs[:] = []  # nie schodz w repozytorium (submoduly pomijamy)
                if len(repos) >= limit:
                    return repos
                continue
            if current.count("/") - base_depth >= max_depth:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
    return repos


# ─── Kubernetes ─────────────────────────────────────────────────────────────

def parse_kube(workloads: dict[str, Any], services: dict[str, Any], ingresses: dict[str, Any]) -> list[KubeApp]:
    """Aplikacje z `kubectl get deploy,sts,ds -A -o json`, `svc` i `ingress`."""
    apps: list[KubeApp] = []
    for item in workloads.get("items", []):
        meta, spec, status = item.get("metadata", {}), item.get("spec", {}), item.get("status", {})
        if meta.get("namespace") in ("kube-system", "kube-public", "kube-node-lease"):
            continue
        labels = ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}
        ready = status.get("readyReplicas", status.get("numberReady", 0)) or 0
        wanted = spec.get("replicas", status.get("desiredNumberScheduled", ""))
        app = KubeApp(meta.get("namespace", ""), meta.get("name", ""), item.get("kind", ""),
                      f"{ready}/{wanted}" if wanted != "" else "")
        for svc in services.get("items", []):
            smeta, sspec = svc.get("metadata", {}), svc.get("spec", {})
            selector = sspec.get("selector") or {}
            if smeta.get("namespace") == app.namespace and selector and all(labels.get(k) == v for k, v in selector.items()):
                app.services.append(smeta.get("name", ""))
        for ing in ingresses.get("items", []):
            if ing.get("metadata", {}).get("namespace") != app.namespace:
                continue
            for rule in (ing.get("spec") or {}).get("rules", []) or []:
                for path in ((rule.get("http") or {}).get("paths") or []):
                    backend = ((path.get("backend") or {}).get("service") or {}).get("name")
                    if backend in app.services and rule.get("host"):
                        app.hosts.append(rule["host"])
        app.hosts = sorted(set(app.hosts))
        apps.append(app)
    return sorted(apps, key=lambda a: (a.namespace, a.name))


async def kube_apps() -> list[KubeApp] | None:
    import shutil
    if not shutil.which("kubectl"):
        return None
    out = []
    for resource in ("deploy,sts,ds", "svc", "ingress"):
        stdout, _, code = await executor.execute(f"kubectl get {resource} -A -o json", timeout=25)
        if code != 0:
            return None
        try:
            out.append(json.loads(stdout))
        except json.JSONDecodeError:
            return None
    return parse_kube(*out)


# ─── Calosc ─────────────────────────────────────────────────────────────────

async def discover(include_kube: bool | None = None) -> Infra:
    infra = Infra()
    infra.hostname, infra.os_name = host_identity()
    infra.containers = await docker_containers()
    infra.routes = await asyncio.to_thread(proxy_routes)
    infra.listeners = await asyncio.to_thread(hostinfo.sockets)
    infra.services = await asyncio.to_thread(enabled_services)
    if include_kube or (include_kube is None and runtime.kind() == "kubernetes"):
        infra.kube = await kube_apps()
    try:
        from backend.core import targets
        infra.targets = [(t.name, t.kind) for t in targets.load_targets()]
    except Exception:
        pass
    if infra.containers is None:
        infra.notes.append("brak dostepu do Dockera (socket nie jest zamontowany?)")
    return infra


# ─── Mermaid ────────────────────────────────────────────────────────────────

def _id(prefix: str, name: str) -> str:
    return f"{prefix}_" + (re.sub(r"[^A-Za-z0-9_]", "_", name) or "x")


def label(*lines: str) -> str:
    """Etykieta wezla: linie laczone <br/>, cudzyslowy i nawiasy katowe escapowane."""
    safe = []
    for line in lines:
        if not line:
            continue
        line = (line.replace("&", "&amp;").replace('"', "#quot;").replace("<", "&lt;")
                .replace(">", "&gt;").replace("`", "'"))
        safe.append(line)
    return '"' + "<br/>".join(safe) + '"'


def _port_list(ports: list[int]) -> str:
    ports = sorted(set(ports))
    shown = ", ".join(f":{p}" for p in ports[:6])
    return shown + (f" +{len(ports) - 6}" if len(ports) > 6 else "")


def _upstream_port(upstream: str) -> tuple[str, int | None]:
    target = re.sub(r"^\w+://", "", upstream).split("/", 1)[0]
    host, _, port = target.rpartition(":")
    if not host:
        return target, None
    try:
        return host.strip("[]"), int(port)
    except ValueError:
        return target, None


MAX_CONTAINER_NODES = 40


def to_mermaid(infra: Infra, title: str = "") -> str:
    """Mapa infrastruktury jako flowchart Mermaid."""
    lines = [
        "---",
        *([f"title: {json.dumps(title.replace(chr(10), ' '), ensure_ascii=False)}"] if title else []),
        "config:",
        "  theme: base",
        "  themeVariables:",
        '    clusterBkg: "#f8fafc"',
        '    clusterBorder: "#94a3b8"',
        '    lineColor: "#475569"',
        '    edgeLabelBackground: "#ffffff"',
        "---",
        "flowchart LR",
    ]
    edges: list[str] = []
    classes: dict[str, list[str]] = {"proxy": [], "db": [], "app": [], "down": [], "ext": [], "svc": []}

    lines.append('  internet(("Internet"))')
    classes["ext"].append("internet")

    host_title = infra.hostname or "serwer"
    if infra.os_name:
        host_title += f" · {infra.os_name}"
    # Bez `direction` w subgrafie — inaczej Mermaid przypina krawedzie z zewnatrz do ramki
    lines.append(f"  subgraph host[{label(host_title)}]")

    containers = infra.containers or []
    by_host_port: dict[int, str] = {}
    by_name: dict[str, str] = {}
    proxy_nodes: dict[str, str] = {}

    collapse = len(containers) > MAX_CONTAINER_NODES
    projects: dict[str, list[Container]] = {}
    for c in containers:
        projects.setdefault(c.project, []).append(c)

    for project, members in sorted(projects.items()):
        indent = "    "
        if project:
            lines.append(f"    subgraph {_id('proj', project)}[{label('compose: ' + project)}]")
            indent = "      "
        if collapse and project:
            node = _id("proj_node", project)
            running = sum(1 for m in members if m.state == "running")
            lines.append(f"{indent}{node}[{label(project, f'{running}/{len(members)} kontenerow dziala')}]")
            classes["app"].append(node)
            for m in members:
                by_name[m.name] = node
                if m.service:
                    by_name[m.service] = node
                for p in m.ports:
                    by_host_port[p.host_port] = node
        else:
            for c in members:
                node = _id("c", c.name)
                ports = [f"{p.host_port}→{p.container_port}" for p in c.ports]
                state = c.state if c.state != "running" else ""
                if c.health and c.health != "healthy":
                    state = f"{state} {c.health}".strip()
                image = c.image.split("@", 1)[0]
                if len(image) > 40:
                    image = "…" + image[-39:]
                shape = f"([{label(c.name, image, ' '.join(ports), state)}])" if c.kind == "db" \
                    else f"[{label(c.name, image, ' '.join(ports), state)}]"
                lines.append(f"{indent}{node}{shape}")
                classes["down" if c.state != "running" or c.health == "unhealthy" else c.kind].append(node)
                by_name[c.name] = node
                if c.service:
                    by_name.setdefault(c.service, node)
                for p in c.ports:
                    by_host_port[p.host_port] = node
                if c.kind == "proxy":
                    proxy_nodes.setdefault("docker-" + c.name, node)
        if project:
            lines.append("    end")

    # Proxy na hoscie (nginx/caddy z systemd) — jeden wezel na rodzaj
    for route in infra.routes:
        if route.proxy in ("nginx", "caddy") and route.proxy not in proxy_nodes:
            node = _id("proxy", route.proxy)
            listen = [s.port for s in infra.listeners if s.port in (80, 443)]
            lines.append(f"    {node}{{{{{label(route.proxy, _port_list(listen) if listen else '')}}}}}")
            classes["proxy"].append(node)
            proxy_nodes[route.proxy] = node

    # Uslugi systemd nasluchujace na portach, ktorych nie maja kontenery
    used_ports = set(by_host_port)
    service_ports: dict[int, str] = {}
    for sock in infra.listeners:
        if sock.port in used_ports or sock.proto.startswith("udp") or sock.port in (80, 443) and proxy_nodes:
            continue
        name = WELL_KNOWN_PORTS.get(sock.port, "")
        if sock.port in (7379,):
            continue
        if not name and not sock.public:
            continue  # nieznany port tylko na localhost — szum (porty tymczasowe, narzedzia deweloperskie)
        service_ports.setdefault(sock.port, name)
    for port, name in sorted(service_ports.items())[:15]:
        node = _id("port", str(port))
        public = any(s.port == port and s.public for s in infra.listeners)
        lines.append(f"    {node}[/{label(f':{port}', name, 'publiczny' if public else 'lokalny')}/]")
        classes["svc"].append(node)
        by_host_port.setdefault(port, node)
        if public:
            edges.append(f"  internet -.-> {node}")

    if infra.services:
        shown = infra.services[:12]
        more = f"+{len(infra.services) - 12}" if len(infra.services) > 12 else ""
        lines.append(f"    systemd[{label('uslugi systemd', ', '.join(shown[:6]), ', '.join(shown[6:]), more)}]")
        classes["svc"].append("systemd")

    lines.append("  end")

    # Trasy proxy: Internet -> proxy -> upstream
    linked_public: set[str] = set()
    for route in infra.routes:
        proxy_node = proxy_nodes.get(route.proxy)
        if not proxy_node:
            continue
        domains = ", ".join(route.domains[:3]) + (" …" if len(route.domains) > 3 else "")
        if proxy_node not in linked_public:
            edges.append(f"  internet -->|{label('80/443')}| {proxy_node}")
            linked_public.add(proxy_node)
        host, port = _upstream_port(route.upstream)
        target = None
        local_names = {"127.0.0.1", "localhost", "0.0.0.0", "::1", infra.hostname}
        if port is not None and host in local_names:
            target = by_host_port.get(port)
        if target is None:
            target = by_name.get(host)
        if target is None and port is not None:
            target = by_host_port.get(port)
        if target is None:
            node = _id("up", route.upstream)
            lines.append(f"  {node}[{label(route.upstream)}]")
            classes["ext"].append(node)
            target = node
        edges.append(f"  {proxy_node} -->|{label(domains or 'domyslny')}| {target}")

    # Kontenery z domenami z etykiet (Traefik, nginx-proxy)
    traefik = next((n for key, n in proxy_nodes.items() if "traefik" in key or "proxy" in key), None)
    for c in containers:
        if c.domains and by_name.get(c.name) and traefik and by_name[c.name] != traefik:
            edges.append(f"  {traefik} -->|{label(', '.join(c.domains[:3]))}| {by_name[c.name]}")
            if traefik not in linked_public:
                edges.append(f"  internet -->|{label('80/443')}| {traefik}")
                linked_public.add(traefik)

    # Publiczne porty kontenerow, ktore nie sa za proxy
    proxied = {e.split("| ")[-1] for e in edges if "-->|" in e}
    for c in containers:
        node = by_name.get(c.name)
        if not node or node in proxied or node in linked_public:
            continue
        public = [p.host_port for p in c.ports if p.public]
        if public:
            edges.append(f"  internet -->|{label(_port_list(public))}| {node}")
            linked_public.add(node)

    # Zaleznosci aplikacja -> baza w tym samym projekcie compose
    if not collapse:
        for project, members in projects.items():
            if not project:
                continue
            dbs = [m for m in members if m.kind == "db"]
            apps = [m for m in members if m.kind == "app"]
            for app in apps[:8]:
                for db in dbs[:4]:
                    edges.append(f"  {by_name[app.name]} -.-> {by_name[db.name]}")

    # Kubernetes
    if infra.kube:
        lines.append(f"  subgraph k8s[{label('Kubernetes')}]")
        for ns in sorted({a.namespace for a in infra.kube}):
            lines.append(f"    subgraph {_id('ns', ns)}[{label('namespace: ' + ns)}]")
            for app in [a for a in infra.kube if a.namespace == ns][:25]:
                node = _id("k", f"{ns}_{app.name}")
                lines.append(f"      {node}[{label(app.name, f'{app.kind} {app.replicas}', ', '.join(app.services))}]")
                classes["app"].append(node)
                if app.hosts:
                    edges.append(f"  internet -->|{label(', '.join(app.hosts[:3]))}| {node}")
            lines.append("    end")
        lines.append("  end")

    # Zdalne cele Pipe
    if infra.targets:
        lines.append(f"  subgraph remote[{label('zdalne cele Pipe')}]")
        for name, kind in infra.targets[:20]:
            node = _id("t", name)
            lines.append(f"    {node}[{label(name, kind)}]")
            classes["ext"].append(node)
        lines.append("  end")
        lines.append(f"  pipe{{{{{label('Pipe')}}}}}")
        edges += [f"  pipe -.-> {_id('t', name)}" for name, _ in infra.targets[:20]]

    lines += list(dict.fromkeys(edges))
    lines += [
        "  classDef proxy fill:#e0f2fe,stroke:#0369a1,color:#0c4a6e",
        "  classDef db fill:#fef3c7,stroke:#b45309,color:#78350f",
        "  classDef app fill:#ecfdf5,stroke:#047857,color:#064e3b",
        "  classDef down fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d",
        "  classDef ext fill:#f3f4f6,stroke:#6b7280,color:#111827",
        "  classDef svc fill:#f5f3ff,stroke:#6d28d9,color:#3b0764",
    ]
    for cls, nodes in classes.items():
        if nodes:
            lines.append(f"  class {','.join(dict.fromkeys(nodes))} {cls}")
    return "\n".join(lines)
