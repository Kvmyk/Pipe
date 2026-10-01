// Maly, bezpieczny renderer Markdowna -> wezly DOM (bez innerHTML: tresc od modelu nie moze wstawic HTML ani skryptu).
// Obsluguje: bloki kodu, naglowki, listy, tabele, akapity, `kod`, **pogrubienie**, *kursywe*, linki http(s).
(function () {
  "use strict";
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  const INLINE = /(`+)([^`]+?)\1|\*\*([^*\n]+?)\*\*|(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|<\/?(?:b|strong|i|em|u|code|pre|br)\s*\/?>/g;

  function inline(text, parent) {
    let last = 0;
    // Osobny obiekt wyrazenia na kazde wywolanie: funkcja wola sama siebie (pogrubienie w pogrubieniu),
    // a wspolny `lastIndex` cofalby zewnetrzna petle na poczatek tekstu — w nieskonczonosc.
    const pattern = new RegExp(INLINE.source, "g");
    let match;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > last) parent.appendChild(document.createTextNode(text.slice(last, match.index)));
      if (match[2] !== undefined) parent.appendChild(el("code", "", match[2]));
      else if (match[3] !== undefined) { const b = el("strong"); inline(match[3], b); parent.appendChild(b); }
      else if (match[4] !== undefined) { const i = el("em"); inline(match[4], i); parent.appendChild(i); }
      else if (match[5] !== undefined) {
        const a = el("a", "", match[5]);
        a.href = match[6]; a.target = "_blank"; a.rel = "noopener noreferrer";
        parent.appendChild(a);
      }
      // tagi HTML z promptu telegramowego (b, i, code...) sa po prostu pomijane — tekst miedzy nimi zostaje
      last = match.index + match[0].length;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }

  function isDiff(lines) {
    let marks = 0;
    for (const line of lines) if (/^(\+|-|@@)/.test(line)) marks++;
    return lines.length > 1 && (lines.some((l) => l.startsWith("@@")) || marks >= Math.max(2, lines.length * 0.3));
  }

  function diffBlock(lines, title) {
    const box = el("div", "diff");
    if (title) { const file = el("div", "file"); file.appendChild(el("b", "", title)); box.appendChild(file); }
    const body = el("div", "lines");
    for (const line of lines) {
      const header = line.startsWith("@@") || line.startsWith("+++ ") || line.startsWith("--- ");
      const cls = header ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "";
      body.appendChild(el("span", "ln " + cls, line || " "));
    }
    box.appendChild(body);
    return box;
  }

  function table(rows) {
    const node = el("table");
    rows.forEach((row, index) => {
      const tr = el("tr");
      row.forEach((cell) => { const c = el(index === 0 ? "th" : "td"); inline(cell.trim(), c); tr.appendChild(c); });
      node.appendChild(tr);
    });
    return node;
  }

  function render(text) {
    const root = el("div", "body");
    const lines = String(text || "").replace(/\r/g, "").split("\n");
    let i = 0;
    let para = [];
    const flush = () => {
      if (!para.length) return;
      const p = el("p");
      para.forEach((line, index) => { if (index) p.appendChild(el("br")); inline(line, p); });
      root.appendChild(p);
      para = [];
    };
    while (i < lines.length) {
      const line = lines[i];
      const fence = line.match(/^\s*(`{3,})\s*([\w+-]*)\s*$/);
      if (fence) {
        flush();
        const body = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++]);
        i++;
        if (fence[2] === "diff" || isDiff(body)) root.appendChild(diffBlock(body));
        else { const pre = el("pre"); pre.appendChild(el("code", "", body.join("\n"))); root.appendChild(pre); }
        continue;
      }
      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) { flush(); const h = el("h" + Math.min(3, heading[1].length)); inline(heading[2], h); root.appendChild(h); i++; continue; }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1]) && lines[i + 1].includes("-")) {
        flush();
        const rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
          if (!/^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i])) rows.push(lines[i].trim().replace(/^\||\|$/g, "").split("|"));
          i++;
        }
        root.appendChild(table(rows));
        continue;
      }
      const item = line.match(/^(\s*)([-*•]|\d+[.)])\s+(.*)$/);
      if (item) {
        flush();
        const ordered = /\d/.test(item[2]);
        const list = el(ordered ? "ol" : "ul");
        while (i < lines.length) {
          const next = lines[i].match(/^(\s*)([-*•]|\d+[.)])\s+(.*)$/);
          if (!next) {
            if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && list.lastChild) {   // kontynuacja pozycji
              list.lastChild.appendChild(el("br")); inline(lines[i].trim(), list.lastChild); i++; continue;
            }
            break;
          }
          const li = el("li"); inline(next[3], li); list.appendChild(li); i++;
        }
        root.appendChild(list);
        continue;
      }
      if (!line.trim()) { flush(); i++; continue; }
      para.push(line);
      i++;
    }
    flush();
    return root;
  }

  window.PipeMd = { render, inline, el, diffBlock };
})();
