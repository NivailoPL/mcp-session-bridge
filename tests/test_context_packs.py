import json

from app.context_packs import ContextPackStore


def test_context_pack_loads_manifest_order(tmp_path) -> None:
    pack_dir = tmp_path / "magic-smoke"
    pack_dir.mkdir()
    (pack_dir / "00-system.md").write_text("Test instruction.", encoding="utf-8")
    (pack_dir / "01-green.md").write_text("GREEN NOTE", encoding="utf-8")
    (pack_dir / "02-blue.md").write_text("BLUE NOTE", encoding="utf-8")
    (pack_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "Magic Smoke",
                "instructions_file": "00-system.md",
                "files": [
                    {"path": "02-blue.md", "title": "Second first"},
                    {"path": "01-green.md", "title": "First second"},
                ],
            }
        ),
        encoding="utf-8",
    )

    pack = ContextPackStore(tmp_path).load_pack("magic-smoke")

    assert pack.name == "Magic Smoke"
    assert pack.instructions == "Test instruction."
    assert [file.path for file in pack.files] == ["02-blue.md", "01-green.md"]
    assert "BLUE NOTE" in pack.files[0].content
    assert "GREEN NOTE" in pack.files[1].content


def test_context_pack_listing_includes_hash(tmp_path) -> None:
    pack_dir = tmp_path / "magic-smoke"
    pack_dir.mkdir()
    (pack_dir / "01.md").write_text("SILVER NOTE", encoding="utf-8")
    (pack_dir / "manifest.json").write_text(
        json.dumps({"name": "Magic Smoke", "files": ["01.md"]}),
        encoding="utf-8",
    )

    packs = ContextPackStore(tmp_path).list_packs()

    assert packs[0]["context_pack_id"] == "magic-smoke"
    assert packs[0]["file_count"] == 1
    assert len(packs[0]["sha256"]) == 64


def test_context_pack_saves_summary_and_updates_manifest(tmp_path) -> None:
    pack_dir = tmp_path / "magic-smoke"
    pack_dir.mkdir()
    for index in range(1, 5):
        (pack_dir / f"0{index}.md").write_text(f"File {index}", encoding="utf-8")
    (pack_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "Magic Smoke",
                "files": [
                    {"path": "01.md", "title": "First"},
                    {"path": "02.md", "title": "Second"},
                    {"path": "03.md", "title": "Third"},
                    {"path": "04.md", "title": "Fourth"},
                ],
            }
        ),
        encoding="utf-8",
    )
    store = ContextPackStore(tmp_path)

    result = store.save_context_summary(
        pack_id="magic-smoke",
        session_id="20260527-101500-rynek-pracy-abcdef",
        model_name="Claude",
        summary_markdown="## Key findings\n\nThe user is testing context summary storage.",
    )

    manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    saved_path = result["path"]
    reloaded_pack = store.load_pack("magic-smoke")

    assert result["context_file_count"] == 5
    assert result["manifest_updated"] is True
    assert saved_path.startswith("05-context-summary-")
    assert saved_path.endswith("-20260527-101500-rynek-pracy-abcdef.md")
    assert manifest["files"][-1] == {
        "path": saved_path,
        "title": "Context summary - Claude",
    }
    assert (pack_dir / saved_path).read_text(encoding="utf-8").strip() == (
        "## Key findings\n\nThe user is testing context summary storage."
    )
    assert reloaded_pack.files[-1].path == saved_path
    assert reloaded_pack.files[-1].content == "## Key findings\n\nThe user is testing context summary storage."
