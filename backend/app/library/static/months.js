// The year-and-month picker of an experience's time (owner redesign, plan 3.1). Without this script every part shows
// and the choices decide on the server; with it, the parts not in use are set aside: the end month while 至今 is
// ticked, and the picker or the words, whichever the person is not using. Nothing is sent or saved from here.
(function () {
  "use strict";

  function update(field) {
    var ongoing = field.querySelector("[data-month-ongoing]");
    var ends = field.querySelectorAll("[data-month-end]");
    var modes = field.querySelectorAll("[data-month-mode]");
    var pickers = field.querySelector("[data-month-pickers]");
    var words = field.querySelector("[data-month-words]");
    var i;

    for (i = 0; i < ends.length; i += 1) {
      ends[i].disabled = Boolean(ongoing && ongoing.checked);
    }

    if (modes.length && pickers) {
      var usingPicker = false;

      for (i = 0; i < modes.length; i += 1) {
        if (modes[i].checked && modes[i].value === "months") {
          usingPicker = true;
        }
      }

      pickers.hidden = !usingPicker;

      if (words) {
        words.hidden = usingPicker;
        var label = field.querySelector('label[for="' + words.id + '"]');

        if (label) {
          label.hidden = usingPicker;
        }
      }
    }
  }

  var fields = document.querySelectorAll("[data-month-field]");

  for (var n = 0; n < fields.length; n += 1) {
    (function (field) {
      update(field);
      field.addEventListener("change", function () {
        update(field);
      });
    })(fields[n]);
  }
})();
