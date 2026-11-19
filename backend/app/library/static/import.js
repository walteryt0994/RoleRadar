// The resume import form (U2-B1).
//
// One sending at a time: once the form is sent, the button is disabled, so a
// second click cannot send it again (the server also notices: every form
// carries a one-time key). What the server would refuse anyway (a file and
// text together, neither, a type it cannot read, an empty file, over 10 MB)
// is explained here before anything is sent. Such a refusal is caught at the
// document before the form's own listeners run, so the unsaved-input guard
// keeps watching. Without JavaScript the form still works, and the server
// explains.
(function () {
  "use strict";
  // The words of this page's language (the html element's data-ui-text, UI bilingual batch); the Chinese is a fallback.
  var UI_TEXT = null;

  function say(key, fallback, values) {
    if (UI_TEXT === null) {
      var root = document.documentElement;
      try {
        UI_TEXT = root && root.getAttribute ? JSON.parse(root.getAttribute("data-ui-text") || "{}") : {};
      } catch (error) {
        UI_TEXT = {};
      }
    }
    var text = UI_TEXT[key] || fallback;
    Object.keys(values || {}).forEach(function (name) {
      text = text.split("{" + name + "}").join(values[name]);
    });
    return text;
  }

  const form = document.querySelector("form[data-import]");

  if (!form) {
    return;
  }

  const MAX_BYTES = 10 * 1024 * 1024;
  const input = form.querySelector("input[type=file]");
  const text = form.querySelector("textarea");
  const button = form.querySelector("[data-import-button]");
  const status = form.querySelector("[data-import-status]");
  const consent = form.querySelector("input[name=consent]");
  const modes = Array.prototype.slice.call(form.querySelectorAll("[data-import-mode]"));
  const panels = Array.prototype.slice.call(form.querySelectorAll("[data-import-panel]"));
  const zone = form.querySelector("[data-drop-zone]");
  const chosen = form.querySelector("[data-chosen-file]");
  let sending = false;

  // Uploading a file or pasting text (owner redesign, plan 3.4): one way shows at a time and keeps what was put into
  // it; the other way's field is set aside (disabled), so only the chosen way is sent.
  function mode() {
    const picked = modes.filter(function (choice) {
      return choice.checked;
    })[0];

    return picked ? picked.value : "file";
  }

  function showMode() {
    const current = mode();

    panels.forEach(function (panel) {
      const on = panel.getAttribute("data-import-panel") === current;
      panel.hidden = !on;
      Array.prototype.slice.call(panel.querySelectorAll("input, textarea")).forEach(function (field) {
        field.disabled = !on;
      });
    });
    ready();
  }

  // The button waits for something to read in the chosen way and the consent ticked; the server checks both again.
  function ready() {
    if (sending) {
      return;
    }

    const has = mode() === "file" ? Boolean(input.files && input.files[0]) : text.value.trim() !== "";
    button.disabled = !(has && consent.checked);
  }

  function problem() {
    const file = mode() === "file" ? input.files && input.files[0] : null;
    const hasText = mode() === "text" && text.value.trim() !== "";

    if (file && hasText) {
      return say("js.import.both", "文件和粘贴的文字只能二选一：请清空文字，或者取消选择的文件。");
    }

    if (!file && !hasText) {
      return say("js.import.none", "请选择一个文件，或者粘贴简历文字。");
    }

    if (!file) {
      return "";
    }

    if (/\.doc$/i.test(file.name)) {
      return say("js.import.doc", "旧版 Word（.doc）文件不能直接读取。可以另存为 .docx 或 PDF 再导入，或者粘贴文字。");
    }

    if (!/\.(pdf|docx)$/i.test(file.name)) {
      return say("js.import.type", "只能导入文字可读的 PDF 或 Word（.docx）文件。");
    }

    if (file.size === 0) {
      return say("js.import.empty", "这个文件是空的（0 字节）。");
    }

    if (file.size > MAX_BYTES) {
      return say("js.import.large", "文件超过 10 MB 的上限。可以改为粘贴需要的文字。");
    }

    return "";
  }

  document.addEventListener(
    "submit",
    function (event) {
      if (event.target !== form) {
        return;
      }

      if (sending) {
        event.preventDefault();
        event.stopPropagation();
        return;
      }

      const found = problem();

      if (found) {
        event.preventDefault();
        event.stopPropagation();
        status.textContent = found;
        (input.files && input.files[0] ? input : text).focus();
        return;
      }

      sending = true;
      button.disabled = true;
      button.textContent = say("js.import.reading", "正在读取…");
      form.setAttribute("aria-busy", "true");
      status.textContent = say("js.import.reading_note", "正在读取，通常只要几秒；请不要关闭这个页面。");
    },
    true
  );

  input.addEventListener("change", function () {
    status.textContent = "";
    const file = input.files && input.files[0];
    chosen.textContent = file ? say("js.import.chosen", "已选择：{name}", { name: file.name }) : "";
    ready();
  });

  text.addEventListener("input", function () {
    status.textContent = "";
    ready();
  });

  consent.addEventListener("change", ready);
  modes.forEach(function (choice) {
    choice.addEventListener("change", showMode);
  });

  // Dropping a file on the area is the same as choosing it with the button (or the keyboard, on the file field).
  if (zone) {
    ["dragenter", "dragover"].forEach(function (name) {
      zone.addEventListener(name, function (event) {
        event.preventDefault();
        zone.classList.add("is-over");
      });
    });
    ["dragleave", "drop"].forEach(function (name) {
      zone.addEventListener(name, function () {
        zone.classList.remove("is-over");
      });
    });
    zone.addEventListener("drop", function (event) {
      event.preventDefault();
      const files = event.dataTransfer && event.dataTransfer.files;

      if (files && files.length && !input.disabled) {
        input.files = files;
        input.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
  }

  showMode();

  // Coming back to this page from the back/forward cache shows an old list
  // and an old form (with a used one-time key): load it again instead.
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      window.location.reload();
    }
  });
})();
