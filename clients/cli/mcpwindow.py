"""
Okno /mcp w CLI (jak /mcp w Claude Code): ramka z lista serwerow MCP i katalogiem gotowych dodatkow, wybor
strzalkami, Enter otwiera szczegoly z akcjami, Esc wraca. Klucze dodatkow wpisuje sie tu ukrytym polem — trafiaja
do backendu komenda `mcp_servers` (`klucz`), nigdy do rozmowy z modelem. Wymaga prompt_toolkit; bez niego cli.py
pokazuje zwykla liste.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Box, Frame

STYLE = Style.from_dict({
    "frame.border": "#5f5f5f", "frame.label": "bold",
    "title": "bold", "dim": "#8a8a8a", "sel": "bold", "pointer": "ansicyan bold", "section": "#8a8a8a bold",
    "ok": "ansigreen", "bad": "ansired", "warn": "ansiyellow", "note": "ansicyan",
})
MAX_TOOLS = 12


@dataclass
class Entry:
    label: str
    hint: str = ""
    value: Any = None
    mark: tuple[str, str] = ("", "")     # (styl, znak) przed etykieta, np. ("class:ok", "●")
    section: bool = False                # naglowek sekcji — nie da sie go wybrac


async def choose(title: str, entries: list[Entry], *, header: list[tuple[str, str]] | None = None,
                 footer: str = "") -> Any:
    """Lista w ramce; zwraca `value` wybranej pozycji albo None (Esc, q, Ctrl+C)."""
    selectable = [i for i, e in enumerate(entries) if not e.section]
    if not selectable:
        return None
    state = {"index": 0}

    def text() -> FormattedText:
        parts: list[tuple[str, str]] = []
        for style, chunk in header or []:
            parts.append((style, chunk))
        if header:
            parts.append(("", "\n"))
        for position, entry in enumerate(entries):
            if entry.section:
                parts.append(("class:section", f"{entry.label}\n"))
                continue
            chosen = position == selectable[state["index"]]
            parts.append(("class:pointer", " ❯ " if chosen else "   "))
            if entry.mark[1]:
                parts.append((entry.mark[0], entry.mark[1] + " "))
            parts.append(("class:sel" if chosen else "", entry.label))
            if entry.hint:
                parts.append(("class:dim", "  " + entry.hint))
            parts.append(("", "\n"))
        if footer:
            parts.append(("class:dim", "\n" + footer))
        return FormattedText(parts)

    keys = KeyBindings()

    @keys.add("up")
    @keys.add("k")
    def _up(event) -> None:
        state["index"] = (state["index"] - 1) % len(selectable)

    @keys.add("down")
    @keys.add("j")
    @keys.add("tab")
    def _down(event) -> None:
        state["index"] = (state["index"] + 1) % len(selectable)

    @keys.add("enter")
    def _enter(event) -> None:
        event.app.exit(result=entries[selectable[state["index"]]].value)

    @keys.add("escape", eager=True)
    @keys.add("q")
    @keys.add("c-c")
    @keys.add("left")
    def _back(event) -> None:
        event.app.exit(result=None)

    body = Window(FormattedTextControl(text, focusable=True, show_cursor=False), wrap_lines=True, dont_extend_height=True)
    app: Application = Application(layout=Layout(HSplit([Frame(Box(body, padding_left=1, padding_right=1, padding_top=0, padding_bottom=0), title=title)])), key_bindings=keys, style=STYLE,
                                   full_screen=False, erase_when_done=True, mouse_support=False)
    return await app.run_async()


async def run(client, console, tr: Callable[[str, str], str], response_data: Callable[[list], dict]) -> None:
    """Petla okna: lista -> szczegoly -> akcja -> lista. Wynik ostatniej akcji zostaje w terminalu."""

    async def fetch(**fields) -> tuple[dict, str]:
        frames = await client.send_command("mcp_servers", **fields)
        error = next((f.get("response", "") for f in frames if f.get("status") == "error"), "")
        return response_data(frames), error

    def status_text(server: dict) -> tuple[str, str, str]:
        status = server.get("status")
        if status == "connected":
            return "class:ok", "●", tr(f"połączony · {len(server.get('tools') or [])} narzędzi",
                                       f"connected · {len(server.get('tools') or [])} tools")
        if status == "error":
            return "class:bad", "●", tr("błąd", "error")
        if status == "disabled":
            return "class:dim", "○", tr("wyłączony", "disabled")
        return "class:dim", "○", tr("niepołączony", "not connected")

    notes: list[str] = []
    data, error = await fetch()
    if error:
        console.print(f"[red]{error}[/red]")
        return
    footer = tr("↑↓ wybór · Enter otwiera · Esc wraca", "↑↓ select · Enter opens · Esc goes back")
    while True:
        entries = [Entry(tr("Podłączone", "Connected"), section=True)]
        for server in data.get("servers") or []:
            style, mark, hint = status_text(server)
            entries.append(Entry(server["name"], hint, ("server", server["name"]), (style, mark)))
        if not data.get("servers"):
            entries.append(Entry(tr("  nic jeszcze — wybierz coś poniżej", "  nothing yet — pick something below"), section=True))
        ready = [a for a in data.get("catalog") or [] if not a.get("installed")]
        if ready:
            entries.append(Entry("", section=True))
            entries.append(Entry(tr("Gotowe do dodania", "Ready to add"), section=True))
            for addon in ready:
                hint = tr("potrzebny klucz", "needs a key") if addon["secrets"] and not addon["ready"] else \
                    tr("logowanie w rozmowie", "sign in from the chat") if addon.get("login") else ""
                entries.append(Entry(addon["title"], hint, ("addon", addon["name"]), ("class:dim", "+")))
        header = [("class:dim", tr("Dodatki, przez które Pipe korzysta z innych aplikacji. Każdy działa w osobnym "
                                   "kontenerze, bez dostępu do plików serwera.\n",
                                   "Add-ons that let Pipe use other apps. Each runs in its own container, without "
                                   "access to the server's files.\n"))]
        if notes:
            header.append(("class:note", notes[-1] + "\n"))
        picked = await choose(tr(" Serwery MCP ", " MCP servers "), entries, header=header, footer=footer)
        if picked is None:
            break
        kind, name = picked
        if kind == "server":
            note = await _server(name, data, fetch, tr, footer)
        else:
            note = await _addon(name, data, fetch, tr, footer)
        if note:
            notes.append(note)
        data, error = await fetch()
        if error:
            notes.append(error)
            break
    for note in notes:
        console.print(note, highlight=False, markup=False)


async def _secret(name: str, tr) -> str:
    try:
        return (await PromptSession().prompt_async(tr(f"{name} (niewidoczne, Enter zapisuje): ",
                                                      f"{name} (hidden, Enter saves): "), is_password=True)).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


async def _act(fetch, **fields) -> str:
    data, error = await fetch(**fields)
    return error or str(data.get("text", "")).split("\n\nSerwery MCP")[0].split("\n\nMCP servers")[0]


async def _server(name: str, data: dict, fetch: Callable[..., Awaitable[tuple[dict, str]]], tr, footer: str) -> str:
    server = next((s for s in data.get("servers") or [] if s["name"] == name), None)
    if server is None:
        return ""
    addon = next((a for a in data.get("catalog") or [] if a["name"] == (server.get("addon") or name)), None)
    header: list[tuple[str, str]] = []
    if server.get("description"):
        header.append(("class:dim", server["description"] + "\n\n"))
    status = server.get("status")
    header += [("class:dim", tr("Stan:      ", "Status:    ")),
               ("class:ok" if status == "connected" else "class:bad" if status == "error" else "class:dim",
                {"connected": tr("połączony", "connected"), "error": tr("błąd", "error"),
                 "disabled": tr("wyłączony", "disabled")}.get(status, tr("niepołączony", "not connected")) + "\n")]
    if server.get("error"):
        header.append(("class:bad", "           " + server["error"][:300] + "\n"))
    if server.get("version"):
        header += [("class:dim", tr("Serwer:    ", "Server:    ")), ("", server["version"] + "\n")]
    header += [("class:dim", tr("Co działa: ", "Runs:      ")), ("", server.get("runs", "") + "\n")]
    secrets = (addon or {}).get("secrets") or [{"name": s["name"], "label": s["name"], "set": s["set"]}
                                                for s in server.get("secrets") or []]
    for secret in secrets:
        header += [("class:dim", tr("Klucz:     ", "Key:       ")), ("", f"{secret['name']} "),
                   ("class:ok" if secret["set"] else "class:warn", tr("zapisany", "saved") if secret["set"] else tr("brak", "missing")),
                   ("", "\n")]
    tools = server.get("tools") or []
    if tools:
        header.append(("class:section", "\n" + tr(f"Narzędzia ({len(tools)})", f"Tools ({len(tools)})") + "\n"))
        for tool in tools[:MAX_TOOLS]:
            header += [("", f"  {tool['name']}"), ("class:warn" if tool["ask"] else "class:dim",
                                                   "  " + (tr("pyta o zgodę", "asks first") if tool["ask"] else
                                                           tr("odczyt", "read") if tool["read"] else tr("bez pytania", "no prompt")) + "\n")]
        if len(tools) > MAX_TOOLS:
            header.append(("class:dim", tr(f"  …i {len(tools) - MAX_TOOLS} więcej\n", f"  …and {len(tools) - MAX_TOOLS} more\n")))
    if addon and addon.get("login"):
        header.append(("class:note", "\n" + addon["login"] + "\n"))
    if not data.get("can_edit"):
        await choose(f" {name} ", [Entry(tr("← Wstecz", "← Back"), value="back")], header=header, footer=footer)
        return ""
    entries = [Entry(tr("Połącz ponownie", "Reconnect"), value="reload"),
               Entry(tr("Wyłącz", "Disable") if server.get("enabled", True) else tr("Włącz", "Enable"), value="toggle")]
    entries += [Entry(tr(f"Wpisz klucz {s['name']}", f"Enter key {s['name']}"), value=("key", s["name"])) for s in secrets]
    entries += [Entry(tr("Usuń", "Remove"), value="remove", mark=("class:bad", "✕")), Entry(tr("← Wstecz", "← Back"), value=None)]
    action = await choose(f" {name} ", entries, header=header, footer=footer)
    if action == "reload":
        return await _act(fetch, args="odswiez")
    if action == "toggle":
        return await _act(fetch, args=("wylacz " if server.get("enabled", True) else "wlacz ") + name)
    if isinstance(action, tuple):
        value = await _secret(action[1], tr)
        return await _act(fetch, args="klucz", name=action[1], key=value) if value else ""
    if action == "remove":
        sure = await choose(tr(f" Usunąć {name}? ", f" Remove {name}? "), [
            Entry(tr("Tak, usuń (zapisane logowanie też)", "Yes, remove (saved sign-in too)"), value=True, mark=("class:bad", "✕")),
            Entry(tr("Anuluj", "Cancel"), value=False)], footer=footer)
        return await _act(fetch, args="usun " + name) if sure else ""
    return ""


async def _addon(name: str, data: dict, fetch, tr, footer: str) -> str:
    addon = next((a for a in data.get("catalog") or [] if a["name"] == name), None)
    if addon is None:
        return ""
    while True:
        header: list[tuple[str, str]] = [("", addon["description"] + "\n\n"),
                                         ("class:dim", tr("Co działa: ", "Runs:      ")), ("", addon["runs"] + "\n")]
        if addon.get("homepage"):
            header += [("class:dim", tr("Projekt:   ", "Project:   ")), ("", addon["homepage"] + "\n")]
        for secret in addon["secrets"]:
            header += [("", "\n"), ("class:title", secret["label"]), ("class:dim", f"  {secret['name']}  "),
                       ("class:ok" if secret["set"] else "class:warn", tr("zapisany", "saved") if secret["set"] else tr("brak", "missing")),
                       ("", "\n"), ("class:dim", secret["help"] + "\n")]
            if secret.get("url"):
                header.append(("class:note", secret["url"] + "\n"))
        if addon.get("login"):
            header += [("", "\n"), ("class:note", tr("Po dodaniu: ", "After adding: ") + addon["login"] + "\n")]
        if not data.get("can_edit"):
            await choose(f" {addon['title']} ", [Entry(tr("← Wstecz", "← Back"))], header=header, footer=footer)
            return ""
        entries = [Entry(tr(f"Wpisz {s['label']}", f"Enter {s['label']}") + (tr(" (zastąp)", " (replace)") if s["set"] else ""),
                         value=("key", s["name"])) for s in addon["secrets"]]
        if addon["ready"]:
            entries.insert(0, Entry(tr("Dodaj", "Add"), value="add", mark=("class:ok", "+")))
        else:
            header.append(("class:warn", "\n" + tr("Najpierw wpisz klucze — potem pojawi się Dodaj.",
                                                   "Enter the keys first — then Add appears.") + "\n"))
        entries.append(Entry(tr("← Wstecz", "← Back"), value=None))
        action = await choose(f" {addon['title']} ", entries, header=header, footer=footer)
        if action == "add":
            return await _act(fetch, args="dodaj " + name)
        if not isinstance(action, tuple):
            return ""
        value = await _secret(action[1], tr)
        if value:
            result = await _act(fetch, args="klucz", name=action[1], key=value)
            fresh, _ = await fetch()
            addon = next((a for a in fresh.get("catalog") or [] if a["name"] == name), addon)
            if result.startswith("[") or not next((s for s in addon["secrets"] if s["name"] == action[1]), {}).get("set"):
                return result
