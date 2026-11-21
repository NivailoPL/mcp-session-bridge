from collections.abc import Callable
from types import ModuleType
from typing import Any


def _start_suite(main: ModuleType, label: str = "chatgpt.com", **extra: Any) -> str:
    suite_id = extra.pop("suite_id", f"suite-test-{label}")
    main.store.create_harness_probe_suite(
        {
            "suite_id": suite_id,
            "harness_label": label,
            "content_profile": "transcript_markdown",
            "start_chars": 32_000,
            "created_by": "test",
            **extra,
        }
    )
    return suite_id


def _model_report(main: ModuleType, step: dict[str, Any], limit: int, *, truncates: bool) -> dict[str, Any]:
    """Act like a model in a harness that delivers at most `limit` payload chars."""
    blocks = main.store.get_output_probe_run(step["run_id"])["blocks"]
    if step["payload_char_count"] > limit and not truncates:
        return main.report_harness_probe(step["suite_id"], [], 0, "", harness_error="Tool result too large")
    visible = [block for block in blocks if block["offset"] + block["char_count"] <= limit]
    checkpoints = [block["canary"] for block in visible if block["checkpoint_labels"]]
    last = visible[-1]
    return main.report_harness_probe(step["suite_id"], checkpoints, last["index"], last["canary"])


def _run(main: ModuleType, suite_id: str, report: Callable[[dict[str, Any]], dict[str, Any]]) -> tuple[list[int], dict[str, Any]]:
    sizes: list[int] = []
    for _ in range(10):
        step = main.next_harness_probe(suite_id)
        if step.get("done"):
            return sizes, step
        sizes.append(step["target_chars"])
        reported = report(step)
        assert reported["ok"], reported
        if reported.get("done"):
            return sizes, reported
    raise AssertionError(f"suite did not finish: {sizes}")


def test_suite_finds_a_silent_cut_through_the_mcp_tools(load_main) -> None:
    main = load_main()
    suite_id = _start_suite(main)

    sizes, done = _run(main, suite_id, lambda step: _model_report(main, step, 98_400, truncates=True))

    assert sizes == [32_000, 64_000, 128_000, 92_000]
    assert done["verdict"] == "truncates"
    assert done["largest_complete_chars"] == 92_000
    assert 97_000 <= done["smallest_failing_chars"] <= 98_400
    suite = main.store.get_harness_probe_suite(suite_id)
    assert suite["status"] == "done"
    assert [run["outcome"] for run in suite["steps"]] == ["complete", "complete", "truncated", "complete"]
    assert [run["cumulative_chars"] for run in suite["steps"]] == [32_000, 96_000, 224_000, 316_000]


def test_suite_walks_down_on_rejections_and_keeps_the_error_text(load_main) -> None:
    main = load_main()
    suite_id = _start_suite(main, "small-harness")

    sizes, done = _run(main, suite_id, lambda step: _model_report(main, step, 12_999, truncates=False))

    assert sizes == [32_000, 16_000, 8_000, 12_000, 14_000]
    assert done["verdict"] == "rejects"
    first = main.store.get_harness_probe_suite(suite_id)["steps"][0]
    assert first["outcome"] == "rejected"
    assert first["harness_error"] == "Tool result too large"


def test_unreported_step_counts_as_no_report_and_the_suite_resumes(load_main) -> None:
    main = load_main()
    suite_id = _start_suite(main)

    first = main.next_harness_probe(suite_id)
    resumed = main.next_harness_probe(f"  {suite_id} ")

    assert first["target_chars"] == 32_000
    assert resumed["target_chars"] == 16_000
    steps = main.store.get_harness_probe_suite(suite_id)["steps"]
    assert [(run["outcome"], run["status"]) for run in steps] == [("no_report", "no_report"), (None, "pending")]


def test_report_without_a_pending_step_or_suite_is_refused(load_main) -> None:
    main = load_main()
    suite_id = _start_suite(main)

    assert main.report_harness_probe(suite_id, [], 0, "")["ok"] is False
    assert main.next_harness_probe("suite-missing")["ok"] is False
    assert main.report_harness_probe("suite-missing", [], 0, "")["ok"] is False


