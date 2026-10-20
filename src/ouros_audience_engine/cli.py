import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime

from ouros_audience_engine.runtime import build_runtime
from ouros_audience_engine.settings import Settings


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
    mode.add_argument(
        "--publish", action="store_true", help="publish changed snapshots and transitions"
    )
    run_once.add_argument(
        "--as-of",
        type=_parse_as_of,
        help="evaluation time (ISO-8601 with offset); non-production only",
    )
    args = parser.parse_args()
    settings = Settings()
    if args.as_of is not None and settings.environment.lower() == "production":
        parser.error("--as-of is disabled in production")

    runtime = build_runtime(settings)
    try:
        with runtime.metadata.publish_job_lock():
            results = [
                runtime.publisher.publish(
                    audience,
                    evaluated_at=args.as_of,
                    dry_run=args.dry_run,
                )
                for audience in runtime.config.audiences
            ]
        print(json.dumps([asdict(result) for result in results], default=_json_default, indent=2))
    finally:
        runtime.redis_client.close()  # type: ignore[no-untyped-call]
        runtime.s3_client.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"audience run failed: {exc}", file=sys.stderr)
        raise
