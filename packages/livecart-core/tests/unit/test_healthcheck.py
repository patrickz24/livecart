import os
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from livecart_core.healthcheck import (
    HEALTHY,
    HEARTBEAT_FILE_ENV,
    UNHEALTHY,
    check_heartbeat,
    check_http,
    main,
)


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


def test_a_fresh_heartbeat_is_healthy(tmp_path: Path) -> None:
    beat = tmp_path / "worker.heartbeat"
    beat.touch()

    assert check_heartbeat(beat, max_age_s=30)


def test_an_old_or_missing_heartbeat_is_unhealthy(tmp_path: Path) -> None:
    beat = tmp_path / "worker.heartbeat"
    assert not check_heartbeat(beat, max_age_s=30)

    beat.touch()
    touched_at = beat.stat().st_mtime
    assert check_heartbeat(beat, max_age_s=30, now=touched_at + 30)
    assert not check_heartbeat(beat, max_age_s=30, now=touched_at + 31)


def test_heartbeat_command_exit_status(tmp_path: Path) -> None:
    beat = tmp_path / "worker.heartbeat"
    assert main(["heartbeat", "--file", str(beat)]) == UNHEALTHY

    beat.touch()
    assert main(["heartbeat", "--file", str(beat)]) == HEALTHY

    old = time.time() - 60
    os.utime(beat, (old, old))
    assert main(["heartbeat", "--file", str(beat), "--max-age", "30"]) == UNHEALTHY
    assert main(["heartbeat", "--file", str(beat), "--max-age", "90"]) == HEALTHY


def test_heartbeat_command_reads_the_worker_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    beat = tmp_path / "custom.heartbeat"
    beat.touch()
    monkeypatch.setenv(HEARTBEAT_FILE_ENV, str(beat))

    assert main(["heartbeat"]) == HEALTHY
