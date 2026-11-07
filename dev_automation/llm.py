"""로컬 LLM(Ollama) 호출. 코드와 데이터가 외부로 나가지 않도록 로컬 호스트만 허용한다."""

import json
import re
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


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_TAG = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)


def _text_tool_calls(content: str, tool_names: set[str]) -> list[dict]:
    """일부 모델(qwen2.5-coder 등)은 도구 호출을 tool_calls 대신 본문에 JSON으로 쓴다. 그걸 읽는다.

    등록된 도구 이름만 받아들인다. 해석이 애매하면 빈 목록을 돌려 평소처럼 '답변'으로 둔다.
    """
    text = content.strip()
    if not text:
        return []
    pieces = _TAG.findall(text) or _FENCE.findall(text) or [text]
    candidates: list = []
    for piece in pieces:
        piece = piece.strip()
        try:
            data = json.loads(piece)
        except ValueError:
            # 한 줄에 하나씩 쓴 경우
            data = []
            for line in piece.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data.append(json.loads(line))
                except ValueError:
                    return []
        candidates.extend(data if isinstance(data, list) else [data])
    calls = []
    for item in candidates:
        if not isinstance(item, dict) or item.get("name") not in tool_names:
            return []
        calls.append({
            "name": item["name"],
            "arguments": _parse_arguments(item.get("arguments", item.get("parameters", {}))),
        })
    return calls


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
        content = message.get("content") or ""
        if not tool_calls:
            names = {t.get("function", {}).get("name") for t in tools if isinstance(t, dict)}
            parsed = _text_tool_calls(content, names - {None})
            if parsed:
                tool_calls, content = parsed, ""
        return {"content": content, "tool_calls": tool_calls}

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
