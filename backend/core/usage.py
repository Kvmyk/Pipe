"""
Licznik tokenow i kosztow LLM — `/koszt` i opcjonalne dzienne limity.

Kazde zapytanie do providera (rozmowa, workery, rutyny, VIBE) dopisuje zuzycie
do DATA_DIR/usage.json: dzien -> suma i podzial na kto (cli-kuba, telegram-123,
worker, rutyna, vibe) i model. Koszt liczymy z cen podanych w .env
(LLM_PRICE_IN/OUT, WORKER_PRICE_IN/OUT — USD za milion tokenow); bez cen
licznik pokazuje same tokeny.

DAILY_TOKEN_LIMIT / DAILY_COST_LIMIT (0 = bez limitu): po przekroczeniu
check_budget() rzuca BudgetExceeded — agent nie wysyla zapytan do polnocy,
a czuwanie dalej dziala (jest bez LLM). Modul nie importuje settings.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from backend.core import memory

KEEP_DAYS = 90
_lock = threading.Lock()


class BudgetExceeded(RuntimeError):
    """Dzienny limit tokenow albo kosztu zostal wyczerpany."""


@dataclass(frozen=True)
class Prices:
    """USD za milion tokenow."""
    input: float = 0.0
    output: float = 0.0

    @property
    def known(self) -> bool:
        return bool(self.input or self.output)

    def cost(self, prompt: int, completion: int) -> float:
        return prompt * self.input / 1_000_000 + completion * self.output / 1_000_000


def usage_path() -> Path:
    return memory.data_dir() / "usage.json"


def _load() -> dict[str, Any]:
    try:
        raw = json.loads(usage_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def who_from_interface(interface: str) -> str:
    """cli:kuba -> cli-kuba, worker:web1@telegram:1 -> worker, routine:x -> rutyna."""
    interface = (interface or "").strip().lower()
    if interface.startswith("worker:"):
        return "worker"
    if interface.startswith("routine:"):
        return "rutyna"
    if interface in ("vibe", "digest", "incydent"):
        return interface
    return memory.vibe_key(interface) if interface else "inne"


def _bump(bucket: dict[str, Any], prompt: int, completion: int, cached: int, cost: float) -> None:
    bucket["calls"] = bucket.get("calls", 0) + 1
    bucket["prompt"] = bucket.get("prompt", 0) + prompt
    bucket["completion"] = bucket.get("completion", 0) + completion
    bucket["cached"] = bucket.get("cached", 0) + cached
    bucket["cost"] = round(bucket.get("cost", 0.0) + cost, 6)


def record(model: str, usage: Any, who: str, prices: Prices, today: date | None = None) -> None:
    """Dopisuje zuzycie jednego zapytania. `usage` to obiekt/slownik z odpowiedzi OpenAI (moze byc None)."""
    if usage is None:
        return
    get = (lambda k: usage.get(k)) if isinstance(usage, dict) else (lambda k: getattr(usage, k, None))
    prompt = int(get("prompt_tokens") or 0)
    completion = int(get("completion_tokens") or 0)
    details = get("prompt_tokens_details")
    cached = 0
    if details is not None:
        cached = int((details.get("cached_tokens") if isinstance(details, dict)
                      else getattr(details, "cached_tokens", 0)) or 0)
    if not (prompt or completion):
        return
    cost = prices.cost(prompt, completion)
    day = (today or date.today()).isoformat()
    with _lock:
        data = _load()
        days = data.setdefault("days", {})
        entry = days.setdefault(day, {})
        _bump(entry.setdefault("total", {}), prompt, completion, cached, cost)
        _bump(entry.setdefault("by", {}).setdefault(who, {}), prompt, completion, cached, cost)
        _bump(entry.setdefault("models", {}).setdefault(model or "?", {}), prompt, completion, cached, cost)
        cutoff = ((today or date.today()) - timedelta(days=KEEP_DAYS)).isoformat()
        for old in [d for d in days if d < cutoff]:
            del days[old]
        memory._write_atomic(usage_path(), json.dumps(data, ensure_ascii=False, indent=1) + "\n")


def day_total(day: date | None = None) -> dict[str, Any]:
    return _load().get("days", {}).get((day or date.today()).isoformat(), {}).get("total", {})


def check_budget(token_limit: int, cost_limit: float, today: date | None = None) -> None:
    """Rzuca BudgetExceeded, gdy dzisiejsze zuzycie przekroczylo limit (0 = bez limitu)."""
    if not token_limit and not cost_limit:
        return
    total = day_total(today)
    tokens = total.get("prompt", 0) + total.get("completion", 0)
    if token_limit and tokens >= token_limit:
        raise BudgetExceeded(
            f"Wyczerpany dzienny limit tokenow LLM ({tokens:,} z {token_limit:,}). Agent wroci o polnocy; "
            "czuwanie dziala dalej. Limit: DAILY_TOKEN_LIMIT w backend/.env.".replace(",", " "))
    if cost_limit and total.get("cost", 0.0) >= cost_limit:
        raise BudgetExceeded(
            f"Wyczerpany dzienny limit kosztu LLM (${total.get('cost', 0):.2f} z ${cost_limit:.2f}). Agent wroci "
            "o polnocy; czuwanie dziala dalej. Limit: DAILY_COST_LIMIT w backend/.env.")


def _tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f} mln"
    if n >= 1_000:
        return f"{n / 1_000:.1f} tys."
    return str(n)


def _line(label: str, bucket: dict[str, Any], priced: bool) -> str:
    tokens = bucket.get("prompt", 0) + bucket.get("completion", 0)
    text = (f"{label}: {_tokens(tokens)} tokenow (wejscie {_tokens(bucket.get('prompt', 0))}, "
            f"wyjscie {_tokens(bucket.get('completion', 0))}"
            + (f", z cache {_tokens(bucket.get('cached', 0))}" if bucket.get("cached") else "")
            + f"), zapytan {bucket.get('calls', 0)}")
    if priced:
        text += f", ~${bucket.get('cost', 0.0):.4f}"
    return text


def report(days: int = 7, *, priced: bool, token_limit: int = 0, cost_limit: float = 0.0,
           today: date | None = None) -> dict[str, Any]:
    """Dane dla /koszt: dzis (z podzialem), ostatnie dni, suma, limity."""
    today = today or date.today()
    data = _load().get("days", {})
    current = data.get(today.isoformat(), {})
    history = []
    for offset in range(days):
        day = (today - timedelta(days=offset)).isoformat()
        total = data.get(day, {}).get("total", {})
        history.append({"day": day, **{k: total.get(k, 0) for k in ("calls", "prompt", "completion", "cached", "cost")}})
    month_prefix = today.strftime("%Y-%m")
    month = {"prompt": 0, "completion": 0, "cost": 0.0, "calls": 0}
    for day, entry in data.items():
        if day.startswith(month_prefix):
            for key in month:
                month[key] += entry.get("total", {}).get(key, 0)
    lines = [_line("Dzis", current.get("total", {}), priced)]
    for who, bucket in sorted(current.get("by", {}).items(), key=lambda kv: -kv[1].get("prompt", 0)):
        lines.append("  " + _line(who, bucket, priced))
    for model, bucket in sorted(current.get("models", {}).items()):
        lines.append("  " + _line(f"model {model}", bucket, priced))
    lines.append("Ostatnie dni:")
    for item in history[1:]:
        if item["calls"]:
            lines.append("  " + _line(item["day"], item, priced))
    lines.append(_line(f"Miesiac {month_prefix}", month, priced))
    if not priced:
        lines.append("Ceny nieustawione — podaj LLM_PRICE_IN i LLM_PRICE_OUT (USD za milion tokenow) "
                     "w backend/.env, a pokaze koszt.")
    if token_limit or cost_limit:
        limits = []
        if token_limit:
            limits.append(f"{_tokens(token_limit)} tokenow")
        if cost_limit:
            limits.append(f"${cost_limit:.2f}")
        lines.append("Dzienny limit: " + " / ".join(limits))
    return {"today": current.get("total", {}), "history": history, "month": month, "text": "\n".join(lines)}


def yesterday_line(priced: bool, today: date | None = None) -> str:
    day = (today or date.today()) - timedelta(days=1)
    total = _load().get("days", {}).get(day.isoformat(), {}).get("total", {})
    if not total:
        return ""
    return _line(f"LLM wczoraj ({day.isoformat()})", total, priced)
