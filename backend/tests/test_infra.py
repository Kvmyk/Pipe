"""
Testy odkrywania infrastruktury (docker inspect, nginx, Caddy, repozytoria,
Kubernetes) i generowania mapy Mermaid.
"""

import asyncio

import pytest

from backend.core import diagram, hostinfo, infra


def container(name, image, ports=(), project="", service="", state="running", labels=None, env=None):
    bindings = {f"{cp}/tcp": [{"HostIp": ip, "HostPort": str(hp)}] for ip, hp, cp in ports}
    lab = {"com.docker.compose.project": project, "com.docker.compose.service": service,
           "com.docker.compose.project.working_dir": f"/srv/{project}"} if project else {}
    lab.update(labels or {})
    return {"Name": f"/{name}", "Config": {"Image": image, "Labels": lab, "Env": env or []},
            "State": {"Status": state}, "NetworkSettings": {"Ports": bindings, "Networks": {"bridge": {}}}}


INSPECT = [
    container("shop-web", "shop:1", [("0.0.0.0", 8080, 3000), ("::", 8080, 3000)], "shop", "web"),
    container("shop-db", "postgres:16", [], "shop", "db"),
    container("traefik", "traefik:v3", [("0.0.0.0", 443, 443)]),
    container("api", "api:2", labels={"traefik.http.routers.api.rule": "Host(`api.example.com`)"}),
    container("legacy", "php:8", env=["VIRTUAL_HOST=old.example.com,www.old.example.com"], state="exited"),
]

NGINX = """
# komentarz { z nawiasem
upstream shop_backend {
    server 127.0.0.1:8080 weight=5;
}
server {
    listen 443 ssl;
    server_name shop.example.com www.shop.example.com;
    location / { proxy_pass http://shop_backend; }
    location /static { root /srv/static; }
}
server {
    listen 80 default_server;
    server_name _;
    return 301 https://$host$request_uri;
}
"""

CADDY = """
blog.example.com, www.blog.example.com {
    reverse_proxy localhost:2368
}
:8081 {
    reverse_proxy 127.0.0.1:9000
}
"""


class TestParsers:

    def test_inspect(self):
        found = {c.name: c for c in infra.parse_inspect(INSPECT)}
        web = found["shop-web"]
        assert [(p.host_port, p.container_port, p.public) for p in web.ports] == [(8080, 3000, True)]
        assert web.project == "shop" and web.workdir == "/srv/shop"
        assert found["shop-db"].kind == "db" and found["traefik"].kind == "proxy"
        assert found["api"].domains == ["api.example.com"]
        assert found["legacy"].domains == ["old.example.com", "www.old.example.com"]

    def test_nginx(self):
        routes = infra.parse_nginx(NGINX, "x.conf")
        assert len(routes) == 1
        assert routes[0].domains == ["shop.example.com", "www.shop.example.com"]
        assert routes[0].upstream == "127.0.0.1:8080"

    def test_caddy(self):
        routes = infra.parse_caddyfile(CADDY)
        assert routes[0].domains == ["blog.example.com", "www.blog.example.com"]
        assert routes[0].upstream == "localhost:2368"
        assert routes[1].upstream == "127.0.0.1:9000"

    def test_kube(self):
        workloads = {"items": [
            {"kind": "Deployment", "metadata": {"namespace": "shop", "name": "web"},
             "spec": {"replicas": 3, "template": {"metadata": {"labels": {"app": "web"}}}},
             "status": {"readyReplicas": 2}},
            {"kind": "Deployment", "metadata": {"namespace": "kube-system", "name": "coredns"},
             "spec": {}, "status": {}},
        ]}
        services = {"items": [{"metadata": {"namespace": "shop", "name": "web-svc"}, "spec": {"selector": {"app": "web"}}}]}
        ingresses = {"items": [{"metadata": {"namespace": "shop"}, "spec": {"rules": [
            {"host": "shop.example.com", "http": {"paths": [{"backend": {"service": {"name": "web-svc"}}}]}}]}}]}
        apps = infra.parse_kube(workloads, services, ingresses)
        assert len(apps) == 1
        assert (apps[0].replicas, apps[0].services, apps[0].hosts) == ("2/3", ["web-svc"], ["shop.example.com"])


class TestRepos:

    def test_finds_repos_without_running_git(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOST_ROOT", str(tmp_path))
        repo = tmp_path / "srv" / "shop" / ".git"
        repo.mkdir(parents=True)
        (repo / "HEAD").write_text("ref: refs/heads/main\n")
        (repo / "config").write_text('[remote "origin"]\n\turl = https://user:ghp_token123@github.com/acme/shop.git\n')
        (tmp_path / "srv" / "shop" / "node_modules" / "dep" / ".git").mkdir(parents=True)
        (tmp_path / "root" / "notes" / ".git").mkdir(parents=True)
        found = {r.path: r for r in infra.git_repos()}
        assert set(found) == {"/srv/shop", "/root/notes"}
        assert found["/srv/shop"].branch == "main"
        assert found["/srv/shop"].remote == "https://github.com/acme/shop.git"  # bez tokenu


class TestMermaid:

    def build(self):
        return infra.Infra(
            hostname="vps-1", os_name="Debian 12",
            containers=infra.parse_inspect(INSPECT),
            routes=infra.parse_nginx(NGINX),
            listeners=[hostinfo.Socket("tcp", "0.0.0.0", 22), hostinfo.Socket("tcp", "0.0.0.0", 443),
                       hostinfo.Socket("tcp", "127.0.0.1", 5432)],
            services=["fail2ban"],
        )

    def test_structure(self):
        source = infra.to_mermaid(self.build(), "Mapa")
        assert "subgraph proj_shop" in source
        assert "proxy_nginx -->|\"shop.example.com, www.shop.example.com\"| c_shop_web" in source
        assert "c_traefik -->|\"api.example.com\"| c_api" in source
        assert "c_shop_web -.-> c_shop_db" in source
        assert "class c_legacy down" in source

    def test_labels_cannot_break_syntax(self):
        evil = infra.Infra(hostname='x"]; click x "http://evil"', containers=infra.parse_inspect(
            [container('a"]-->b', "img<script>")]))
        source = infra.to_mermaid(evil)
        assert '"]; click' not in source and "<script>" not in source

    def test_renders(self):
        pytest.importorskip("mermaidx")
        rendered = asyncio.run(diagram.render(infra.to_mermaid(self.build(), "Mapa")))
        assert rendered.png.startswith(b"\x89PNG")
        width, height = diagram.png_size(rendered.png)
        assert 0 < width + height <= diagram.MAX_PNG_SIDES


class TestDiagramSource:

    def test_clean_strips_fence_and_click(self):
        assert diagram.clean_source("```mermaid\nflowchart LR\n  a-->b\n  click a \"http://x\"\n```") == "flowchart LR\n  a-->b"

    def test_empty_and_huge(self):
        with pytest.raises(diagram.DiagramError):
            diagram.clean_source("  ")
        with pytest.raises(diagram.DiagramError):
            diagram.clean_source("flowchart LR\n" + "a-->b\n" * 10_000)
