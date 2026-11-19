// The count beside a list of experiences to choose (owner redesign, image 01: 已选 N 段) follows the ticks. Without this
// script the count says what the page was shown with; nothing is sent from here.
(function () {
  "use strict";

  Array.prototype.slice.call(document.querySelectorAll("[data-pick-count]")).forEach(function (count) {
    var form = count.closest("form");
    var template = count.getAttribute("data-pick-template");

    if (!form || !template) {
      return;
    }

    function update() {
      var n = form.querySelectorAll('input[name="source"]:checked').length;
      count.textContent = template.split("{n}").join(String(n));
    }

    form.addEventListener("change", update);
    update();
  });
})();
