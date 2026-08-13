function wireWorkspaceNavKeyboard() {
  const nav = document.querySelector(".sb-nav[role=\"tablist\"]");
  if (!nav) return;
  const tabs = [...nav.querySelectorAll(".sb-nav__tab[role=\"tab\"]")]
    .filter((tab) => tab.getAttribute("aria-disabled") !== "true");
  nav.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
    const current = tabs.indexOf(document.activeElement);
    if (current < 0) return;
    event.preventDefault();
    const offset = event.key === "ArrowRight" ? 1 : -1;
    const next = tabs[(current + offset + tabs.length) % tabs.length];
    next.focus();
    const href = next.getAttribute("href");
    if (href) window.location.assign(href);
  });
}

wireWorkspaceNavKeyboard();
