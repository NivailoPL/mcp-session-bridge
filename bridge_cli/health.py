from __future__ import annotations

import json

from bridge_cli.runner import Runner


LOCAL_HEALTH_URL = "http://127.0.0.1:8787/healthz"


def verify_local_health(runner: Runner) -> None:
    result = runner.run(
        "curl", "--fail", "--silent", "--show-error", "--retry", "10",
        "--retry-delay", "1", "--retry-connrefused",
        "--connect-timeout", "3", "--max-time", "5",
        "--retry-max-time", "60", LOCAL_HEALTH_URL,
    )
    require_health_payload(result.stdout, LOCAL_HEALTH_URL)


def require_health_payload(raw: str, url: str) -> None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{url} returned invalid health JSON.") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError(f"{url} did not return the Bridge health contract.")
