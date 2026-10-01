"""
Zgody — czlowiek w petli dla zewnetrznych agentow (MCP).

Agent podlaczony przez MCP nie moze nacisnac TAK. Gdy jego komenda wymaga
potwierdzenia, Pipe tworzy zgode: pokazuje ja administratorom (Telegram:
przyciski *Zatwierdz* / *Odrzuc*, CLI: /zgody) razem z planem bezpiecznika,
a agent sprawdza wynik narzedziem get_approval. Po zatwierdzeniu operacja
idzie przez bezpiecznik (kopia, sprawdzenia, dziennik) — tak samo jak TAK w
rozmowie z Pipe.

Zgody zyja w pamieci procesu (restart = przepadaja) i wygasaja po TTL.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from backend.core.i18n import tr

TTL_SECONDS = 1800
MAX_PENDING = 50


@dataclass
class Approval:
    id: str
    command: str              # dokladnie to, co zostanie uruchomione
    inner: str                # co ocenil klasyfikator (dla celu zdalnego — komenda docelowa)
    target: str
    requested_by: str         # tozsamosc tokenu agenta
    reason: str
    plan_text: str = ""
    created: float = field(default_factory=time.time)
    status: str = "pending"   # pending | approved | denied | expired | done | failed
    decided_by: str = ""
    result: str = ""
    plan: Any = None

    @property
    def expired(self) -> bool:
        return self.status == "pending" and time.time() - self.created > TTL_SECONDS

    def to_event(self) -> dict[str, Any]:
        return {"type": "approval", "id": self.id, "command": self.command, "target": self.target,
                "requested_by": self.requested_by, "reason": self.reason, "plan": self.plan_text,
                "status": self.status}

    def describe(self) -> str:
        head = f"Zgoda {self.id} [{self.status}] — {self.target}: {self.command}"
        if self.status in ("done", "failed", "denied", "expired") and self.result:
            return f"{head}\n{self.result}"
        return head


class ApprovalError(ValueError):
    pass


class Approvals:
    def __init__(self) -> None:
        self._items: dict[str, Approval] = {}

    def _expire(self) -> None:
        for item in self._items.values():
            if item.expired:
                item.status = "expired"
                item.result = tr("Nikt nie zatwierdzil operacji w ciagu 30 minut.",
                                 "Nobody approved the operation within 30 minutes.")
        if len(self._items) > MAX_PENDING * 4:
            for key in sorted(self._items, key=lambda k: self._items[k].created)[:len(self._items) - MAX_PENDING * 2]:
                del self._items[key]

    def create(self, *, command: str, inner: str, target: str, requested_by: str, reason: str = "",
               plan: Any = None) -> Approval:
        self._expire()
        if sum(1 for a in self._items.values() if a.status == "pending") >= MAX_PENDING:
            raise ApprovalError(tr("Za duzo oczekujacych zgod — poczekaj na decyzje administratora.",
                                   "Too many pending approvals — wait for the administrator to decide."))
        approval = Approval(secrets.token_hex(4), command, inner, target, requested_by, reason[:500],
                            plan.describe() if plan is not None else "", plan=plan)
        self._items[approval.id] = approval
        return approval

    def get(self, approval_id: str) -> Approval | None:
        self._expire()
        return self._items.get((approval_id or "").strip().lower())

    def pending(self) -> list[Approval]:
        self._expire()
        return sorted((a for a in self._items.values() if a.status == "pending"), key=lambda a: a.created)

    async def decide(self, approval_id: str, approve: bool, decided_by: str, *, cwd: str | None = None,
                     auto_restore: bool = True, sites_enabled: bool = True) -> Approval:
        """Decyzja administratora. Zatwierdzenie wykonuje operacje przez bezpiecznik."""
        from backend.core import audit, executor, safety
        from backend.core.handlers.common import format_result

        approval = self.get(approval_id)
        if approval is None:
            raise ApprovalError(tr("Nie ma takiej zgody (mogla wygasnac albo serwer zostal zrestartowany).",
                                   "No such approval (it may have expired or the server was restarted)."))
        if approval.status != "pending":
            raise ApprovalError(tr(f"Zgoda {approval.id} ma juz status {approval.status}.",
                                   f"Approval {approval.id} already has status {approval.status}."))
        approval.decided_by = decided_by
        if not approve:
            approval.status, approval.result = "denied", tr("Administrator odrzucil operacje.", "The administrator rejected the operation.")
            await audit.log_blocked(f"mcp:{approval.requested_by}", f"odrzucono: {approval.command}")
            return approval
        approval.status = "approved"

        async def run() -> tuple[str, int]:
            stdout, stderr, code = await executor.execute(approval.command, cwd=cwd, timeout=900)
            await audit.log_confirmed(f"mcp:{approval.requested_by} (zatwierdzil {decided_by})", approval.command, code)
            return format_result(stdout, stderr, code), code

        outcome = None
        async for item in safety.guarded(approval.plan or safety.Plan(), run, interface=f"mcp:{approval.requested_by}",
                                         tool="mcp", description=approval.command, cwd=cwd,
                                         auto_restore=auto_restore, sites_enabled=sites_enabled):
            if isinstance(item, safety.Outcome):
                outcome = item
        approval.result = outcome.text if outcome else tr("Brak wyniku.", "No result.")
        approval.status = "done" if outcome and outcome.exit_code == 0 else "failed"
        return approval


_approvals: Approvals | None = None


def get_approvals() -> Approvals:
    global _approvals
    if _approvals is None:
        _approvals = Approvals()
    return _approvals
