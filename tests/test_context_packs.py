import json

from app.context_packs import ContextPackStore


def test_context_pack_loads_manifest_order(tmp_path) -> None:
    pack_dir = tmp_path / "magic-smoke"
    pack_dir.mkdir()
    (pack_dir / "00-system.md").write_text("Instrukcja testowa.", encoding="utf-8")
    (pack_dir / "01-green.md").write_text("ZIELONY KURCZAK", encoding="utf-8")
    (pack_dir / "02-blue.md").write_text("NIEBIESKI CZAJNIK", encoding="utf-8")
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
    assert pack.instructions == "Instrukcja testowa."
    assert [file.path for file in pack.files] == ["02-blue.md", "01-green.md"]
    assert "NIEBIESKI CZAJNIK" in pack.files[0].content
    assert "ZIELONY KURCZAK" in pack.files[1].content


def test_context_pack_listing_includes_hash(tmp_path) -> None:
    pack_dir = tmp_path / "magic-smoke"
    pack_dir.mkdir()
    (pack_dir / "01.md").write_text("SREBRNY PARASOL", encoding="utf-8")
    (pack_dir / "manifest.json").write_text(
        json.dumps({"name": "Magic Smoke", "files": ["01.md"]}),
        encoding="utf-8",
    )

    packs = ContextPackStore(tmp_path).list_packs()

    assert packs[0]["context_pack_id"] == "magic-smoke"
    assert packs[0]["file_count"] == 1
    assert len(packs[0]["sha256"]) == 64
