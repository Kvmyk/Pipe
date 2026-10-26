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
