// Unsaved-changes guard for the experience forms.
//
// Leaving through a link (or another form) inside this site: if the form has
// unsaved changes, ask first: save draft / keep editing / discard changes.
// Closing or reloading the tab: only ask the browser to show its own generic
// prompt. The browser decides whether that prompt appears, so the page never
// promises it will.
(function () {
  "use strict";

  const form = document.querySelector("form[data-leave-guard]");
  const dialog = document.getElementById("leave-dialog");

  if (!form || !dialog) {
    return;
  }

  const fields = Array.from(form.querySelectorAll("input[type=text], textarea"));
  const initial = new Map(fields.map((field) => [field, field.value]));
  const status = document.getElementById("dirty-status");
  const nextField = form.querySelector("input[name=next]");
  const afterSaveField = form.querySelector("input[name=after_save]");
  const saveButton = form.querySelector("[data-save-button]");
  // A page shown again after a failed save holds input that is not saved yet.
  const startedDirty = form.hasAttribute("data-initially-dirty");
  let leaving = false;
  let pendingLeave = null;

  function isDirty() {
    return startedDirty || fields.some((field) => field.value !== initial.get(field));
  }

  function showState() {
    const dirty = isDirty();
    form.dataset.dirty = dirty ? "true" : "false";

    if (status) {
      status.textContent = dirty ? "有还没保存的修改。" : "";
    }
  }

  function ask(discard, nextPath, afterSave) {
    pendingLeave = { discard: discard, nextPath: nextPath, afterSave: afterSave };
    dialog.showModal();
  }

  form.addEventListener("input", showState);
  form.addEventListener("submit", function () {
    leaving = true;
  });

  document.addEventListener("click", function (event) {
    const link = event.target.closest("a[href]");

    if (!link || leaving || !isDirty()) {
      return;
    }

    if (link.hasAttribute("download") || link.hasAttribute("data-skip-leave-guard")) {
      return;
    }

    const url = new URL(link.href, window.location.href);

    if (url.origin !== window.location.origin || link.target === "_blank") {
      return;
    }

    event.preventDefault();
    ask(
      function () {
        window.location.assign(url.href);
      },
      url.pathname + url.search,
      ""
    );
  });

  // Where "save draft" continues to after another form of this site was
  // sent: a search (a GET form) goes on with exactly the conditions it
  // carries, encoded by the browser's own rules. A form that would change
  // something is never sent again on its own; the list opens instead.
  function continuation(other, submitter) {
    const target = new URL(other.action, window.location.href);

    if (other.method !== "get" || target.origin !== window.location.origin) {
      return "/experiences";
    }

    target.search = new URLSearchParams(new FormData(other, submitter)).toString();

    return target.pathname + target.search;
  }

  // Other forms on the page leave it too. For the logout button, "save
  // draft" sends the save with after_save=logout: the server logs out only
  // after the save succeeded, and keeps the session when it fails.
  document.addEventListener(
    "submit",
    function (event) {
      const other = event.target;

      if (other === form || leaving || !isDirty()) {
        return;
      }

      event.preventDefault();
      const loggingOut = other.hasAttribute("data-logout");
      ask(
        function () {
          other.submit();
        },
        loggingOut ? "" : continuation(other, event.submitter),
        loggingOut ? "logout" : ""
      );
    },
    true
  );

  dialog.addEventListener("click", function (event) {
    const choice = event.target.closest("[data-leave-choice]");

    if (!choice || !pendingLeave) {
      return;
    }

    const leave = pendingLeave;
    pendingLeave = null;
    dialog.close();

    if (choice.dataset.leaveChoice === "save") {
      if (nextField) {
        nextField.value = leave.nextPath;
      }

      if (afterSaveField) {
        afterSaveField.value = leave.afterSave;
      }

      leaving = true;
      form.requestSubmit(saveButton);
    } else if (choice.dataset.leaveChoice === "discard") {
      leaving = true;
      leave.discard();
    }
  });

  // Closing the dialog (a choice, or Escape) forgets the leave it asked
  // about. The browser fires this event a moment after the dialog closed; if
  // a new leave has opened the dialog again by then, the event is stale and
  // must not forget the new one, or none of its buttons would act.
  dialog.addEventListener("close", function () {
    if (!dialog.open) {
      pendingLeave = null;
    }
  });

  window.addEventListener("beforeunload", function (event) {
    if (leaving || !isDirty()) {
      return;
    }

    event.preventDefault();
    // Older browsers only show their prompt when returnValue is set.
    event.returnValue = "";
  });

  showState();
})();
