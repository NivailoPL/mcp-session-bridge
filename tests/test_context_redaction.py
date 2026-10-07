from app.context_redaction import Redactor


SESSION_A = "20261005-120000-projekt-dzialu-contexts-a1b2c3"
SESSION_B = "20260922-081530-redakcja-id-model-zagrozen-0f9e8d"
CONTEXT_A = "ctx_7hq2lm9xk3pr4tvw"
HOST = "https://mcp.example.test"


def redactor(**overrides) -> Redactor:
    values = {"session_ids": [SESSION_A, SESSION_B], "context_ids": [CONTEXT_A], "hosts": [HOST]}
    values.update(overrides)
    return Redactor(**values)


def test_known_session_id_is_replaced_with_a_numbered_placeholder() -> None:
    result = redactor().redact(f"Szkic zapisałem w sesji {SESSION_A}.")

    assert result.text == "Szkic zapisałem w sesji [SESSION-1]."
    assert [(r.kind, r.original) for r in result.redactions] == [("SESSION", SESSION_A)]


def test_same_id_keeps_one_placeholder_and_different_ids_count_up() -> None:
    text = f"{SESSION_A} then {SESSION_B} and again {SESSION_A}"

    assert redactor().redact(text).text == "[SESSION-1] then [SESSION-2] and again [SESSION-1]"


def test_numbering_starts_again_on_every_run() -> None:
    r = redactor()

    assert r.redact(SESSION_B).text == "[SESSION-1]"
    assert r.redact(f"{SESSION_A} {SESSION_B}").text == "[SESSION-1] [SESSION-2]"


def test_id_in_backticks_quotes_and_markdown_is_replaced() -> None:
    text = f"- session_id: `{SESSION_A}`\n\"{SESSION_B}\"\n**{SESSION_A}**"

    assert redactor().redact(text).text == '- session_id: `[SESSION-1]`\n"[SESSION-2]"\n**[SESSION-1]**'


def test_uppercased_id_is_replaced() -> None:
    assert redactor().redact(SESSION_A.upper()).text == "[SESSION-1]"


def test_unknown_id_with_the_session_shape_is_replaced() -> None:
    stranger = "20250101-000000-old-session-from-elsewhere-abcdef"

    result = redactor(session_ids=[]).redact(f"see {stranger}")

    assert result.text == "see [SESSION-1]"


def test_ordinary_dates_and_hex_are_left_alone() -> None:
    text = "On 20261005 at 120000 the color #a1b2c3 and commit a1b2c3 shipped."

    assert redactor().redact(text).text == text


def test_id_glued_to_letters_is_not_a_match() -> None:
    text = f"x{SESSION_A}y"

    assert redactor(session_ids=[]).redact(text).text == text


def test_short_known_ids_are_never_matched_literally() -> None:
    text = "The s1 session and the word test stay."

    assert Redactor(session_ids=["s1", "test"]).redact(text).text == text


def test_admin_link_is_replaced_as_a_whole_url() -> None:
    text = f"Open {HOST}/admin/sessions?session={SESSION_A} to check."

    result = redactor().redact(text)

    assert result.text == "Open [URL-1] to check."
    assert [r.kind for r in result.redactions] == ["URL"]


def test_link_keeps_trailing_sentence_punctuation() -> None:
    assert redactor().redact(f"Look at {HOST}/admin.").text == "Look at [URL-1]."


def test_link_without_scheme_and_bare_host_are_replaced() -> None:
    text = "deploy to mcp.example.test then mcp.example.test/admin/lab"

    assert redactor().redact(text).text == "deploy to [URL-1] then [URL-2]"


def test_same_link_with_and_without_scheme_shares_a_placeholder() -> None:
    text = f"{HOST}/admin and mcp.example.test/admin"

    assert redactor().redact(text).text == "[URL-1] and [URL-1]"


def test_other_hosts_are_left_alone() -> None:
    text = "See https://example.org/docs and notmcp.example.test.org"

    assert redactor().redact(text).text == text


def test_link_inside_markdown_link_and_parentheses() -> None:
    text = f"[session]({HOST}/admin/sessions/{SESSION_A})"

    assert redactor().redact(text).text == "[session]([URL-1])"


def test_context_ids_are_replaced_known_or_not() -> None:
    text = f"Load {CONTEXT_A} or ctx_Zz9yy8xx7ww6 please"

    assert redactor().redact(text).text == "Load [CONTEXT-1] or [CONTEXT-2] please"


def test_word_ctx_alone_is_left_alone() -> None:
    text = "the ctx_ prefix and ctx_ab are fine"

    assert redactor().redact(text).text == text


def test_redaction_spans_point_into_the_output_text() -> None:
    result = redactor().redact(f"a {SESSION_A} b {CONTEXT_A}")

    for redaction in result.redactions:
        assert result.text[redaction.start:redaction.end] == redaction.placeholder


def test_find_reports_ids_without_changing_text() -> None:
    text = f"{SESSION_A} {CONTEXT_A} {HOST}/admin"

    found = redactor().find(text)

    assert found == {"SESSION": [SESSION_A], "CONTEXT": [CONTEXT_A]}


def test_find_lowercases_session_ids() -> None:
    assert redactor().find(SESSION_A.upper()) == {"SESSION": [SESSION_A]}


def test_no_hosts_still_redacts_ids() -> None:
    result = Redactor(session_ids=[SESSION_A]).redact(f"https://mcp.example.test/admin/{SESSION_A}")

    assert result.text == "https://mcp.example.test/admin/[SESSION-1]"


def test_find_reports_ids_hidden_inside_a_link() -> None:
    found = redactor().find(f"{HOST}/admin/sessions?session={SESSION_B}")

    assert found == {"SESSION": [SESSION_B]}
