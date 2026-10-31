"""
Pipe w przegladarce pod publicznym adresem (`/web`): parowanie urzadzen (core/webaccess.py), wystawienie przez Caddy
(core/webpublic.py), most pipe web w trybie publicznym (clients/webui/server.py) i komenda serwera `web`.
"""

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

import backend.server as server
from backend import tokens
from backend.config import settings
from backend.core import feed, webaccess, webpublic
from backend.tests.test_server_commands import exchange

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "webui"))
import server as web  # noqa: E402

IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"


@pytest.fixture(autouse=True)
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "AGENT_TOKEN", "")
    from backend.core import audit
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    webaccess.reset()
    yield tmp_path
    webaccess.reset()


class TestPairing:

    def test_code_is_readable_and_typing_variants_match(self):
        pairing, secret = webaccess.start(IPHONE, "203.0.113.5")
        assert len(pairing.code) == 7 and pairing.code[3] == "-" and not set(pairing.code) & set("01OIL")
        loose = pairing.code.replace("-", "").lower()
        assert webaccess.find(loose) is pairing and webaccess.find(pairing.id) is pairing
        assert pairing.device == "iPhone · Safari" and secret

    def test_device_labels(self):
        assert webaccess.device_label(WINDOWS) == "Windows · Chrome"
        assert webaccess.device_label("") in ("nieznane urzadzenie", "unknown device")

    def test_approval_gives_the_token_once_and_only_to_the_asking_page(self):
        pairing, secret = webaccess.start(IPHONE, "203.0.113.5")
        assert webaccess.collect(pairing.id, secret)["status"] == "pending"
        webaccess.decide(pairing.code, True, role="admin", by="telegram:1")
        assert webaccess.collect(pairing.id, "obcy-sekret") == {"status": "unknown"}
        first = webaccess.collect(pairing.id, secret)
        assert first["status"] == "approved" and first["role"] == "admin"
        assert webaccess.collect(pairing.id, secret) == {"status": "collected"}
        name, role = webaccess.device(first["token"])
        assert name.startswith("web-iphone-safari-") and role == "admin"
        stored = json.loads(tokens.tokens_path().read_text())
        assert first["token"] not in json.dumps(stored)                      # tylko skrot
        assert stored[0]["approved_by"] == "telegram:1" and stored[0]["ip"] == "203.0.113.5"

    def test_viewer_role_and_denial(self):
        viewer, secret = webaccess.start(WINDOWS, "203.0.113.6")
        webaccess.decide(viewer.code, True, role="viewer")
        assert webaccess.device(webaccess.collect(viewer.id, secret)["token"])[1] == "viewer"
        denied, secret = webaccess.start(WINDOWS, "203.0.113.7")
        webaccess.decide(denied.code, False)
        assert webaccess.collect(denied.id, secret) == {"status": "denied"}
        with pytest.raises(webaccess.AccessError):
            webaccess.decide(denied.code, True)                               # decyzja raz

    def test_codes_expire_and_an_uncollected_token_is_removed(self):
        pairing, secret = webaccess.start(IPHONE, "1.2.3.4", now=1000)
        assert webaccess.collect(pairing.id, secret, now=1000 + webaccess.PAIR_TTL + 1) == {"status": "expired"}
        with pytest.raises(webaccess.AccessError):
            webaccess.decide(pairing.code, True, now=1000 + webaccess.PAIR_TTL + 2)
        late, secret = webaccess.start(IPHONE, "1.2.3.5", now=2000)
        webaccess.decide(late.code, True, now=2000)
        assert len(tokens.load()) == 1
        assert webaccess.collect(late.id, secret, now=2000 + webaccess.COLLECT_TTL + 1) == {"status": "expired"}
        assert tokens.load() == []                                            # strona nie odebrala — token znika

    def test_requests_are_limited_per_address(self):
        for _ in range(webaccess.IP_LIMIT):
            webaccess.start(IPHONE, "198.51.100.1")
        with pytest.raises(webaccess.AccessError):
            webaccess.start(IPHONE, "198.51.100.1")
        webaccess.start(IPHONE, "198.51.100.2")                               # inny adres przechodzi

    def test_idle_device_expires_and_use_extends_it(self, monkeypatch):
        pairing, secret = webaccess.start(IPHONE, "1.2.3.4", now=1_000_000)
        webaccess.decide(pairing.code, True, now=1_000_000)
        token = webaccess.collect(pairing.id, secret, now=1_000_000)["token"]
        name = tokens.load()[0]["name"]
        tokens.update(name, expires=1, seen=1)                                # dawno nieuzywane
        assert webaccess.device(token) is None and tokens.match(token) is None
        tokens.update(name, expires=2_000_000_000, seen=1)
        assert webaccess.device(token)[0] == name
        assert tokens.load()[0]["seen"] > 1                                   # uzycie przesuwa wygasniecie

    def test_revoke_by_number_name_and_all(self):
        made = []
        for agent in (IPHONE, WINDOWS, WINDOWS):
            pairing, secret = webaccess.start(agent, "1.2.3.4")
            webaccess.decide(pairing.code, True)
            made.append(webaccess.collect(pairing.id, secret)["token"])
        assert len(webaccess.devices()) == 3
        webaccess.revoke("1")
        assert webaccess.device(made[0]) is None and len(webaccess.devices()) == 2
        webaccess.revoke(webaccess.devices()[0]["name"])
        webaccess.revoke("wszystkie")
        assert webaccess.devices() == []
        with pytest.raises(webaccess.AccessError):
            webaccess.revoke("7")

    def test_other_tokens_are_not_devices(self):
        token = tokens.add("laptop-kuba", "admin")
        assert webaccess.device(token) is None and webaccess.devices() == []