def test_finished_suite_keeps_answering_done(load_main) -> None:
    main = load_main()
    suite_id = _start_suite(main)
    _run(main, suite_id, lambda step: _model_report(main, step, 10_000_000, truncates=True))

    again = main.next_harness_probe(suite_id)

    assert again["done"] is True
    assert again["verdict"] == "no_limit"
    assert len(main.store.get_harness_probe_suite(suite_id)["steps"]) == 4


def test_creating_a_suite_keeps_only_the_newest_five_per_harness(load_main) -> None:
    main = load_main()
    for index in range(7):
        suite_id = _start_suite(main, suite_id=f"suite-{index}")
        main.next_harness_probe(suite_id)
    other = _start_suite(main, "claude.ai", suite_id="suite-other")

    kept = [f"suite-{index}" for index in range(7) if main.store.get_harness_probe_suite(f"suite-{index}")]

    assert len(kept) == 5
    assert main.store.get_harness_probe_suite(other) is not None
    suite_runs = [run for run in main.store.list_output_probe_runs(limit=200) if run["suite_id"]]
    assert {run["suite_id"] for run in suite_runs} <= set(kept)


def test_standalone_probe_still_works_without_a_suite(load_main) -> None:
    main = load_main()

    started = main.run_output_probe("manual-check", 8_000, "transport_canary")

    stored = main.store.get_output_probe_run(started["run_id"])
    assert stored["suite_id"] is None
    assert "submit_output_probe_observation" in started["report_instructions"]


BASE = "/admin/api/settings/harness-tests"


def test_admin_starts_a_simple_suite_and_returns_a_prompt(load_main, admin_client) -> None:
    main = load_main()
    client, csrf = admin_client(main)

    started = client.post(BASE, json={"harness_label": "  chatgpt.com "}, headers={"x-csrf-token": csrf})

    assert started.status_code == 200
    body = started.json()
    suite_id = body["suite"]["suite_id"]
    assert body["suite"]["harness_label"] == "chatgpt.com"
    assert body["suite"]["content_profile"] == "transcript_markdown"
    assert body["suite"]["start_chars"] == 32_000
    assert f'suite_id="{suite_id}"' in body["prompt"]
    assert "report_harness_probe" in body["prompt"]
    assert body["harness_tests"]["suites"][0]["suite_id"] == suite_id


def test_admin_rejects_bad_input_and_missing_csrf(load_main, admin_client) -> None:
    main = load_main()
    client, csrf = admin_client(main)
    headers = {"x-csrf-token": csrf}

    assert client.post(BASE, json={"harness_label": "x"}).status_code == 403
    assert client.post(BASE, json={"harness_label": "   "}, headers=headers).status_code == 400
    assert client.post(
        BASE, json={"harness_label": "x", "mode": "advanced", "start_chars": 10}, headers=headers
    ).status_code == 400
    assert client.post(
        BASE, json={"harness_label": "x", "mode": "advanced", "content_profile": "nope"}, headers=headers
    ).status_code == 400
    assert client.get(f"{BASE}/suite-missing").status_code == 404


def test_admin_reads_live_progress_without_canaries(load_main, admin_client) -> None:
    main = load_main()
    client, csrf = admin_client(main)
    suite_id = client.post(
        BASE,
        json={"harness_label": "claude.ai", "mode": "advanced", "content_profile": "transport_canary", "start_chars": 8_000},
        headers={"x-csrf-token": csrf},
    ).json()["suite"]["suite_id"]
    step = main.next_harness_probe(suite_id)
    _model_report(main, step, 10_000_000, truncates=True)
    main.next_harness_probe(suite_id)

    suite = client.get(f"{BASE}/{suite_id}").json()["suite"]

    assert suite["status"] == "running"
    assert [item["target_chars"] for item in suite["steps"]] == [8_000, 16_000]
    assert [item["outcome"] for item in suite["steps"]] == ["complete", None]
    assert suite["verdict"]["verdict"] == "passes_up_to"
    assert "blocks" not in suite["steps"][0]
    assert "observed_canaries" not in suite["steps"][0]
    assert "last_complete_canary" not in suite["steps"][0]


