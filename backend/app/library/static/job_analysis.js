// The page of a running analysis.
//
// It asks the server for the task's step now and then and shows it; once the
// task is no longer running it loads the page again, which shows the result.
// Leaving the page while the task runs asks first: keep watching, or cancel
// the analysis and leave. Without JavaScript the page still shows the step
// it was loaded with, a link to refresh it and the cancel button.
(function () {
  "use strict";

  const page = document.querySelector("main[data-task-running]");

  if (!page) {
    return;
  }

  const POLL_MS = 1500;
  const status = page.querySelector("[data-task-step]");
  const cancelForm = page.querySelector("form[data-task-cancel]");
  const dialog = document.getElementById("task-leave-dialog");
  let pending = null;
  let leaving = false;

  function poll() {
    window
      .fetch(page.dataset.taskStatus, { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        return response.json();
      })
      .then(function (task) {
        if (task.stage !== "running") {
          leaving = true;
          window.location.reload();
          return;
        }

        status.textContent = "正在" + task.step + "。已交给服务的请求：" + task.requests_sent + " 次。";
        window.setTimeout(poll, POLL_MS);
      })
      .catch(function () {
        // Keep the page as it is; the refresh link still works.
        window.setTimeout(poll, POLL_MS * 2);
      });
  }

  window.setTimeout(poll, POLL_MS);

  if (!dialog || !cancelForm) {
    return;
  }

  cancelForm.addEventListener("submit", function () {
    leaving = true;
  });

  function ask(leave) {
    pending = leave;
    dialog.showModal();
  }

  document.addEventListener("click", function (event) {
    const link = event.target.closest("a[href]");

    if (!link || leaving || link.hasAttribute("download") || link.hasAttribute("data-skip-leave-guard")) {
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

      if (other === cancelForm || leaving) {
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

    const leave = pending;
    pending = null;
    dialog.close();

    if (choice.dataset.taskChoice !== "cancel") {
      return;
    }

    leaving = true;

    if (leave.form) {
      leave.form.submit();
    } else {
      cancelForm.querySelector("input[name=next]").value = leave.path;
      cancelForm.submit();
    }
  });

  dialog.addEventListener("close", function () {
    if (!dialog.open) {
      pending = null;
    }
  });
})();
