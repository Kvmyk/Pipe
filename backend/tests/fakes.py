"""
Atrapy do testow petli agenta — klient OpenAI zwracajacy zaplanowane odpowiedzi.
"""

from __future__ import annotations

import json
from typing import Any

from openai.types.chat import ChatCompletion


def completion(content: str | None = None, tool_calls: list[tuple[str, str, dict]] | None = None) -> ChatCompletion:
    """Odpowiedz modelu: tekst albo wywolania narzedzi [(id, nazwa, argumenty)]."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for call_id, name, args in tool_calls
        ]
    return ChatCompletion.model_validate({
        "id": "x", "object": "chat.completion", "created": 0, "model": "fake",
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
    })


class FakeCompletions:
    def __init__(self, script: list[ChatCompletion]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> ChatCompletion:
        self.calls.append(kwargs)
        if not self.script:
            return completion("koniec")
        return self.script.pop(0)


class FakeClient:
    def __init__(self, script: list[ChatCompletion]) -> None:
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeCompletions(script)

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.chat.completions.calls


def assert_history_valid(messages: list[dict[str, Any]]) -> None:
    """Kazde wywolanie narzedzia ma dokladnie jedna odpowiedz, zanim pojawi sie kolejna wiadomosc."""
    pending: list[str] = []
    for message in messages:
        if message.get("role") == "assistant":
            assert not pending, f"nieodpowiedziane wywolania: {pending}"
            pending = [c["id"] for c in message.get("tool_calls") or []]
        elif message.get("role") == "tool":
            assert message["tool_call_id"] in pending, f"odpowiedz bez wywolania: {message['tool_call_id']}"
            pending.remove(message["tool_call_id"])
        elif message.get("role") == "user":
            assert not pending, f"wiadomosc uzytkownika przed odpowiedzia na {pending}"
