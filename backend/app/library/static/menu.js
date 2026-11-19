// The "资料管理" menu in the bar under the top bar.
//
// It is a native <details>, so it opens and closes by click, touch or
// keyboard, also without JavaScript. This only adds what people expect of a
// menu: Escape, or a click anywhere else, closes it. Escape gives the focus
// back to the menu's own button when the focus was inside the menu.
(function () {
  "use strict";

  function openMenus() {
    return Array.from(document.querySelectorAll("details[data-menu][open]"));
  }

  document.addEventListener("click", function (event) {
    openMenus().forEach(function (menu) {
      if (!menu.contains(event.target)) {
        menu.open = false;
      }
    });
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") {
      return;
    }

    openMenus().forEach(function (menu) {
      const hadFocus = menu.contains(document.activeElement);
      menu.open = false;

      if (hadFocus) {
        menu.querySelector("summary").focus();
      }
    });
  });

  // A "more" menu opens under its button, aligned to the button's right edge; where the button sits at the left of a
  // narrow screen, that would reach past the screen's left edge, so it opens rightwards instead (owner redesign
  // revision, 2026-11-18). Without JavaScript it keeps its right alignment.
  document.addEventListener("toggle", function (event) {
    const menu = event.target;

    if (!(menu instanceof HTMLDetailsElement) || !menu.matches("details.more-menu") || !menu.open) {
      return;
    }

    const panel = menu.querySelector(".menu-panel");

    if (!panel) {
      return;
    }

    panel.classList.remove("menu-panel-start");

    if (panel.getBoundingClientRect().left < 8) {
      panel.classList.add("menu-panel-start");
    }
  }, true);
})();
