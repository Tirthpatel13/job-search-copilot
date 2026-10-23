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

  function getJSON(url, options) {
    return fetch(url, Object.assign({ credentials: "same-origin" }, options)).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  function giveUp(message) {
    button.disabled = false;
    button.textContent = "Fetch now";
    if (statusEl) statusEl.textContent = message;
  }

  var pollErrors = 0;
  function poll() {
    getJSON("/api/fetch/status")
      .then(function (data) {
        pollErrors = 0;
        if (data.running) {
          setTimeout(poll, 4000);
        } else {
          window.location.reload();
        }
      })
      .catch(function () {
        pollErrors += 1;
        if (pollErrors >= 5) {
          giveUp("Lost contact with the app. Reload the page to see if the fetch finished.");
        } else {
          setTimeout(poll, 8000);
        }
      });
  }

  if (button) {
    button.addEventListener("click", function () {
      button.disabled = true;
      button.textContent = "Fetching…";
      if (statusEl) statusEl.textContent = "Fetching and scoring jobs. This can take a few minutes.";
      getJSON("/api/fetch", { method: "POST" })
        .then(function () { setTimeout(poll, 3000); })
        .catch(function () {
          giveUp("Could not start the fetch. Check that the app is still running.");
        });
    });
    getJSON("/api/fetch/status")
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
