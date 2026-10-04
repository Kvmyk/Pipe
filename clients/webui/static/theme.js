// Motyw przed pierwszym malowaniem (bez migniecia): domyslnie ciemny, jasny tylko gdy uzytkownik go wybral.
(function () {
  "use strict";
  var theme = "dark";
  try { theme = localStorage.getItem("pipe-theme") || "dark"; } catch (_) { /* tryb prywatny */ }
  document.documentElement.dataset.theme = theme === "light" ? "light" : "dark";
})();
