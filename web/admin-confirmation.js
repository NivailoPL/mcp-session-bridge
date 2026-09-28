(() => {
  "use strict";

  const ids = {
    dialog: "adminConfirmationDialog",
    title: "adminConfirmationTitle",
    message: "adminConfirmationMessage",
    detail: "adminConfirmationDetail",
    inputWrap: "adminConfirmationInputWrap",
    inputLabel: "adminConfirmationInputLabel",
    input: "adminConfirmationInput",
    cancel: "adminConfirmationCancel",
    confirm: "adminConfirmationConfirm",
  };

  function node(tag, className, id) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (id) element.id = id;
    return element;
  }

  function buildDialog() {
    const dialog = node("dialog", "admin-confirmation", ids.dialog);
    dialog.setAttribute("aria-labelledby", ids.title);
    dialog.setAttribute("aria-describedby", ids.message);

    const form = node("form", "admin-confirmation__form");
    const head = node("div", "admin-confirmation__head");
    const icon = node("span", "admin-confirmation__icon");
    icon.setAttribute("aria-hidden", "true");
    icon.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4"></path><path d="M12 17h.01"></path><path d="M10.3 3.7 2.4 18a2 2 0 0 0 1.8 3h15.6a2 2 0 0 0 1.8-3L13.7 3.7a2 2 0 0 0-3.4 0z"></path></svg>';
    const title = node("h3", "admin-confirmation__title", ids.title);
    head.append(icon, title);

    const body = node("div", "admin-confirmation__body");
    const message = node("p", "admin-confirmation__message", ids.message);
    const detail = node("p", "admin-confirmation__detail", ids.detail);
    const inputWrap = node("label", "admin-confirmation__input-wrap", ids.inputWrap);
    const inputLabel = node("span", "", ids.inputLabel);
    const input = node("input", "", ids.input);
    input.type = "text";
    input.setAttribute("aria-labelledby", ids.inputLabel);
    inputWrap.append(inputLabel, input);

    const actions = node("div", "admin-confirmation__actions");
    const cancel = node("button", "ghost", ids.cancel);
    cancel.type = "button";
    const confirm = node("button", "primary admin-confirmation__confirm", ids.confirm);
    confirm.type = "submit";
    actions.append(cancel, confirm);
    body.append(message, detail, inputWrap, actions);
    form.append(head, body);
    dialog.append(form);
    document.body.append(dialog);

    return { dialog, form, title, message, detail, inputWrap, inputLabel, input, cancel, confirm };
  }

  const dom = buildDialog();
  let activeRequest = null;

  function restoreFocus(target) {
    if (!(target instanceof HTMLElement)) return;
    window.requestAnimationFrame(() => target.focus());
  }

  function settle(confirmed) {
    const request = activeRequest;
    if (!request) return;
    activeRequest = null;
    const value = confirmed && request.hasInput ? dom.input.value : null;
    if (dom.dialog.open) dom.dialog.close();
    request.resolve({ confirmed, value });
    restoreFocus(request.focusTarget);
  }

  function open(options = {}) {
    if (activeRequest) {
      return Promise.reject(new Error("A confirmation dialog is already open."));
    }

    const tone = ["default", "warning", "danger"].includes(options.tone)
      ? options.tone
      : "default";
    const input = options.input || null;
    const focusTarget = document.activeElement;

    dom.dialog.dataset.tone = tone;
    dom.title.textContent = options.title || "Confirm action";
    dom.message.textContent = options.message || "Do you want to continue?";
    dom.detail.textContent = options.detail || "";
    dom.detail.hidden = !options.detail;
    dom.cancel.textContent = options.cancelLabel || "Cancel";
    dom.confirm.textContent = options.confirmLabel || "Continue";
    dom.confirm.dataset.tone = tone;
    dom.confirm.className = tone === "danger"
      ? "admin-confirmation__confirm"
      : "primary admin-confirmation__confirm";

    dom.inputWrap.hidden = !input;
    dom.inputLabel.textContent = input?.label || "Note";
    dom.input.value = input?.defaultValue || "";
    dom.input.placeholder = input?.placeholder || "";
    dom.input.required = Boolean(input?.required);

    return new Promise((resolve) => {
      activeRequest = { resolve, focusTarget, hasInput: Boolean(input) };
      dom.dialog.showModal();
      window.requestAnimationFrame(() => {
        if (options.initialFocus === "input" && input) dom.input.focus();
        else if (options.initialFocus === "confirm") dom.confirm.focus();
        else dom.cancel.focus();
      });
    });
  }

  dom.form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!dom.inputWrap.hidden && !dom.input.reportValidity()) return;
    settle(true);
  });
  dom.cancel.addEventListener("click", () => settle(false));
  dom.dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    settle(false);
  });
  dom.dialog.addEventListener("close", () => settle(false));
  dom.dialog.addEventListener("click", (event) => {
    if (event.target !== dom.dialog) return;
    const bounds = dom.dialog.getBoundingClientRect();
    const outside = event.clientX < bounds.left || event.clientX > bounds.right
      || event.clientY < bounds.top || event.clientY > bounds.bottom;
    if (outside) settle(false);
  });

  window.adminConfirmation = {
    confirm(options = {}) {
      return open(options).then((result) => result.confirmed);
    },
    prompt(options = {}) {
      return open({
        ...options,
        input: {
          label: options.inputLabel || "Note",
          defaultValue: options.defaultValue || "",
          placeholder: options.placeholder || "",
          required: Boolean(options.required),
        },
      }).then((result) => result.confirmed ? result.value : null);
    },
  };
})();
