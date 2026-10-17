import asyncio
import json

import httpx
import pytest

from app.jev_client import JEV_ENDPOINT, JevClient, JevError

CRITERIA = {"radio_job": "Radio.", "unsorted": "Do ułożenia."}
STATE = {"chapter_title": "Grafik", "session_title": "Radio", "archive_group": "Work"}
ANSWER = {
    "answers": {"topic": {"choice": "radio_job", "confidence": 0.93, "probabilities": {"radio_job": 0.93, "unsorted": 0.07}}},
    "usage": {"cost": 0.00007},
}


def client_with(responses: list, seen: list | None = None) -> JevClient:
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item
        return httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)

    async def no_sleep(_: float) -> None:
        return None

    return JevClient("sk-test", "typesafe/jev-1.14", transport=httpx.MockTransport(handler), sleep=no_sleep)


def run(coro):
    return asyncio.run(coro)


def test_request_shape_and_parsed_answer() -> None:
    seen: list[httpx.Request] = []
    client = client_with([(200, ANSWER)], seen)
    answer = run(client.classify(STATE, CRITERIA))
    request = seen[0]
    body = json.loads(request.content)
    assert str(request.url) == JEV_ENDPOINT
    assert request.headers["authorization"] == "Bearer sk-test"
    assert body["model"] == "typesafe/jev-1.14"
    assert body["state"] == STATE
    assert body["questions"]["topic"]["type"] == "choice"
    assert body["questions"]["topic"]["criteria"] == CRITERIA
    assert answer["choice"] == "radio_job" and answer["confidence"] == 0.93
    assert answer["cost_usd"] == 0.00007 and answer["raw"] == ANSWER


def test_retries_rate_limit_and_network_errors() -> None:
    client = client_with([(429, "slow down"), httpx.ConnectError("boom"), (200, ANSWER)])
    assert run(client.classify(STATE, CRITERIA))["choice"] == "radio_job"


@pytest.mark.parametrize(
    ("responses", "code"),
    [
        ([(401, "no")], "jev_auth"),
        ([(400, "bad model")], "jev_rejected"),
        ([(503, "down")] * 5, "jev_unavailable"),
        ([(200, {"answers": {"topic": {"choice": "car"}}})], "jev_invalid"),
    ],
)
def test_errors_have_codes(responses, code) -> None:
    client = client_with(responses)
    with pytest.raises(JevError) as caught:
        run(client.classify(STATE, CRITERIA))
    assert caught.value.code == code
    assert "sk-test" not in str(caught.value)
