// Zakladka "Dodatki" (oba widoki): skille (umiejetnosci z wlasna komenda), serwery MCP (dodatki do aplikacji
// zewnetrznych: GitHub, kalendarz, poczta...) i dodawanie nowych z GitHuba. Bez LLM: komendy `list_skills`, `skill`,
// `mcp_servers`; instalacja idzie przez rozmowe (/pobierz z podgladem i przyciskiem, /mcp dodaj).
(function () {
  "use strict";

  function create(deps) {
    const { el, md, t, command, onRunSkill, onSlash } = deps;
    const root = document.getElementById("addons");
    let loading = false;

    function section(title, count) {
      const box = el("section", "home-sec"), head = el("div", "home-sec-head");
      head.appendChild(el("h3", "", title));
      if (count) head.appendChild(el("span", "hint", String(count)));
      box.appendChild(head);
      return box;
    }
    function skillRow(skill) {
      const row = el("div", "addon-row");
      const head = el("div", "addon-head");
      head.appendChild(el("b", "", skill.name));
      if (skill.command) head.appendChild(el("span", "cmd-chip", "/" + skill.command));
      row.appendChild(head);
      if (skill.description) row.appendChild(el("div", "addon-desc", skill.description));
      const fold = el("div", "fold"), inner = el("div");
      fold.appendChild(inner); row.appendChild(fold);
      const actions = el("div", "addon-actions");
      const go = el("button", "btn run small", t("runSkill")), peek = el("button", "btn no small", t("showSkill"));
      go.type = peek.type = "button";
      go.addEventListener("click", () => onRunSkill(skill));
      peek.addEventListener("click", async () => {
        if (fold.classList.contains("open")) { fold.classList.remove("open"); peek.textContent = t("showSkill"); return; }
        if (!inner.children.length) {
          try { inner.appendChild(md((await command({ command: "skill", name: skill.name })).content || "")); }
          catch (error) { inner.appendChild(el("div", "addon-desc", error.message)); }
        }
        requestAnimationFrame(() => fold.classList.add("open"));
        peek.textContent = t("hideDiff");
      });
      actions.appendChild(go); actions.appendChild(peek);
      row.appendChild(actions);
      return row;
    }
    function serverRow(server) {
      const row = el("div", "addon-row");
      const head = el("div", "addon-head");
      head.appendChild(el("b", "", server.name));
      const ok = server.status === "connected";
      head.appendChild(el("span", "tag " + (ok ? "add" : "mute"), ok ? t("ad_connected") : t("ad_notConnected")));
      row.appendChild(head);
      const actions = el("div", "addon-actions");
      const remove = el("button", "btn no small", t("ad_remove"));
      remove.type = "button";
      remove.addEventListener("click", () => { onSlash("/mcp usun " + server.name); setTimeout(() => refresh(), 1500); });
      actions.appendChild(remove);
      row.appendChild(actions);
      return row;
    }
    function adder(label, placeholder, buttons) {
      const box = el("div", "addon-add");
      box.appendChild(el("span", "field-label", label));
      const field = el("div", "field"), input = el("input");
      input.placeholder = placeholder; input.spellcheck = false; input.autocomplete = "off";
      field.appendChild(input);
      buttons.forEach(([text, run]) => {
        const button = el("button", "btn no small", text);
        button.type = "button";
        button.addEventListener("click", () => { const value = input.value.trim(); if (!value) { input.focus(); return; } run(value); input.value = ""; });
        field.appendChild(button);
      });
      input.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); field.querySelector("button").click(); } });
      box.appendChild(field);
      return box;
    }

    async function refresh() {
      if (loading) return;
      loading = true;
      let skills = [], servers = [], mcpError = "";
      try { skills = (await command({ command: "list_skills" })).skills || []; } catch (_) { /* pusta lista */ }
      try { servers = (await command({ command: "mcp_servers" })).servers || []; } catch (error) { mcpError = error.message; }
      loading = false;
      root.textContent = "";
      root.appendChild(el("p", "lead addons-lead", t("ad_lead")));

      const skillBox = section(t("skills"), skills.length);
      if (skills.length) skills.forEach((skill) => skillBox.appendChild(skillRow(skill)));
      else skillBox.appendChild(el("div", "home-empty", t("noSkills")));
      root.appendChild(skillBox);

      const appBox = section(t("ad_apps"), servers.length);
      if (servers.length) servers.forEach((server) => appBox.appendChild(serverRow(server)));
      else appBox.appendChild(el("div", "home-empty", mcpError || t("ad_noApps")));
      root.appendChild(appBox);

      const addBox = section(t("ad_add"));
      addBox.appendChild(adder(t("ad_fromGithub"), t("ad_fromGithubPh"), [
        [t("ad_skill"), (value) => onSlash("/pobierz skill " + value)],
        [t("ad_plugin"), (value) => onSlash("/pobierz plugin " + value)],
      ]));
      addBox.appendChild(adder(t("ad_mcp"), t("ad_mcpPh"), [
        [t("ad_addButton"), (value) => { onSlash("/mcp dodaj " + value); setTimeout(() => refresh(), 2500); }],
      ]));
      addBox.appendChild(el("p", "hint", t("ad_hint")));
      root.appendChild(addBox);
    }

    return { refresh };
  }

  window.PipeAddons = { create };
})();
