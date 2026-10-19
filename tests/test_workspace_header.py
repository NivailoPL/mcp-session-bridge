"""Every workspace shares one header: Search, Settings and the owner, no Log out."""
from __future__ import annotations

from pathlib import Path

import pytest

PAGES = {
    "web/admin-viewer.html": "sessions",
    "web/contexts-viewer.html": "contexts",
    "web/topics-viewer.html": "topics",
}


@pytest.mark.parametrize(("path", "workspace"), PAGES.items())
def test_header_actions_are_the_same_everywhere(path: str, workspace: str) -> None:
    page = Path(path).read_text(encoding="utf-8")
    header = page[page.index('<header class="workspace-bar">') : page.index("</header>")]
    assert 'data-header-action="search"' in header and "<kbd>⌘K</kbd>" in header
    assert 'data-header-action="settings"' in header and "data-header-dot" in header
    assert "data-header-user" in header
    assert "/admin/logout" not in header
    assert f'data-workspace="{workspace}"' in page
    assert page.count('src="/admin/assets/admin-header.js"') == 1


def test_sessions_sidebar_no_longer_holds_search_and_settings() -> None:
    page = Path("web/admin-viewer.html").read_text(encoding="utf-8")
    assert 'class="settings-row"' not in page
    header = page[page.index('<header class="workspace-bar">') : page.index("</header>")]
    assert 'id="searchOpenButton"' in header and 'id="aiSettingsButton"' in header
    assert 'data-settings-tab="topics"' in page and 'data-settings-panel="topics"' in page


def test_settings_can_be_framed_only_by_bridge_itself(load_main, admin_client) -> None:
    main = load_main()
    client, _ = admin_client(main)
    plain = client.get("/admin/sessions")
    assert plain.headers["x-frame-options"] == "DENY"
    framed = client.get("/admin/sessions?embed=settings&tab=topics")
    assert framed.headers["x-frame-options"] == "SAMEORIGIN"
    assert framed.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert client.get("/admin/assets/admin-header.js").status_code == 200
