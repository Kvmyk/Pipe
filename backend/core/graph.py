"""
Schemat infrastruktury jako DANE (wezly i polaczenia) oraz mapowanie dzialan agenta na wezly.

`core/infra.py` rysuje gotowy obraz (Mermaid -> PNG) dla Telegrama i CLI. Interfejs webowy
rysuje i animuje schemat sam, wiec potrzebuje danych, a nie obrazka — i informacji, ktorego
elementu dotyczy to, co agent wlasnie robi.

Trzy poziomy widoku (kazdy to osobny maly graf):
  fleet            ten serwer + zdalne cele (serwery SSH, kontenery, klastry)
  host             wnetrze serwera: internet, proxy, projekty compose, kontenery, uslugi
  p:<projekt>      wnetrze projektu compose: jego kontenery, porty, katalog

Identyfikatory wezlow sa stabilne: host, internet, systemd, t:<cel>, p:<projekt>,
c:<kontener>, proxy:<nazwa>, port:<numer>, up:<upstream>, k:<namespace>/<nazwa>.
`index` mowi, gdzie dany wezel widac na kazdym poziomie (np. kontener: host -> projekt -> kontener),
zeby klient podswietlil wlasciwy element niezaleznie od tego, ktory widok jest otwarty.
"""

from __future__ import annotations

import os
import re
import shlex
import time
from typing import Any

from backend.core import runtime
from backend.core.i18n import tr
from backend.core.infra import WELL_KNOWN_PORTS, Container, Infra, _upstream_port

MAX_PROJECT_NODES = 40
_latest: Infra | None = None
_latest_at = 0.0


def remember(infra: Infra) -> None:
    """Zapamietuje ostatnio odkryta infrastrukture — z niej korzysta mapowanie dzialan na wezly."""
    global _latest, _latest_at
    _latest, _latest_at = infra, time.time()


def latest() -> Infra | None:
    return _latest


def _state(container: Container) -> str:
    if container.state != "running":
        return "down"
    if container.health == "unhealthy":
        return "warn"
    return "ok"


def _container_node(c: Container) -> dict[str, Any]:
    ports = " ".join(f"{p.host_port}→{p.container_port}" for p in c.ports[:4])
    image = c.image.split("@", 1)[0]
    status = c.state if c.state != "running" else (c.health if c.health and c.health != "healthy" else "")
    return {"id": f"c:{c.name}", "kind": c.kind, "label": c.name, "sub": image[-38:], "state": _state(c),
            "meta": {"image": image, "ports": ports, "status": status or tr("dziala", "running"),
                     "service": c.service, "project": c.project, "workdir": c.workdir,
                     "restarts": c.restart_count, "public": [p.host_port for p in c.ports if p.public]}}


def _worst(states: list[str]) -> str:
    """Stan grupy: czerwony dopiero, gdy nic nie dziala; pojedynczy zatrzymany kontener (np. zadanie init) to ostrzezenie."""
    if states and all(s == "down" for s in states):
        return "down"
    return "warn" if any(s != "ok" for s in states) else "ok"


