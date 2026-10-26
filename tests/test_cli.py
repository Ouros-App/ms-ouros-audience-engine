import json
import sys
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ouros_audience_engine import cli
from ouros_audience_engine.services.audience_publisher import PublishResult


def test_run_once_continues_after_audience_failure_and_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failed_audience = SimpleNamespace(key="failed")
    successful_audience = SimpleNamespace(key="successful")
    result = PublishResult(
        audience_key="successful",
        status="PUBLISHED",
        snapshot_version=3,
        member_count=2,
        unknown_farms=0,
        evaluated_farms=2,
        added_count=2,
        resolved_count=0,
        access_revoked_count=0,
        source_watermark=datetime(2026, 10, 1, tzinfo=UTC),
    )
    publisher = Mock()
    publisher.publish.side_effect = [RuntimeError("database password=secret"), result]
    mongo_client = Mock()
    redis_client = Mock()
    runtime = SimpleNamespace(
        metadata=SimpleNamespace(publish_job_lock=nullcontext),
        publisher=publisher,
        config=SimpleNamespace(audiences=(failed_audience, successful_audience)),
        mongo_client=mongo_client,
        redis_client=redis_client,
    )
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(environment="development"))
    monkeypatch.setattr(cli, "build_runtime", lambda _settings: runtime)
    monkeypatch.setattr(sys, "argv", ["ouros-audience-engine", "run-once", "--dry-run"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert [call.args[0].key for call in publisher.publish.call_args_list] == [
        "failed",
        "successful",
    ]
    assert publisher.publish.call_args_list[1].kwargs == {
        "evaluated_at": None,
        "dry_run": True,
    }
    captured = capsys.readouterr()
    assert json.loads(captured.out) == [
        {"audience_key": "failed", "status": "FAILED", "error": "RuntimeError"},
        {
            "audience_key": "successful",
            "status": "PUBLISHED",
            "snapshot_version": 3,
            "member_count": 2,
            "unknown_farms": 0,
            "evaluated_farms": 2,
            "added_count": 2,
            "resolved_count": 0,
            "access_revoked_count": 0,
            "source_watermark": "2026-10-01T00:00:00+00:00",
        },
    ]
    assert "failed (RuntimeError)" in captured.err
    assert "password=secret" not in captured.err
    mongo_client.close.assert_called_once_with()
    redis_client.close.assert_called_once_with()


def test_run_once_requires_production_infisical_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli.get_settings.cache_clear()
    monkeypatch.setenv("ENVIRONMENT", "production")
    for name in (
        "INFISICAL_TOKEN",
        "INFISICAL_PROJECT_ID",
        "INFISICAL_ENV",
        "INFISICAL_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "argv", ["ouros-audience-engine", "run-once", "--publish"])
    build_runtime = Mock()
    monkeypatch.setattr(cli, "build_runtime", build_runtime)

    with (
        patch("ouros_audience_engine.infisical.load_dotenv", return_value=False),
        pytest.raises(RuntimeError, match="INFISICAL_ENV='prod'"),
    ):
        cli.main()

    build_runtime.assert_not_called()
    cli.get_settings.cache_clear()
