"""Run functions lifted out of admin-viewer.html under Node.

The admin viewer is a single HTML file with its JavaScript inline, so the only
way to test that JavaScript is to cut a function out of the file and execute it.
Asserting that a given source line is present proves nothing -- it passes on a
feature that is wired up wrong and fails on a rename -- so tests that care about
viewer behaviour should call :func:`run_js` and assert on the result instead.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

VIEWER_PATH = Path(__file__).parents[1] / "admin-viewer.html"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="viewer JavaScript is executed with Node, which is not installed here",
)

# A DOM small enough to run rendering helpers and inspect what they built.
DOM_STUB = r"""
class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.textContent = "";
    this.attributes = {};
    this.listeners = {};
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this.id = "";
    this._innerHTML = "";
    this.classList = {
      values: new Set(),
      add: (...names) => names.forEach((name) => this.classList.values.add(name)),
      remove: (...names) => names.forEach((name) => this.classList.values.delete(name)),
      toggle: (name, force) => {
        const next = force === undefined ? !this.classList.values.has(name) : force;
        if (next) this.classList.values.add(name);
        else this.classList.values.delete(name);
        return next;
      },
      contains: (name) => this.classList.values.has(name),
    };
    this.style = { setProperty() {} };
    this.dataset = {};
  }
  set innerHTML(value) { this._innerHTML = String(value); }
  get innerHTML() { return this._innerHTML; }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); return node; }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name]; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  querySelectorAll() { return []; }
  focus() {}
  click() { if (this.listeners.click) this.listeners.click({}); }
}
global.Element = Element;
global.document = {
  createElement: (tag) => new Element(tag),
  createTextNode: (text) => {
    const node = new Element("#text");
    node.textContent = String(text);
    return node;
  },
  querySelectorAll: () => [],
  getElementById: () => null,
};

// Serialise a node tree into something a Python assertion can read.
global.describe = function describe(node) {
  if (node === null || node === undefined) return null;
  if (!(node instanceof Element)) return { value: node };
  return {
    tag: node.tag,
    text: node.textContent,
    className: node.className || [...node.classList.values].join(" "),
    attributes: node.attributes,
    innerHTML: node._innerHTML,
    children: node.children.map(describe),
  };
};
global.flatText = function flatText(node) {
  if (!node) return "";
  if (!(node instanceof Element)) return String(node);
  return (node.textContent || "") + node.children.map(flatText).join("");
};
"""


@lru_cache(maxsize=1)
def viewer_source() -> str:
    return VIEWER_PATH.read_text(encoding="utf-8")


def slice_source(start: str, end: str) -> str:
    """Return the region of the viewer between two anchors.

    Raises a readable error when an anchor moves, so a renamed function reports
    itself as a stale test anchor instead of a confusing ValueError.
    """
    source = viewer_source()
    try:
        first = source.index(start)
    except ValueError:
        raise AssertionError(f"anchor not found in admin-viewer.html: {start!r}") from None
    try:
        last = source.index(end, first)
    except ValueError:
        raise AssertionError(f"anchor not found in admin-viewer.html: {end!r}") from None
    return source[first:last]


def run_js(script: str, *, payload: object = None, dom: bool = True) -> object:
    """Execute JavaScript under Node and return whatever it passes to ``emit``.

    ``payload`` is available to the script as ``input``; the script reports its
    result by calling ``emit(value)``.
    """
    prelude = DOM_STUB if dom else ""
    program = f"""
{prelude}
const input = {json.dumps(payload)};
function emit(value) {{ process.stdout.write(JSON.stringify(value === undefined ? null : value)); }}
{script}
"""
    completed = subprocess.run(
        ["node", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"viewer JavaScript failed under Node:\n{completed.stderr.strip()}"
        )
    if not completed.stdout:
        raise AssertionError("viewer JavaScript produced no result; did it call emit()?")
    return json.loads(completed.stdout)
