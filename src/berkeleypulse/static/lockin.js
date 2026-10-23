(function () {
  var button = document.querySelector("[data-lockin]");
  if (!button) return;
  var minutes = parseInt(button.getAttribute("data-minutes"), 10);
  var label = button.querySelector(".lockin-label");
  var clock = button.querySelector(".lockin-time");
  var key = "pulse-lockin-until";
  var wait = 0;
  if (!minutes || minutes < 1) minutes = 50;

  function left() {
    var until = Number(window.localStorage.getItem(key) || 0);
    if (!until) return 0;
    return until - Date.now();
  }

  function showIdle(text) {
    window.clearTimeout(wait);
    button.classList.remove("is-on");
    button.classList.toggle("is-done", text === "Done");
    button.setAttribute("aria-pressed", "false");
    button.setAttribute("aria-label", text === "Done" ? "Lock in finished" : "Lock in");
    label.hidden = false;
    label.textContent = text;
    clock.hidden = true;
    clock.textContent = "";
  }

  function paint() {
    var remain = left();
    if (remain <= 0) {
      if (window.localStorage.getItem(key)) window.localStorage.removeItem(key);
      showIdle(remain < 0 ? "Done" : "Lock in");
      return;
    }
    var total = Math.ceil(remain / 1000);
    var whole = Math.floor(total / 60);
    var seconds = total % 60;
    button.classList.add("is-on");
    button.classList.remove("is-done");
    button.setAttribute("aria-pressed", "true");
    label.hidden = true;
    clock.hidden = false;
    clock.textContent = whole + ":" + (seconds < 10 ? "0" : "") + seconds;
    clock.setAttribute("datetime", "PT" + whole + "M" + seconds + "S");
    button.setAttribute("aria-label", "Lock in, " + clock.textContent + " left");
    wait = window.setTimeout(paint, 250);
  }

  button.addEventListener("click", function () {
    if (button.classList.contains("is-on")) {
      window.localStorage.removeItem(key);
      showIdle("Lock in");
      return;
    }
    if (button.classList.contains("is-done")) {
      showIdle("Lock in");
      return;
    }
    window.localStorage.setItem(key, String(Date.now() + minutes * 60 * 1000));
    paint();
  });

  if (left() > 0) paint();
})();
