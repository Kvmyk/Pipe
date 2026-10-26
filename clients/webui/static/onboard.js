// Pierwsze uruchomienie: co uzytkownik chce robic (widok prosty albo techniczny — wspolny dla CLI, pipe web
// i Telegrama, komenda `view`) oraz — gdy Pipe jeszcze nigdzie nie dziala — gdzie ma dzialac: na tym komputerze
// (Docker) albo na serwerze (SSH z tego komputera: clients/cli/appsetup.py przez POST /api/setup mostu).
// Haslo do serwera idzie tylko do lokalnego mostu (ten sam komputer) i nie jest nigdzie zapisywane.
(function () {
  "use strict";
  // Te same rdzenie slow co backend/core/views.py (TECH_WORDS / SIMPLE_WORDS) — test_web.py pilnuje zgodnosci.
  const TECH_WORDS = ["serwer", "server", "vps", "docker", "kontener", "container", "kubernetes", "k8s", "nginx", "apache", "caddy",
    "traefik", "deploy", "wdroz", "wdraz", "ssh", "devops", "sysadmin", "admin", "infrastruk", "infrastruc", "monitor", "logi",
    "logs", "port", "dns", "certyfik", "certific", "ssl", "tls", "backup", "kopi", "baza danych", "database", "postgres", "mysql",
    "redis", "linux", "ubuntu", "debian", "systemd", "cron", "git", "repo", "ci/cd", "pipeline", "podatnos", "vulnerab", "latk",
    "patch", "aktualizacj", "firewall", "siec", "network", "programi", "programm", "kod", "code", "api", "aplikacj", "app",
    "strona", "site", "sklep", "hosting", "skrypt", "script", "terminal", "komend", "command"];
  const SIMPLE_WORDS = ["mail", "poczt", "email", "kalendarz", "calendar", "spotkan", "meeting", "dokument", "document", "notatk",
    "note", "przypomn", "remind", "zakup", "shopping", "list", "tekst", "text", "napisz", "write", "pisa", "tlumacz", "translat",
    "streszcz", "summar", "plan", "organiz", "codzien", "everyday", "daily", "asystent", "assistant", "pomysl", "idea", "nauk",
    "learn", "przepis", "recipe", "podroz", "travel", "rachun", "invoice", "faktur", "excel", "arkusz", "spreadsheet",
    "prezentacj", "presentation", "zdjec", "photo", "social", "nie znam", "nie jestem", "laik", "nietechn", "non-techn",
    "not techn", "zwykl", "proste", "simple"];

  function plain(text) {
    return String(text || "").toLowerCase().replace(/ł/g, "l").normalize("NFKD").replace(/[̀-ͯ]/g, "");
  }
  // Widok z opisu zwyklymi slowami: "simple" / "tech"; remis albo brak trafien -> "".
  function guess(text) {
    const joined = " " + (plain(text).match(/[a-z0-9/+-]+/g) || []).join(" ") + " ";
    const hits = (stems) => stems.filter((stem) => joined.includes(" " + stem)).length;
    const tech = hits(TECH_WORDS), simple = hits(SIMPLE_WORDS);
    return tech === simple ? "" : tech > simple ? "tech" : "simple";
  }

  const VIEW_ICONS = {
    simple: "M4 5h16v11H9l-5 4zM8 9h8M8 12h5",
    tech: "M3 5h18v6H3zM3 13h18v6H3zM7 8h.01M7 16h.01",
    local: "M3 5h18v11H3zM8 20h8M12 16v4",
    server: "M4 4h16v7H4zM4 13h16v7H4zM8 7.5h.01M8 16.5h.01M12 7.5h5M12 16.5h5",
  };

  function create(deps) {
    const { el, t, command, onView } = deps;
    const sheet = document.getElementById("onboard"), card = sheet.querySelector(".sheet-card");
    let chosen = "", where = "", mode = "first", app = null, running = false, closable = true, resolveClose = null;
    const form = { host: "", port: "22", user: "root", auth: "password", password: "", key: "" };

    function svg(name, size) {
      const NS = "http://www.w3.org/2000/svg", node = document.createElementNS(NS, "svg"), path = document.createElementNS(NS, "path");
      node.setAttribute("viewBox", "0 0 24 24"); node.setAttribute("width", String(size)); node.setAttribute("height", String(size));
      node.setAttribute("aria-hidden", "true");
      path.setAttribute("d", VIEW_ICONS[name]); path.setAttribute("fill", "none"); path.setAttribute("stroke", "currentColor");
      path.setAttribute("stroke-width", "1.7"); path.setAttribute("stroke-linecap", "round"); path.setAttribute("stroke-linejoin", "round");
      node.appendChild(path);
      return node;
    }
    function choice(name, title, points, note, disabled) {
      const button = el("button", "choice-card");
      button.type = "button"; button.dataset.value = name;
      const head = el("span", "choice-head");
      const mark = el("span", "choice-ico"); mark.appendChild(svg(name, 20));
      head.appendChild(mark); head.appendChild(el("b", "", title));
      button.appendChild(head);
      const list = el("ul", "choice-points");
      points.forEach((point) => list.appendChild(el("li", "", point)));
      button.appendChild(list);
      if (note) button.appendChild(el("span", "choice-note", note));
      if (disabled) { button.disabled = true; button.classList.add("off"); }
      return button;
    }
    function foot(hintText, buttons) {
      const bar = el("div", "sheet-foot");
      const hint = el("span", "hint", hintText || "");
      bar.appendChild(hint); bar.appendChild(el("div", "spacer"));
      buttons.forEach((button) => bar.appendChild(button));
      return { bar, hint };
    }
    function frame(title, lead) {
      card.textContent = "";
      if (closable && !running) {
        const close = el("button", "icon close", "×");
        close.type = "button"; close.setAttribute("aria-label", t("setupClose"));
        close.addEventListener("click", () => closeSheet());
        card.appendChild(close);
      }
      card.appendChild(el("h2", "", title));
      if (lead) card.appendChild(el("p", "lead", lead));
    }

    // ------------------------------------------------------------ krok 1: co chcesz robic
    function viewStep() {
      frame(t("ob_title"), t("ob_lead"));
      const text = el("textarea", "ob-text");
      text.rows = 2; text.placeholder = t("ob_placeholder");
      card.appendChild(text);
      const grid = el("div", "choice-grid");
      const simple = choice("simple", t("ob_simple"), [t("ob_simple1"), t("ob_simple2"), t("ob_simple3")]);
      const tech = choice("tech", t("ob_tech"), [t("ob_tech1"), t("ob_tech2"), t("ob_tech3")]);
      grid.appendChild(simple); grid.appendChild(tech);
      card.appendChild(grid);
      const next = el("button", "btn yes", mode === "setup" ? t("ob_next") : t("ob_done"));
      next.type = "button";
      const { bar, hint } = foot(t("ob_change"), [next]);
      card.appendChild(bar);
      function select(value, byGuess) {
        chosen = value;
        [simple, tech].forEach((button) => button.classList.toggle("active", button.dataset.value === value));
        next.disabled = !value;
        hint.textContent = byGuess && value ? t("ob_guess_" + value) : t("ob_change");
      }
      select(chosen, false);
      text.addEventListener("input", () => { const found = guess(text.value); if (found) select(found, true); });
      [simple, tech].forEach((button) => button.addEventListener("click", () => select(button.dataset.value, false)));
      next.addEventListener("click", async () => {
        if (!chosen) { const found = guess(text.value); if (found) select(found, true); else { hint.textContent = t("ob_pick"); return; } }
        if (mode === "setup") { if (!where) where = chosen === "simple" && app && !app.local ? "local" : ""; whereStep(); return; }
        next.disabled = true;
        try {
          const data = await command({ command: "view", args: chosen });
          onView(data.view, data.text);
          closeSheet();
        } catch (error) { hint.textContent = error.message; next.disabled = false; }
      });
      requestAnimationFrame(() => text.focus());
    }

    // ------------------------------------------------------------ krok 2: gdzie ma dzialac Pipe
    function input(label, key, type, placeholder) {
      const wrap = el("label", "ob-field");
      wrap.appendChild(el("span", "field-label", label));
      const box = el("div", "field"), field = el("input");
      field.type = type || "text"; field.value = form[key]; field.placeholder = placeholder || "";
      field.autocomplete = type === "password" ? "current-password" : "off"; field.spellcheck = false;
      field.addEventListener("input", () => { form[key] = field.value; });
      box.appendChild(field); wrap.appendChild(box);
      return { wrap, field };
    }
    function whereStep() {
      frame(t("ob_whereTitle"), chosen === "simple" ? t("ob_whereLeadSimple") : t("ob_whereLeadTech"));
      const grid = el("div", "choice-grid");
      const localProblem = app ? app.local : "";
      const local = choice("local", t("ob_local"), [t("ob_local1"), t("ob_local2")], localProblem, !!localProblem);
      const server = choice("server", t("ob_server"), [t("ob_server1"), t("ob_server2")]);
      grid.appendChild(local); grid.appendChild(server);
      card.appendChild(grid);
      const details = el("div", "ob-server");
      card.appendChild(details);
      const back = el("button", "btn no", t("back")), start = el("button", "btn yes", t("ob_start"));
      back.type = start.type = "button";
      const { bar, hint } = foot("", mode === "setup" ? [back, start] : [start]);
      card.appendChild(bar);
      back.addEventListener("click", viewStep);

      function renderServer() {
        details.textContent = "";
        if (where !== "server") return;
        if (app && !app.paramiko) details.appendChild(el("p", "lead privacy", t("ob_noParamiko")));
        const row = el("div", "ob-row");
        const host = input(t("ob_host"), "host", "text", "203.0.113.7 / vps.example.com");
        const port = input(t("ob_port"), "port", "text", "22");
        const user = input(t("ob_user"), "user", "text", "root");
        host.wrap.classList.add("grow");
        row.appendChild(host.wrap); row.appendChild(port.wrap); row.appendChild(user.wrap);
        details.appendChild(row);
        const auth = el("div", "segmented");
        [["password", t("ob_authPassword")], ["key", t("ob_authKey")], ["agent", t("ob_authAgent")]].forEach(([value, label]) => {
          const button = el("button", value === form.auth ? "on" : "", label);
          button.type = "button";
          button.addEventListener("click", () => { form.auth = value; renderServer(); });
          auth.appendChild(button);
        });
        details.appendChild(auth);
        if (form.auth === "password") details.appendChild(input(t("ob_password"), "password", "password", "").wrap);
        if (form.auth === "key") details.appendChild(input(t("ob_keyPath"), "key", "text", "~/.ssh/id_ed25519").wrap);
        if (form.auth === "agent") details.appendChild(el("p", "hint", t("ob_agentHint")));
        details.appendChild(el("p", "lead privacy", t("ob_privacy")));
        requestAnimationFrame(() => { if (!form.host) host.field.focus(); });
      }
      function select(value) {
        where = value;
        [local, server].forEach((button) => button.classList.toggle("active", button.dataset.value === value));
        start.disabled = !value || (value === "server" && app && !app.paramiko);
        hint.textContent = value === "local" ? t("ob_localHint") : value === "server" ? t("ob_serverHint") : "";
        renderServer();
      }
      [local, server].forEach((button) => button.addEventListener("click", () => { if (!button.disabled) select(button.dataset.value); }));
      select(where === "local" && localProblem ? "" : where);
      start.addEventListener("click", () => {
        if (where === "server" && !form.host.trim()) { hint.textContent = t("ob_needHost"); return; }
        const request = { kind: where, view: mode === "setup" ? chosen : "" };
        if (where === "server") {
          Object.assign(request, { host: form.host.trim(), port: form.port.trim(), user: form.user.trim() });
          if (form.auth === "password") request.password = form.password;
          if (form.auth === "key") request.key = form.key.trim();
        }
        progressStep(request);
      });
    }

    // ------------------------------------------------------------ krok 3: postep
    async function progressStep(request) {
      running = true;
      frame(request.kind === "local" ? t("ob_workingLocal") : t("ob_workingServer"), t("ob_workingLead"));
      const status = el("div", "status busy", t("ob_starting"));
      const log = el("pre", "ob-log");
      card.appendChild(status); card.appendChild(log);
      const lines = [];
      const push = (line) => {
        lines.push(line); if (lines.length > 400) lines.shift();
        log.textContent = lines.join("\n"); log.scrollTop = log.scrollHeight;
        status.textContent = line.length > 140 ? line.slice(0, 140) + "…" : line;
      };
      let result = null;
      try {
        const response = await fetch("/api/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) });
        request.password = "";
        if (!response.ok) {
          let message = "HTTP " + response.status;
          try { message = (await response.json()).error || message; } catch (_) { /* brak tresci */ }
          throw new Error(message);
        }
        const reader = response.body.getReader(), decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let cut;
          while ((cut = buffer.indexOf("\n")) >= 0) {
            const line = buffer.slice(0, cut).trim(); buffer = buffer.slice(cut + 1);
            if (!line) continue;
            let frameData; try { frameData = JSON.parse(line); } catch (_) { continue; }
            if (frameData.progress) push(frameData.progress);
            if (frameData.done) result = frameData;
          }
        }
      } catch (error) { result = { ok: false, error: error.message }; }
      running = false;
      form.password = "";
      if (result && result.ok) {
        status.className = "status ok"; status.textContent = t("ob_ready");
        const go = el("button", "btn yes", t("ob_open"));
        go.type = "button"; go.addEventListener("click", () => location.reload());
        card.appendChild(foot(t("ob_readyHint"), [go]).bar);
        setTimeout(() => location.reload(), 1600);
        return;
      }
      status.className = "status bad"; status.textContent = (result && result.error) || t("ob_failed");
      const back = el("button", "btn no", t("back"));
      back.type = "button"; back.addEventListener("click", whereStep);
      card.appendChild(foot(t("ob_failedHint"), [back]).bar);
    }

    // ------------------------------------------------------------ otwieranie
    async function loadApp() {
      try { const response = await fetch("/api/app"); app = response.ok ? await response.json() : null; } catch (_) { app = null; }
    }
    // mode: "first" — tylko wybor widoku (backend juz jest), "setup" — widok + gdzie (nic jeszcze nie dziala),
    // "where" — tylko gdzie (zmiana serwera z paska u gory)
    async function open(options) {
      mode = options.mode || "first";
      closable = options.closable !== false;
      chosen = options.view || "";
      if (mode !== "first") await loadApp();
      sheet.hidden = false;
      if (mode === "where") whereStep(); else viewStep();
      return new Promise((resolve) => { resolveClose = resolve; });     // po zamknieciu (np. potem ekran providera)
    }
    function closeSheet() {
      if (running || !closable || sheet.hidden) return;
      sheet.classList.add("out");
      setTimeout(() => { sheet.hidden = true; sheet.classList.remove("out"); }, 160);
      if (resolveClose) { resolveClose(); resolveClose = null; }
    }
    document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !sheet.hidden) closeSheet(); });

    return { open, close: closeSheet, get isOpen() { return !sheet.hidden; } };
  }

  window.PipeOnboard = { create, guess };
})();