def build(infra: Infra, alerts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Graf wszystkich poziomow dla klienta (bez sekretow: same nazwy, obrazy, porty, sciezki)."""
    remember(infra)
    containers = infra.containers or []
    hostname = infra.hostname or tr("serwer", "server")
    index: dict[str, list[str]] = {"host": ["host", "host"], "internet": ["host", "internet"],
                                   "systemd": ["host", "systemd"]}
    views: dict[str, dict[str, Any]] = {}

    # --- flota --------------------------------------------------------------
    host_state = _worst([_state(c) for c in containers])
    fleet_nodes = [{"id": "host", "kind": "server", "label": hostname, "sub": infra.os_name, "state": host_state,
                    "opens": "host", "meta": {"containers": len(containers),
                                              "running": sum(1 for c in containers if c.state == "running")}}]
    fleet_edges = []
    for name, kind in infra.targets[:30]:
        node_kind = {"ssh": "server", "docker": "app", "kubernetes": "cluster"}.get(kind, "server")
        fleet_nodes.append({"id": f"t:{name}", "kind": node_kind, "label": name, "sub": kind, "state": "ok",
                            "meta": {"target": name, "type": kind}})
        fleet_edges.append({"from": "host", "to": f"t:{name}", "kind": "manage"})
        index[f"t:{name}"] = [f"t:{name}"]
    views["fleet"] = {"title": tr("Serwery", "Servers"), "parent": "", "nodes": fleet_nodes, "edges": fleet_edges}

    # --- wnetrze serwera ----------------------------------------------------
    nodes: list[dict[str, Any]] = [{"id": "internet", "kind": "internet", "label": "Internet", "sub": "", "state": "ok"}]
    edges: list[dict[str, Any]] = []
    by_port: dict[int, str] = {}
    by_name: dict[str, str] = {}
    proxies: dict[str, str] = {}
    projects: dict[str, list[Container]] = {}
    for c in containers:
        projects.setdefault(c.project, []).append(c)

    for project, members in sorted(projects.items()):
        if project:
            pid = f"p:{project}"
            running = sum(1 for m in members if m.state == "running")
            workdir = next((m.workdir for m in members if m.workdir), "")
            kinds = {m.kind for m in members}
            nodes.append({"id": pid, "kind": "project", "label": project,
                          "sub": tr(f"{running}/{len(members)} kontenerow", f"{running}/{len(members)} containers"),
                          "state": _worst([_state(m) for m in members]), "opens": pid,
                          "meta": {"workdir": workdir, "containers": [m.name for m in members],
                                   "has_db": "db" in kinds, "has_proxy": "proxy" in kinds}})
            index[pid] = ["host", pid, pid]
            for m in members:
                index[f"c:{m.name}"] = ["host", pid, f"c:{m.name}"]
                by_name[m.name] = pid
                if m.service:
                    by_name.setdefault(m.service, pid)
                for p in m.ports:
                    by_port[p.host_port] = pid
                if m.kind == "proxy":
                    proxies.setdefault("docker-" + m.name, pid)
        else:
            for m in members:
                node = _container_node(m)
                nodes.append(node)
                index[node["id"]] = ["host", node["id"]]
                by_name[m.name] = node["id"]
                for p in m.ports:
                    by_port[p.host_port] = node["id"]
                if m.kind == "proxy":
                    proxies.setdefault("docker-" + m.name, node["id"])

    for route in infra.routes:
        if route.proxy in ("nginx", "caddy") and route.proxy not in proxies:
            pid = f"proxy:{route.proxy}"
            listen = sorted({s.port for s in infra.listeners if s.port in (80, 443)})
            nodes.append({"id": pid, "kind": "proxy", "label": route.proxy,
                          "sub": " ".join(f":{p}" for p in listen), "state": "ok",
                          "meta": {"routes": [{"domains": r.domains, "upstream": r.upstream}
                                              for r in infra.routes if r.proxy == route.proxy][:30]}})
            proxies[route.proxy] = pid
            index[pid] = ["host", pid]

    used = set(by_port)
    ports: dict[int, str] = {}
    for sock in infra.listeners:
        if sock.port in used or sock.proto.startswith("udp") or sock.port == 7379 or (sock.port in (80, 443) and proxies):
            continue
        name = WELL_KNOWN_PORTS.get(sock.port, "")
        if not name and not sock.public:
            continue
        ports.setdefault(sock.port, name)
    for port, name in sorted(ports.items())[:15]:
        public = any(s.port == port and s.public for s in infra.listeners)
        pid = f"port:{port}"
        reach = tr("publiczny", "public") if public else tr("lokalny", "local")
        nodes.append({"id": pid, "kind": "service", "label": name or tr(f"port {port}", f"port {port}"),
                      "sub": f":{port} · {reach}" if name else reach,
                      "state": "ok", "meta": {"port": port, "public": public}})
        index[pid] = ["host", pid]
        by_port.setdefault(port, pid)
        if public:
            edges.append({"from": "internet", "to": pid, "kind": "public", "label": f":{port}"})

    if infra.services:
        nodes.append({"id": "systemd", "kind": "service", "label": "systemd",
                      "sub": tr(f"{len(infra.services)} uslug", f"{len(infra.services)} services"), "state": "ok",
                      "meta": {"services": infra.services[:40]}})

    linked: set[str] = set()
    local_names = {"127.0.0.1", "localhost", "0.0.0.0", "::1", infra.hostname}
    for route in infra.routes:
        proxy = proxies.get(route.proxy)
        if not proxy:
            continue
        if proxy not in linked:
            edges.append({"from": "internet", "to": proxy, "kind": "public", "label": "80/443"})
            linked.add(proxy)
        host, port = _upstream_port(route.upstream)
        target = by_port.get(port) if port is not None and host in local_names else None
        target = target or by_name.get(host) or (by_port.get(port) if port is not None else None)
        if target is None:
            target = f"up:{route.upstream}"
            if not any(n["id"] == target for n in nodes):
                nodes.append({"id": target, "kind": "external", "label": route.upstream, "sub": "", "state": "ok"})
                index[target] = ["host", target]
        if target != proxy:
            edges.append({"from": proxy, "to": target, "kind": "route",
                          "label": ", ".join(route.domains[:2]) + (" …" if len(route.domains) > 2 else "")})

    docker_proxy = next((n for key, n in proxies.items() if key.startswith("docker-")), None)
    for c in containers:
        target = by_name.get(c.name)
        if c.domains and docker_proxy and target and target != docker_proxy:
            edges.append({"from": docker_proxy, "to": target, "kind": "route", "label": ", ".join(c.domains[:2])})
            if docker_proxy not in linked:
                edges.append({"from": "internet", "to": docker_proxy, "kind": "public", "label": "80/443"})
                linked.add(docker_proxy)

    routed = {e["to"] for e in edges if e["kind"] == "route"}
    for c in containers:
        target = by_name.get(c.name)
        public = sorted({p.host_port for p in c.ports if p.public})
        if target and public and target not in routed and target not in linked:
            edges.append({"from": "internet", "to": target, "kind": "public",
                          "label": " ".join(f":{p}" for p in public[:3])})
            linked.add(target)

    if infra.kube:
        for app in infra.kube[:25]:
            kid = f"k:{app.namespace}/{app.name}"
            nodes.append({"id": kid, "kind": "app", "label": app.name, "sub": f"{app.kind} {app.replicas}",
                          "state": "ok", "meta": {"namespace": app.namespace, "hosts": app.hosts}})
            index[kid] = ["host", kid]
            if app.hosts:
                edges.append({"from": "internet", "to": kid, "kind": "public", "label": ", ".join(app.hosts[:2])})

    views["host"] = {"title": hostname, "parent": "fleet", "nodes": nodes, "edges": _unique(edges)}

    # --- wnetrze projektow --------------------------------------------------
    for project, members in projects.items():
        if not project:
            continue
        pid = f"p:{project}"
        workdir = next((m.workdir for m in members if m.workdir), "")
        p_nodes = [_container_node(m) for m in members[:MAX_PROJECT_NODES]]
        p_edges: list[dict[str, Any]] = []
        names = {m.name: f"c:{m.name}" for m in members}
        if any(any(p.public for p in m.ports) or m.domains for m in members):
            p_nodes.insert(0, {"id": "internet", "kind": "internet", "label": "Internet", "sub": "", "state": "ok"})
        proxy_members = [m for m in members if m.kind == "proxy"]
        for m in members:
            public = sorted({p.host_port for p in m.ports if p.public})
            if m.domains and proxy_members and m not in proxy_members:
                p_edges.append({"from": names[proxy_members[0].name], "to": names[m.name], "kind": "route",
                                "label": ", ".join(m.domains[:2])})
            elif public or m.domains:
                p_edges.append({"from": "internet", "to": names[m.name], "kind": "public",
                                "label": ", ".join(m.domains[:2]) or " ".join(f":{p}" for p in public[:3])})
        apps = [m for m in members if m.kind == "app"]
        for app in apps[:8]:
            for db in [m for m in members if m.kind == "db"][:4]:
                p_edges.append({"from": names[app.name], "to": names[db.name], "kind": "uses"})
        if workdir:
            p_nodes.append({"id": f"dir:{project}", "kind": "folder", "label": os.path.basename(workdir) or workdir,
                            "sub": workdir, "state": "ok", "meta": {"path": workdir}})
        views[pid] = {"title": project, "parent": "host", "nodes": p_nodes, "edges": _unique(p_edges),
                      "workdir": workdir}

    _mark_alerts(views, index, alerts or [])
    return {"root": "fleet" if infra.targets else "host", "hostname": hostname, "views": views, "index": index,
            "notes": infra.notes, "generated": time.strftime("%Y-%m-%d %H:%M:%S")}


def _unique(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for edge in edges:
        key = (edge["from"], edge["to"])
        if key not in seen and edge["from"] != edge["to"]:
            seen.add(key)
            out.append(edge)
    return out


def _mark_alerts(views: dict[str, dict[str, Any]], index: dict[str, list[str]], alerts: list[dict[str, Any]]) -> None:
    """Aktywne alerty czuwania na wezlach: kontenery po nazwie z klucza alertu, reszta na serwerze."""
    for alert in alerts:
        key = str(alert.get("key", ""))
        name = key.split(":", 1)[1] if key.startswith("container:") else ""
        path = index.get(f"c:{name}") if name else None
        targets = set(path or ["host"]) | {"host"}
        for view in views.values():
            for node in view["nodes"]:
                if node["id"] in targets:
                    node.setdefault("alerts", []).append({"severity": alert.get("severity", "warning"),
                                                          "title": alert.get("title", "")})
                    if node["state"] == "ok":
                        node["state"] = "warn"


# ─── Dzialania agenta -> wezly ──────────────────────────────────────────────

_PROXY_PATHS = (("/etc/nginx", "proxy:nginx"), ("/etc/caddy", "proxy:caddy"))
_PROXY_UNITS = {"nginx": "proxy:nginx", "caddy": "proxy:caddy"}


def _project_for_path(path: str, infra: Infra | None) -> str:
    if not path or infra is None:
        return ""
    best, found = "", ""
    for c in infra.containers or []:
        if c.project and c.workdir and (path == c.workdir or path.startswith(c.workdir.rstrip("/") + "/")):
            if len(c.workdir) > len(best):
                best, found = c.workdir, c.project
    return f"p:{found}" if best else ""


def _node_for_path(path: str, infra: Infra | None) -> str:
    for prefix, node in _PROXY_PATHS:
        if path.startswith(prefix):
            return node
    return _project_for_path(path, infra)


def _nodes_for_command(command: str, cwd: str, infra: Infra | None) -> list[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    names = {c.name for c in (infra.containers or [])} if infra else set()
    nodes: list[str] = []
    for index, token in enumerate(tokens):
        base = os.path.basename(token)
        if token in names:
            nodes.append(f"c:{token}")
        elif base == "systemctl":
            for unit in tokens[index + 1:index + 6]:
                unit = unit.removesuffix(".service")
                if unit in _PROXY_UNITS:
                    nodes.append(_PROXY_UNITS[unit])
                elif not unit.startswith("-") and unit not in ("start", "stop", "restart", "reload", "status", "enable",
                                                               "disable", "is-active", "show", "cat"):
                    nodes.append("systemd")
        elif base in ("nginx", "caddy"):
            nodes.append(_PROXY_UNITS[base])
        elif token.startswith("/"):
            node = _node_for_path(runtime.to_host(token), infra)
            if node:
                nodes.append(node)
    if not nodes and ("docker compose" in command or "docker-compose" in command or tokens[:1] == ["git"]):
        node = _project_for_path(cwd, infra)
        if node:
            nodes.append(node)
    return list(dict.fromkeys(nodes))


def locate(tool: str, args: dict[str, Any], cwd: str = "/") -> list[str]:
    """Wezly, ktorych dotyczy wywolanie narzedzia (najlepsze przyblizenie; domyslnie caly serwer)."""
    infra = _latest
    nodes: list[str] = []
    if tool == "remote_exec":
        nodes = [f"t:{str(args.get('target', '')).strip().lower()}"]
    elif tool == "delegate":
        nodes = [f"t:{str(task.get('target', '')).strip().lower()}" for task in args.get("tasks") or []
                 if isinstance(task, dict)]
    elif tool == "docker_manage":
        target = str(args.get("target", "") or "").strip()
        names = {c.name for c in (infra.containers or [])} if infra else set()
        nodes = [f"c:{target}"] if target in names else _nodes_for_command(target, cwd, infra) if target else []
        if not nodes and str(args.get("operation", "")).startswith("compose"):
            nodes = [n for n in [_project_for_path(runtime.to_host(target) if target.startswith("/") else cwd, infra)] if n]
    elif tool in ("read_file", "write_file", "change_directory"):
        path = str(args.get("path", "") or "")
        host_path = runtime.to_host(path if path.startswith("/") else os.path.join(cwd, path))
        nodes = [n for n in [_node_for_path(host_path, infra)] if n]
    elif tool in ("execute_command", "git_command"):
        command = str(args.get("command", "") or "")
        nodes = _nodes_for_command(("git " + command) if tool == "git_command" else command, cwd, infra)
    elif tool.startswith("mcp__"):
        nodes = []
    cleaned = [n for n in nodes if n not in ("t:", "t:local")]
    return cleaned or ["host"]


_LABELS = {
    "execute_command": ("{command}", "{command}"),
    "read_file": ("czyta {path}", "reads {path}"),
    "write_file": ("zapisuje {path}", "writes {path}"),
    "change_directory": ("przechodzi do {path}", "moves to {path}"),
    "git_command": ("git {command}", "git {command}"),
    "system_stats": ("sprawdza zasoby serwera", "checks server resources"),
    "docker_manage": ("docker {operation} {target}", "docker {operation} {target}"),
    "network_info": ("sprawdza siec ({type})", "checks the network ({type})"),
    "cron_manage": ("cron: {operation}", "cron: {operation}"),
    "diagram": ("rysuje diagram", "draws a diagram"),
    "server_md": ("notatki o serwerze ({operation})", "server notes ({operation})"),
    "directory": ("mapa katalogow ({operation})", "directory map ({operation})"),
    "skill_manage": ("skill {name} ({operation})", "skill {name} ({operation})"),
    "vibe": ("styl rozmowy ({operation})", "conversation style ({operation})"),
    "target_manage": ("cel {name} ({operation})", "target {name} ({operation})"),
    "remote_exec": ("{target}: {command}", "{target}: {command}"),
    "delegate": ("wysyla workerow", "sends workers"),
    "routine_manage": ("rutyna {name} ({operation})", "routine {name} ({operation})"),
    "reminder": ("przypomnienie ({operation})", "reminder ({operation})"),
    "server_history": ("historia serwera ({operation})", "server history ({operation})"),
    "web_search": ("szukam w sieci: {query}", "web search: {query}"),
    "web_fetch": ("czytam strone {url}", "reading {url}"),
    "software_info": ("oprogramowanie: {operation} {product}{package} {version}", "software: {operation} {product}{package} {version}"),
    "journal": ("dziennik zmian ({operation})", "change journal ({operation})"),
    "security_audit": ("audyt bezpieczenstwa", "security audit"),
    "mcp_manage": ("serwery MCP ({operation})", "MCP servers ({operation})"),
}
_BLANK = re.compile(r"\s+")


def describe(tool: str, args: dict[str, Any]) -> str:
    """Krotki opis dzialania dla osi czasu w interfejsie (bez tresci plikow)."""
    if tool.startswith("mcp__"):
        return tool.replace("mcp__", "MCP ").replace("__", ": ")

    class _Fields(dict):
        def __missing__(self, key: str) -> str:
            return ""

    def short(key: str, value: Any) -> str:
        text = _BLANK.sub(" ", str(value))
        if key == "path" and len(text) > 46:                  # dluga sciezka: zostaje koniec, ktory mowi, co to za plik
            text = "…/" + "/".join(text.rstrip("/").split("/")[-2:])
        return text[:120]

    fields = _Fields({k: short(k, v) for k, v in args.items() if isinstance(v, (str, int, float))})
    polish, english = _LABELS.get(tool, (tool, tool))
    text = tr(polish, english).format_map(fields)
    return _BLANK.sub(" ", text).replace(" ()", "").strip() or tool