def test_context_suspect_marks_failures_after_a_large_cumulative_payload(load_main, admin_client) -> None:
    main = load_main()
    client, _ = admin_client(main)
    suite_id = _start_suite(main)
    _run(main, suite_id, lambda step: _model_report(main, step, 200_000, truncates=False))

    steps = client.get(f"{BASE}/{suite_id}").json()["suite"]["steps"]

    # 224k chars were already in the chat before the 250k step was rejected.
    assert [(item["target_chars"], item["context_suspect"]) for item in steps][:4] == [
        (32_000, False), (64_000, False), (128_000, False), (250_000, True),
    ]


def test_cleanup_keeps_latest_per_harness_or_clears_everything(load_main, admin_client) -> None:
    main = load_main()
    client, csrf = admin_client(main)
    headers = {"x-csrf-token": csrf}
    for index in range(3):
        _start_suite(main, "chatgpt.com", suite_id=f"suite-gpt-{index}")
    _start_suite(main, "claude.ai", suite_id="suite-claude")
    main.run_output_probe("manual", 8_000, "transcript_markdown")
    assert client.get(BASE).json()["harness_tests"]["cleanup"] == {
        "older_suites": 2, "standalone_runs": 1, "standalone_labels": ["manual"], "suites": 4,
    }

    kept = client.post(f"{BASE}/cleanup", json={"mode": "keep_latest"}, headers=headers).json()

    assert kept["deleted"] == {"suites": 2, "standalone_runs": 1}
    assert {suite["harness_label"] for suite in kept["harness_tests"]["suites"]} == {"chatgpt.com", "claude.ai"}

    one = client.delete(f"{BASE}?harness=claude.ai", headers=headers).json()
    assert [suite["harness_label"] for suite in one["harness_tests"]["suites"]] == ["chatgpt.com"]

    cleared = client.post(f"{BASE}/cleanup", json={"mode": "all"}, headers=headers).json()
    assert cleared["harness_tests"]["suites"] == []
    assert client.post(f"{BASE}/cleanup", json={"mode": "x"}, headers=headers).status_code == 400
    assert client.delete(BASE, headers=headers).status_code == 400


def test_safe_chunk_ignores_transport_canary_results(load_main, admin_client) -> None:
    main = load_main()
    client, _ = admin_client(main)
    for label, profile, size in (("gpt", "transcript_markdown", 40_000), ("gpt", "transport_canary", 8_000)):
        started = main.run_output_probe(label, size, profile)
        blocks = main.store.get_output_probe_run(started["run_id"])["blocks"]
        main.submit_output_probe_observation(
            started["run_id"],
            [block["canary"] for block in blocks if block["checkpoint_labels"]],
            blocks[-1]["index"],
            blocks[-1]["canary"],
        )

    transcript = client.get("/admin/api/settings").json()["settings"]["transcript"]

    assert len(transcript["recommendations"]) == 2
    assert transcript["safe_chunk"]["content_profile"] == "transcript_markdown"
    assert transcript["safe_chunk"]["recommended_chunk_chars"] == 30_000


def test_run_output_probe_refuses_a_suite_id_as_harness_label(load_main) -> None:
    main = load_main()
    suite_id = _start_suite(main)

    refused = main.run_output_probe(suite_id, 10_000, "transcript_markdown")

    assert refused["ok"] is False
    assert "next_harness_probe" in refused["error"]
    assert main.store.list_output_probe_runs() == []


def test_prompt_tells_a_model_without_the_tools_to_stop() -> None:
    from app.output_probe import harness_test_prompt

    prompt = harness_test_prompt("suite-x")

    assert "refresh this connector's tools" in prompt
    assert "do not use run_output_probe" in prompt
