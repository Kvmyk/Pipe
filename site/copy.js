// Przycisk "Kopiuj" przy blokach z komendami. Kopiuje komendy bez komentarzy (# ...).
(function () {
  "use strict";
  var label = document.documentElement.lang === "en" ? ["Copy", "Copied"] : ["Kopiuj", "Skopiowano"];
  document.querySelectorAll("pre.cmd").forEach(function (block) {
    if (!navigator.clipboard) return;
    var button = document.createElement("button");
    button.type = "button";
    button.textContent = label[0];
    button.addEventListener("click", function () {
      var text = block.querySelector("code").innerText.split("\n")
        .map(function (line) { return line.replace(/\s+#.*$/, "").trimEnd(); })
        .filter(function (line) { return line && !line.startsWith("#"); }).join("\n");
      navigator.clipboard.writeText(text).then(function () {
        button.textContent = label[1];
        setTimeout(function () { button.textContent = label[0]; }, 1600);
      });
    });
    block.appendChild(button);
  });
})();
