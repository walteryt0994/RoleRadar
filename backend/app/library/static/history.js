// One version open at a time in a record's history (owner redesign, plan 3.1). Browsers that know <details name> do
// this themselves; this does the same for the others. Without it, versions can still be opened one by one.
(function () {
  "use strict";

  var rows = Array.prototype.slice.call(document.querySelectorAll("[data-history] details.history-row"));

  rows.forEach(function (row) {
    row.addEventListener("toggle", function () {
      if (!row.open) {
        return;
      }

      rows.forEach(function (other) {
        if (other !== row && other.open) {
          other.open = false;
        }
      });
    });
  });
})();
