import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "export_openapi.py"


def load_script() -> ModuleType:
    # scripts/ is not a package: load the file the way `python <path>` would.
    spec = importlib.util.spec_from_file_location("export_openapi", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_settings_pass_the_production_validators_and_reach_nothing() -> None:
    settings = load_script().export_settings()

    assert settings.is_production
    for url in (settings.database_url, settings.redis_url):
        assert ".invalid/" in url.get_secret_value()


def test_the_export_is_deterministic_sorted_and_newline_terminated(tmp_path: Path) -> None:
    script = load_script()
    first, second = tmp_path / "first.json", tmp_path / "second.json"

    assert script.main(["--output", str(first)]) == 0
    assert script.main(["--output", str(second)]) == 0

    raw = first.read_bytes()
    assert raw == second.read_bytes()
    assert raw.endswith(b"}\n")
    assert b"\r" not in raw
    text = raw.decode("utf-8")
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_the_export_ignores_the_environment_and_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = load_script()
    baseline = tmp_path / "baseline.json"
    script.main(["--output", str(baseline)])

    # Values that would fail validation if the export read them.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("APP_ENV=staging\nLOG_LEVEL=LOUD\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "mysql://root@localhost/livecart")
    monkeypatch.setenv("REDIS_URL", "memcached://localhost")
    exported = tmp_path / "exported.json"

    assert script.main(["--output", str(exported)]) == 0
    assert exported.read_bytes() == baseline.read_bytes()


def test_the_exported_schema_holds_the_probes_only(tmp_path: Path) -> None:
    output = tmp_path / "openapi.json"
    load_script().main(["--output", str(output)])

    schema = json.loads(output.read_text(encoding="utf-8"))

    assert set(schema["paths"]) == {"/health", "/ready"}
    assert schema["info"]["title"] == "LiveCart API"
