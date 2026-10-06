// Small progressive enhancements; every page also works without JavaScript.
(function () {
  "use strict";

  // Show a full-screen spinner while a slow Claude action posts.
  document.querySelectorAll("form[data-busy]").forEach(function (form) {
    form.addEventListener("submit", function () {
      document.getElementById("busy").hidden = false;
    });
  });

  // "Fetch now": start a background fetch, then poll until it finishes.
  var button = document.getElementById("fetch-now");
  var statusEl = document.getElementById("fetch-status");

  function poll() {
    fetch("/api/fetch/status", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.running) {
          setTimeout(poll, 4000);
        } else {
          window.location.reload();
        }
      })
      .catch(function () { setTimeout(poll, 8000); });
  }

  if (button) {
    button.addEventListener("click", function () {
      button.disabled = true;
      button.textContent = "Fetching…";
      if (statusEl) statusEl.textContent = "Fetching and scoring jobs. This can take a few minutes.";
      fetch("/api/fetch", { method: "POST", credentials: "same-origin" })
        .then(function () { setTimeout(poll, 3000); })
        .catch(function () {
          button.disabled = false;
          button.textContent = "Fetch now";
          if (statusEl) statusEl.textContent = "Could not start the fetch. Are you online?";
        });
    });
    fetch("/api/fetch/status", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.running) {
          button.disabled = true;
          button.textContent = "Fetching…";
          poll();
        }
      })
      .catch(function () {});
  }

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(function () {});
  }
})();
