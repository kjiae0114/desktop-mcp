"""로컬 LLM(Ollama) 호출. 코드와 데이터가 외부로 나가지 않도록 로컬 호스트만 허용한다."""

import json
from typing import Protocol
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})


class LLMError(RuntimeError):
    """모델 호출에 실패했을 때 발생한다."""


class LLMClient(Protocol):
    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        """{"content": str, "tool_calls": [{"name": str, "arguments": dict}]}를 반환한다."""
        ...


def _parse_arguments(arguments: object) -> dict:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return {}
    return arguments if isinstance(arguments, dict) else {}


class OllamaClient:
    def __init__(
        self,
        model: str,
        base_url: str = "http://127.0.0.1:11434",
        timeout: float = 600,
        num_ctx: int = 8192,
    ) -> None:
        if urlparse(base_url).hostname not in LOCAL_HOSTS:
            raise ValueError(f"로컬 호스트(127.0.0.1, localhost)만 허용됩니다: {base_url}")
        self._model = model
        self._url = base_url.rstrip("/") + "/api/chat"
        self._timeout = timeout
        self._num_ctx = num_ctx

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        body = {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "think": False,
            "options": {"temperature": 0.2, "num_ctx": self._num_ctx},
        }
        request = Request(
            self._url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            message = data["message"]
        except (URLError, OSError) as exc:
            raise LLMError(
                f"Ollama에 연결하지 못했습니다. Ollama가 실행 중인지 확인하세요: {exc}"
            ) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMError(f"Ollama 응답을 해석하지 못했습니다: {exc}") from exc

        tool_calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            tool_calls.append({
                "name": function.get("name", ""),
                "arguments": _parse_arguments(function.get("arguments")),
            })
        return {"content": message.get("content") or "", "tool_calls": tool_calls}
