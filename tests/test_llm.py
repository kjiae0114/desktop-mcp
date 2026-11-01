import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

import pytest

from dev_automation.llm import LLMError, OllamaClient


@pytest.fixture
def server():
    state = {"requests": [], "reply": {}}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            state["requests"].append((self.path, json.loads(self.rfile.read(length))))
            body = json.dumps(state["reply"]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{httpd.server_port}"
    yield state
    httpd.shutdown()
    httpd.server_close()


def test_chat_sends_expected_request_and_parses_tool_calls(server):
    server["reply"] = {
        "message": {
            "content": "",
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "a.py"}}},
                {"function": {"name": "list_files", "arguments": '{"path": "."}'}},
                {"function": {"name": "run_tests", "arguments": "깨진 json"}},
            ],
        }
    }
    client = OllamaClient("m", server["url"])

    reply = client.chat([{"role": "user", "content": "hi"}], [{"type": "function"}])

    path, body = server["requests"][0]
    assert path == "/api/chat"
    assert body["model"] == "m"
    assert body["stream"] is False and body["think"] is False
    assert body["options"] == {"temperature": 0.2, "num_ctx": 8192}
    assert reply["tool_calls"] == [
        {"name": "read_file", "arguments": {"path": "a.py"}},
        {"name": "list_files", "arguments": {"path": "."}},
        {"name": "run_tests", "arguments": {}},
    ]


def test_chat_returns_content(server):
    server["reply"] = {"message": {"content": "끝"}}

    reply = OllamaClient("m", server["url"]).chat([], [])

    assert reply == {"content": "끝", "tool_calls": []}


def test_connection_failure_raises_llm_error():
    with pytest.raises(LLMError, match="Ollama가 실행 중인지"):
        OllamaClient("m", "http://127.0.0.1:9", timeout=2).chat([], [])


def test_invalid_response_raises_llm_error(server):
    server["reply"] = {"unexpected": 1}

    with pytest.raises(LLMError):
        OllamaClient("m", server["url"]).chat([], [])


@pytest.mark.parametrize(
    "url",
    ["http://example.com:11434", "http://192.168.0.5:11434", "http://127.0.0.1.evil.com"],
)
def test_non_local_host_is_rejected(url):
    with pytest.raises(ValueError):
        OllamaClient("m", url)


def test_localhost_is_allowed():
    OllamaClient("m", "http://localhost:11434")
