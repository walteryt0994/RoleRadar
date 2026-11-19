// The direction cards (owner redesign, plan 4.2): "查看详情" opens a card's details under its row, one at a time, and
// "收起详情" closes them; the cards stay where they are. Without this script the same links open a card by its address
// (?open=). Nothing is sent or saved from here.
(function () {
  "use strict";

  var grid = document.querySelector("[data-direction-grid]");

  if (!grid) {
    return;
  }

  var details = Array.prototype.slice.call(grid.querySelectorAll("[data-direction-detail]"));

  function show(anchor, focus) {
    details.forEach(function (detail) {
      var name = detail.getAttribute("data-direction-detail");
      var open = name === anchor;
      var card = document.getElementById(name);
      var toggle = card && card.querySelector(".direction-toggle");

      detail.hidden = !open;

      if (card) {
        card.classList.toggle("is-open", open);
      }

      if (toggle) {
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
        toggle.classList.toggle("primary", open);
        toggle.classList.toggle("secondary", !open);
        toggle.querySelector("[data-toggle-open]").hidden = open;
        toggle.querySelector("[data-toggle-close]").hidden = !open;
      }

      if (open && focus) {
        var title = detail.querySelector("h3");
        title.setAttribute("tabindex", "-1");
        title.focus();
      }
    });
  }

  Array.prototype.slice.call(grid.querySelectorAll("[data-direction-toggle]")).forEach(function (link) {
    link.addEventListener("click", function (event) {
      event.preventDefault();
      var anchor = link.getAttribute("data-direction-toggle");
      var detail = document.getElementById(anchor + "-detail");
      var opening = detail.hidden;

      show(opening ? anchor : "", opening);

      if (!opening) {
        var card = document.getElementById(anchor);
        var toggle = card && card.querySelector(".direction-toggle");

        if (toggle) {
          toggle.focus();
        }
      }
    });
  });
})();
