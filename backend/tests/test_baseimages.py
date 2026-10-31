"""
Podbijanie `FROM` w Dockerfile przy /zalataj obrazy (core/baseimages.py + patching.Run._images): ktore linie,
jaka linia wersji, tagi z Docker Hub, zapis tylko tagu, kopia w dzienniku i przebudowa po podbiciu.
"""

import asyncio
import json

import pytest

from backend.core import baseimages, journal, patching, vulns, websearch
from backend.tests.test_patching import image_run
from backend.tests.test_vulns import FakeShell, image_target, write


@pytest.fixture(autouse=True)
def host(tmp_path, monkeypatch):
    root = tmp_path / "host"
    root.mkdir()
    monkeypatch.setenv("HOST_ROOT", str(root))
    monkeypatch.setenv("HOST_PROC", str(tmp_path / "proc"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return root


def hub(*pages):
    """Podrobione API tagow Docker Hub: kolejne strony z polem `next`."""
    calls = []

    def fetch(url):
        calls.append(url)
        index = len(calls) - 1
        names = pages[index] if index < len(pages) else []
        nxt = "https://hub.docker.com/v2/next" if index + 1 < len(pages) else None
        return {"results": [{"name": n} for n in names], "next": nxt}
    fetch.calls = calls
    return fetch


DOCKERFILE = """# syntax=docker/dockerfile:1
ARG NODE=20
FROM --platform=$BUILDPLATFORM node:20.11-slim AS build
RUN npm ci
FROM build AS test
FROM node:${NODE}
FROM python:3.12
FROM nginx@sha256:abc
FROM docker.io/library/nginx:1.25.3-alpine
COPY --from=build /app /usr/share/nginx/html
"""


class TestLines:

    def test_only_versioned_hub_images_are_considered(self):
        assert baseimages.from_lines(DOCKERFILE) == [(2, "node", "20.11-slim"), (6, "python", "3.12"),
                                                     (8, "library/nginx", "1.25.3-alpine")]

    @pytest.mark.parametrize("tag, matches, rejects", [
        ("20.11-slim", ["20.18-slim", "20.9-slim"], ["22.1-slim", "20.18.0-slim", "20.18-alpine"]),
        ("3.11.4-slim-bookworm", ["3.11.10-slim-bookworm"], ["3.12.1-slim-bookworm", "3.11.10-slim"]),
        ("15.4", ["15.8"], ["16.1", "15.8-alpine"]),
    ])
    def test_same_line_only_the_last_number_moves(self, tag, matches, rejects):
        _, pattern = baseimages.line_of(tag)
        assert all(pattern.match(t) for t in matches) and not any(pattern.match(t) for t in rejects)

    def test_floating_tags_are_left_to_build_pull(self):
        assert baseimages.line_of("20") is None and baseimages.line_of("latest") is None
        assert baseimages.line_of("bookworm-slim") is None

    def test_newest_tag_across_pages(self):
        fetch = hub(["20.11-slim", "20.12-slim", "22.1-slim"], ["20.18-slim", "20.18.0-slim"])
        assert baseimages.newer_tag("node", "20.11-slim", fetch) == "20.18-slim"
        assert "library%2Fnode" not in fetch.calls[0] and "library/node" in fetch.calls[0] and "name=20." in fetch.calls[0]
        assert baseimages.newer_tag("node", "20.18-slim", hub(["20.18-slim", "20.11-slim"])) == ""

    def test_apply_changes_only_the_tag(self):
        bumps = [baseimages.Bump("/srv/Dockerfile", 2, "node", "20.11-slim", "20.18-slim"),
                 baseimages.Bump("/srv/Dockerfile", 8, "library/nginx", "1.25.3-alpine", "1.25.5-alpine")]
        new = baseimages.apply(DOCKERFILE, bumps)
        assert "FROM --platform=$BUILDPLATFORM node:20.18-slim AS build" in new
        assert "FROM docker.io/library/nginx:1.25.5-alpine" in new
        assert new.replace("20.18-slim", "20.11-slim").replace("1.25.5", "1.25.3") == DOCKERFILE

    def test_dockerfiles_from_compose_config(self):
        config = {"services": {"web": {"build": {"context": "/srv/shop", "dockerfile": "docker/Dockerfile.prod"}},
                               "api": {"build": {"context": "/srv/shop/api"}}, "db": {"image": "postgres:16"},
                               "evil": {"build": {"context": "/srv/shop", "dockerfile": "../../etc/x"}}}}
        assert baseimages.dockerfiles(config, ["web", "api", "db", "evil"]) == [
            "/srv/shop/docker/Dockerfile.prod", "/srv/shop/api/Dockerfile"]


class TestComposeTag:

    @pytest.mark.parametrize("image, tag, tags, new", [
        ("redis", "7.2.3", ["7.2.5", "7.4.0"], "7.2.5"),
        ("mysql", "8.0", ["8.0.45", "8.4.1"], "8.0.45"),                  # nigdy nowa linia bazy danych
        ("caddy", "2.11-alpine", ["2.11.7-alpine", "2.11.7", "2.12.0-alpine"], "2.11.7-alpine"),
        ("postgres", "15.4", ["15.8", "16.1"], "15.8"),                   # nie ma 15.4.x: rusza sie ostatnia liczba
        ("redis", "7", ["7.2.5"], ""),                                    # tag plywa
    ])
    def test_safe_tag_stays_in_the_release_line(self, image, tag, tags, new):
        assert baseimages.safe_tag(image, tag, lambda url: {"results": [{"name": n} for n in tags]}) == new

    def test_only_a_literal_image_line_changes(self):
        text = 'services:\n  a:\n    image: "redis:7.2.3"  # cache\n  b:\n    image: ${REDIS:-redis:7.2.3}\n'
        assert baseimages.compose_has(text, "redis:7.2.3") and not baseimages.compose_has(text, "redis:7.2")
        assert baseimages.compose_apply(text, "redis:7.2.3", "redis:7.2.5") == text.replace(
            '"redis:7.2.3"', '"redis:7.2.5"')

    def test_only_docker_hub_images(self):
        assert baseimages.parse_ref("docker.io/library/redis:7.2.3") == ("library/redis", "7.2.3")
        assert baseimages.parse_ref("bitnami/redis:7.2.3") == ("bitnami/redis", "7.2.3")
        assert baseimages.parse_ref("ghcr.io/org:1.2") is None and baseimages.parse_ref("ghcr.io/org/app:1.2") is None

    def test_tag_is_bumped_when_pull_changes_nothing(self, host, monkeypatch):
        write(host, "/srv/shop/compose.yml", "services:\n  web:\n    image: redis:7.2.3\n")
        monkeypatch.setattr(websearch, "get_json", lambda url, timeout=0: {"results": [{"name": "7.2.5"}]})
        target = image_target(name="redis:7.2.3")
        shell = FakeShell([("{{.Id}}", (target.meta["image_id"], "", 0))])
        result = image_run(monkeypatch, shell, target)
        assert "image: redis:7.2.5" in (host / "srv/shop/compose.yml").read_text()
        assert result.tagged == ["redis:7.2.3 → redis:7.2.5"] and result.refreshed == ["redis:7.2.3"]
        assert not shell.having("docker run --name") and len(shell.having("pull --ignore-buildable")) == 2
        assert [f.label for f in journal.load(result.entry_id).files] == ["/srv/shop/compose.yml"]

    def test_newer_image_under_the_tag_leaves_compose_alone(self, host, monkeypatch):
        write(host, "/srv/shop/compose.yml", "services:\n  web:\n    image: redis:7.2.3\n")
        monkeypatch.setattr(websearch, "get_json", lambda url, timeout=0: {"results": [{"name": "7.2.5"}]})
        shell = FakeShell([("{{.Id}}", ("sha256:" + "f" * 64, "", 0)), ("{{json .Config.Labels}}", ("null", "", 0))])
        result = image_run(monkeypatch, shell, image_target(name="redis:7.2.3"))
        assert result.tagged == [] and "image: redis:7.2.3" in (host / "srv/shop/compose.yml").read_text()

    def test_failed_pull_of_the_new_tag_restores_the_file(self, host, monkeypatch):
        write(host, "/srv/shop/compose.yml", "services:\n  web:\n    image: redis:7.2.3\n")
        monkeypatch.setattr(websearch, "get_json", lambda url, timeout=0: {"results": [{"name": "7.2.5"}]})
        target = image_target(name="redis:7.2.3", image_id="sha256:" + "e" * 64)
        pulls = []

        class Shell(FakeShell):
            async def __call__(self, cmd, cwd=None, timeout=None):
                if "pull --ignore-buildable" in cmd:
                    pulls.append(cmd)
                    if len(pulls) == 2:
                        self.ran.append(cmd)
                        return "", "manifest unknown", 1
                return await super().__call__(cmd, cwd, timeout)
        shell = Shell([("{{.Id}}", (target.meta["image_id"], "", 0)), ("{{json .Config.Labels}}", ("null", "", 0)),
                       ("{{json .Config}}", (json.dumps({"User": "", "Cmd": ["x"]}), "", 0))])
        result = image_run(monkeypatch, shell, target)
        assert "image: redis:7.2.3" in (host / "srv/shop/compose.yml").read_text() and result.tagged == []
        assert shell.having("docker run --name")                          # zostaje warstwa z poprawkami


class TestPatch:

    def config(self):
        return json.dumps({"services": {"web": {"build": {"context": "/srv/shop"}}}})

    def test_from_is_bumped_backed_up_then_rebuilt(self, host, monkeypatch):
        write(host, "/srv/shop/Dockerfile", "FROM node:20.11-slim\nRUN npm ci\n")
        monkeypatch.setattr(websearch, "get_json", lambda url, timeout=0: {"results": [{"name": "20.18-slim"}]})
        shell = FakeShell([("config --format json", (self.config(), "", 0))])
        result = image_run(monkeypatch, shell, image_target())
        assert (host / "srv/shop/Dockerfile").read_text() == "FROM node:20.18-slim\nRUN npm ci\n"
        assert result.based == ["node:20.11-slim → node:20.18-slim"] and result.exit_code == 0
        [build] = shell.having("build --pull")
        assert shell.ran.index(build) > shell.ran.index(shell.having("config --format json")[0])
        entry = journal.load(result.entry_id)
        assert [f.label for f in entry.files] == ["/srv/shop/Dockerfile"]
        assert "obraz bazowy podbity w Dockerfile: node:20.11-slim → node:20.18-slim" in "\n".join(patching._fixed_lines(result))

    def test_nothing_newer_leaves_the_dockerfile_alone(self, host, monkeypatch):
        write(host, "/srv/shop/Dockerfile", "FROM node:20.18-slim\n")
        monkeypatch.setattr(websearch, "get_json", lambda url, timeout=0: {"results": [{"name": "20.18-slim"}]})
        result = image_run(monkeypatch, FakeShell([("config --format json", (self.config(), "", 0))]), image_target())
        assert result.based == [] and (host / "srv/shop/Dockerfile").read_text() == "FROM node:20.18-slim\n"
        assert not journal.load(result.entry_id).files

    def test_images_from_a_registry_are_not_touched(self, host, monkeypatch):
        calls = []
        monkeypatch.setattr(websearch, "get_json", lambda url, timeout=0: calls.append(url) or {})
        target = image_target(name="redis:7.2.3")                    # obraz z rejestru, nie budowany przez compose
        result = image_run(monkeypatch, FakeShell(), target)
        assert result.based == [] and calls == []

    def test_read_only_host_writes_through_the_helper(self, host, monkeypatch, tmp_path):
        import errno

        from backend.core import attachments

        write(host, "/srv/shop/Dockerfile", "FROM node:20.11-slim\n")
        monkeypatch.setattr(websearch, "get_json", lambda url, timeout=0: {"results": [{"name": "20.18-slim"}]})
        real_open = open

        def read_only(path, mode="r", *args, **kwargs):
            if str(path).endswith("srv/shop/Dockerfile") and "w" in mode:
                raise OSError(errno.EROFS, "Read-only file system")
            return real_open(path, mode, *args, **kwargs)
        monkeypatch.setattr("builtins.open", read_only)
        monkeypatch.setattr(attachments, "host_path", lambda p: "/opt/pipe/data/tmp/" + p.name)
        shell = FakeShell([("config --format json", (self.config(), "", 0))])
        init = patching.Run.__init__

        def with_helper(self, *args, **kwargs):            # tryb docker: kontener pomocniczy jest
            init(self, *args, **kwargs)
            self._image = "sha256:" + "a" * 64
        monkeypatch.setattr(patching.Run, "__init__", with_helper)
        monkeypatch.setattr(patching.Run, "host", lambda self, c: f"HOST[{c}]")
        result = image_run(monkeypatch, shell, image_target())
        [write_cmd] = shell.having("> /srv/shop/Dockerfile")
        assert write_cmd.startswith("HOST[cat /opt/pipe/data/tmp/pipe-write-") and result.based
