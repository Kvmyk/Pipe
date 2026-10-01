// Motyw przed pierwszym malowaniem (bez migniecia): domyslnie jasny, ciemny tylko gdy uzytkownik go wybral.
(function () {
  "use strict";
  var theme = "light";
  try { theme = localStorage.getItem("pipe-theme") || "light"; } catch (_) { /* tryb prywatny */ }
  document.documentElement.dataset.theme = theme === "dark" ? "dark" : "light";
})();
