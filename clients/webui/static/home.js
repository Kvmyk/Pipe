// Pulpit — pierwsze, co widzi uzytkownik: czy wszystko dziala (zwyklymi slowami), procesor / pamiec / dysk z przebiegiem
// doby, wiadomosci, ktore Pipe wyslal sam (z przyciskami akcji), projekty z warsztatu i koszt modelu. Wszystko bez LLM:
// komendy `overview`, `feed`, `feed_act`, `prefs`, `projects`. Grafika w SVG budowanym w DOM (bez innerHTML).
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const PATHS = {
    check: "M5 12.5l4.5 4.5L19 7.5",
    alert: "M12 8v5M12 16.5h.01M10.3 3.9L2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
    shield: "M12 3l7 3v5c0 4.4-3 8.3-7 10-4-1.7-7-5.6-7-10V6z",
    bulb: "M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z",
    sun: "M12 4V2M12 22v-2M4 12H2M22 12h-2M5.6 5.6 4.2 4.2M19.8 19.8l-1.4-1.4M5.6 18.4l-1.4 1.4M19.8 4.2l-1.4 1.4M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z",
    search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4-4",
    clock: "M12 7v5l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z",
    moon: "M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z",
    hand: "M8 13V5.5a1.5 1.5 0 0 1 3 0V12M11 11V4.5a1.5 1.5 0 0 1 3 0V11M14 11V6.5a1.5 1.5 0 0 1 3 0v7.5a6 6 0 0 1-6 6h-1a6 6 0 0 1-5.2-3l-2-3.5a1.5 1.5 0 0 1 2.6-1.5L8 15",
    box: "M21 8l-9-5-9 5 9 5 9-5zM3 8v8l9 5 9-5V8M12 13v8",
    bell: "M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15zM10 20a2 2 0 0 0 4 0",
    play: "M8 5l11 7-11 7z",
    stop: "M7 7h10v10H7z",
    logs: "M5 5h14M5 9h14M5 13h9M5 17h11",
    trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13",
    plus: "M12 5v14M5 12h14",
    link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
    coin: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM9.5 9.5c.5-1 1.5-1.5 2.5-1.5 1.7 0 3 1 3 2.2 0 2.8-5 1.5-5 4.3 0 1.2 1.3 2 3 2 1.1 0 2-.5 2.5-1.5M12 6.5v1.5M12 16v1.5",
    refresh: "M20 12a8 8 0 1 1-2.3-5.6M20 4v4h-4",
    chevron: "M6 9l6 6 6-6",
  };
  // Zwiniete sekcje i wiadomosci — tylko w tej przegladarce (localStorage), na innym urzadzeniu pulpit jest rozwiniety
  const FOLD_KEY = "pipe-home-fold";
  const TYPE_ICON = { alert: "alert", insight: "bulb", digest: "sun", welcome: "hand", investigation: "search",
                      routine: "clock", notice: "moon" };

  function create(deps) {
    const { el, md, t, fmt, command, toast, onAction, onAsk } = deps;
    const root = document.getElementById("home");
    let overview = null, feed = { items: [], unread: 0 }, canEdit = true, loading = false, timer = 0, view = "tech";
    const expanded = new Set();
    const folded = loadFold();
    const shown = new Set();          // co juz bylo na ekranie: przy ponownym rysowaniu bez animacji wejscia
    let drawn = false;

    function loadFold() {
      try {
        const saved = JSON.parse(localStorage.getItem(FOLD_KEY) || "{}");
        return { sections: new Set(saved.sections || []), cards: new Set(saved.cards || []) };
      } catch (_) { return { sections: new Set(), cards: new Set() }; }
    }
    function saveFold() {
      try {
        localStorage.setItem(FOLD_KEY, JSON.stringify({ sections: [...folded.sections], cards: [...folded.cards].slice(-200) }));
      } catch (_) { /* tryb prywatny */ }
    }
    function toggle(set, key) { if (set.has(key)) set.delete(key); else set.add(key); saveFold(); }

    function icon(name, size, cls) {
      const svg = document.createElementNS(NS, "svg"), path = document.createElementNS(NS, "path");
      svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("width", String(size || 16)); svg.setAttribute("height", String(size || 16));
      svg.setAttribute("aria-hidden", "true"); if (cls) svg.setAttribute("class", cls);
      path.setAttribute("d", PATHS[name] || PATHS.bulb); path.setAttribute("fill", "none"); path.setAttribute("stroke", "currentColor");
      path.setAttribute("stroke-width", "1.8"); path.setAttribute("stroke-linecap", "round"); path.setAttribute("stroke-linejoin", "round");
      svg.appendChild(path);
      return svg;
    }
    function node(tag, attrs) {
      const n = document.createElementNS(NS, tag);
      Object.entries(attrs || {}).forEach(([k, v]) => n.setAttribute(k, String(v)));
      return n;
    }

    // ---------------------------------------------------------------- grafika
    // Pierscien: procent jako luk; kolor tylko z poziomem (ok / warn / crit) — jak w calym interfejsie.
    function ring(pct, level, label) {
      const r = 26, c = 2 * Math.PI * r, value = Math.max(0, Math.min(100, Number(pct) || 0));
      const svg = node("svg", { viewBox: "0 0 64 64", width: 64, height: 64, class: "ring lvl-" + (level || "ok"), role: "img" });
      svg.setAttribute("aria-label", label + ": " + value + "%");
      svg.appendChild(node("circle", { cx: 32, cy: 32, r, class: "track" }));
      const arc = node("circle", { cx: 32, cy: 32, r, class: "arc", "stroke-dasharray": c.toFixed(1) + " " + c.toFixed(1),
                                   "stroke-dashoffset": (c * (1 - value / 100)).toFixed(1), transform: "rotate(-90 32 32)" });
      svg.appendChild(arc);
      const text = node("text", { x: 32, y: 36.5, "text-anchor": "middle", class: "pct" });
      text.textContent = value + "%";
      svg.appendChild(text);
      return svg;
    }
    // Przebieg z ostatniej doby (0–100%): linia + delikatne pole; ostatni punkt zaznaczony.
    function sparkline(values, level) {
      if (!values || values.length < 2) return el("div", "spark-empty", t("h_noHistory"));
      const w = 140, h = 30, pad = 2, n = values.length;
      const x = (i) => pad + (i * (w - pad * 2)) / (n - 1), y = (v) => h - pad - (Math.max(0, Math.min(100, v)) / 100) * (h - pad * 2);
      const line = values.map((v, i) => (i ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1)).join(" ");
      const svg = node("svg", { viewBox: `0 0 ${w} ${h}`, width: w, height: h, class: "spark lvl-" + (level || "ok"), preserveAspectRatio: "none" });
      svg.setAttribute("aria-hidden", "true");
      svg.appendChild(node("path", { d: line + ` L${x(n - 1).toFixed(1)} ${h} L${x(0).toFixed(1)} ${h} Z`, class: "area" }));
      svg.appendChild(node("path", { d: line, class: "line" }));
      svg.appendChild(node("circle", { cx: x(n - 1).toFixed(1), cy: y(values[n - 1]).toFixed(1), r: 2.2, class: "last" }));
      return svg;
    }

    // ---------------------------------------------------------------- dane
    async function refresh(quiet) {
      if (loading) return;
      loading = true;
      try {
        const [view, inbox] = await Promise.all([command({ command: "overview" }), command({ command: "feed" })]);
        overview = view; feed = inbox;
        render();
        if (deps.onUnread) deps.onUnread(feed.unread || 0);
      } catch (error) {
        if (!overview && !quiet) { root.textContent = ""; root.appendChild(el("div", "empty", t("errorPrefix") + ": " + error.message)); }
      } finally {
        loading = false;
        clearTimeout(timer);
        timer = setTimeout(() => { if (!document.hidden) refresh(true); }, 60000);
      }
    }
    async function markSeen() {
      if (!feed.unread) return;
      try { feed = await command({ command: "feed", args: "seen" }); render(); if (deps.onUnread) deps.onUnread(0); } catch (_) { /* nastepnym razem */ }
    }

    // ---------------------------------------------------------------- uklad
    // `key` — sekcja zwijana do samego naglowka (stan w FOLD_KEY); `isFolded(key)` mowi, czy rysowac tresc.
    function section(title, extra, key) {
      const shut = Boolean(key) && folded.sections.has(key);
      const box = el("section", "home-sec" + (shut ? " folded" : "")), head = el("div", "home-sec-head");
      const h3 = el("h3", "");
      if (key) {
        const button = el("button", "home-fold", "");
        button.type = "button"; button.setAttribute("aria-expanded", String(!shut));
        button.title = t(shut ? "h_unfold" : "h_fold");
        button.appendChild(icon("chevron", 14, "chev"));
        button.appendChild(document.createTextNode(title));
        button.addEventListener("click", () => { toggle(folded.sections, key); render(); });
        h3.appendChild(button);
      } else {
        h3.textContent = title;
      }
      head.appendChild(h3);
      if (extra) head.appendChild(extra);
      box.appendChild(head);
      return box;
    }
    function isFolded(key) { return folded.sections.has(key); }
    function unfold(key) { if (folded.sections.delete(key)) { saveFold(); render(); } }
    function hero() {
      const status = overview.status || "ok", box = el("section", "hero st-" + status);
      const mark = el("div", "hero-mark");
      mark.appendChild(icon(status === "ok" ? "check" : "alert", 22));
      box.appendChild(mark);
      const text = el("div", "hero-text");
      text.appendChild(el("h2", "", overview.headline || ""));
      const facts = [overview.hostname, ...(overview.facts || [])].filter(Boolean);
      if (facts.length) text.appendChild(el("p", "", facts.join(" · ")));
      box.appendChild(text);
      const again = el("button", "icon", "");
      again.type = "button"; again.title = t("refresh"); again.setAttribute("aria-label", t("refresh"));
      again.appendChild(icon("refresh", 16));
      again.addEventListener("click", () => { again.classList.add("spin"); refresh(false).finally(() => again.classList.remove("spin")); });
      box.appendChild(again);
      return box;
    }
    // Zasoby zwiniete: w naglowku male znaczniki z procentem (kolor tylko z poziomem)
    function resources() {
      if (!isFolded("gauges")) {
        const box = section(t("h_resources"), null, "gauges");
        box.appendChild(gauges());
        return box;
      }
      const chips = el("div", "gauge-chips");
      (overview.gauges || []).forEach((gauge) => {
        const chip = el("span", "gauge-chip lvl-" + (gauge.level || "ok"));
        chip.appendChild(el("i", ""));
        chip.appendChild(document.createTextNode(gauge.label + " " + Math.round(Number(gauge.pct) || 0) + "%"));
        chips.appendChild(chip);
      });
      return section(t("h_resources"), chips, "gauges");
    }
    function gauges() {
      const grid = el("div", "gauges");
      const series = overview.series || {};
      let diskShown = false;          // przebieg doby jest dla jednego dysku (glownego) — pierwszego na liscie
      (overview.gauges || []).forEach((gauge) => {
        const card = el("div", "gauge lvl-" + gauge.level + (drawn ? " settled" : ""));
        card.appendChild(ring(gauge.pct, gauge.level, gauge.label));
        const side = el("div", "gauge-side");
        side.appendChild(el("b", "", gauge.label));
        side.appendChild(el("span", "", gauge.value));
        let key = gauge.id === "cpu" ? "cpu" : gauge.id === "mem" ? "mem" : "";
        if (!key && gauge.id.startsWith("disk:") && !diskShown) { key = "disk"; diskShown = true; }
        if (key) side.appendChild(sparkline(series[key], gauge.level));
        card.appendChild(side);
        grid.appendChild(card);
      });
      return grid;
    }
    function feedCard(item) {
      const shut = folded.cards.has(item.id);
      const card = el("article", "feed-card sev-" + (item.severity || "info") + (item.unread ? " unread" : "") + (item.done ? " done" : "")
                                 + (shut ? " shut" : "") + (shown.has(item.id) ? " settled" : ""));
      shown.add(item.id);
      // Naglowek zwija wiadomosc do jednej linii (ikona, tytul, czas) i rozwija ja z powrotem
      const head = el("button", "feed-head", "");
      head.type = "button"; head.setAttribute("aria-expanded", String(!shut));
      head.title = t(shut ? "h_unfold" : "h_fold");
      const badge = el("span", "feed-ico");
      badge.appendChild(icon(TYPE_ICON[item.type] || "bulb", 15));
      head.appendChild(badge);
      head.appendChild(el("b", "", item.title));
      head.appendChild(el("span", "feed-at", (item.at || "").slice(5)));
      head.appendChild(icon("chevron", 14, "chev"));
      head.addEventListener("click", () => { toggle(folded.cards, item.id); card.replaceWith(feedCard(item)); });
      card.appendChild(head);
      if (shut) return card;
      const text = String(item.text || "").trim();
      if (text) {
        const long = text.length > 320 && !expanded.has(item.id);
        const body = md(long ? text.slice(0, 300).replace(/\s+\S*$/, "") + "…" : text);
        body.classList.add("feed-text");
        card.appendChild(body);
        if (text.length > 320) {
          const more = el("button", "feed-more", expanded.has(item.id) ? t("hideDetails") : t("details"));
          more.type = "button";
          more.addEventListener("click", () => { if (expanded.has(item.id)) expanded.delete(item.id); else expanded.add(item.id); render(); });
          card.appendChild(more);
        }
      }
      if (item.done) {
        card.appendChild(el("div", "feed-done", "✓ " + item.done));
      } else if ((item.actions || []).length) {
        const foot = el("div", "feed-actions");
        item.actions.forEach((action) => {
          if (action.admin && !canEdit) return;
          const quiet = ["snooze", "mute", "dismiss"].includes(action.kind);
          const button = el("button", "btn small " + (action.primary ? "yes" : quiet ? "quiet" : "no"), action.label);
          button.type = "button";
          button.addEventListener("click", () => act(item, action, card));
          foot.appendChild(button);
        });
        card.appendChild(foot);
      }
      return card;
    }
    async function act(item, action, card) {
      if (["snooze", "mute", "dismiss"].includes(action.kind)) {
        try {
          const data = await command({ command: "feed_act", id: item.id, action: action.index });
          item.done = data.done || action.label; item.unread = false;
          card.replaceWith(feedCard(item));
          if (data.text) toast(action.label, data.text);
        } catch (error) { toast(t("errorPrefix"), error.message); }
        return;
      }
      item.done = action.label; item.unread = false;
      card.replaceWith(feedCard(item));
      await onAction(item, action);
      refresh(true);
    }
    function inbox() {
      const tools = el("div", "home-tools");
      if (feed.unread) {
        const seen = el("button", "linkish", t("h_markRead"));
        seen.type = "button"; seen.addEventListener("click", markSeen);
        tools.appendChild(seen);
      }
      const settings = el("button", "icon", "");
      settings.type = "button"; settings.title = t("h_notifySettings"); settings.setAttribute("aria-label", t("h_notifySettings"));
      settings.appendChild(icon("bell", 16));
      settings.addEventListener("click", openSettings);
      tools.appendChild(settings);
      const all = feed.items || [];
      const count = feed.unread ? " · " + fmt("h_new", { n: feed.unread }) : isFolded("inbox") && all.length ? " · " + all.length : "";
      const box = section(t("h_inbox") + count, tools, "inbox");
      box.id = "home-inbox";
      if (isFolded("inbox")) return box;
      const items = all.slice(0, 12);
      if (!items.length) box.appendChild(el("div", "home-empty", t("h_inboxEmpty")));
      items.forEach((item) => box.appendChild(feedCard(item)));
      return box;
    }
    function projectCard(project) {
      const card = el("div", "project-card st-" + (project.status || "none") + (drawn ? " settled" : ""));
      const head = el("div", "project-head");
      const dot = el("span", "pdot");
      dot.title = t("pst_" + (project.status || "none"));
      head.appendChild(dot);
      head.appendChild(el("b", "", project.name));
      if ((project.stack || []).length) head.appendChild(el("span", "tag", project.stack.slice(0, 2).join(" · ")));
      card.appendChild(head);
      const meta = el("div", "project-meta", t("pst_" + (project.status || "none")) + (project.url && project.status === "running" ? " · " + project.url : ""));
      card.appendChild(meta);
      const foot = el("div", "feed-actions");
      const button = (label, name, handler) => {
        const b = el("button", "btn small no", "");
        b.type = "button"; b.appendChild(icon(name, 13)); b.appendChild(document.createTextNode(label));
        b.addEventListener("click", handler); foot.appendChild(b);
      };
      button(t("p_logs"), "logs", () => projectOp(project, "logs"));
      if (canEdit && project.status === "running") button(t("p_stop"), "stop", () => projectOp(project, "stop"));
      if (canEdit && ["stopped", "partial"].includes(project.status)) button(t("p_start"), "play", () => projectOp(project, "start"));
      if (canEdit && project.status === "none") button(t("p_run"), "play", () => onAsk(fmt("p_runAsk", { name: project.name })));
      card.appendChild(foot);
      return card;
    }
    async function projectOp(project, op) {
      try {
        const data = await command({ command: "projects", args: op, name: project.name });
        if (op === "logs") deps.showText(t("p_logs") + " · " + project.name, data.text || "");
        else toast(project.name, data.text || "");
      } catch (error) { toast(t("errorPrefix"), error.message); }
      if (op !== "logs") refresh(true);
    }
    function projects() {
      const add = el("button", "btn small no", "");
      add.type = "button"; add.appendChild(icon("plus", 13)); add.appendChild(document.createTextNode(t("p_fromGit")));
      add.addEventListener("click", deps.askRepo);
      const list = overview.projects || [];
      const box = section(t("h_projects") + (isFolded("projects") && list.length ? " · " + list.length : ""), add, "projects");
      if (isFolded("projects")) return box;
      if (!list.length) {
        const empty = el("div", "home-empty");
        empty.appendChild(document.createTextNode(t("p_empty")));
        box.appendChild(empty);
      } else {
        const grid = el("div", "project-grid");
        list.forEach((project) => grid.appendChild(projectCard(project)));
        box.appendChild(grid);
      }
      return box;
    }
    function usage() {
      const u = overview.usage || {};
      const box = el("div", "usage-line");
      box.appendChild(icon("coin", 14));
      const parts = [fmt("u_tokens", { n: tokens(u.tokens || 0) })];
      if (u.cost) parts.push("$" + Number(u.cost).toFixed(u.cost < 1 ? 3 : 2));
      if (u.prompt && u.cached) parts.push(fmt("u_cached", { n: Math.round((u.cached * 100) / u.prompt) }));
      if (u.saved) parts.push(fmt("u_saved", { n: tokens(u.saved) }));
      box.appendChild(el("span", "", t("u_today") + ": " + parts.join(" · ")));
      return box;
    }
    function tokens(n) {
      if (n >= 1e6) return (n / 1e6).toFixed(1) + (window.PipeI18n.lang === "en" ? "M" : " mln");
      if (n >= 1e3) return Math.round(n / 1e3) + (window.PipeI18n.lang === "en" ? "k" : " tys.");
      return String(n);
    }
    function render() {
      if (!overview) return;
      const scroll = root.scrollTop;
      root.textContent = "";
      const top = hero();
      if (drawn) top.classList.add("settled");
      root.appendChild(top);
      if (view !== "simple") root.appendChild(resources());   // widok prosty: bez wykresow i warsztatu
      root.appendChild(inbox());
      if (view !== "simple") root.appendChild(projects());
      root.appendChild(usage());
      root.scrollTop = scroll;
      drawn = true;
    }

    // ---------------------------------------------------------------- ustawienia powiadomien
    const sheet = document.getElementById("notify");
    async function openSettings() {
      let prefs;
      try { prefs = await command({ command: "prefs" }); } catch (error) { toast(t("errorPrefix"), error.message); return; }
      const card = sheet.querySelector(".sheet-card");
      card.textContent = "";
      const close = el("button", "icon close", "×");
      close.type = "button"; close.setAttribute("aria-label", t("close"));
      close.addEventListener("click", closeSettings);
      card.appendChild(close);
      card.appendChild(el("h2", "", t("n_title")));
      card.appendChild(el("p", "lead", t("n_lead")));
      const form = el("div", "notify-form");
      const disabled = !prefs.can_edit;
      // poziom
      form.appendChild(el("div", "field-label", t("n_level")));
      const levels = el("div", "seg");
      [["all", "n_all"], ["important", "n_important"], ["critical", "n_critical"]].forEach(([value, key]) => {
        const b = el("button", "seg-item" + (prefs.level === value ? " on" : ""), t(key));
        b.type = "button"; b.disabled = disabled;
        b.addEventListener("click", () => save({ level: value }));
        levels.appendChild(b);
      });
      form.appendChild(levels);
      form.appendChild(el("div", "hint", t("n_levelHint_" + prefs.level)));
      // cisza nocna
      form.appendChild(el("div", "field-label", t("n_quiet")));
      const quiet = el("div", "seg");
      [["", "n_off"], ["22:00-07:00", "22–7"], ["23:00-08:00", "23–8"], ["21:00-08:00", "21–8"]].forEach(([value, key]) => {
        const b = el("button", "seg-item" + ((prefs.quiet || "") === value ? " on" : ""), key.startsWith("n_") ? t(key) : key);
        b.type = "button"; b.disabled = disabled;
        b.addEventListener("click", () => save({ quiet: value || "off" }));
        quiet.appendChild(b);
      });
      form.appendChild(quiet);
      form.appendChild(el("div", "hint", t("n_quietHint")));
      // podpowiedzi dziennie
      form.appendChild(el("div", "field-label", t("n_perDay")));
      const per = el("div", "seg");
      [0, 1, 3, 5, 10].forEach((value) => {
        const b = el("button", "seg-item" + (prefs.per_day === value ? " on" : ""), String(value));
        b.type = "button"; b.disabled = disabled;
        b.addEventListener("click", () => save({ per_day: value }));
        per.appendChild(b);
      });
      form.appendChild(per);
      if ((prefs.muted || []).length) {
        form.appendChild(el("div", "field-label", t("n_muted")));
        const list = el("div", "muted-list");
        prefs.muted.forEach((kind) => {
          const row = el("div", "muted-row");
          row.appendChild(el("span", "", t("kind_" + kind.replace(/[:]/g, "_")) === "kind_" + kind.replace(/[:]/g, "_") ? kind : t("kind_" + kind.replace(/[:]/g, "_"))));
          const back = el("button", "linkish", t("n_unmute"));
          back.type = "button"; back.disabled = disabled;
          back.addEventListener("click", () => save({ args: "unmute " + kind }));
          row.appendChild(back);
          list.appendChild(row);
        });
        form.appendChild(list);
      }
      if (disabled) form.appendChild(el("div", "hint", t("n_viewer")));
      card.appendChild(form);
      sheet.classList.remove("out"); sheet.hidden = false;
    }
    async function save(change) {
      try { await command({ command: "prefs", ...change }); await openSettings(); }
      catch (error) { toast(t("errorPrefix"), error.message); }
    }
    function closeSettings() {
      if (sheet.hidden) return;
      sheet.classList.add("out");
      setTimeout(() => { sheet.hidden = true; sheet.classList.remove("out"); }, 180);
    }
    sheet.addEventListener("mousedown", (event) => { if (event.target === sheet) closeSettings(); });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeSettings(); });

    // zdarzenie na zywo: nowa wiadomosc w skrzynce albo zmiana stanu — odswiezamy po cichu
    let pending = 0;
    function onEvent(event) {
      if (!event || !event.type) return;
      clearTimeout(pending);
      pending = setTimeout(() => refresh(true), event.type === "alert" ? 300 : 800);
    }
    function setCanEdit(value) { canEdit = value; }
    function setView(value) { if (value !== view) { view = value; render(); } }

    return { refresh, onEvent, markSeen, openSettings, setCanEdit, setView, unfold, get unread() { return feed.unread || 0; } };
  }

  window.PipeHome = { create };
})();
