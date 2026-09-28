"""
Odczyty stanu hosta prosto z /proc i systemu plikow — bez uruchamiania komend.

Uzywaja ich system_stats, czuwanie (core/watch.py) i mapa infrastruktury
(core/infra.py). Kazda funkcja przyjmuje opcjonalny katalog `proc`
(domyslnie runtime.host_proc()), zeby testy mogly podac wlasne pliki.
Zadna nie rzuca wyjatku przy braku pliku — zwraca pusty wynik.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass

from backend.core import runtime

# Systemy plikow, ktore opisuja prawdziwe dyski (bez tmpfs, overlay, proc...).
REAL_FILESYSTEMS = frozenset({
    "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs", "vfat", "exfat", "ntfs", "ntfs3",
    "nfs", "nfs4", "cifs", "fuse.sshfs", "fuseblk", "bcachefs",
})


def _proc(proc: str | None) -> str:
    return proc or runtime.host_proc()


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


# ─── Pamiec, CPU, uptime ────────────────────────────────────────────────────

def meminfo(proc: str | None = None) -> dict[str, int]:
    """/proc/meminfo jako {klucz: kB}."""
    result: dict[str, int] = {}
    for line in _read(f"{_proc(proc)}/meminfo").splitlines():
        key, _, value = line.partition(":")
        number = value.strip().split(" ")[0]
        if number.isdigit():
            result[key.strip()] = int(number)
    return result


@dataclass(frozen=True)
class Memory:
    total_mb: int
    available_mb: int
    swap_total_mb: int
    swap_used_mb: int

    @property
    def used_mb(self) -> int:
        return self.total_mb - self.available_mb

    @property
    def used_pct(self) -> int:
        return round(self.used_mb * 100 / self.total_mb) if self.total_mb else 0


def memory(proc: str | None = None) -> Memory | None:
    info = meminfo(proc)
    if "MemTotal" not in info:
        return None
    available = info.get("MemAvailable", info.get("MemFree", 0))
    swap_total = info.get("SwapTotal", 0)
    return Memory(info["MemTotal"] // 1024, available // 1024, swap_total // 1024,
                  (swap_total - info.get("SwapFree", swap_total)) // 1024)


def loadavg(proc: str | None = None) -> tuple[float, float, float] | None:
    parts = _read(f"{_proc(proc)}/loadavg").split()
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except (IndexError, ValueError):
        return None


def cpu_count(proc: str | None = None) -> int:
    count = sum(1 for line in _read(f"{_proc(proc)}/cpuinfo").splitlines() if line.startswith("processor"))
    return count or os.cpu_count() or 1


def uptime_seconds(proc: str | None = None) -> float | None:
    try:
        return float(_read(f"{_proc(proc)}/uptime").split()[0])
    except (IndexError, ValueError):
        return None


def format_duration(seconds: float) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} dni, {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def human_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit not in ("B", "KB") else f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


# ─── Dyski ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Disk:
    mount: str       # punkt montowania na hoscie
    device: str
    fstype: str
    total: int       # bajty
    used: int
    available: int

    @property
    def used_pct(self) -> int:
        # jak df: used / (used + available), bo rezerwa roota nie jest dostepna
        denominator = self.used + self.available
        return round(self.used * 100 / denominator) if denominator else 0


def mounts(proc: str | None = None) -> list[tuple[str, str, str]]:
    """(urzadzenie, punkt montowania hosta, typ) z /proc/1/mounts (namespace hosta)."""
    text = _read(f"{_proc(proc)}/1/mounts") or _read(f"{_proc(proc)}/mounts")
    result = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 3:
            mount = fields[1].replace("\\040", " ")
            result.append((fields[0], mount, fields[2]))
    return result


def disks(proc: str | None = None) -> list[Disk]:
    """Zajetosc prawdziwych dyskow hosta (jeden wpis na urzadzenie)."""
    seen: set[str] = set()
    result: list[Disk] = []
    candidates = [m for m in mounts(proc) if m[2] in REAL_FILESYSTEMS]
    # krotsze punkty montowania najpierw — dla urzadzenia zostaje `/`, a nie bind-mount
    for device, mount, fstype in sorted(candidates, key=lambda m: len(m[1])):
        if device in seen:
            continue
        try:
            stat = os.statvfs(runtime.to_local(mount))
        except OSError:
            continue
        seen.add(device)
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bfree * stat.f_frsize
        available = stat.f_bavail * stat.f_frsize
        if total:
            result.append(Disk(mount, device, fstype, total, total - free, available))
    if not result:
        # Brak /proc/1/mounts (np. inny runtime) — przynajmniej korzen
        try:
            stat = os.statvfs(runtime.to_local("/"))
            total = stat.f_blocks * stat.f_frsize
            result.append(Disk("/", "?", "?", total, total - stat.f_bfree * stat.f_frsize,
                               stat.f_bavail * stat.f_frsize))
        except OSError:
            pass
    return result


# ─── Siec ───────────────────────────────────────────────────────────────────

_TCP_LISTEN = "0A"
_TCP_ESTABLISHED = "01"


def decode_addr(hex_addr: str) -> tuple[str, int]:
    """Dekoduje `ADRES:PORT` z /proc/net/{tcp,udp}[6] (adres w kolejnosci hosta)."""
    addr, port = hex_addr.split(":")
    raw = bytes.fromhex(addr)
    if len(raw) == 4:
        ip = ".".join(str(b) for b in reversed(raw))
    else:
        # IPv6: cztery 32-bitowe slowa, kazde w kolejnosci little-endian
        words = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
        ip = f"[{ipaddress.IPv6Address(words).compressed}]"
    return ip, int(port, 16)


@dataclass(frozen=True)
class Socket:
    proto: str
    ip: str
    port: int
    remote: str = ""

    @property
    def public(self) -> bool:
        """Nasluchuje na wszystkich interfejsach albo na adresie publicznym."""
        bare = self.ip.strip("[]")
        if bare in ("0.0.0.0", "::"):
            return True
        try:
            address = ipaddress.ip_address(bare)
        except ValueError:
            return False
        return not (address.is_loopback or address.is_private or address.is_link_local)

    def __str__(self) -> str:
        entry = f"{self.proto:<5} {self.ip}:{self.port}"
        return f"{entry} -> {self.remote}" if self.remote else entry


def parse_sockets(text: str, proto: str, established: bool = False) -> list[Socket]:
    """
    Gniazda z pliku /proc/<pid>/net/<proto>. Domyslnie nasluchujace
    (TCP LISTEN, kazde UDP), z `established=True` zestawione polaczenia TCP.
    """
    out = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4:
            continue
        local, remote, state = fields[1], fields[2], fields[3]
        if proto.startswith("tcp"):
            if state != (_TCP_ESTABLISHED if established else _TCP_LISTEN):
                continue
        elif established:
            continue
        ip, port = decode_addr(local)
        remote_text = ""
        if established:
            rip, rport = decode_addr(remote)
            remote_text = f"{rip}:{rport}"
        out.append(Socket(proto, ip, port, remote_text))
    return out


def sockets(proc: str | None = None, established: bool = False) -> list[Socket]:
    """
    Gniazda hosta. Kontener ma wlasny namespace sieciowy, wiec czytamy
    /proc/1/net — przy `pid: host` PID 1 to init hosta.
    """
    result: list[Socket] = []
    for proto in ("tcp", "tcp6", "udp", "udp6"):
        if established and proto.startswith("udp"):
            continue
        text = _read(f"{_proc(proc)}/1/net/{proto}")
        if text:
            result += parse_sockets(text, proto, established)
    unique = {str(s): s for s in result}
    return [unique[k] for k in sorted(unique)]
