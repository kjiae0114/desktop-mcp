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
        keep_alive: str = "5m",
    ) -> None:
        if urlparse(base_url).hostname not in LOCAL_HOSTS:
            raise ValueError(f"로컬 호스트(127.0.0.1, localhost)만 허용됩니다: {base_url}")
        self._model = model
        self._base = base_url.rstrip("/")
        self._url = self._base + "/api/chat"
        self._timeout = timeout
        self._num_ctx = num_ctx
        self._keep_alive = keep_alive

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        body = {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "think": False,
            "keep_alive": self._keep_alive,
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

    def unload(self) -> None:
        """모델을 GPU·메모리에서 바로 내린다. 실패해도 작업 결과에는 영향이 없으므로 무시한다.

        Ollama는 마지막 요청 뒤 keep_alive(기본 5분) 동안 모델을 올려 둔다.
        에이전트가 끝난 뒤 그 시간 동안 VRAM 약 4GB를 붙잡지 않도록 끝날 때 호출한다.
        """
        body = {"model": self._model, "keep_alive": 0}
        request = Request(
            self._base + "/api/generate",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                response.read()
        except (URLError, OSError):
            pass
