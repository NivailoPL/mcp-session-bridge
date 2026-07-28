from app.output_probe import build_output_probe, classify_probe_observation, probe_recommendations


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
