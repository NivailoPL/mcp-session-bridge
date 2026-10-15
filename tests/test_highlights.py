import pytest

from app.highlights import (
    STATUS_OK,
    STATUS_SOURCE_CHANGED,
    STATUS_SOURCE_EXCLUDED,
    STATUS_SOURCE_MASKED,
    HighlightError,
    normalize_for_match,
)
from app.storage import SCHEMA_VERSION, Store

SESSION = "20261015-200000-organic-synthetic-a1b2c3"
OTHER = "20261013-090000-caddy-limity-d4e5f6"


@pytest.fixture()
def store(tmp_path) -> Store:
    store = Store(tmp_path / "bridge.sqlite3")
    store.create_session(SESSION, "Organic vs synthetic")
    store.create_session(OTHER, "Caddy")
    return store


def exchange(store: Store, user: str = "Pytanie usera.", model: str = "Odpowiedź modelu.", session: str = SESSION):
    return store.save_exchange(session, "Claude Opus 5.5", user, model)


def test_quote_from_rendered_bold_text_matches_stored_markdown(store):
    ex = exchange(store, model="To jest **bliskość człowieka** do generacji.")
    highlight = store.create_highlight(SESSION, ex.exchange_id, "model", "bliskość człowieka do generacji")
    assert highlight["status"] == STATUS_OK
    assert highlight["quote"] == "bliskość człowieka do generacji"


def test_whitespace_line_breaks_and_links_are_tolerated(store):
    ex = exchange(store, user="Pierwsza linia\n\n- druga   linia z [linkiem](https://example.test)")
    highlight = store.create_highlight(SESSION, ex.exchange_id, "user", "Pierwsza linia druga linia z linkiem")
    assert highlight["side"] == "user"


def test_quote_missing_from_turn_is_rejected(store):
    ex = exchange(store, user="Tylko to.", model="Kotwica to dotyk człowieka.")
    with pytest.raises(HighlightError) as error:
        store.create_highlight(SESSION, ex.exchange_id, "user", "dotyk człowieka")
    assert error.value.code == "quote_not_found"


def test_exchange_from_another_session_is_rejected(store):
    ex = exchange(store, session=OTHER)
    with pytest.raises(HighlightError) as error:
        store.create_highlight(SESSION, ex.exchange_id, "user", "Pytanie usera")
    assert error.value.code == "exchange_unknown"


def test_deleted_exchange_and_masked_response_are_rejected(store):
    deleted = exchange(store)
    store.delete_exchange(deleted.exchange_id, reason="noise")
    with pytest.raises(HighlightError):
        store.create_highlight(SESSION, deleted.exchange_id, "user", "Pytanie usera")
    masked = exchange(store)
    store.mask_exchange_response(masked.exchange_id)
    with pytest.raises(HighlightError) as error:
        store.create_highlight(SESSION, masked.exchange_id, "model", "Odpowiedź modelu")
    assert error.value.code == "source_masked"


@pytest.mark.parametrize("quote", ["", "  ", "ab", "x" * 2_001])
def test_empty_short_and_oversized_quotes_are_rejected(store, quote):
    ex = exchange(store, user="x" * 2_100)
    with pytest.raises(HighlightError):
        store.create_highlight(SESSION, ex.exchange_id, "user", quote)


def test_same_quote_twice_returns_one_highlight(store):
    ex = exchange(store)
    first = store.create_highlight(SESSION, ex.exchange_id, "user", "Pytanie usera")
    second = store.create_highlight(SESSION, ex.exchange_id, "user", "  Pytanie   usera ")
    assert first["highlight_id"] == second["highlight_id"]
    assert len(store.list_highlights(SESSION)) == 1


def test_edited_source_keeps_quote_and_reports_changed(store):
    ex = exchange(store, model="Kotwica to dotyk człowieka.")
    store.create_highlight(SESSION, ex.exchange_id, "model", "dotyk człowieka")
    store.update_exchange(ex.exchange_id, assistant_response="Zupełnie inna odpowiedź.")
    [highlight] = store.list_highlights(SESSION)
    assert highlight["status"] == STATUS_SOURCE_CHANGED
    assert highlight["quote"] == "dotyk człowieka"


def test_excluded_and_masked_sources_are_reported(store):
    excluded = exchange(store)
    masked = exchange(store, model="Maskowana odpowiedź.")
    store.create_highlight(SESSION, excluded.exchange_id, "user", "Pytanie usera")
    store.create_highlight(SESSION, masked.exchange_id, "model", "Maskowana odpowiedź")
    store.delete_exchange(excluded.exchange_id, reason="noise")
    store.mask_exchange_response(masked.exchange_id)
    statuses = {item["exchange_id"]: item["status"] for item in store.list_highlights(SESSION)}
    assert statuses == {excluded.exchange_id: STATUS_SOURCE_EXCLUDED, masked.exchange_id: STATUS_SOURCE_MASKED}


def test_delete_highlight(store):
    ex = exchange(store)
    highlight = store.create_highlight(SESSION, ex.exchange_id, "user", "Pytanie usera")
    assert store.delete_highlight(highlight["highlight_id"]) is True
    assert store.delete_highlight(highlight["highlight_id"]) is False
    assert store.list_highlights(SESSION) == []


def test_tables_are_additive_and_schema_version_is_unchanged(store, tmp_path):
    assert SCHEMA_VERSION == 2
    assert store.schema_version() == 2


def test_normalize_for_match_drops_markup():
    assert normalize_for_match("**Bold** _and_ `code`\n\n> quote") == "bold and code quote"
