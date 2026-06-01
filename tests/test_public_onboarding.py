import os
import subprocess
import sys
from pathlib import Path


def test_env_example_contains_required_public_settings() -> None:
    text = Path(".env.example").read_text(encoding="utf-8")

    assert "BRIDGE_PUBLIC_BASE_URL=http://127.0.0.1:8787" in text
    assert "BRIDGE_OWNER_USERNAME=owner" in text
    assert "BRIDGE_OWNER_PASSWORD_HASH=" in text
    assert "BRIDGE_SECRET_KEY=" in text
    assert "panch" + "murka" not in text
    assert "woj" + "tek" not in text.lower()


def test_demo_session_script_creates_expected_files(tmp_path) -> None:
    output_dir = tmp_path / "demo-output"
    env = {**os.environ, "PYTHONPATH": str(Path.cwd())}

    result = subprocess.run(
        [sys.executable, "scripts/demo_session.py", "--output-dir", str(output_dir)],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    transcript_path = output_dir / "demo-transcript.md"
    session_json_path = output_dir / "demo-session.json"

    assert "Created session: demo-" in result.stdout
    assert "Saved exchange #1: USER -> Claude" in result.stdout
    assert "Saved exchange #2: USER -> GPT" in result.stdout
    assert "Overview: 2 exchanges, 4 turns, 1 transcript chunk" in result.stdout
    assert "OK" in result.stdout
    assert transcript_path.exists()
    assert session_json_path.exists()
    assert "# MCP Session Bridge Session Transcript" in transcript_path.read_text(encoding="utf-8")


def test_public_docs_are_linked_and_anonymized() -> None:
    expected_docs = [
        "docs/installation.md",
        "docs/client-setup.md",
        "docs/model-instructions.md",
        "docs/deployment.md",
        "docs/security.md",
        "docs/limitations.md",
        "docs/operations.md",
    ]
    expected_repo_files = [
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CHANGELOG.md",
        "LICENSE",
    ]
    readme = Path("README.md").read_text(encoding="utf-8")

    for doc_path in expected_docs:
        path = Path(doc_path)
        assert path.exists(), f"Missing {doc_path}"
        assert f"({doc_path})" in readme

    for file_path in expected_repo_files:
        path = Path(file_path)
        assert path.exists(), f"Missing {file_path}"
        assert f"({file_path})" in readme

    public_paths = [
        Path("README.md"),
        Path(".env.example"),
        *map(Path, expected_docs),
        *map(Path, expected_repo_files),
        Path("docs/project-prompt-template.md"),
    ]
    forbidden = ["WW" + "-MCP", "Woj" + "tek", "panch" + "murka", "89." + "167.57.190"]
    for path in public_paths:
        text = path.read_text(encoding="utf-8")
        for value in forbidden:
            assert value not in text, f"{value!r} leaked into {path}"


def test_github_actions_runs_tests_and_demo() -> None:
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")

    assert "uv sync --frozen" in workflow
    assert "uv run pytest" in workflow
    assert "uv run python scripts/demo_session.py" in workflow
    assert "pull_request:" in workflow
    assert "workflow_dispatch:" in workflow
