"""Session titles: the AI rename chain and automatic naming of new sessions.

A rename asks Codex first and, when Codex fails, the OpenAI API. Automatic
naming runs in the background after an exchange is saved, only for sessions
created after it was switched on, only while the session still carries its
title cut from its first message, and at most once per exchange for the first few exchanges.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from app.codex_app_server import CodexAppServerError, CodexUnavailableError
from app.storage import Store
from app.strands import STRAND_EFFORTS

logger = logging.getLogger(__name__)

SESSION_TITLE_MAX_CHARS = 72
AI_RENAME_MAX_WORDS_SETTING = "ai_rename.max_words"
AI_RENAME_DEFAULT_MAX_WORDS = 6
AI_RENAME_MIN_MAX_WORDS = 2
AI_RENAME_MAX_MAX_WORDS = 10
CODEX_MODELS_SETTING_KEY = "codex.models.catalog"
TITLE_SETTINGS_KEY = "titles.settings"
# Automatic naming tries on each of the first few exchanges, then leaves the session alone.
AUTO_TITLE_MAX_EXCHANGES = 3
# A busy Codex is waited for, never replaced by the paid API.
CODEX_BUSY_RETRIES = 3
CODEX_BUSY_WAIT_SECONDS = 30.0
CODEX_TITLE_TIMEOUT_SECONDS = 90.0
CODEX_MODEL_MAX_CHARS = 96
TITLE_INPUT_MAX_CHARS = 4000
# Estimate only: Codex does not report usage. The rename instructions are about this long.
TITLE_PROMPT_TOKENS = 250
TITLE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
    "additionalProperties": False,
}

# Words a title must not end on once it has been cut to fit (Polish and English).
TITLE_DANGLING_WORDS = frozenset({
    "a", "ale", "albo", "ani", "bo", "czy", "dla", "do", "i", "jak", "lub", "na", "nad", "o", "od", "oraz",
    "po", "pod", "przez", "przy", "u", "w", "we", "z", "za", "ze", "że",
    "an", "and", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "vs", "with",
})


class TitleError(ValueError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class CodexBusy(Exception):
    """Codex has no free conversation slot right now."""


def default_title_settings() -> dict[str, Any]:
    return {
        "auto_enabled": False,
        "enabled_since": None,
        # empty means "the newest Luna in the Codex model list"
        "codex_model": "",
        "codex_effort": "low",
        "api_fallback": False,
        "show_ai_button": True,
        "include_sensitive": False,
    }


def validate_title_settings(payload: Any, current: dict[str, Any], *, now: int) -> dict[str, Any]:
    allowed = {"auto_enabled", "codex_model", "codex_effort", "api_fallback", "show_ai_button", "include_sensitive"}
    if not isinstance(payload, dict) or not payload or not set(payload) <= allowed:
        raise TitleError(
            "Send auto_enabled, codex_model, codex_effort, api_fallback, show_ai_button and/or include_sensitive.",
            code="settings_invalid",
        )
    result = dict(current)
    for key in ("auto_enabled", "api_fallback", "show_ai_button", "include_sensitive"):
        if key in payload:
            if not isinstance(payload[key], bool):
                raise TitleError(f"{key} must be true or false.", code="settings_invalid")
            result[key] = payload[key]
    if "codex_model" in payload:
        model = payload["codex_model"]
        if not isinstance(model, str):
            raise TitleError("codex_model must be text.", code="model_invalid")
        model = model.strip()
        if len(model) > CODEX_MODEL_MAX_CHARS or (model and not re.fullmatch(r"[A-Za-z0-9._-]+", model)):
            raise TitleError("codex_model must be a valid model identifier.", code="model_invalid")
        result["codex_model"] = model
    if "codex_effort" in payload:
        if payload["codex_effort"] not in STRAND_EFFORTS:
            raise TitleError("codex_effort must be low, medium, high, xhigh, max or ultra.", code="effort_invalid")
        result["codex_effort"] = payload["codex_effort"]
    # Only sessions created from the moment it is switched on get named automatically.
    if result["auto_enabled"] and not current.get("auto_enabled"):
        result["enabled_since"] = now
    if not result["auto_enabled"]:
        result["enabled_since"] = None
    return result


def title_tokens(first_user_message: str) -> int:
    """Estimated tokens of one naming request: the message as sent plus the instructions."""
    return min(len(first_user_message), TITLE_INPUT_MAX_CHARS) // 4 + TITLE_PROMPT_TOKENS


def rename_prompt(max_words: int) -> str:
    return (
        "You name conversation sessions for a sidebar list. Read the first user message and write a title "
        "that tells the reader what the conversation is about.\n"
        f"Hard limits: at most {max_words} words and at most {SESSION_TITLE_MAX_CHARS} characters. "
        "Count the words before answering.\n"
        "Condense the meaning instead of shortening a sentence: keep the specific subject, drop filler. "
        "Do not start with words that only say it is a conversation, such as \"Session\", \"Conversation about\", "
        "\"Question about\", \"Sesja\", \"Rozmowa o\" or \"Pytanie o\". "
        "The title must be a complete phrase and must not end with a conjunction or preposition.\n"
        "Write in the language of the user message.\n"
        "Examples (limit 5 words): "
        "\"Brainstorming session about AI tools and how our team could use them\" becomes "
        "\"AI tools for the team\"; "
        "\"Sesja brainstormingu o AI i jego zastosowaniach w firmie\" becomes \"Zastosowania AI w firmie\".\n"
        "Return JSON only with one key: title. No markdown, no quotes around the title, no trailing ellipsis, "
        "no bracket noise."
    )


def parse_ai_title(content: str) -> str:
    raw = content.strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            raw = str(parsed.get("title", ""))
    except json.JSONDecodeError:
        pass
    return clean_title(raw)


def clean_title(raw: str) -> str:
    title = " ".join(raw.strip().strip(chr(34) + chr(39)).split())
    return title.rstrip(" .,-;:...")


def title_fits(title: str, max_words: int) -> bool:
    return len(title.split()) <= max_words and len(title) <= SESSION_TITLE_MAX_CHARS


def fit_title(title: str, max_words: int) -> str:
    """Last resort when the model overshoots: cut on word boundaries."""
    if title_fits(title, max_words):
        return title
    words = title.split()[:max_words]
    while len(" ".join(words)) > SESSION_TITLE_MAX_CHARS and len(words) > 1:
        words.pop()
    while len(words) > 1 and words[-1].lower().strip(" .,-;:") in TITLE_DANGLING_WORDS:
        words.pop()
    return " ".join(words).rstrip(" .,-;:...")[:SESSION_TITLE_MAX_CHARS]


def newest_luna(models: list[dict[str, Any]]) -> str:
    """The Luna model with the highest version number, else the catalog default."""

    def version(name: str) -> tuple[int, ...]:
        match = re.search(r"(\d+(?:\.\d+)*)", name)
        return tuple(int(part) for part in match.group(1).split(".")) if match else ()

    lunas = [m["model"] for m in models if "luna" in str(m.get("model", "")).lower()]
    if lunas:
        return max(lunas, key=version)
    for model in models:
        if model.get("is_default"):
            return str(model["model"])
    return ""


class SessionTitleRuntime:
    def __init__(
        self,
        store: Store,
        codex: Any | None,
        *,
        api_configured: Callable[[], bool],
        api_suggest: Callable[[str, int], str],
        fallback_model: Callable[[], str],
    ):
        self.store = store
        self.codex = codex
        self._api_configured = api_configured
        self._api_suggest = api_suggest
        self._fallback_model = fallback_model
        self._loop: asyncio.AbstractEventLoop | None = None
        self._in_flight: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self.busy_wait_seconds = CODEX_BUSY_WAIT_SECONDS

    # ---------- settings ----------

    def get_settings(self) -> dict[str, Any]:
        settings = default_title_settings()
        raw = self.store.get_app_setting(TITLE_SETTINGS_KEY)
        if raw:
            try:
                stored = json.loads(raw)
            except json.JSONDecodeError:
                stored = {}
            if isinstance(stored, dict):
                settings.update({key: stored[key] for key in settings if key in stored})
        return settings

    def save_settings(self, payload: Any) -> dict[str, Any]:
        settings = validate_title_settings(payload, self.get_settings(), now=int(time.time()))
        self.store.set_app_setting(TITLE_SETTINGS_KEY, json.dumps(settings))
        return settings

    def max_words(self) -> int:
        try:
            value = int(self.store.get_app_setting(AI_RENAME_MAX_WORDS_SETTING) or AI_RENAME_DEFAULT_MAX_WORDS)
        except ValueError:
            return AI_RENAME_DEFAULT_MAX_WORDS
        return min(max(value, AI_RENAME_MIN_MAX_WORDS), AI_RENAME_MAX_MAX_WORDS)

    def resolved_codex_model(self) -> str:
        chosen = self.get_settings()["codex_model"]
        if chosen:
            return chosen
        return newest_luna(self._catalog()) or self._fallback_model()

    def _catalog(self) -> list[dict[str, Any]]:
        raw = self.store.get_app_setting(CODEX_MODELS_SETTING_KEY)
        try:
            catalog = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            catalog = {}
        models = catalog.get("models") if isinstance(catalog, dict) else None
        return [m for m in models or [] if isinstance(m, dict) and m.get("model")]

    def payload(self) -> dict[str, Any]:
        settings = self.get_settings()
        return {
            **settings,
            "resolved_codex_model": self.resolved_codex_model(),
            "codex_configured": self.codex is not None,
            "api_configured": self._api_configured(),
            "max_words": self.max_words(),
            "auto_max_exchanges": AUTO_TITLE_MAX_EXCHANGES,
        }

    # ---------- the rename chain ----------

    async def suggest(self, first_user_message: str, *, use_api: bool) -> tuple[str, str]:
        """A title and the provider that wrote it ("codex" or "api")."""
        max_words = self.max_words()
        codex_error: Exception | None = None
        if self.codex is not None:
            try:
                return await self._codex_title(first_user_message, max_words), "codex"
            except CodexBusy:
                raise
            except (CodexAppServerError, TitleError, ValueError) as exc:
                codex_error = exc
                logger.info("Codex rename failed, %s: %s", "trying the API" if use_api else "no fallback", exc)
        if use_api and self._api_configured():
            title = await asyncio.to_thread(self._api_suggest, first_user_message, max_words)
            return title, "api"
        if codex_error is not None and not isinstance(codex_error, CodexUnavailableError):
            raise TitleError(f"Codex could not name the session: {codex_error}", code="codex_failed")
        raise TitleError("Sign in to Codex or add an OpenAI key to rename with AI.", code="not_configured")

    async def _codex_title(self, first_user_message: str, max_words: int) -> str:
        assert self.codex is not None
        settings = self.get_settings()
        prompt = (
            f"{rename_prompt(max_words)}\n\nFirst user message:\n<<<\n"
            f"{first_user_message[:TITLE_INPUT_MAX_CHARS]}\n>>>"
        )
        try:
            raw = await self.codex.extract_structured(
                prompt,
                model=self.resolved_codex_model(),
                effort=settings["codex_effort"],
                output_schema=json.loads(json.dumps(TITLE_OUTPUT_SCHEMA)),
                timeout_seconds=CODEX_TITLE_TIMEOUT_SECONDS,
            )
        except ValueError as exc:
            # The client raises ValueError when every conversation slot is taken.
            if "Codex" in str(exc) and ("active" in str(exc) or "in progress" in str(exc)):
                raise CodexBusy(str(exc)) from exc
            raise
        title = fit_title(clean_title(str(raw.get("title") or "")), max_words)
        if not title:
            raise TitleError("Codex returned an empty title.", code="codex_failed")
        return title

    async def rename_now(self, session_id: str) -> tuple[Any, str]:
        """The AI rename button: Codex first, then the API, whatever the fallback switch says."""
        session = await asyncio.to_thread(self.store.get_session, session_id)
        if session is None:
            raise TitleError(f"Unknown session_id: {session_id}", code="session_unknown")
        exchanges = await asyncio.to_thread(self.store.list_exchanges, session_id)
        if not exchanges:
            raise TitleError("Session has no user message to rename from.", code="no_message")
        try:
            title, provider = await self.suggest(exchanges[0].user_message, use_api=True)
        except CodexBusy:
            if not self._api_configured():
                raise TitleError("Codex is busy. Try again in a moment.", code="codex_busy") from None
            title = await asyncio.to_thread(self._api_suggest, exchanges[0].user_message, self.max_words())
            provider = "api"
        updated = await asyncio.to_thread(self.store.set_session_title, session_id, title, "ai")
        return updated, provider

    # ---------- automatic naming ----------

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def notify_exchange_saved(self, session_id: str) -> None:
        """Called after save_exchange. Never raises and never blocks the tool call."""
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                self._start(session_id)
            elif self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._start, session_id)
        except Exception:
            logger.exception("Could not queue automatic naming for %s", session_id)

    def _start(self, session_id: str) -> None:
        if session_id in self._in_flight:
            return
        self._in_flight.add(session_id)
        task = asyncio.get_running_loop().create_task(self._auto_title(session_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def auto_candidate(self, session_id: str) -> str | None:
        """The first user message when this session should be named now, else None."""
        settings = self.get_settings()
        if not settings["auto_enabled"] or settings["enabled_since"] is None:
            return None
        if not self.store.get_token_features()["enabled"]:
            return None
        session = self.store.get_session(session_id)
        if session is None or session.title_source != "derived" or session.created_at < settings["enabled_since"]:
            return None
        group = self.store.get_session_group(session.group_id)
        if group is not None and group.is_sensitive and not settings["include_sensitive"]:
            return None
        exchanges = self.store.list_exchanges(session_id)
        if not exchanges or len(exchanges) > AUTO_TITLE_MAX_EXCHANGES:
            return None
        return exchanges[0].user_message

    async def _auto_title(self, session_id: str) -> None:
        try:
            message = await asyncio.to_thread(self.auto_candidate, session_id)
            if message is None:
                return
            use_api = bool(self.get_settings()["api_fallback"])
            for attempt in range(CODEX_BUSY_RETRIES + 1):
                try:
                    title, provider = await self.suggest(message, use_api=use_api)
                    break
                except CodexBusy:
                    if attempt == CODEX_BUSY_RETRIES:
                        logger.info("Codex stayed busy; %s keeps its title until its next exchange", session_id)
                        return
                    await asyncio.sleep(self.busy_wait_seconds)
            applied = await asyncio.to_thread(
                lambda: self.store.set_auto_session_title(
                    session_id, title, provider=provider, estimated_tokens=title_tokens(message)
                )
            )
            if applied:
                logger.info("Session %s named automatically by %s", session_id, provider)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("Automatic naming of %s failed: %s", session_id, exc)
        finally:
            self._in_flight.discard(session_id)

    async def close(self) -> None:
        tasks = tuple(task for task in self._tasks if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
