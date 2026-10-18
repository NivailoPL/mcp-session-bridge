/* ------------------------------------------------------------------
   MCP Session Bridge — shared workspace header actions

   Every workspace bar carries Search (⌘K), Settings and the owner name.
   Sessions owns the Search and Settings dialogs and wires its own buttons.
   Other workspaces open Settings as the same Sessions overlay in a frame,
   and Search on Sessions (the search redesign comes later).
------------------------------------------------------------------- */
(() => {
  "use strict";

  const body = document.body;
  const workspace = body.dataset.workspace || "";
  const ownsDialogs = workspace === "sessions";
  const searchButton = document.querySelector('[data-header-action="search"]');
  const settingsButton = document.querySelector('[data-header-action="settings"]');
  const userNode = document.querySelector("[data-header-user]");
  const dot = document.querySelector("[data-header-dot]");

  async function getJson(path) {
    const response = await fetch(path, { credentials: "same-origin" });
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
  }

  function openSettingsFrame() {
    if (document.querySelector(".wa-settings-frame")) return;
    const frame = document.createElement("iframe");
    frame.className = "wa-settings-frame";
    frame.title = "Settings";
    const tab = body.dataset.settingsTab || "general";
    frame.src = `/admin/sessions?embed=settings&tab=${encodeURIComponent(tab)}`;
    body.append(frame);
    frame.addEventListener("load", () => frame.focus());
  }

  window.addEventListener("message", (event) => {
    if (event.origin !== window.location.origin) return;
    if (event.data && event.data.type === "bridge-settings-closed") {
      document.querySelector(".wa-settings-frame")?.remove();
      settingsButton?.focus();
      if (event.data.changed && typeof window.onBridgeSettingsChanged === "function") window.onBridgeSettingsChanged();
    }
  });

  if (!ownsDialogs) {
    searchButton?.addEventListener("click", () => window.location.assign("/admin/sessions?search=1"));
    settingsButton?.addEventListener("click", openSettingsFrame);
    getJson("/admin/api/status")
      .then((payload) => { if (dot) dot.hidden = payload?.status?.update?.state !== "available"; })
      .catch(() => {});
  }

  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === "k") {
      if (document.querySelector("dialog[open]") || document.querySelector(".wa-settings-frame")) return;
      event.preventDefault();
      searchButton?.click();
    }
  });

  if (userNode && !userNode.textContent.trim()) {
    getJson("/admin/api/me")
      .then((me) => { userNode.textContent = me.username || ""; })
      .catch(() => {});
  }

  window.BridgeHeader = Object.freeze({ openSettings: ownsDialogs ? null : openSettingsFrame });
})();