class TestPublic:

    def test_parse(self):
        assert webpublic.parse("") == ("status", [])
        assert webpublic.parse("wlacz 8443 pipe.example.com") == ("on", ["8443", "pipe.example.com"])
        assert webpublic.parse("ON")[0] == "on" and webpublic.parse("wpusc K7P-4QX")[0] == "approve"
        assert webpublic.parse("cokolwiek")[0] == "help"
        assert webpublic.parse_on(["https://Pipe.Example.com/", "8443"]) == (8443, "pipe.example.com", "")
        assert webpublic.parse_on(["tunel"]) == (0, "", "tunnel")
        assert webpublic.parse_on(["bezposrednio", "9443"]) == (9443, "", "direct")
        assert webpublic.parse_on(["mikrus"]) == (0, "", "mikrus")
        assert webpublic.parse("tunel")[0] == "tunnel"
        for bad in (["99999"], ["nie domena"], ["evil.com;rm"], ["tunel", "8443"]):
            with pytest.raises(ValueError):
                webpublic.parse_on(bad)

    def test_caddy_runs_only_the_site_with_little_privilege(self):
        command = webpublic.caddy_command("1-2-3-4.sslip.io", 8443)
        joined = " ".join(command)
        assert "--network host" in joined and "--cap-drop ALL" in joined and "no-new-privileges" in joined
        assert "docker.sock" not in joined and "--privileged" not in joined
        config = next(a for a in command if a.startswith("PIPE_CADDYFILE="))
        assert "1-2-3-4.sslip.io:8443 {" in config and "admin off" in config
        assert "reverse_proxy unix//run/pipe-web/web.sock" in config and "flush_interval -1" in config
        assert "X-Pipe-Client-Ip {remote_host}" in config and "tls internal" not in config

    @pytest.mark.parametrize("wanted,busy,port", [(0, set(), 443), (0, {443}, 8443), (0, {443, 8443, 22}, None),
                                                  (8443, set(), 8443), (8443, {8443}, None), (0, {80, 443}, None),
                                                  (443, {80}, 443)])
    def test_port_choice(self, wanted, busy, port):
        if port is None:
            with pytest.raises(ValueError):
                webpublic.choose_port(wanted, busy)
        else:
            assert webpublic.choose_port(wanted, busy) == port

    def test_ufw_rules_from_files(self, tmp_path):
        ufw = tmp_path / "etc" / "ufw"
        ufw.mkdir(parents=True)
        (ufw / "ufw.conf").write_text("ENABLED=yes\n")
        (ufw / "user.rules").write_text("### tuple ### allow tcp 22 0.0.0.0/0 any 0.0.0.0/0 in\n"
                                        "### tuple ### allow tcp 80,443 0.0.0.0/0 any 0.0.0.0/0 Nginx%20Full - in\n"
                                        "### tuple ### allow any 8000:8002 0.0.0.0/0 any 0.0.0.0/0 in\n"
                                        "### tuple ### deny tcp 8443 0.0.0.0/0 any 0.0.0.0/0 in\n")
        enabled, allowed = webpublic.ufw_rules(str(tmp_path))
        assert enabled and allowed == {22, 80, 443, 8000, 8001, 8002}
        (ufw / "ufw.conf").write_text("ENABLED=no\n")
        assert webpublic.ufw_rules(str(tmp_path))[0] is False

    def test_enable_and_disable(self, monkeypatch, tmp_path):
        sock_dir = Path(tempfile.mkdtemp(prefix="pw"))
        monkeypatch.setattr(webpublic, "SOCKET_DIR", str(sock_dir))
        monkeypatch.setattr(webpublic.runtime, "kind", lambda: "native")
        monkeypatch.setattr(webpublic.runtime, "observe", lambda: False)
        monkeypatch.setattr(webpublic, "busy_ports", lambda: {22, 443})
        monkeypatch.setattr(webpublic, "ufw_rules", lambda root="": (True, {22}))
        monkeypatch.setattr(webpublic, "local_ipv4", lambda proc=None: {"203.0.113.9", "127.0.0.1"})
        monkeypatch.setattr(webpublic, "mikrus_name", lambda: "")
        calls = []

        async def fake_run(*args, timeout=120):
            calls.append(list(args))
            if args[:2] == ("docker", "logs"):
                return '{"level":"info","msg":"certificate obtained successfully"}', 0
            return "ok", 0

        async def ip():
            return "203.0.113.9"

        monkeypatch.setattr(webpublic, "_run", fake_run)
        monkeypatch.setattr(webpublic, "public_ip", ip)
        progress = []

        async def emit(text):
            progress.append(text)

        async def go():
            ok, text = await webpublic.enable(port=0, domain="", backend_socket="/nonexistent", emit=emit)
            running = webpublic.bridge_running() and (sock_dir / "web.sock").exists()
            state = webpublic.load_state()
            off = await webpublic.disable(emit)
            return ok, text, running, state, off

        ok, text, running, state, off = asyncio.run(go())
        assert ok and "https://203-0-113-9.sslip.io:8443" in text and running
        assert (state.on, state.port, state.firewall) == (True, 8443, [80, 8443])
        shells = [c[-1] for c in calls if c[:2] == ["sh", "-c"]]
        assert "ufw allow 8443/tcp comment 'pipe web'" in shells and "ufw allow 80/tcp comment 'pipe web'" in shells
        assert any(c[:3] == ["docker", "run", "-d"] for c in calls)
        assert "ufw delete allow 8443/tcp" in [c[-1] for c in calls if c[:2] == ["sh", "-c"]]
        assert not webpublic.load_state().on and not webpublic.bridge_running() and "nie jest juz" in off

    def test_tunnel_runs_without_ports_or_privileges(self):
        command = webpublic.tunnel_command("container:abc123")
        joined = " ".join(command)
        assert "--network container:abc123" in joined and "-p" not in command and "-v" not in command
        assert "docker.sock" not in joined and "--cap-drop ALL" in joined and "no-new-privileges" in joined
        # szybki tunel startuje tylko z jawnym --url (z samym --unix-socket cloudflared konczy sie bledem)
        assert command[-4:] == ["tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{webpublic.TUNNEL_PORT}"]
        assert "--unix-socket" not in command
        logs = ("INF Requesting new quick Tunnel on trycloudflare.com...\n"
                "INF |  https://brave-lion-ab12.trycloudflare.com  |\nINF later https://second-one-cd34.trycloudflare.com")
        assert webpublic.tunnel_host(logs) == "second-one-cd34.trycloudflare.com" and webpublic.tunnel_host("nic") == ""

    def test_local_addresses_from_fib_trie(self, tmp_path):
        net = tmp_path / "1" / "net"
        net.mkdir(parents=True)
        (net / "fib_trie").write_text(
            "Main:\n  +-- 0.0.0.0/0 3 0 5\n     |-- 0.0.0.0\n        /0 universe UNICAST\n"
            "     |-- 10.0.0.5\n        /32 host LOCAL\n     |-- 10.0.0.255\n        /32 link BROADCAST\n"
            "     |-- 127.0.0.1\n        /32 host LOCAL\n")
        assert webpublic.local_ipv4(str(tmp_path)) == {"10.0.0.5", "127.0.0.1"}

    def _fake_env(self, monkeypatch, *, local, logs, busy=frozenset({22}), mikrus="", check=lambda url: ""):
        sock_dir = Path(tempfile.mkdtemp(prefix="pt"))
        monkeypatch.setattr(webpublic, "SOCKET_DIR", str(sock_dir))
        monkeypatch.setattr(webpublic.runtime, "kind", lambda: "native")
        monkeypatch.setattr(webpublic.runtime, "observe", lambda: False)
        monkeypatch.setattr(webpublic, "busy_ports", lambda: set(busy))
        monkeypatch.setattr(webpublic, "ufw_rules", lambda root="": (False, set()))
        monkeypatch.setattr(webpublic, "local_ipv4", lambda proc=None: set(local))
        monkeypatch.setattr(webpublic, "DIRECT_TIMEOUT", 0.5)
        monkeypatch.setattr(webpublic, "TUNNEL_WATCH", 3600)
        calls = []

        async def fake_run(*args, timeout=120):
            calls.append(list(args))
            if args[:2] == ("docker", "logs"):
                return logs(calls), 0
            return "ok", 0

        async def ip():
            return "198.51.100.7"

        async def no_tls(*a, **k):
            return "refused"
        monkeypatch.setattr(webpublic, "_run", fake_run)
        monkeypatch.setattr(webpublic, "public_ip", ip)
        monkeypatch.setattr(webpublic, "_tls_check", lambda *a: "refused")
        monkeypatch.setattr(webpublic, "mikrus_name", lambda: mikrus)
        monkeypatch.setattr(webpublic, "tunnel_network", lambda: "container:pipe")
        monkeypatch.setattr(webpublic, "TUNNEL_PORT", 0)              # testy obok siebie — dowolny wolny port
        monkeypatch.setattr(webpublic, "SELF_CHECK_TIMEOUT", 0)
        monkeypatch.setattr(webpublic, "_fetch_page", check)
        return calls

    def _enable(self, **kwargs):
        progress = []

        async def emit(text):
            progress.append(text)

        async def go():
            result = await webpublic.enable(port=0, domain="", backend_socket="/nonexistent", emit=emit, **kwargs)
            state = webpublic.load_state()
            await webpublic.stop_bridge()
            return result, state
        (ok, text), state = asyncio.run(go())
        return ok, text, state, progress

    def test_shared_address_goes_straight_to_the_tunnel(self, monkeypatch):
        """mikr.us i podobni: publiczny IPv4 nie jest adresem serwera — porty 80/443 do niego nie dojda."""
        calls = self._fake_env(monkeypatch, local={"10.0.0.5"},
                               logs=lambda c: "INF |  https://quiet-river-1a2b.trycloudflare.com  |")
        ok, text, state, _ = self._enable()
        assert ok and "https://quiet-river-1a2b.trycloudflare.com" in text and "NAT" in text and "Cloudflare" in text
        assert (state.mode, state.host, state.port) == ("tunnel", "quiet-river-1a2b.trycloudflare.com", 443)
        assert not any(webpublic.CADDY_IMAGE in c for c in calls)              # Caddy nawet nie startowal

    def test_failed_certificate_falls_back_to_the_tunnel(self, monkeypatch):
        def logs(calls):
            if any(webpublic.TUNNEL_IMAGE in c for c in calls):
                return "INF https://late-sun-9z8y.trycloudflare.com"
            return '{"level":"error","msg":"could not get certificate from issuer","error":"timeout during connect"}'
        calls = self._fake_env(monkeypatch, local={"198.51.100.7"}, logs=logs)
        ok, text, state, progress = self._enable()
        assert ok and state.mode == "tunnel" and "late-sun-9z8y" in text and "certyfikat" in text
        assert any("Przechodze na tunel" in p for p in progress)
        runs = [c for c in calls if c[:3] == ["docker", "run", "-d"]]
        assert webpublic.CADDY_IMAGE in runs[0] and webpublic.TUNNEL_IMAGE in runs[1]

    def test_forced_direct_does_not_switch(self, monkeypatch):
        self._fake_env(monkeypatch, local={"10.0.0.5"}, logs=lambda c: '{"level":"error","msg":"challenge failed","error":"x"}')
        ok, text, state, _ = self._enable(mode="direct")
        assert not ok and state.mode == "direct" and "/web wlacz tunel" in text

    def test_new_tunnel_address_is_noticed_and_sent(self, monkeypatch):
        current = {"logs": "INF https://first-aa11.trycloudflare.com"}
        self._fake_env(monkeypatch, local=set(), logs=lambda c: current["logs"])
        published = []

        async def go():
            ok, _ = await webpublic.enable(port=0, domain="", backend_socket="/nonexistent", emit=lambda t: asyncio.sleep(0),
                                           mode="tunnel", publish=published.append)
            current["logs"] += "\nINF https://second-bb22.trycloudflare.com"
            changed = await webpublic.refresh_tunnel(published.append)
            again = await webpublic.refresh_tunnel(published.append)
            hosts = webpublic._bridge.public.hosts
            await webpublic.stop_bridge()
            return ok, changed, again, hosts
        ok, changed, again, hosts = asyncio.run(go())
        assert ok and changed and not again and "second-bb22.trycloudflare.com" in hosts
        assert webpublic.load_state().host == "second-bb22.trycloudflare.com"
        assert published[-1]["type"] == "notice" and "second-bb22" in published[-1]["text"]

    def test_mikrus_server_uses_its_own_https_subdomain(self, monkeypatch):
        checked = []
        calls = self._fake_env(monkeypatch, local=set(), logs=lambda c: "", mikrus="adam100",
                               check=lambda url: checked.append(url) or "")
        ok, text, state, _ = self._enable()
        assert ok and "https://adam100-7443.mikrus.cloud" in text and "mikr.us" in text
        assert (state.mode, state.listen, state.host) == ("mikrus", 7443, "adam100-7443.mikrus.cloud")
        assert checked == ["https://adam100-7443.mikrus.cloud/"]                     # Pipe sprawdzil link, zanim go podal
        caddy = next(c for c in calls if c[:3] == ["docker", "run", "-d"])
        config = next(a for a in caddy if a.startswith("PIPE_CADDYFILE="))
        assert "bind tcp6/[::]" in config and "http://:7443" in config and "header_up Host adam100-7443.mikrus.cloud" in config
        assert not any(webpublic.TUNNEL_IMAGE in c for c in calls)

    def test_mikrus_failure_falls_back_to_the_tunnel(self, monkeypatch):
        def check(url):
            return "HTTP 502" if "mikrus.cloud" in url else ""
        self._fake_env(monkeypatch, local=set(), mikrus="adam100", check=check,
                       logs=lambda c: "INF https://calm-bay-77aa.trycloudflare.com")
        ok, text, state, _ = self._enable()
        assert ok and state.mode == "tunnel" and "calm-bay-77aa" in text and "HTTP 502" in text

    def test_tunnel_errors_show_cloudflared_logs(self, monkeypatch):
        self._fake_env(monkeypatch, local=set(), logs=lambda c: "ERR failed to request quick Tunnel: 429 Too Many Requests")
        monkeypatch.setattr(webpublic, "TUNNEL_TIMEOUT", 0.3)
        ok, text, state, _ = self._enable(mode="tunnel")
        assert not ok and "429 Too Many Requests" in text and not state.on

    def test_tunnel_page_must_answer_before_the_link_is_given(self, monkeypatch):
        self._fake_env(monkeypatch, local=set(), logs=lambda c: "INF https://dead-end-00aa.trycloudflare.com",
                       check=lambda url: "timed out")
        ok, text, state, _ = self._enable(mode="tunnel")
        assert not ok and "dead-end-00aa" in text and "timed out" in text and not state.on

    def test_mikrus_detection(self, monkeypatch, tmp_path):
        monkeypatch.setattr(webpublic.runtime, "to_local", lambda path: str(tmp_path) + path)
        (tmp_path / "etc").mkdir()
        (tmp_path / "etc" / "hostname").write_text("Adam100\n")
        assert webpublic.mikrus_name() == ""                                   # bez /klucz_api to nie mikr.us
        (tmp_path / "klucz_api").write_text("x")
        assert webpublic.mikrus_name() == "adam100"
        (tmp_path / "etc" / "hostname").write_text("zla nazwa;rm\n")
        assert webpublic.mikrus_name() == ""

    def test_kubernetes_and_observe_are_refused(self, monkeypatch):
        monkeypatch.setattr(webpublic.runtime, "observe", lambda: False)
        monkeypatch.setattr(webpublic.runtime, "kind", lambda: "kubernetes")
        assert "Ingress" in asyncio.run(webpublic.blocker())
        monkeypatch.setattr(webpublic.runtime, "observe", lambda: True)
        assert "PIPE_OBSERVE" in asyncio.run(webpublic.blocker())

    def test_cert_failure_is_read_from_caddy_logs(self):
        logs = '{"level":"error","msg":"could not get certificate from issuer","error":"HTTP 400 urn:ietf:params:acme:error:connection"}'
        assert webpublic._log_verdict(logs) == ("fail", "HTTP 400 urn:ietf:params:acme:error:connection")
        assert webpublic._log_verdict('{"level":"info","msg":"certificate obtained successfully"}')[0] == "ok"
        assert webpublic._log_verdict('{"level":"info","msg":"serving"}')[0] == ""


