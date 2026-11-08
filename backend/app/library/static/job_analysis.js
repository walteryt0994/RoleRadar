// The page of a running analysis, or of a running search for postings (找岗位, U3-A3).
//
// It asks the server for the task's step now and then and shows it; once the
// task is no longer running it loads the page again, which shows the result.
// A search's status says its whole line and lists the steps taken so far:
// steps are only ever added, so an answer with fewer steps than shown (an
// older one) changes nothing. Leaving the page while the task runs asks
// first: keep watching, or cancel and leave. Without JavaScript the page
// still shows what it was loaded with, a link to refresh it and the cancel
// button.
//
// Asking ends for good (stop) once the user cancels, confirms leaving, or an
// answer says the task is no longer running: no request goes out after that,
// the one still out is aborted where the browser can, and whatever comes back
// late, an answer or a failure, neither changes the page, nor asks again, nor
// reloads it. A task that ended while the leave dialog was open is shown once
// the dialog closes, unless the user chose to leave: their choice is not
// taken from them.
(function () {
  "use strict";

  const page = document.querySelector("main[data-task-running]");

  if (!page) {
    return;
  }

  const POLL_MS = 1500;
  const status = page.querySelector("[data-task-step]");
  const steps = page.querySelector("[data-task-steps]");
  const cancelForm = page.querySelector("form[data-task-cancel]");
  const dialog = document.getElementById("task-leave-dialog");
  let pending = null;
  // Asking has ended; the user is leaving (a cancel or another form sent); the task ended while the dialog was open.
  let stopped = false;
  let leaving = false;
  let ended = false;
  // The request out, when the browser can abort it.
  let request = null;

  function stop() {
    stopped = true;

    if (request !== null) {
      request.abort();
      request = null;
    }
  }

  function poll() {
    // A timer set before asking ended does nothing.
    if (stopped) {
      return;
    }

    const controller = typeof AbortController === "function" ? new AbortController() : null;
    request = controller;

    window
      .fetch(page.dataset.taskStatus, {
        credentials: "same-origin",
        cache: "no-store",
        signal: controller ? controller.signal : undefined,
      })
      .then(function (response) {
        return response.json();
      })
      .then(function (task) {
        if (stopped) {
          return;
        }

        request = null;

        if (task.stage !== "running") {
          stop();
          showResult();
          return;
        }

        if (Array.isArray(task.steps)) {
          showSteps(task.steps, task.status);
        } else {
          status.textContent = "正在" + task.step + "。已交给服务的请求：" + task.sends + "。";
        }

        window.setTimeout(poll, POLL_MS);
      })
      .catch(function () {
        if (stopped) {
          return;
        }

        request = null;
        // Keep the page as it is; the refresh link still works.
        window.setTimeout(poll, POLL_MS * 2);
      });
  }

  // The task ended: load the page again for its result, unless the user is deciding whether to leave.
  function showResult() {
    if (dialog && dialog.open) {
      ended = true;
      return;
    }

    window.location.reload();
  }

  // Each step in the shape the page renders it (discover_task.html, step_item); words only, as text.
  function showSteps(taken, line) {
    if (!steps || taken.length < steps.children.length) {
      return;
    }

    status.textContent = line;

    for (let index = steps.children.length; index < taken.length; index += 1) {
      const item = document.createElement("li");

      [
        ["step-title", taken[index].title],
        ["step-params", taken[index].params],
        ["step-summary", taken[index].summary],
        ["step-note", taken[index].note],
      ].forEach(function (part) {
        if (part[1]) {
          const text = document.createElement("p");
          text.className = part[0];
          text.textContent = part[1];
          item.appendChild(text);
        }
      });

      steps.appendChild(item);
    }
  }

  window.setTimeout(poll, POLL_MS);

  // Coming back to this page from the back/forward cache shows an old state, its asking perhaps ended: load it again.
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      window.location.reload();
    }
  });

  if (!dialog || !cancelForm) {
    return;
  }

  function leave() {
    leaving = true;
    stop();
  }

  cancelForm.addEventListener("submit", leave);

  function ask(leaveWith) {
    pending = leaveWith;
    dialog.showModal();
  }

  document.addEventListener("click", function (event) {
    const link = event.target.closest("a[href]");

    if (!link || stopped || link.hasAttribute("download") || link.hasAttribute("data-skip-leave-guard")) {
      return;
    }

    const url = new URL(link.href, window.location.href);

    if (url.origin !== window.location.origin || link.target === "_blank") {
      return;
    }

    event.preventDefault();
    ask({ path: url.pathname + url.search });
  });

  document.addEventListener(
    "submit",
    function (event) {
      const other = event.target;

      if (other === cancelForm || stopped) {
        return;
      }

      // Logging out also cancels the analysis on the server.
      event.preventDefault();
      ask({ form: other });
    },
    true
  );

  dialog.addEventListener("click", function (event) {
    const choice = event.target.closest("[data-task-choice]");

    if (!choice || !pending) {
      return;
    }

    const leaveWith = pending;
    pending = null;

    if (choice.dataset.taskChoice === "cancel") {
      // Before the dialog closes, so its closing shows nothing in place of leaving.
      leave();
    }

    dialog.close();

    if (!leaving) {
      return;
    }

    if (leaveWith.form) {
      leaveWith.form.submit();
    } else {
      cancelForm.querySelector("input[name=next]").value = leaveWith.path;
      cancelForm.submit();
    }
  });

  dialog.addEventListener("close", function () {
    if (dialog.open) {
      return;
    }

    pending = null;

    // The task ended while the user decided, and they stayed: now show its result.
    if (ended && !leaving) {
      ended = false;
      window.location.reload();
    }
  });
})();
