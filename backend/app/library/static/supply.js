// 补充岗位 (owner redesign, plan 6.1): paste a link or paste the text, one shown at a time. Each way is its own form,
// so only the way used is sent, and each keeps what was put into it. Without this script both forms show, one after the
// other. Nothing is sent from here.
(function () {
  "use strict";

  var switcher = document.querySelector("[data-supply-switch]");

  if (!switcher) {
    return;
  }

  var choices = Array.prototype.slice.call(switcher.querySelectorAll("input[name=supply_mode]"));
  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-supply-panel]"));

  function show() {
    var current = choices.filter(function (choice) {
      return choice.checked;
    })[0];
    var name = current ? current.value : "link";

    panels.forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-supply-panel") !== name;
    });
  }

  switcher.hidden = false;
  document.documentElement.classList.add("supply-switched");
  choices.forEach(function (choice) {
    choice.addEventListener("change", show);
  });
  show();
})();
