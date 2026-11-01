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

  const form = document.querySelector("form[data-import]");

  if (!form) {
    return;
  }

  const MAX_BYTES = 10 * 1024 * 1024;
  const input = form.querySelector("input[type=file]");
  const text = form.querySelector("textarea");
  const button = form.querySelector("[data-import-button]");
  const status = form.querySelector("[data-import-status]");
  let sending = false;

  function problem() {
    const file = input.files && input.files[0];
    const hasText = text.value.trim() !== "";

    if (file && hasText) {
      return "文件和粘贴的文字只能二选一：请清空文字，或者取消选择的文件。";
    }

    if (!file && !hasText) {
      return "请选择一个文件，或者粘贴简历文字。";
    }

    if (!file) {
      return "";
    }

    if (/\.doc$/i.test(file.name)) {
      return "旧版 Word（.doc）文件不能直接读取。可以另存为 .docx 或 PDF 再导入，或者粘贴文字。";
    }

    if (!/\.(pdf|docx)$/i.test(file.name)) {
      return "只能导入文字可读的 PDF 或 Word（.docx）文件。";
    }

    if (file.size === 0) {
      return "这个文件是空的（0 字节）。";
    }

    if (file.size > MAX_BYTES) {
      return "文件超过 10 MB 的上限。可以改为粘贴需要的文字。";
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
      button.textContent = "正在读取…";
      form.setAttribute("aria-busy", "true");
      status.textContent = "正在读取，通常只要几秒；请不要关闭这个页面。";
    },
    true
  );

  input.addEventListener("change", function () {
    status.textContent = "";
  });

  text.addEventListener("input", function () {
    status.textContent = "";
  });

  // Coming back to this page from the back/forward cache shows an old list
  // and an old form (with a used one-time key): load it again instead.
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      window.location.reload();
    }
  });
})();
