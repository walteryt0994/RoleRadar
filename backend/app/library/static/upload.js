// Upload form of the attachment section on a record's reading page.
//
// One upload at a time: once the form is sent, the button is disabled, so a
// second click cannot send the file again (the server would also notice:
// every form carries a one-time key). A file the server would refuse anyway
// (wrong type, empty, over 10 MB) is explained here before it is sent.
// Without JavaScript the form still works, and the server explains.
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

  const MAX_BYTES = 10 * 1024 * 1024;
  const ALLOWED = /\.(pdf|png|jpe?g)$/i;

  document.addEventListener("DOMContentLoaded", function () {
    const form = document.querySelector("form[data-upload]");

    if (!form) {
      return;
    }

    const input = form.querySelector("input[type=file]");
    const button = form.querySelector("[data-upload-button]");
    const status = form.querySelector("[data-upload-status]");
    let sending = false;

    function problemWith(file) {
      if (!file) {
        return say("js.upload.none", "请先选择一个文件。");
      }

      if (!ALLOWED.test(file.name)) {
        return say("js.upload.type", "只能上传 PDF、PNG 或 JPEG 文件（扩展名 .pdf、.png、.jpg 或 .jpeg），这个文件没有上传。");
      }

      if (file.size === 0) {
        return say("js.upload.empty", "这个文件是空的（0 字节），没有上传。");
      }

      if (file.size > MAX_BYTES) {
        return say("js.upload.large", "文件超过 10 MB 的上限，没有上传。");
      }

      return "";
    }

    input.addEventListener("change", function () {
      status.textContent = "";
    });

    form.addEventListener("submit", function (event) {
      if (sending) {
        event.preventDefault();
        return;
      }

      const problem = problemWith(input.files && input.files[0]);

      if (problem) {
        event.preventDefault();
        status.textContent = problem;
        input.focus();
        return;
      }

      sending = true;
      button.disabled = true;
      button.textContent = say("js.upload.uploading", "正在上传…");
      form.setAttribute("aria-busy", "true");
      status.textContent = say("js.upload.uploading_note", "正在上传，请不要关闭这个页面。");
    });

    // Coming back to this page from the back/forward cache shows an old list
    // and an old form: load it again instead.
    window.addEventListener("pageshow", function (event) {
      if (event.persisted) {
        window.location.reload();
      }
    });
  });
})();
