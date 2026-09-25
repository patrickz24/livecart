import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from livecart_core.healthcheck import HEALTHY, UNHEALTHY, check_http, main


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status = {"/health": 200, "/ready": 503}.get(self.path, 404)
        self.send_response(status)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return None


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def closed_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/health"


def test_2xx_is_healthy(base_url: str) -> None:
    assert check_http(f"{base_url}/health", timeout_s=2)


def test_5xx_is_unhealthy(base_url: str) -> None:
    assert not check_http(f"{base_url}/ready", timeout_s=2)


def test_refused_connection_is_unhealthy(closed_port_url: str) -> None:
    assert not check_http(closed_port_url, timeout_s=0.5)


def test_only_http_urls_are_probed() -> None:
    with pytest.raises(ValueError, match="http"):
        check_http("file:///etc/passwd", timeout_s=1)


def test_command_exit_status(base_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["http", f"{base_url}/health"]) == HEALTHY
    assert main(["http", f"{base_url}/ready", "--timeout", "2"]) == UNHEALTHY
    assert "unhealthy" in capsys.readouterr().err


def test_command_rejects_other_schemes() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["http", "ftp://example.invalid/"])

    assert excinfo.value.code == 2
