from collections.abc import Callable
from typing import Any

from app.output_probe import (
    build_output_probe,
    classify_probe_observation,
    cut_point_from_observation,
    next_probe_step,
    probe_recommendations,
    step_outcome,
    suite_verdict,
)


def _canary_factory():
    counter = 0

    def next_canary() -> str:
        nonlocal counter
        counter += 1
        return f"CANARY{counter:010d}"

    return next_canary


def test_build_output_probe_is_exact_and_checkpointed() -> None:
    probe = build_output_probe(
        run_id="probe-test-000001",
        target_chars=12_000,
        content_profile="transcript_markdown",
        canary_factory=_canary_factory(),
    )

    assert len(probe["payload"]) == 12_000
    assert probe["payload_char_count"] == 12_000
    assert probe["block_count"] >= 10
    assert probe["payload"].endswith(
        f'END|BLOCK={probe["block_count"]:06d}|CANARY={probe["blocks"][-1]["canary"]}\n'
    )
    labels = {
        label
        for block in probe["blocks"]
        for label in block["checkpoint_labels"]
    }
    assert labels == {"FIRST", "Q1", "MID", "Q3", "FINAL"}
    assert "Do not infer or reconstruct missing canaries" in probe["payload"]


def test_transport_profile_is_not_natural_language_filler() -> None:
    probe = build_output_probe(
        run_id="probe-test-000002",
        target_chars=8_000,
        content_profile="transport_canary",
        canary_factory=_canary_factory(),
    )

    assert len(probe["payload"]) == 8_000
    assert "synthetic transcript" not in probe["payload"].lower()


def test_probe_supports_minimum_and_maximum_payload_boundaries() -> None:
    minimum = build_output_probe(
        run_id="probe-test-minimum",
        target_chars=4_096,
        content_profile="transcript_markdown",
        canary_factory=_canary_factory(),
    )
    maximum = build_output_probe(
        run_id="probe-test-maximum",
        target_chars=250_000,
        content_profile="transcript_markdown",
        canary_factory=_canary_factory(),
    )

    assert len(minimum["payload"]) == 4_096
    assert len(maximum["payload"]) == 250_000
    assert maximum["block_count"] > minimum["block_count"]