class FakeAccess:
    hosts = {"pipe.example.com"}
    ip_header = "x-pipe-client-ip"
    events = "sse"

    def __init__(self):
        self.devices = {"dev-token": ("web-iphone-safari-ab12", "viewer")}
        self.logged_out = []

    def device(self, token):
        return self.devices.get(token)

    def pair_start(self, user_agent, ip):
        if ip == "198.51.100.9":
            raise webaccess.AccessError("za duzo prob")
        return {"id": "p1", "code": "K7P-4QX", "secret": "s", "device": user_agent, "ip": ip, "expires_in": 300}

    def pair_poll(self, pairing_id, secret):
        return {"status": "approved", "token": "dev-token", "role": "viewer"} if secret == "s" else {"status": "unknown"}

    def logout(self, token):
        self.logged_out.append(token)


class TestPublicBridge:
    """Most w trybie publicznym na prawdziwych gniazdach; backend to atrapa po gniezdzie Unix."""

    async def _http(self, path_sock, method, path, headers=None, body=b""):
        reader, writer = await asyncio.open_unix_connection(path_sock)
        head = {"Host": "pipe.example.com", "Content-Length": str(len(body)), **(headers or {})}
        writer.write((f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in head.items()) + "\r\n").encode() + body)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(-1), 5)
        writer.close()
        top, _, payload = raw.partition(b"\r\n\r\n")
        return int(top.split()[1]), top.decode("latin-1"), payload

    def _run(self, scenario, frames=()):
        async def go():
            base = Path(tempfile.mkdtemp(prefix="pb"))
            received = []

            async def handle(reader, writer):
                received.append(json.loads(await reader.readline()))
                for frame in frames:
                    writer.write((json.dumps(frame) + "\n").encode())
                await writer.drain()
                writer.close()

            backend = await asyncio.start_unix_server(handle, path=str(base / "b.sock"))
            access = FakeAccess()
            bridge = web.WebBridge("unix:" + str(base / "b.sock"), 0, "", lang="pl", server_label="pipe.example.com",
                                   public=access)
            front = await asyncio.start_unix_server(bridge.handle, path=str(base / "w.sock"))
            try:
                return await scenario(str(base / "w.sock"), access, received)
            finally:
                front.close(); backend.close()
        return asyncio.run(go())

    def test_guest_sees_only_the_pairing_page(self):
        async def scenario(sock, access, received):
            page = await self._http(sock, "GET", "/")
            api = await self._http(sock, "POST", "/api/request", body=b'{"command": "alerts"}')
            events = await self._http(sock, "GET", "/api/events")
            script = await self._http(sock, "GET", "/static/pair.js")
            key = await self._http(sock, "GET", "/?k=cokolwiek")
            # ekran glowny: telefon pobiera ikone i manifest bez ciasteczka urzadzenia
            icons = [await self._http(sock, "GET", f"/static/{name}")
                     for name in ("apple-touch-icon.png", "manifest.webmanifest")]
            return page, api[0], events[0], script[0], key, icons, received
        page, api, events, script, key, icons, received = self._run(scenario)
        assert page[0] == 200 and b"/static/pair.js" in page[2] and b'"public": true' in page[2]
        assert api == 401 and events == 401 and script == 200 and received == []
        assert [status for status, _, _ in icons] == [200, 200]
        assert "Content-Type: image/png" in icons[0][1] and "Content-Type: application/manifest+json" in icons[1][1]
        assert b"pair.js" in key[2]                                     # klucz z adresu nic nie znaczy publicznie

    def test_foreign_host_and_plain_http_origin_are_rejected(self):
        async def scenario(sock, access, received):
            host = await self._http(sock, "GET", "/", {"Host": "evil.example"})
            origin = await self._http(sock, "POST", "/api/pair/start", {"Origin": "http://pipe.example.com"})
            good = await self._http(sock, "POST", "/api/pair/start", {"Origin": "https://pipe.example.com",
                                                                      "X-Pipe-Client-Ip": "203.0.113.5",
                                                                      "User-Agent": "iPhone"})
            return host[0], origin[0], good
        host, origin, good = self._run(scenario)
        assert host == 403 and origin == 403
        assert good[0] == 200 and json.loads(good[2])["code"] == "K7P-4QX" and json.loads(good[2])["ip"] == "203.0.113.5"

    def test_rate_limit_and_spoofed_ip(self):
        async def scenario(sock, access, received):
            limited = await self._http(sock, "POST", "/api/pair/start", {"X-Pipe-Client-Ip": "198.51.100.9"})
            spoofed = await self._http(sock, "POST", "/api/pair/start", {"X-Pipe-Client-Ip": "<b>nie-ip</b>"})
            return limited, spoofed
        limited, spoofed = self._run(scenario)
        assert limited[0] == 429 and b"za duzo prob" in limited[2]
        assert json.loads(spoofed[2])["ip"] == "?"

    def test_poll_sets_a_secure_device_cookie_and_requests_carry_the_device_token(self):
        frames = [{"response": "ok", "status": "ok", "done": False}, {"response": "", "status": "ok", "done": True}]

        async def scenario(sock, access, received):
            wrong = await self._http(sock, "POST", "/api/pair/poll", body=b'{"id": "p1", "secret": "zly"}')
            poll = await self._http(sock, "POST", "/api/pair/poll", body=b'{"id": "p1", "secret": "s"}')
            cookie = {"Cookie": "pipe_device=dev-token"}
            page = await self._http(sock, "GET", "/", cookie)
            body = json.dumps({"message": "hej", "session": "abc", "token": "podmieniony"}).encode()
            proxied = await self._http(sock, "POST", "/api/request", {**cookie, "Origin": "https://pipe.example.com"}, body)
            logout = await self._http(sock, "POST", "/api/logout", cookie)
            return wrong, poll, page, proxied, logout, received, access
        wrong, poll, page, proxied, logout, received, access = self._run(scenario, frames)
        assert "Set-Cookie" not in wrong[1] and json.loads(wrong[2]) == {"status": "unknown"}
        assert json.loads(poll[2]) == {"status": "approved", "role": "viewer"}             # token tylko w ciasteczku
        assert "pipe_device=dev-token; HttpOnly; Secure; SameSite=Lax" in poll[1]
        assert page[0] == 200 and b"/static/app.js" in page[2] and b'"role": "viewer"' in page[2]
        assert "pipe_key" not in page[1]
        assert proxied[0] == 200
        (request,) = received
        assert request["token"] == "dev-token" and request["interface"] == "web:web-iphone-safari-ab12"
        assert request["session_id"] == "web-abc"
        assert logout[0] == 200 and access.logged_out == ["dev-token"] and "Max-Age=0" in logout[1]

    def test_poll_delivers_events_and_keeps_the_subscription(self):
        frames = [{"response": "", "status": "ok", "done": False, "event": {"type": "subscribed"}},
                  {"response": "", "status": "ok", "done": False, "event": {"type": "reminder", "text": "kawa"}}]

        async def scenario(sock, access, received):
            cookie = {"Cookie": "pipe_device=dev-token"}
            first = await self._http(sock, "GET", "/api/poll", cookie)
            guest = await self._http(sock, "GET", "/api/poll")
            return first, guest, received
        first, guest, received = self._run(scenario, frames)
        events = json.loads(first[2])["events"]
        assert first[0] == 200 and [e["type"] for e in events] == ["subscribed", "reminder"]
        assert received[0]["command"] == "subscribe" and received[0]["token"] == "dev-token"
        assert guest[0] == 401

    def test_tunnel_reads_the_cloudflare_client_address(self):
        async def scenario(sock, access, received):
            access.ip_header = "cf-connecting-ip"
            spoofed = await self._http(sock, "POST", "/api/pair/start", {"Cf-Connecting-Ip": "203.0.113.77",
                                                                         "X-Pipe-Client-Ip": "1.1.1.1"})
            return json.loads(spoofed[2])["ip"]
        assert self._run(scenario) == "203.0.113.77"

    def test_revoked_cookie_falls_back_to_the_pairing_page(self):
        async def scenario(sock, access, received):
            return await self._http(sock, "GET", "/", {"Cookie": "pipe_device=odwolany"})
        status, head, body = self._run(scenario)
        assert status == 200 and b"pair.js" in body and "pipe_device=; " in head and "Max-Age=0" in head

    def test_pair_page_has_no_inline_scripts(self):
        import re
        page = (web.STATIC_DIR / "pair.html").read_text(encoding="utf-8")
        inline = [m for m in re.findall(r"<script(?![^>]*\bsrc=)([^>]*)>", page) if "application/json" not in m]
        assert inline == [] and "__PIPE_CONFIG__" in page
        assert ".innerHTML" not in (web.STATIC_DIR / "pair.js").read_text(encoding="utf-8")


