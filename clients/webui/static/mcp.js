// Okno /mcp (jak w Claude Code): lista serwerow MCP ze stanem, katalog gotowych dodatkow (backend/addons), szczegoly
// po wyborze — narzedzia, co dokladnie dziala, klucze (wpisywane tutaj, nigdy w rozmowie), akcje. Strzalki, Enter, Esc.
// Wszystko przez komende `mcp_servers` (bez LLM). Bez innerHTML.
(function () {
  "use strict";

  function create(deps) {
    const { el, t, command, onChanged } = deps;
    const sheet = document.getElementById("mcp");
    const card = sheet.querySelector(".sheet-card");
    let data = null, current = null, note = "", busy = false;

    function fmt(key, values) { return t(key).replace(/\{(\w+)\}/g, (_, k) => (values[k] ?? "")); }
    function statusLabel(server) {
      if (server.status === "connected") return fmt("m_connectedTools", { n: server.tools.length });
      return t("m_" + (server.status === "error" ? "error" : server.status === "disabled" ? "disabled" : "disconnected"));
    }
    function dot(status) { return el("span", "mcp-dot " + (status === "connected" ? "ok" : status === "error" ? "bad" : "")); }
    function link(href, text) {
      const a = el("a", "mcp-link", text);
      a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer";
      return a;
    }

    async function load() {
      try { data = await command({ command: "mcp_servers" }); }
      catch (error) { data = { servers: [], catalog: [], can_edit: false, error: error.message }; }
    }
    async function act(payload, back) {
      if (busy) return;
      busy = true;
      render();
      try { note = (await command({ command: "mcp_servers", ...payload })).text || ""; note = note.split("\n\n")[0]; }
      catch (error) { note = t("errorPrefix") + " " + error.message; }
      busy = false;
      await load();
      if (back) current = null;
      render();
      if (onChanged) onChanged();
    }

    // ---------------------------------------------------------------- widoki
    function frame(title, lead, onBack) {
      card.textContent = "";
      const close = el("button", "icon close", "×");
      close.type = "button"; close.setAttribute("aria-label", t("close"));
      close.addEventListener("click", hide);
      card.appendChild(close);
      const head = el("div", "mcp-head");
      if (onBack) {
        const back = el("button", "icon mcp-back", "‹");
        back.type = "button"; back.setAttribute("aria-label", t("m_back"));
        back.addEventListener("click", onBack);
        head.appendChild(back);
      }
      head.appendChild(el("h2", "", title));
      card.appendChild(head);
      if (lead) card.appendChild(el("p", "lead", lead));
      if (note || busy) card.appendChild(el("div", "mcp-note" + (busy ? " busy" : ""), busy ? t("m_working") : note));
      const body = el("div", "mcp-body");
      card.appendChild(body);
      return body;
    }
    function row(main, side, onOpen, extraClass) {
      const r = el("button", "mcp-row" + (extraClass ? " " + extraClass : ""));
      r.type = "button";
      main.forEach((node) => r.appendChild(node));
      r.appendChild(el("span", "mcp-side", side || ""));
      r.appendChild(el("span", "mcp-chev", "›"));
      r.addEventListener("click", onOpen);
      return r;
    }

    function renderList() {
      const body = frame(t("m_title"), t("m_lead"));
      if (data.error) body.appendChild(el("div", "mcp-empty", data.error));
      body.appendChild(el("div", "mcp-section", t("m_servers")));
      if (!data.servers.length) body.appendChild(el("div", "mcp-empty", t("m_none")));
      data.servers.forEach((server) => {
        const name = el("span", "mcp-name", server.name);
        body.appendChild(row([dot(server.status), name], statusLabel(server), () => { current = { server: server.name }; note = ""; render(); }));
      });
      const ready = data.catalog.filter((a) => !a.installed);
      if (ready.length) {
        body.appendChild(el("div", "mcp-section", t("m_catalog")));
        ready.forEach((addon) => {
          const text = el("span", "mcp-two");
          text.appendChild(el("span", "mcp-name", addon.title));
          text.appendChild(el("span", "mcp-desc", addon.description));
          const side = addon.secrets.length ? (addon.ready ? t("m_keysReady") : t("m_needKey")) : (addon.login ? t("m_loginInChat") : "");
          body.appendChild(row([el("span", "mcp-plus", "+"), text], side, () => { current = { addon: addon.name }; note = ""; render(); }, "catalog"));
        });
      }
      body.appendChild(el("p", "hint mcp-foot", data.can_edit ? t("m_hint") : t("m_viewOnly")));
      focusFirst();
    }

    function secretsForm(body, secrets, canEdit) {
      if (!secrets.length) return;
      body.appendChild(el("div", "mcp-section", t("m_secrets")));
      secrets.forEach((secret) => {
        const box = el("div", "mcp-secret");
        const head = el("div", "mcp-secret-head");
        head.appendChild(el("b", "", secret.label || secret.name));
        head.appendChild(el("code", "", secret.name));
        head.appendChild(el("span", "tag " + (secret.set ? "add" : "mute"), secret.set ? t("m_secretSet") : t("m_secretMissing")));
        box.appendChild(head);
        if (secret.help) box.appendChild(el("div", "mcp-desc", secret.help));
        if (secret.url) box.appendChild(link(secret.url, t("m_where")));
        if (canEdit) {
          const form = el("form", "field mcp-key");
          const input = el("input");
          input.type = "password"; input.autocomplete = "off"; input.spellcheck = false;
          input.placeholder = secret.set ? t("m_keyReplace") : t("m_keyPh");
          input.setAttribute("aria-label", secret.name);
          const save = el("button", "btn no small", t("m_save"));
          save.type = "submit";
          form.appendChild(input); form.appendChild(save);
          form.addEventListener("submit", (event) => {
            event.preventDefault();
            const value = input.value.trim();
            if (!value) { input.focus(); return; }
            input.value = "";
            act({ args: "klucz", name: secret.name, key: value });
          });
          box.appendChild(form);
        }
        body.appendChild(box);
      });
      body.appendChild(el("p", "hint", t("m_keysHint")));
    }

    function actions(body, buttons) {
      const bar = el("div", "mcp-actions");
      buttons.forEach(([label, cls, run]) => {
        const b = el("button", "btn small " + cls, label);
        b.type = "button"; b.disabled = busy;
        b.addEventListener("click", run);
        bar.appendChild(b);
      });
      body.appendChild(bar);
    }

    function renderServer(server) {
      const back = () => { current = null; note = ""; render(); };
      const body = frame(server.name, server.description, back);
      const facts = el("dl", "mcp-facts");
      const fact = (key, value, cls) => { if (!value) return; facts.appendChild(el("dt", "", t(key))); facts.appendChild(el("dd", cls || "", value)); };
      const status = el("dd", "mcp-status");
      status.appendChild(dot(server.status)); status.appendChild(document.createTextNode(statusLabel(server)));
      facts.appendChild(el("dt", "", t("m_status"))); facts.appendChild(status);
      fact("m_errorLabel", server.error, "bad");
      fact("m_version", server.version);
      fact("m_runs", server.runs, "mono");
      fact("m_source", server.source);
      body.appendChild(facts);
      const addon = data.catalog.find((a) => a.name === (server.addon || server.name) && a.installed);
      secretsForm(body, addon ? addon.secrets : server.secrets.map((s) => ({ ...s, label: s.name })), data.can_edit);
      if (addon && addon.login) body.appendChild(el("p", "mcp-login", addon.login));
      if (server.tools.length) {
        body.appendChild(el("div", "mcp-section", fmt("m_toolsCount", { n: server.tools.length })));
        const list = el("div", "mcp-tools");
        server.tools.forEach((tool) => {
          const item = el("div", "mcp-tool");
          const head = el("div", "mcp-tool-head");
          head.appendChild(el("code", "", tool.name));
          head.appendChild(el("span", "tag " + (tool.ask ? "warn" : "mute"), tool.ask ? t("m_asks") : tool.read ? t("m_read") : t("m_noAsk")));
          item.appendChild(head);
          if (tool.description) item.appendChild(el("div", "mcp-desc", tool.description));
          list.appendChild(item);
        });
        body.appendChild(list);
      }
      if (data.can_edit) {
        let confirmRemove = false;
        const buttons = [
          [t("m_reconnect"), "no", () => act({ args: "odswiez" })],
          [server.enabled ? t("m_disable") : t("m_enable"), "no", () => act({ args: (server.enabled ? "wylacz " : "wlacz ") + server.name })],
          [t("m_remove"), "no danger", (event) => {
            if (!confirmRemove) { confirmRemove = true; event.target.textContent = t("m_removeConfirm"); return; }
            act({ args: "usun " + server.name }, true);
          }],
        ];
        actions(body, buttons);
      }
      focusFirst();
    }

    function renderAddon(addon) {
      const back = () => { current = null; note = ""; render(); };
      const body = frame(addon.title, addon.description, back);
      const facts = el("dl", "mcp-facts");
      facts.appendChild(el("dt", "", t("m_runs"))); facts.appendChild(el("dd", "mono", addon.runs));
      body.appendChild(facts);
      if (addon.homepage) body.appendChild(link(addon.homepage, t("m_homepage")));
      secretsForm(body, addon.secrets, data.can_edit);
      if (addon.login) body.appendChild(el("p", "mcp-login", t("m_afterAdd") + " " + addon.login));
      if (data.can_edit) {
        const add = [addon.ready ? t("m_add") : t("m_addNeedsKeys"), "yes", () => act({ args: "dodaj " + addon.name }, true)];
        actions(body, [add]);
        const button = body.querySelector(".mcp-actions .btn");
        if (!addon.ready) button.disabled = true;
      }
      focusFirst();
    }

    function render() {
      if (!data) return;
      if (current && current.server) {
        const server = data.servers.find((s) => s.name === current.server);
        if (server) return renderServer(server);
      }
      if (current && current.addon) {
        const addon = data.catalog.find((a) => a.name === current.addon);
        if (addon && !addon.installed) return renderAddon(addon);
        const installed = addon && data.servers.find((s) => s.name === addon.name);
        if (installed) { current = { server: installed.name }; return renderServer(installed); }
      }
      current = null;
      renderList();
    }

    // ---------------------------------------------------------------- klawiatura
    function focusFirst() {
      if (busy) return;
      requestAnimationFrame(() => {
        // lista: pierwsza pozycja; szczegoly: strzalka wstecz u gory (fokus na przycisku na dole przewijal okno)
        const first = card.querySelector(current ? ".mcp-back" : ".mcp-row");
        if (first) first.focus({ preventScroll: true });
      });
    }
    sheet.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        if (current) { current = null; note = ""; render(); } else hide();
        return;
      }
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      if (event.target.tagName === "INPUT") return;
      const rows = [...card.querySelectorAll(".mcp-row, .mcp-actions .btn:not(:disabled)")];
      if (!rows.length) return;
      event.preventDefault();
      const index = rows.indexOf(document.activeElement);
      const next = event.key === "ArrowDown" ? Math.min(rows.length - 1, index + 1) : Math.max(0, index - 1);
      rows[index < 0 ? 0 : next].focus();
    });
    sheet.addEventListener("click", (event) => { if (event.target === sheet) hide(); });

    async function open(name) {
      note = ""; current = name ? { server: name, addon: name } : null;
      card.textContent = "";
      card.appendChild(el("div", "mcp-note busy", t("m_loading")));
      sheet.classList.remove("out"); sheet.hidden = false;
      await load();
      if (current && !data.servers.some((s) => s.name === name)) current = { addon: name };
      render();
    }
    function hide() {
      sheet.classList.add("out");
      setTimeout(() => { sheet.hidden = true; sheet.classList.remove("out"); }, 160);
    }

    return { open, hide };
  }

  window.PipeMcp = { create };
})();