def test_probe_observation_classifies_complete_tail_and_middle_loss() -> None:
    probe = build_output_probe(
        run_id="probe-test-000003",
        target_chars=12_000,
        content_profile="transcript_markdown",
        canary_factory=_canary_factory(),
    )
    checkpoints = {
        label: block["canary"]
        for block in probe["blocks"]
        for label in block["checkpoint_labels"]
    }
    last = probe["blocks"][-1]

    complete = classify_probe_observation(
        probe["blocks"],
        list(checkpoints.values()),
        last["index"],
        last["canary"],
    )
    assert complete["result_class"] == "complete"
    assert complete["matched_checkpoints"] == ["FIRST", "Q1", "MID", "Q3", "FINAL"]

    tail = classify_probe_observation(
        probe["blocks"],
        [checkpoints["FIRST"], checkpoints["Q1"], checkpoints["MID"]],
        probe["blocks"][probe["block_count"] // 2]["index"],
        probe["blocks"][probe["block_count"] // 2]["canary"],
    )
    assert tail["result_class"] == "tail_truncated"

    middle = classify_probe_observation(
        probe["blocks"],
        [checkpoints["FIRST"], checkpoints["FINAL"]],
        last["index"],
        last["canary"],
    )
    assert middle["result_class"] == "middle_removed"


def test_probe_recommendations_do_not_mix_tool_output_modes() -> None:
    runs = [
        {
            "run_id": "optimized-run",
            "harness_label": "codex",
            "content_profile": "transcript_markdown",
            "tool_output_mode": "optimized",
            "payload_char_count": 80_000,
            "result_class": "complete",
        },
        {
            "run_id": "compatibility-run",
            "harness_label": "codex",
            "content_profile": "transcript_markdown",
            "tool_output_mode": "maximum_compatibility",
            "payload_char_count": 120_000,
            "result_class": "complete",
        },
    ]

    recommendations = probe_recommendations(runs, tool_output_mode="optimized")

    assert len(recommendations) == 1
    assert recommendations[0]["run_id"] == "optimized-run"
    assert recommendations[0]["tool_output_mode"] == "optimized"
    assert recommendations[0]["recommended_chunk_chars"] == 60_000


def _harness(limit: int, *, truncates: bool) -> Callable[[int], dict[str, Any]]:
    def receive(target: int) -> dict[str, Any]:
        if target <= limit:
            return {"target_chars": target, "outcome": "complete", "cut_at_chars": None}
        if truncates:
            return {"target_chars": target, "outcome": "truncated", "cut_at_chars": limit}
        return {"target_chars": target, "outcome": "rejected", "cut_at_chars": None}

    return receive


def _run_suite(receive: Callable[[int], dict[str, Any]]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    while (target := next_probe_step(steps)) is not None:
        assert len(steps) < 10, f"suite did not stop: {steps}"
        steps.append(receive(target))
    return steps


def _sizes(steps: list[dict[str, Any]]) -> list[int]:
    return [step["target_chars"] for step in steps]


def test_suite_on_a_large_harness_grows_to_the_ceiling_and_stops() -> None:
    steps = _run_suite(_harness(10_000_000, truncates=True))

    assert _sizes(steps) == [32_000, 64_000, 128_000, 250_000]
    assert suite_verdict(steps)["verdict"] == "no_limit"


def test_suite_reads_a_silent_cut_and_confirms_just_below_it() -> None:
    steps = _run_suite(_harness(98_400, truncates=True))

    assert _sizes(steps) == [32_000, 64_000, 128_000, 93_000]
    verdict = suite_verdict(steps)
    assert verdict["verdict"] == "truncates"
    assert verdict["largest_complete_chars"] == 93_000
    assert verdict["smallest_failing_chars"] == 98_400
    assert verdict["recommended_chunk_chars"] == 69_000


def test_suite_walks_down_when_the_start_size_is_already_too_big() -> None:
    steps = _run_suite(_harness(12_999, truncates=False))

    assert _sizes(steps) == [32_000, 16_000, 8_000, 12_000, 14_000]
    verdict = suite_verdict(steps)
    assert verdict["verdict"] == "rejects"
    assert (verdict["largest_complete_chars"], verdict["smallest_failing_chars"]) == (12_000, 14_000)


def test_suite_stops_at_the_floor_when_even_the_smallest_probe_fails() -> None:
    steps = _run_suite(_harness(0, truncates=False))

    assert _sizes(steps) == [32_000, 16_000, 8_000, 4_096]
    assert suite_verdict(steps)["verdict"] == "fails_small"


def test_suite_confirms_a_cut_found_on_the_first_step() -> None:
    steps = [{"target_chars": 32_000, "outcome": "truncated", "cut_at_chars": 18_200}]

    assert next_probe_step(steps) == 17_000


def test_suite_treats_unreported_altered_and_misread_steps_as_failures() -> None:
    for outcome in ("no_report", "altered", "verification_failed", "rejected"):
        steps = [{"target_chars": 32_000, "outcome": outcome, "cut_at_chars": None}]
        assert next_probe_step(steps) == 16_000, outcome


def test_suite_stops_after_the_step_budget() -> None:
    steps = [
        {"target_chars": 32_000 + index, "outcome": "verification_failed", "cut_at_chars": None}
        for index in range(7)
    ]

    assert next_probe_step(steps) is None
    assert suite_verdict(steps)["step_count"] == 7


def test_suite_verdict_tracks_cumulative_chars_and_unfinished_growth() -> None:
    steps = [
        {"target_chars": 32_000, "outcome": "complete", "cut_at_chars": None},
        {"target_chars": 64_000, "outcome": "complete", "cut_at_chars": None},
    ]

    verdict = suite_verdict(steps)
    assert verdict["verdict"] == "passes_up_to"
    assert verdict["cumulative_chars"] == 96_000
    assert suite_verdict([])["verdict"] == "no_steps"


def test_cut_point_is_the_end_of_the_last_complete_block() -> None:
    probe = build_output_probe(
        run_id="probe-test-cut",
        target_chars=12_000,
        content_profile="transcript_markdown",
        canary_factory=_canary_factory(),
    )
    blocks = probe["blocks"]

    assert cut_point_from_observation(blocks, "tail_truncated", 3, True) == blocks[3]["offset"]
    assert cut_point_from_observation(blocks, "tail_truncated", 3, False) is None
    assert cut_point_from_observation(blocks, "head_removed", 3, True) is None
    assert cut_point_from_observation(blocks, "tail_truncated", 0, True) is None


def test_step_outcome_maps_observations_and_harness_errors() -> None:
    assert step_outcome("complete") == "complete"
    assert step_outcome("tail_truncated") == "truncated"
    assert step_outcome("middle_removed") == "altered"
    assert step_outcome("verification_failed") == "verification_failed"
    assert step_outcome(None, harness_error="Tool result too large") == "rejected"
    assert step_outcome(None, reported=False) == "no_report"


def test_suite_verdict_names_the_limit_after_how_it_failed() -> None:
    def steps_failing_with(outcome: str) -> list[dict[str, Any]]:
        return [
            {"target_chars": 32_000, "outcome": "complete", "cut_at_chars": None},
            {"target_chars": 64_000, "outcome": outcome, "cut_at_chars": None},
        ]

    assert suite_verdict(steps_failing_with("altered"))["verdict"] == "drops_middle"
    assert suite_verdict(steps_failing_with("rejected"))["verdict"] == "rejects"
    assert suite_verdict(steps_failing_with("no_report"))["verdict"] == "fails_above"
    assert suite_verdict(steps_failing_with("verification_failed"))["verdict"] == "fails_above"