class TestCommand:

    @pytest.fixture
    def agent(self, monkeypatch):
        from backend.tests.test_server_commands import FakeAgent
        fake = FakeAgent()
        fake.note = lambda *a, **k: True
        monkeypatch.setattr(server, "get_agent", lambda: fake)
        return fake

    def test_status_and_help(self, agent):
        [[frame]] = exchange([{"command": "web"}])
        assert frame["data"]["on"] is False and "/web wlacz" in frame["data"]["text"]
        [[bad]] = exchange([{"command": "web", "args": "zrob cos"}])
        assert bad["status"] == "error" and "/web wlacz" in bad["response"]

    def test_devices_do_not_close_open_mode(self, agent):
        """Bez AGENT_TOKEN CLI i Telegram lacza sie bez tokenu — pierwsze urzadzenie pipe web ich nie odcina."""
        from backend.core import auth
        pairing, secret = webaccess.start(IPHONE, "203.0.113.5")
        webaccess.decide(pairing.code, True, role="viewer")
        token = webaccess.collect(pairing.id, secret)["token"]
        assert auth.authorize("") == ("open", "admin")
        assert auth.authorize(token)[1] == "viewer"                    # urzadzenie ma swoja role i w trybie otwartym
        tokens.add("laptop", "admin")
        assert auth.authorize("") is None                              # zwykly token klienta zamyka jak dotad

    def test_feed_buttons_let_the_device_in(self, agent):
        pairing, secret = webaccess.start(IPHONE, "203.0.113.5")
        server._publish_login({**pairing.to_event(), **webpublic.login_texts(pairing)})
        item = next(i for i in feed.recent() if i.type == "login")
        assert item.push and pairing.code in item.text and [a["label"] for a in item.actions] == ["Wpusc", "Tylko podglad", "Odrzuc"]
        assert all(a["admin"] for a in item.actions)
        [[frame]] = exchange([{"command": "feed_act", "id": item.id, "action": 1}])
        assert "tylko podglad" in frame["data"]["text"]
        assert webaccess.device(webaccess.collect(pairing.id, secret)["token"])[1] == "viewer"
        assert feed.get(item.id).done

    def test_typed_code_and_denial(self, agent):
        first, _ = webaccess.start(WINDOWS, "203.0.113.5")
        [[frame]] = exchange([{"command": "web", "args": f"wpusc {first.code.lower()}"}])
        assert "pelny dostep" in frame["data"]["text"] and first.status == "approved"
        second, _ = webaccess.start(WINDOWS, "203.0.113.6")
        [[frame]] = exchange([{"command": "web", "args": f"odrzuc {second.code}"}])
        assert second.status == "denied", frame
        [[again]] = exchange([{"command": "web", "args": f"wpusc {second.code}"}])
        assert again["status"] == "error"

    def test_viewer_cannot_change_access(self, agent, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_VIEWER_TOKEN", "oglad")
        monkeypatch.setattr(settings, "AGENT_TOKEN", "admin-token")
        pairing, _ = webaccess.start(WINDOWS, "203.0.113.5")
        replies = exchange([{"command": "web", "args": f"wpusc {pairing.code}", "token": "oglad"},
                            {"command": "web", "token": "oglad"}])
        assert replies[0][0]["status"] == "error" and pairing.status == "pending"
        assert replies[1][0]["data"]["devices"] == [] and replies[1][0]["data"]["can_edit"] is False


class TestWebContainerRefresh:
    """Kontener /web (pipe-web) nie ma compose, ale Pipe zna jego opcje — /zalataj i /aktualizuj odswiezaja go same."""

    def test_patch_takes_the_web_container_instead_of_a_manual_hint(self):
        from backend.core import patching, vulns
        from backend.tests.test_vulns import image_target
        target = image_target(name="caddy:2.11-alpine", containers=("pipe-web",))
        target.meta["compose"] = []
        scan = vulns.Scan(targets=[target])
        patch = vulns.image_patch(scan)
        assert patch.web == [target] and patch.manual == [] and not patch.projects
        job = patching.plan(scan, "images")
        assert [p.kind for p in job.parts] == ["images"]                    # nie "nie ma nic do zainstalowania"
        assert "kontener bez compose" not in vulns.images_alert(scan, {})

    def test_refresh_recreates_only_on_a_newer_image(self, monkeypatch):
        webpublic.save_state(webpublic.State(on=True, host="adam100-7443.mikrus.cloud", port=443, mode="mikrus", listen=7443))
        ids = {"running": "sha256:old", "latest": "sha256:new"}
        calls = []

        async def fake_run(*args, timeout=120):
            calls.append(list(args))
            if args[:3] == ("docker", "inspect", "--format"):
                return ids["running"], 0
            if args[:3] == ("docker", "image", "inspect"):
                return (ids["latest"] if args[4] == "{{.Id}}" else "<no value>"), 0
            return "ok", 0
        monkeypatch.setattr(webpublic, "_run", fake_run)
        changed, text = asyncio.run(webpublic.refresh_proxy())
        run = next(c for c in calls if c[:3] == ["docker", "run", "-d"])
        assert changed and webpublic.CADDY_IMAGE in run and any("bind tcp6/[::]" in a for a in run)
        calls.clear()
        ids["running"] = "sha256:new"
        changed, _ = asyncio.run(webpublic.refresh_proxy())
        assert not changed and not any(c[:3] == ["docker", "run", "-d"] for c in calls)

    def test_held_web_image_is_still_tried(self):
        """Tag caddy:2.11-alpine plywa: po wstrzymaniu moze juz miec poprawke, a pull nic nie kosztuje."""
        from backend.core import patching, vulns
        from backend.tests.test_vulns import image_target
        target = image_target(name="caddy:2.11-alpine", containers=("pipe-web",))
        target.meta["compose"] = []
        vulns.update_state(lambda s: s.setdefault("stale_images", {}).update(
            {target.name: {"ids": vulns.target_fingerprint(target), "at": time.time()}}))
        scan = vulns.Scan(targets=[target])
        assert vulns.image_patch(scan).web == [target] and vulns.image_patch(scan).waiting == []
        assert [p.kind for p in patching.plan(scan, "images").parts] == ["images"]

    def test_refresh_reports_a_failed_pull_and_keeps_the_container(self, monkeypatch):
        webpublic.save_state(webpublic.State(on=True, host="a.example.com", port=443))
        calls = []

        async def fake_run(*args, timeout=120):
            calls.append(list(args))
            return "denied", 1
        monkeypatch.setattr(webpublic, "_run", fake_run)
        changed, text = asyncio.run(webpublic.refresh_proxy())
        assert changed is None and "denied" in text and calls == [["docker", "pull", "-q", webpublic.CADDY_IMAGE]]

    def test_container_on_a_patched_image_survives_the_pull(self, monkeypatch):
        """pull przywrocil pod tagiem obraz bazowy — kontener z warstwa poprawek Pipe nie wraca na niego."""
        from backend.core import vulns
        assert webpublic.PATCH_LABEL == vulns.PATCH_LABEL
        webpublic.save_state(webpublic.State(on=True, host="a.example.com", port=443))

        async def fake_run(*args, timeout=120):
            if args[:3] == ("docker", "inspect", "--format"):
                return "sha256:layered", 0
            if args[:3] == ("docker", "image", "inspect"):
                return "sha256:base", 0
            return "ok", 0
        monkeypatch.setattr(webpublic, "_run", fake_run)
        assert asyncio.run(webpublic.refresh_proxy())[0] is False

    def test_web_image_without_a_newer_version_gets_the_fix_layer(self, monkeypatch):
        from backend.core import patching, vulns
        from backend.tests.test_vulns import image_target
        target = image_target(name="caddy:2.11-alpine", containers=("pipe-web",))
        target.meta["compose"] = []
        patch = vulns.image_patch(vulns.Scan(targets=[target]))
        part = patching.Part("images", "images", [target], image_patch=patch)
        pulls = []

        async def refresh(*, pull=True):
            pulls.append(pull)
            return (False, "juz najnowszy") if pull else (True, "odtworzony")

        async def layer(self, tgt, result):
            result.layered.append(tgt.name)
            return ""
        monkeypatch.setattr(webpublic, "refresh_proxy", refresh)
        monkeypatch.setattr(patching.Run, "_refresh_or_layer", layer)
        result = patching.PartResult(part)
        asyncio.run(patching.Run(patching.Job("images", [part], None), "cli:t")._web_container([result]))
        assert pulls == [True, False] and result.layered == [target.name] and result.refreshed == []

    def test_web_container_is_refreshed_last(self, monkeypatch):
        """Latanie z przegladarki idzie przez pipe-web — jego odtworzenie zrywa strone, wiec dopiero po wszystkim."""
        from backend.core import patching, vulns
        from backend.tests.test_vulns import image_target
        target = image_target(name="caddy:2.11-alpine", containers=("pipe-web",))
        target.meta["compose"] = []
        patch = vulns.image_patch(vulns.Scan(targets=[target]))
        job = patching.Job("all", [patching.Part("images", "images", [target], image_patch=patch),
                                   patching.Part("server", "web2", [])], None)
        order = []

        async def part(kind):
            async def runner(p):
                order.append(kind)
                return patching.PartResult(p)
            return runner

        async def refresh():
            order.append("pipe-web")
            return True, "odtworzony"

        async def go():
            run = patching.Run(job, "web:telefon")
            run._images, run._server = await part("images"), await part("server")
            monkeypatch.setattr(webpublic, "refresh_proxy", refresh)

            async def no_cleanup():
                return 0
            run._cleanup = no_cleanup
            await run._execute(None)
            return run
        run = asyncio.run(go())
        assert order == ["images", "server", "pipe-web"]
        assert any("na koniec odswiezam kontener strony /web" in getattr(e, "text", "") for e in run.events)
