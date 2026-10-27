import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime

from ouros_audience_engine.runtime import build_runtime
from ouros_audience_engine.services.tip_audience_seed import seed_tip_audiences
from ouros_audience_engine.settings import get_settings


def _parse_as_of(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="ouros-audience-engine")
    commands = parser.add_subparsers(dest="command", required=True)
    run_once = commands.add_parser("run-once", help="evaluate every configured audience once")
    mode = run_once.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run", action="store_true", help="evaluate without publishing snapshots"
    )
    mode.add_argument("--publish", action="store_true", help="publish snapshots to Redis")
    run_once.add_argument(
        "--as-of",
        type=_parse_as_of,
        help="evaluation time (ISO-8601 with offset); non-production only",
    )
    commands.add_parser(
        "seed-tip-audiences",
        help="create disabled POC rules for the 32 current tips",
    )
    args = parser.parse_args()
    settings = get_settings()
    if (
        args.command == "run-once"
        and args.as_of is not None
        and settings.environment.lower() == "production"
    ):
        parser.error("--as-of is disabled in production")

    runtime = build_runtime(settings)
    try:
        if args.command == "seed-tip-audiences":
            definitions = runtime.api.definitions
            if definitions is None:
                raise RuntimeError("Mongo audience definitions are unavailable")
            with runtime.metadata.publish_job_lock():
                seed_result = seed_tip_audiences(definitions, runtime.metadata)
            print(json.dumps(asdict(seed_result), indent=2))
            return

        failed = False
        results: list[dict[str, object]] = []
        with runtime.metadata.publish_job_lock():
            for audience in runtime.config.audiences:
                try:
                    publish_result = runtime.publisher.publish(
                        audience,
                        evaluated_at=args.as_of,
                        dry_run=args.dry_run,
                    )
                except Exception as exc:
                    failed = True
                    error_type = type(exc).__name__
                    print(
                        f"audience {audience.key} failed ({error_type})",
                        file=sys.stderr,
                    )
                    results.append(
                        {
                            "audience_key": audience.key,
                            "status": "FAILED",
                            "error": error_type,
                        }
                    )
                else:
                    results.append(asdict(publish_result))
        print(json.dumps(results, default=_json_default, indent=2))
        if failed:
            raise SystemExit(1)
    finally:
        runtime.mongo_client.close()
        runtime.redis_client.close()  # type: ignore[no-untyped-call]


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"audience run failed: {exc}", file=sys.stderr)
        raise
