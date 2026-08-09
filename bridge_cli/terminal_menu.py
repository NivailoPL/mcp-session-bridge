from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


STATUS_COLORS = {
    "ACTIVE": "\x1b[38;2;34;197;94m",
    "HEALTHY": "\x1b[38;2;34;197;94m",
    "PASS": "\x1b[38;2;34;197;94m",
    "READY": "\x1b[38;2;56;189;248m",
    "DETECTED": "\x1b[38;2;250;204;21m",
    "NEEDS INPUT": "\x1b[38;2;251;146;60m",
    "ATTENTION": "\x1b[38;2;251;146;60m",
    "WAITING": "\x1b[38;2;167;139;250m",
    "FAILED": "\x1b[38;2;248;113;113m",
    "NOT INSTALLED": "\x1b[38;2;148;163;184m",
    "NOT STARTED": "\x1b[38;2;148;163;184m",
}
RESET = "\x1b[0m"
INDYGO = "\x1b[38;2;99;102;241m"


def render_status(status: str, *, color: bool, resume: str = "") -> str:
    text = f"({status})"
    if not color:
        return text
    prefix = STATUS_COLORS.get(status, "\x1b[38;2;148;163;184m")
    return f"{prefix}{text}{RESET}{resume}"


@dataclass(frozen=True)
class MenuItem:
    id: str
    label: str
    detail: str = ""
    status: str = ""
    disabled_reason: str | None = None


class MenuDriver(Protocol):
    def choose(self, title: str, items: list[MenuItem], *, selected_id: str | None = None) -> str | None: ...


class PlainMenuDriver:
    def __init__(self, input_fn: Callable[[str], str] = input, output_fn: Callable[[str], None] = print):
        self.input = input_fn
        self.output = output_fn

    def choose(self, title: str, items: list[MenuItem], *, selected_id: str | None = None) -> str | None:
        self.output(title)
        for index, item in enumerate(items, start=1):
            status = f" {render_status(item.status, color=False)}" if item.status else ""
            disabled = f" (unavailable: {item.disabled_reason})" if item.disabled_reason else ""
            self.output(f"  {index}. {item.label}{status}{disabled}")
            if item.detail:
                self.output(f"     {item.detail}")
        default = next((index for index, item in enumerate(items, 1) if item.id == selected_id), 1)
        raw = self.input(f"Choose [{default}]: ").strip().lower()
        if raw in {"q", "quit", "exit"}:
            return None
        selection = int(raw) if raw.isdigit() else default if not raw else 0
        if selection not in range(1, len(items) + 1):
            return ""
        item = items[selection - 1]
        return "" if item.disabled_reason else item.id


class PromptToolkitMenuDriver:
    """Small prompt-toolkit adapter; business behavior lives in the controller."""

    def choose(self, title: str, items: list[MenuItem], *, selected_id: str | None = None) -> str | None:
        from prompt_toolkit.application import Application
        from prompt_toolkit.formatted_text import ANSI
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout as PromptLayout
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.layout.containers import Window

        enabled = [index for index, item in enumerate(items) if not item.disabled_reason]
        cursor = next((index for index, item in enumerate(items) if item.id == selected_id), enabled[0] if enabled else 0)

        def render() -> ANSI:
            heading = title
            for status_name in STATUS_COLORS:
                heading = heading.replace(
                    f"({status_name})",
                    render_status(status_name, color=True, resume=INDYGO),
                )
            lines = [f"{INDYGO}{heading}{RESET}", ""]
            for index, item in enumerate(items):
                pointer = "›" if index == cursor else " "
                mark = "*" if item.status in {"READY", "ACTIVE", "HEALTHY", "PASS"} else " "
                status = f"  {render_status(item.status, color=True)}" if item.status else ""
                disabled = f" — unavailable: {item.disabled_reason}" if item.disabled_reason else ""
                lines.append(f" {pointer} [{mark}] {item.label}{status}{disabled}")
                if item.detail:
                    lines.append(f"       {item.detail}")
            lines.extend(["", " ↑/↓ move   Enter select   Esc/q return"])
            return ANSI("\n".join(lines))

        control = FormattedTextControl(render)
        bindings = KeyBindings()

        def move(delta: int) -> None:
            nonlocal cursor
            if not enabled:
                return
            position = enabled.index(cursor) if cursor in enabled else 0
            cursor = enabled[(position + delta) % len(enabled)]

        @bindings.add("up")
        def _up(event) -> None:
            move(-1)
            event.app.invalidate()

        @bindings.add("down")
        def _down(event) -> None:
            move(1)
            event.app.invalidate()

        @bindings.add("enter")
        def _enter(event) -> None:
            if items and not items[cursor].disabled_reason:
                event.app.exit(result=items[cursor].id)

        @bindings.add("escape")
        @bindings.add("q")
        def _return(event) -> None:
            event.app.exit(result=None)

        @bindings.add("c-c")
        def _interrupt(event) -> None:
            event.app.exit(exception=KeyboardInterrupt())

        application: Application[str | None] = Application(
            layout=PromptLayout(Window(control)),
            key_bindings=bindings,
            full_screen=False,
            erase_when_done=True,
        )
        return application.run()
