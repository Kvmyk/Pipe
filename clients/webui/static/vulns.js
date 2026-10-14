// Podatnosci w pipe web: raport (/podatnosci, audyt, stan po latce), podglad latki z instalacja na zywo i wynik.
// Dane przychodza gotowe z backendu (vulns.report_view / patch_view) — tu tylko rysowanie, bez innerHTML.
// Telegram i CLI dostaja z tych samych komend Markdown (`text`); ten plik czyta `view` / `report`.
(function () {
  "use strict";

  const LEVELS = ["kev", "critical", "high", "medium", "low", "unknown"];
  const ROWS = 5;                 // pakietow widocznych w grupie przed "Pokaz wszystkie"

  function create(deps) {
    const { el, icon, t, fmt, request, add, dataMessage, isBusy, setBusy, setWorking, onPatched } = deps;
    const reports = new Set();    // raporty w rozmowie — po latce oznaczane jako nieaktualne

    const level = (name) => (LEVELS.includes(name) ? name : "unknown");
    const sevLabel = (name) => t("vxs_" + level(name));
    function pill(name, text) {
      return el("span", "vx-sev s-" + level(name), text || sevLabel(name));
    }
    function time(at) {
      if (!at) return "";
      const d = new Date(at * 1000);
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    }

    // ---- wiersz pakietu
    function itemRow(item, compact) {
      const row = el("div", "vx-item" + (item.held ? " held" : ""));
      const head = el("div", "vx-item-head");
      head.appendChild(pill(item.level));
      const name = el("div", "vx-name");
      name.appendChild(el("b", "", item.name));
      const ver = el("span", "vx-ver", item.version);
      if (item.fixed) {
        ver.appendChild(el("span", "vx-arrow", " → "));
        ver.appendChild(el("span", "vx-fixed", item.fixed));
      }
      name.appendChild(ver);
      head.appendChild(name);
      if (item.count > 1) head.appendChild(el("span", "vx-count", fmt("vx_vulns_n", { n: item.count })));
      if (item.held) head.appendChild(el("span", "tag warn", item.pro_only ? t("vx_pro") : t("vx_held")));
      row.appendChild(head);
      // najpowazniejsza podatnosc z opisem; kolejne (do 3 z backendu) po kliknieciu — reszta jest tylko liczba
      const vulns = item.vulns || [];
      const line = (vuln, index) => {
        const node = el("div", "vx-desc");
        if (index > 0) node.appendChild(el("span", "vx-word s-" + level(vuln.level), sevLabel(vuln.level) + " · "));
        node.appendChild(document.createTextNode(vuln.text));
        node.appendChild(el("span", "vx-ref", vuln.ref));
        return node;
      };
      if (vulns.length) row.appendChild(line(vulns[0], 0));
      if (vulns.length > 1 && !compact) {
        const extra = el("div", "vx-extra");
        extra.hidden = true;
        vulns.slice(1).forEach((vuln, index) => extra.appendChild(line(vuln, index + 1)));
        const toggle = el("button", "vx-more small", fmt("vx_more_vulns", { n: vulns.length - 1 }));
        toggle.type = "button";
        toggle.addEventListener("click", () => {
          extra.hidden = !extra.hidden;
          toggle.textContent = extra.hidden ? fmt("vx_more_vulns", { n: vulns.length - 1 }) : t("vx_less");
        });
        row.appendChild(extra);
        row.appendChild(toggle);
      }
      return row;
    }

    // ---- grupa (serwer, obraz, aplikacja)
    function group(g, options) {
      const open = options.open !== false;
      const box = el("section", "vx-group" + (open ? " open" : ""));
      const head = el("button", "vx-group-head");
      head.type = "button";
      head.appendChild(el("span", "vx-kind k-" + g.kind, t("vx_kind_" + g.kind)));
      const title = el("span", "vx-group-title");
      title.appendChild(el("b", "", g.title));
      if (g.subtitle) title.appendChild(el("span", "vx-sub", g.subtitle));
      head.appendChild(title);
      const badges = el("span", "vx-badges");
      if (g.kev) badges.appendChild(pill("kev", "KEV " + g.kev));
      if (g.urgent) badges.appendChild(el("span", "vx-n urgent", fmt("vx_urgent_n", { n: g.urgent })));
      badges.appendChild(el("span", "vx-n", fmt("vx_packages_n", { n: g.count })));
      head.appendChild(badges);
      head.appendChild(el("span", "vx-chev"));
      head.addEventListener("click", () => box.classList.toggle("open"));
      box.appendChild(head);

      const body = el("div", "vx-group-body");
      if (g.note) {
        const note = el("div", "vx-note");
        note.appendChild(icon("info", 13)); note.appendChild(el("span", "", g.note));
        body.appendChild(note);
      }
      const items = g.items || [];
      const list = el("div", "vx-items");
      items.slice(0, ROWS).forEach((item) => list.appendChild(itemRow(item, options.compact)));
      body.appendChild(list);
      const hidden = Math.max(0, g.count - ROWS);
      if (hidden) {
        const more = el("button", "vx-more", fmt("vx_more", { n: g.count }));
        more.type = "button";
        let expanded = false;
        more.addEventListener("click", () => {
          expanded = !expanded;
          list.textContent = "";
          (expanded ? items : items.slice(0, ROWS)).forEach((item) => list.appendChild(itemRow(item, options.compact)));
          if (expanded && g.count > items.length) list.appendChild(el("div", "vx-desc muted", fmt("vx_not_shown", { n: g.count - items.length })));
          more.textContent = expanded ? t("vx_less") : fmt("vx_more", { n: g.count });
        });
        body.appendChild(more);
      }
      box.appendChild(body);
      return box;
    }

    // kafelek czesci: ile z poprawka, pod spodem ile pilnych (albo "nic pilnego")
    function stat(value, label, urgent) {
      const tile = el("div", "vx-stat" + (value ? "" : " zero"));
      const top = el("div", "vx-stat-top");
      top.appendChild(el("b", "", String(value)));
      top.appendChild(el("span", "", label));
      tile.appendChild(top);
      if (value) tile.appendChild(el("span", "vx-stat-sub" + (urgent ? " bad" : ""), urgent ? fmt("vx_urgent_n", { n: urgent }) : t("vx_none_urgent")));
      return tile;
    }

    function notes(list, into) {
      (list || []).filter(Boolean).forEach((text) => {
        const line = el("div", "vx-foot-note");
        line.appendChild(icon("info", 13)); line.appendChild(el("span", "", text));
        into.appendChild(line);
      });
    }

    // ---- raport
    function report(view, options) {
      options = options || {};
      const card = el("div", "vx");
      const head = el("div", "vx-head");
      const title = el("div", "vx-title");
      title.appendChild(icon("shield", 15));
      title.appendChild(el("b", "", options.title || t("vx_title")));
      if (view.at) title.appendChild(el("span", "vx-when", fmt("vx_scanned", { time: time(view.at) })));
      head.appendChild(title);
      if (options.refresh !== false) {
        const refresh = el("button", "btn no small", t("vx_refresh"));
        refresh.type = "button";
        refresh.addEventListener("click", () => {
          if (isBusy()) return;
          dataMessage({ command: "vulns" }, (d) => vulnsMessage(d));
        });
        head.appendChild(refresh);
      }
      card.appendChild(head);
      const stale = el("div", "vx-stale", t("vx_stale"));
      stale.hidden = true;
      card.appendChild(stale);

      const totals = view.totals || {}, groups = view.groups || [];
      if (!groups.length) {
        const empty = el("div", "vx-empty");
        empty.appendChild(icon("shieldCheck", 18));
        const text = el("div");
        text.appendChild(el("b", "", t("vx_empty")));
        if (view.checked) text.appendChild(el("span", "", fmt("vx_empty_sub", { n: view.checked })));
        empty.appendChild(text);
        card.appendChild(empty);
      } else {
        const stats = el("div", "vx-stats");
        if (view.supported) stats.appendChild(stat(totals.system || 0, t("vx_stat_system"), totals.system_urgent));
        stats.appendChild(stat(totals.images || 0, t("vx_stat_images"), totals.images_urgent));
        stats.appendChild(stat(totals.apps || 0, t("vx_stat_apps"), totals.apps_urgent));
        card.appendChild(stats);
        const actions = view.actions || [];
        if (actions.length && options.actions !== false) {
          const row = el("div", "vx-actions");
          actions.forEach((action, index) => {
            const button = el("button", "btn small " + (index === 0 ? "yes" : "no"), fmt("vx_act_" + action.scope, { n: action.count }));
            button.type = "button";
            button.addEventListener("click", () => {
              if (isBusy()) return;
              add("user", button.textContent);
              dataMessage({ command: "patch", scope: action.scope }, patchBox);
            });
            row.appendChild(button);
          });
          card.appendChild(row);
        }
        const list = el("div", "vx-groups");
        groups.forEach((g, index) => list.appendChild(group(g, { open: index === 0 || groups.length <= 2 })));
        card.appendChild(list);
      }
      const foot = el("div", "vx-foot");
      if (view.unfixed) foot.appendChild(el("div", "vx-foot-note plain", fmt("vx_unfixed", { n: view.unfixed })));
      notes(view.notes, foot);
      if (foot.children.length) card.appendChild(foot);
      if (options.track !== false) { reports.add(card); card._stale = stale; }
      return card;
    }

    function markStale() {
      reports.forEach((card) => { card.classList.add("is-stale"); card._stale.hidden = false; });
      reports.clear();
    }

    // /podatnosci: dane z `view`; starszy backend bez `view` — Markdown jak dotad
    function vulnsMessage(data, fallback) {
      if (data && data.view && (data.view.groups || data.view.notes)) return report(data.view);
      return fallback(data);
    }

    // ---- podglad latki i instalacja
    function patchBox(preview) {
      const view = preview.view;
      if (!view || !preview.command) {
        const box = el("div", "vx vx-plain");
        const text = el("div", "vx-empty");
        text.appendChild(icon(preview.command ? "shield" : "shieldCheck", 18));
        const lines = el("div");
        String(preview.text || "").split(/\n{2,}/).filter((p) => p.trim()).forEach((p, i) => lines.appendChild(el(i ? "span" : "b", "", p.replace(/^- /gm, "• "))));
        text.appendChild(lines);
        box.appendChild(text);
        return box;
      }
      const images = view.scope === "images";
      const count = (view.groups || []).reduce((n, g) => n + (images ? 1 : g.count), 0);
      const card = el("div", "confirm vx-patch");
      const title = el("div", "title");
      title.appendChild(icon("shield", 15));
      const label = el("span", "label", fmt(images ? "vx_patch_images" : "vx_patch_pkgs", { n: count }));
      title.appendChild(label);
      card.appendChild(title);

      const body = el("div", "body");
      (view.groups || []).forEach((g) => body.appendChild(group(g, { compact: true, open: true })));
      const cmdHead = el("div", "vx-label", t("vx_command"));
      body.appendChild(cmdHead);
      const cmd = el("code", "cmd-line vx-cmd", preview.command);
      body.appendChild(cmd);
      if (preview.command.length > 240) {
        cmd.classList.add("clipped");
        const toggle = el("button", "vx-more", t("vx_show_command"));
        toggle.type = "button";
        toggle.addEventListener("click", () => { const clipped = cmd.classList.toggle("clipped"); toggle.textContent = clipped ? t("vx_show_command") : t("vx_less"); });
        body.appendChild(toggle);
      }
      const fuse = el("div", "vx-notes");
      notes(view.notes, fuse);
      if (fuse.children.length) body.appendChild(fuse);
      if (preview.reason) {
        const why = el("div", "vx-blocked");
        why.appendChild(icon("info", 14)); why.appendChild(el("span", "", preview.reason));
        body.appendChild(why);
      }
      card.appendChild(body);

      const actions = el("div", "actions");
      if (preview.can_execute && preview.token) {
        const yes = el("button", "btn yes", t("patchInstall")), no = el("button", "btn no", t("cancel"));
        actions.appendChild(yes); actions.appendChild(no);
        no.addEventListener("click", () => finish(card, label, false, t("patchCancelled")));
        yes.addEventListener("click", () => install(card, label, actions, preview));
      }
      if (actions.children.length) card.appendChild(actions);
      return card;
    }

    function finish(card, label, ok, text) {
      card.classList.add("done");
      if (ok) card.classList.add("ok");
      card.querySelector(".actions") && card.querySelector(".actions").remove();
      label.textContent = text;
      const icon0 = card.querySelector(".title svg");
      if (icon0) icon0.replaceWith(icon(ok ? "check" : "shield", 15));
      const more = el("button", "more", t("details"));
      more.type = "button";
      more.addEventListener("click", () => card.classList.toggle("open"));
      card.querySelector(".title").appendChild(more);
    }

    async function install(card, label, actions, preview) {
      if (isBusy()) return;
      setBusy(true); setWorking(t("patching"));
      actions.textContent = "";
      const steps = el("div", "vx-steps");
      actions.replaceWith(steps);
      let current = null;
      const step = (text) => {
        if (current) current.classList.add("done");
        current = el("div", "vx-step");
        current.appendChild(el("span", "dot"));
        current.appendChild(el("span", "", text));
        steps.appendChild(current);
      };
      step(t("vx_installing"));
      let data = null, error = "";
      try {
        await request({ command: "patch", scope: preview.scope, patch_token: preview.token, execute: true }, (frame) => {
          if (frame.event && frame.event.type === "progress" && frame.event.text) step(frame.event.text);
          if (frame.data !== undefined) data = frame.data;
          if (frame.status === "error" && frame.response) error = frame.response;
        });
      } catch (e) { error = e.message; }
      if (current) current.classList.add("done");
      setBusy(false); setWorking("");
      steps.remove();
      if (!data) {
        finish(card, label, false, t("vx_done_fail"));
        const why = el("div", "vx-blocked");
        why.appendChild(icon("info", 14)); why.appendChild(el("span", "", String(error || t("errorPrefix")).replace(/\[(BLAD|ODMOWA)\]\s*/g, "")));
        card.appendChild(why);
        card.classList.add("open");
        return;
      }
      const ok = data.exit_code === 0;
      const fixed = data.fixed || [], unchanged = data.unchanged || [];
      finish(card, label, ok && !unchanged.length, ok ? (fixed.length || unchanged.length
        ? fmt("vx_done_counts", { fixed: fixed.length, same: unchanged.length }) : t("vx_done_ok")) : t("vx_done_fail"));
      card.appendChild(resultBody(data));
      markStale();
      if (data.report) add("agent", report(data.report, { title: t("vx_after") }));
      if (onPatched) onPatched(data);
    }

    function resultBody(data) {
      const box = el("div", "vx-result");
      const chips = (title, list, cls) => {
        if (!list.length) return;
        const row = el("div", "vx-chips");
        row.appendChild(el("span", "vx-label", title));
        list.slice(0, 30).forEach((name) => row.appendChild(el("span", "tag " + cls, name)));
        if (list.length > 30) row.appendChild(el("span", "vx-n", "+" + (list.length - 30)));
        box.appendChild(row);
      };
      chips(t("vx_fixed"), data.fixed || [], "add");
      chips(t("vx_unchanged"), data.unchanged || [], "warn");
      if (data.summary) box.appendChild(el("div", "vx-summary", data.summary));
      if (data.output) {
        const details = el("details", "vx-output");
        const summary = el("summary", "", t("vx_output") + (data.entry_id ? " · #" + data.entry_id : ""));
        details.appendChild(summary);
        const pre = el("pre");
        pre.appendChild(el("code", "", data.output));
        details.appendChild(pre);
        box.appendChild(details);
      }
      return box;
    }

    return { report, patchBox, vulnsMessage, markStale };
  }

  window.PipeVulns = { create };
})();
