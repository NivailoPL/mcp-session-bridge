import json

from app.context_packs import ContextPackStore
from app.session_package import render_session_package
from app.storage import Store


def test_store_saves_session_and_exchange(tmp_path) -> None:
    store = Store(tmp_path / "bridge.sqlite3")
    session = store.create_session("s1", "Test session", "magic-smoke")

    exchange = store.save_exchange(
        session_id=session.session_id,
        model_name="Claude",
        user_message="Czy widzisz ZIELONY KURCZAK?",
        assistant_response="Odpowiada model Claude. Widzę ZIELONY KURCZAK.",
    )

    sessions = store.list_sessions()
    exchanges = store.list_exchanges("s1")

    assert exchange.exchange_id == 1
    assert sessions[0]["exchange_count"] == 1
    assert exchanges[0].assistant_response.startswith("Odpowiada model Claude")


def test_session_package_contains_context_and_transcript(tmp_path) -> None:
    pack_dir = tmp_path / "magic-smoke"
    pack_dir.mkdir()
    (pack_dir / "01.md").write_text("ZIELONY KURCZAK", encoding="utf-8")
    (pack_dir / "manifest.json").write_text(
        json.dumps({"name": "Magic Smoke", "files": ["01.md"]}),
        encoding="utf-8",
    )
    pack = ContextPackStore(tmp_path).load_pack("magic-smoke")
    store = Store(tmp_path / "bridge.sqlite3")
    session = store.create_session("s1", "Magic test", "magic-smoke")
    store.save_exchange("s1", "ChatGPT", "Dodaj zwrot.", "Odpowiada model ChatGPT. FIOLETOWA LATARNIA.")

    package = render_session_package(session, pack, store.list_exchanges("s1"))

    assert package["ok"] if "ok" in package else True
    assert "ZIELONY KURCZAK" in package["package_markdown"]
    assert "FIOLETOWA LATARNIA" in package["package_markdown"]
    assert package["exchange_count"] == 1
    assert len(package["sha256"]) == 64
