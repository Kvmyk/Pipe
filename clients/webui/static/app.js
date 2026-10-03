// Pipe Web — czat + schemat na zywo + widok zmian. Rozmawia z lokalnym mostem (clients/webui/server.py):
// POST /api/request (strumien ramek NDJSON) i GET /api/events (zdarzenia czuwania, SSE).
(function () {
  "use strict";
  const config = JSON.parse(document.getElementById("pipe-config").textContent || "{}");
  const { t } = window.PipeI18n;
  window.PipeI18n.setLang(config.lang);
  const { render: md, el, diffBlock } = window.PipeMd;
  const $ = (id) => document.getElementById(id);
  let session = config.session || String(Date.now());
  // Po zmianie jezyka strona odswieza sie w nowym — rozmowa z agentem (sesja) zostaje ta sama.
  let languageNote = "";
  try {
    session = sessionStorage.getItem("pipe-session") || session;
    languageNote = sessionStorage.getItem("pipe-language-note") || "";
    sessionStorage.removeItem("pipe-session"); sessionStorage.removeItem("pipe-language-note");
  } catch (_) { /* tryb prywatny */ }
  if (location.search.includes("k=")) window.history.replaceState(null, "", location.pathname);   // klucz jest juz w ciasteczku

  // ---------------------------------------------------------------- most
  async function request(payload, onFrame) {
    const response = await fetch("/api/request", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...payload, session }),
    });
    if (!response.ok) {
      let message = "HTTP " + response.status;
      try { message = (await response.json()).error || message; } catch (_) { /* brak tresci */ }
      setLink(false, message);
      throw new Error(message);
    }
    setLink(true);
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, cut).trim();
        buffer = buffer.slice(cut + 1);
        if (!line) continue;
        let frame;
        try { frame = JSON.parse(line); } catch (_) { continue; }
        await onFrame(frame);
      }
    }
  }
  async function command(payload) {
    let data = null, error = "";
    await request(payload, (frame) => {
      if (frame.data !== undefined) data = frame.data;
      if (frame.status === "error" && frame.response) error = frame.response;
    });
    if (data === null) throw new Error(stripTags(error) || "no data");
    return data;
  }
  function setLink(ok, message) {
    $("link-dot").className = "dot " + (ok ? "ok" : "bad");
    $("server-pill").title = ok ? t("online") : (message || t("offline"));
  }
  function stripTags(text) { return String(text || "").replace(/\[(BLAD|ODMOWA|POTWIERDZ|OSTRZEZENIE|PAMIEC|SUKCES)\]\s*/g, "").trim(); }

  // ---------------------------------------------------------------- czat
  const messages = $("messages"), working = $("working"), input = $("input");
  let busy = false, pendingCard = null;
  // Przewijamy na dol tylko wtedy, gdy uzytkownik juz tam byl (mierzone PRZED dodaniem wiadomosci) —
  // kto przewinal wyzej, zeby cos przeczytac, nie zostanie zerwany nowa odpowiedzia.
  function nearBottom() { return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 140; }
  function scrollDown() { requestAnimationFrame(() => messages.scrollTo({ top: messages.scrollHeight, behavior: "smooth" })); }

  function welcome() {
    const box = el("div", "welcome");
    box.appendChild(el("h1", "", t("welcomeTitle")));
    box.appendChild(el("p", "", t("welcomeText")));
    messages.appendChild(box);
  }
  function add(kind, node) {
    const first = messages.querySelector(".welcome");
    if (first) first.remove();
    const follow = kind === "user" || nearBottom();
    const wrap = el("div", "msg " + kind);
    if (typeof node === "string") wrap.textContent = node; else wrap.appendChild(node);
    messages.appendChild(wrap);
    if (follow) scrollDown();
    return wrap;
  }
  function setWorking(text) {
    working.hidden = !text;
    $("working-text").textContent = text || "";
  }

  function showText(text, status) {
    const raw = String(text || "").trim();
    if (!raw) return;
    if (raw.startsWith("[PAMIEC]")) { add("note", t("memory") + ": " + stripTags(raw)); return; }
    if (status === "confirm" || raw.includes("[POTWIERDZ]")) { confirmCard(stripTags(raw)); return; }
    if (raw.startsWith("[OSTRZEZENIE]")) { add("note", t("warning") + ": " + stripTags(raw)); return; }
    const refused = raw.includes("[ODMOWA]"), failed = status === "error" || raw.includes("[BLAD]");
    const body = md(stripTags(raw));
    if (refused || failed) body.insertBefore(el("span", "tag " + (refused ? "warn" : "del"), refused ? t("refused") : t("errorPrefix")), body.firstChild);
    add("agent" + (failed && !refused ? " error" : ""), body);
  }

  function confirmCard(text) {
    const card = el("div", "confirm");
    const title = el("div", "title");
    title.appendChild(el("span", "tag warn", t("needsConfirm")));
    title.appendChild(document.createTextNode(t("confirmTitle")));
    card.appendChild(title);
    card.appendChild(md(text));
    const actions = el("div", "actions");
    const yes = el("button", "btn yes", t("yes")), no = el("button", "btn no", t("no"));
    actions.appendChild(yes); actions.appendChild(no);
    card.appendChild(actions);
    const settle = (label) => { actions.remove(); card.classList.add("done"); card.appendChild(el("div", "verdict", label)); pendingCard = null; };
    yes.addEventListener("click", () => { settle(t("approved")); run({ confirm: true }); });
    no.addEventListener("click", () => { settle(t("rejected")); run({ confirm: false }); });
    card.supersede = () => settle(t("superseded"));
    pendingCard = card;
    add("agent", card);
  }

  function showAttachment(attachment) {
    const body = el("div", "body");
    if ((attachment.mime || "").startsWith("image/") && attachment.data) {
      const img = el("img");
      img.src = `data:${attachment.mime};base64,${attachment.data}`;
      img.alt = attachment.caption || attachment.name || t("attachment");
      img.addEventListener("click", () => { $("lightbox").firstElementChild.src = img.src; $("lightbox").hidden = false; });
      body.appendChild(img);
    }
    if (attachment.caption) body.appendChild(el("div", "caption", attachment.caption));
    add("agent", body);
  }

  async function run(payload, label) {
    if (busy) return;
    busy = true;
    $("send").disabled = true;
    setWorking(label || t("thinking"));
    const changed = [];
    try {
      await request(payload, async (frame) => {
        if (frame.attachment) showAttachment(frame.attachment);
        const event = frame.event;
        if (event && event.type === "progress") { setWorking(event.text); activityStep(event); }
        else if (event && event.type === "activity") { const entry = await onActivity(event); if (entry) changed.push(entry); }
        else if (frame.response) { showText(frame.response, frame.status); setWorking(t("working")); }
      });
    } catch (error) {
      add("agent error", md(t("errorPrefix") + ": " + error.message));
    } finally {
      busy = false;
      $("send").disabled = false;
      setWorking("");
      settleActivities();
      input.focus();
    }
    for (const entry of changed) await showChange(entry);
    if (changed.length) loadChanges();
    loadSkills();
    loadGraph(true);
  }

  async function send(text) {
    text = text.trim();
    if (!text || busy) return;
    closePalette();
    input.value = ""; autosize();
    if (text.startsWith("/") && await slash(text)) return;
    if (pendingCard) pendingCard.supersede();
    add("user", text);
    await run({ message: text });
  }
  function autosize() { input.style.height = "auto"; input.style.height = Math.min(160, input.scrollHeight) + "px"; }
  input.addEventListener("input", () => { autosize(); updatePalette(); });
  input.addEventListener("keydown", (event) => {
    if (paletteKey(event)) return;
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(input.value); }
  });
  input.addEventListener("blur", () => setTimeout(closePalette, 140));
  $("composer").addEventListener("submit", (event) => { event.preventDefault(); send(input.value); });

  // szybkie akcje
  const chips = [
    ["chipStatus", () => { add("user", t("chipStatus")); run({ command: "status" }, t("working")); }],
    ["chipChanges", async () => { add("user", t("chipChanges")); await dataMessage({ command: "changes" }, (d) => listing(d.text)); }],
    ["chipAudit", async () => { add("user", t("chipAudit")); await dataMessage({ command: "audit" }, (d) => listing(d.text)); }],
    ["chipReport", async () => { add("user", t("chipReport")); await dataMessage({ command: "digest" }, digestBody); }],
  ];
  chips.forEach(([key, action]) => {
    const chip = el("button", "chip", t(key));
    chip.type = "button";
    chip.addEventListener("click", () => { if (!busy) action(); });
    $("chips").appendChild(chip);
  });
  function plain(text) {
    const body = el("div", "body"), pre = el("pre");
    pre.appendChild(el("code", "", text || ""));
    body.appendChild(pre);
    return body;
  }
  // Zestawienie od backendu (zmiany, zdrowie, koszt, audyt...) jako czytelna lista: linia zakonczona ":" to naglowek,
  // "- " to pozycja, wciecie 4+ spacji to szczegol (np. roznica) czcionka o stalej szerokosci, #ab12cd — identyfikator.
  function listing(text, title) {
    const box = el("div", "body listing");
    if (title) box.appendChild(el("div", "lh", title));
    const withIds = (line, node) => {
      const match = line.match(/^(.*?)(#[0-9a-f]{6,10})\b(.*)$/);
      if (!match) { node.appendChild(document.createTextNode(line)); return node; }
      node.appendChild(document.createTextNode(match[1])); node.appendChild(el("code", "", match[2])); node.appendChild(document.createTextNode(match[3]));
      return node;
    };
    String(text || "").replace(/\r/g, "").split("\n").forEach((raw) => {
      const line = raw.trim(), indent = raw.length - raw.trimStart().length;
      if (!line) return;
      if (indent >= 4) box.appendChild(el("div", "ld", line));
      else if (/^[-•] /.test(line)) box.appendChild(withIds(line.slice(2), el("div", "li")));
      else if (/^\d+\. /.test(line) && indent === 0) box.appendChild(el("div", "lh", line));
      else if (line.endsWith(":") && !line.startsWith("#")) box.appendChild(el("div", "lh", line));
      else if (indent >= 2 || line.startsWith("#")) box.appendChild(withIds(line, el("div", "li")));
      else box.appendChild(withIds(line, el("div", "lp")));
    });
    if (box.children.length === (title ? 1 : 0)) box.appendChild(el("div", "lp", t("emptyList")));
    return box;
  }
  function digestBody(data) {
    const lines = ["**" + (data.title || t("report")) + "**"];
    (data.sections || []).forEach((section) => { lines.push("", "**" + section.title + "**"); (section.lines || []).forEach((l) => lines.push("- " + l)); });
    return md(lines.join("\n"));
  }
  async function dataMessage(payload, build) {
    if (busy) return;
    busy = true; setWorking(t("working"));
    try {
      let data = null, error = "";
      await request(payload, (frame) => {
        if (frame.attachment) showAttachment(frame.attachment);
        if (frame.data !== undefined) data = frame.data;
        if (frame.status === "error") error = frame.response;
      });
      if (data) add("agent", build(data)); else add("agent error", md(stripTags(error) || t("errorPrefix")));
    } catch (error) { add("agent error", md(t("errorPrefix") + ": " + error.message)); }
    finally { busy = false; setWorking(""); }
  }

  // ---------------------------------------------------------------- schemat
  let follow = true, hinted = false;
  const activities = new Map();      // id -> { event, steps, row }
  const pastActions = [];            // zakonczone dzialania (do szczegolow wezla)
  const graph = window.PipeGraph.create({
    svg: $("graph"), t,
    onUserMove: () => setFollow(false, true),
    onOpen: (node) => { closeDetail(); graph.show(node.opens); },
    onSelect: (node) => (node ? openDetail(node) : closeDetail()),
    onView: (viewId) => renderCrumbs(viewId),
  });

  function setFollow(on, byUser) {
    if (follow === on) return;
    follow = on;
    $("follow").classList.toggle("on", on);
    $("follow").setAttribute("aria-pressed", String(on));
    $("follow-label").textContent = on ? t("follow") : t("followOff");
    if (!on && byUser && !hinted && activities.size) { hinted = true; toast(t("followOff"), t("followHint")); }
    if (on) followAgent();
  }
  $("follow").addEventListener("click", () => setFollow(!follow));
  $("fit").addEventListener("click", () => graph.fit());
  $("refresh").addEventListener("click", () => loadGraph(false));

  function renderCrumbs(viewId) {
    const data = graph.data, crumbs = $("crumbs");
    crumbs.textContent = "";
    const chain = [];
    for (let id = viewId; id && data.views[id]; id = data.views[id].parent) chain.unshift(id);
    if (data.root === "host" && chain[0] === "fleet") chain.shift();
    chain.forEach((id, index) => {
      if (index) crumbs.appendChild(el("i", "", "›"));
      const button = el("button", id === viewId ? "here" : "", data.views[id].title);
      if (id !== viewId) button.addEventListener("click", () => { setFollow(false, true); closeDetail(); graph.show(id); });
      crumbs.appendChild(button);
    });
  }

  let graphTimer = 0, graphLoading = false;
  async function loadGraph(quiet) {
    if (graphLoading) return;
    graphLoading = true;
    $("refresh").classList.add("spin");
    if (!graph.data) { $("map-empty").hidden = false; $("map-empty").textContent = t("loadingMap"); }
    try {
      const data = await command({ command: "graph" });
      graph.setData(data);
      $("map-empty").hidden = true;
      $("server-name").textContent = data.hostname || config.server || "";
      pushStates();
      if (!quiet && (data.notes || []).length) toast(t("warning"), data.notes.join(" "));
    } catch (error) {
      if (!graph.data) { $("map-empty").hidden = false; $("map-empty").textContent = t("mapEmpty") + " " + error.message; }
    } finally {
      graphLoading = false;
      $("refresh").classList.remove("spin");
      clearTimeout(graphTimer);
      graphTimer = setTimeout(() => { if (!document.hidden && !busy) loadGraph(true); }, 60000);
    }
  }

  function pushStates() {
    const states = new Map();
    activities.forEach(({ event, lingering }) => {
      const state = event.phase === "wait" ? "waiting" : event.phase === "start" || lingering ? "active" : null;
      if (state) event.nodes.forEach((id) => { if (state === "active" || !states.has(id)) states.set(id, state); });
    });
    graph.setStates(states);
    $("map").dataset.busy = states.get("host") && graph.view === "host" ? states.get("host") : "";
  }
  // Sledzenie bez szarpania: po zmianie widoku zostajemy w nim co najmniej DWELL ms, nawet gdy agent
  // w tym czasie zdazy zrobic kilka szybkich krokow gdzie indziej — wtedy przechodzimy od razu do ostatniego.
  const DWELL = 1500, LINGER = 950;
  let lastMove = 0, followTimer = 0;
  async function followAgent() {
    if (!follow || !graph.data) return;
    const live = [...activities.values()].reverse().find((a) => a.event.phase !== "end" || a.lingering);
    if (!live) return;
    const id = live.event.nodes[0];
    const view = graph.deepestView(id);
    if (view && view !== graph.view) {
      const wait = lastMove + DWELL - performance.now();
      if (wait > 0) { clearTimeout(followTimer); followTimer = setTimeout(followAgent, wait + 20); return; }
      closeDetail();
      lastMove = performance.now();
      if (!(await graph.show(view))) { clearTimeout(followTimer); followTimer = setTimeout(followAgent, 300); return; }
    }
    const local = graph.inView(id, graph.view);
    if (local && local !== "host") graph.focus(local);
  }

  async function onActivity(event) {
    let item = activities.get(event.id);
    if (!item) { item = { event, steps: [], row: null, started: performance.now() }; activities.set(event.id, item); }
    if (event.phase === "start") item.started = performance.now();
    item.event = event;
    if (event.phase === "end") {
      // Szybkie kroki (kilka ms) bylyby niewidoczne — wezel zostaje podswietlony przez chwile, potem blysk wyniku.
      const finish = () => {
        item.lingering = false;
        if (activities.get(event.id) === item) activities.delete(event.id);
        renderActivity(item);
        if (event.ok !== null) event.nodes.forEach((id) => graph.flash(id, event.ok));
        pastActions.unshift({ event, steps: item.steps.slice(), at: new Date() });
        pastActions.splice(60);
        pushStates(); refreshDetail();
      };
      const rest = LINGER - (performance.now() - item.started);
      if (rest > 0 && event.ok !== null) { item.lingering = true; pushStates(); setTimeout(finish, rest); } else finish();
      return event.entry || "";
    }
    renderActivity(item);
    pushStates();
    setWorking(event.phase === "wait" ? "" : event.label);
    await followAgent();
    refreshDetail();
    return "";
  }
  function activityStep(event) {
    // postep bezpiecznika i workerow dopisujemy do trwajacego dzialania
    const running = [...activities.values()].reverse().find((a) => a.event.phase === "start");
    if (!running) return;
    const source = String(event.source || "");
    running.steps.push((source.startsWith("worker:") ? source.slice(7) + ": " : "") + event.text);
    running.steps.splice(0, Math.max(0, running.steps.length - 6));
    renderActivity(running);
    if (source.startsWith("worker:")) {
      const target = running.event.workers && running.event.workers[source.slice(7)];
      if (target) { workerLog[target] = (workerLog[target] || []).concat(event.text).slice(-12); refreshDetail(); }
    }
  }
  const workerLog = {};
  function settleActivities() {
    // koniec tury: nic nie moze zostac w stanie "trwa" (np. po bledzie polaczenia); oczekujace na zgode zostaja
    activities.forEach((item, id) => {
      if (item.event.phase === "start" && !item.lingering) { item.event = { ...item.event, phase: "end", ok: null }; renderActivity(item); activities.delete(id); }
    });
    pushStates();
  }

  const timeline = $("timeline");
  timeline.dataset.empty = t("timelineEmpty");
  function renderActivity(item) {
    const { event } = item;
    if (!item.row) {
      item.row = el("div", "act");
      item.row.appendChild(el("span", "mark"));
      item.row.appendChild(el("span", "what"));
      item.row.appendChild(el("span", "where"));
      item.row.appendChild(el("span", "steps"));
      item.row.addEventListener("click", () => {
        setFollow(false, true);
        const id = item.event.nodes[0], view = graph.deepestView(id);
        const go = view && view !== graph.view ? graph.show(view) : Promise.resolve();
        go.then(() => { const local = graph.inView(id, graph.view); if (local && local !== "host") { graph.focus(local); graph.select(local); const n = graph.node(graph.view, local); if (n) openDetail(n); } });
      });
      timeline.insertBefore(item.row, timeline.firstChild);
      while (timeline.children.length > 40) timeline.lastChild.remove();
    }
    const mark = item.row.children[0];
    mark.className = "mark " + (event.phase === "start" || item.lingering ? "run" : event.phase === "wait" ? "wait" : event.ok === true ? "ok" : event.ok === false ? "fail" : "skip");
    const what = item.row.children[1];
    what.textContent = "";
    what.appendChild(["execute_command", "remote_exec", "git_command", "docker_manage"].includes(event.tool) ? el("code", "", event.label) : document.createTextNode(event.label));
    what.title = event.label;
    item.row.children[2].textContent = event.nodes.map((id) => graph.label(id)).slice(0, 2).join(", ");
    const steps = item.row.children[3];
    steps.textContent = "";
    item.steps.slice(-3).forEach((step) => steps.appendChild(el("span", "", "· " + step)));
  }

  // szczegoly wezla
  const detail = $("detail");
  let detailNode = null;
  function closeDetail() { detail.hidden = true; detailNode = null; graph.select(null); }
  function refreshDetail() { if (detailNode) openDetail(detailNode, true); }
  function touches(event, node) { return event.nodes.some((id) => graph.inView(id, graph.view) === node.id); }
  function openDetail(node, keep) {
    detailNode = node;
    detail.textContent = "";
    const close = el("button", "icon close", "×");
    close.setAttribute("aria-label", t("close"));
    close.addEventListener("click", closeDetail);
    detail.appendChild(close);
    detail.appendChild(el("h3", "", node.label));
    detail.appendChild(el("div", "kind", t("k_" + node.kind) + (node.sub ? " · " + node.sub : "")));
    const meta = node.meta || {}, dl = el("dl");
    const row = (key, value) => { if (value === undefined || value === null || value === "" || (Array.isArray(value) && !value.length)) return;
      dl.appendChild(el("dt", "", t("m_" + key))); dl.appendChild(el("dd", "", Array.isArray(value) ? value.join(", ") : String(value))); };
    row("status", meta.status); row("image", meta.image); row("ports", meta.ports); row("service", meta.service);
    row("workdir", meta.workdir); row("path", meta.path); row("port", meta.port); row("type", meta.type); row("namespace", meta.namespace);
    if (meta.public !== undefined && typeof meta.public === "boolean") row("public", meta.public ? t("yesWord") : t("noWord"));
    if (meta.restarts) row("restarts", meta.restarts);
    if (meta.containers && Array.isArray(meta.containers)) row("containers", meta.containers);
    if (dl.children.length) detail.appendChild(dl);
    const section = (title, rows) => { if (!rows.length) return; detail.appendChild(el("h4", "", title)); rows.forEach((text) => detail.appendChild(el("div", "row", text))); };
    section(t("alertsHere"), (node.alerts || []).map((a) => a.title));
    section(t("routes"), (meta.routes || []).map((r) => (r.domains.join(", ") || "*") + " → " + r.upstream));
    section(t("services"), (meta.services || []).slice(0, 24));
    const live = [...activities.values()].filter((a) => touches(a.event, node));
    section(t("nowHere"), live.filter((a) => a.event.phase === "start").flatMap((a) => [a.event.label, ...a.steps.slice(-4).map((s) => "· " + s)]));
    section(t("waitingHere"), live.filter((a) => a.event.phase === "wait").map((a) => a.event.label));
    const target = meta.target && workerLog[meta.target];
    if (target) section(t("worker"), target.slice(-8));
    const past = pastActions.filter((h) => touches(h.event, node)).slice(0, 8)
      .map((h) => `${h.at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} ${h.event.ok === false ? "✕" : "✓"} ${h.event.label}`);
    if (past.length) section(t("recent"), past); else if (!live.length) { detail.appendChild(el("h4", "", t("recent"))); detail.appendChild(el("div", "row", t("nothingHere"))); }
    detail.hidden = false;
    if (!keep) {
      detail.style.animation = "none"; void detail.offsetWidth; detail.style.animation = "";
      graph.focus(node.id, 420, detail.offsetWidth + 20);        // wezel nie moze zostac pod panelem
    }
  }

  // ---------------------------------------------------------------- zmiany
  const changesList = $("changes");
  let unseenChanges = 0;
  function statusTag(status) { return el("span", "tag " + ({ added: "add", deleted: "del" }[status] || ""), t("st_" + status)); }
  function changeBody(data) {
    const box = el("div");
    const files = (data.files || []).filter((f) => f.diff || f.note || f.status !== "unchanged");
    files.forEach((file) => {
      if (file.diff) {
        const block = diffBlock(file.diff.split("\n"), file.path);
        block.querySelector(".file").appendChild(statusTag(file.status));
        box.appendChild(block);
      } else {
        const line = el("div", "meta");
        line.appendChild(document.createTextNode(file.path + " — "));
        line.appendChild(document.createTextNode(file.note || t("st_" + file.status)));
        box.appendChild(line);
      }
    });
    if (!files.length) box.appendChild(el("div", "meta", t("noFileDiff")));
    if ((data.inverse || []).length) box.appendChild(el("div", "cmd", t("inverse") + ": " + data.inverse.join("; ")));
    if ((data.notes || []).length) box.appendChild(el("div", "meta", t("notes") + ": " + data.notes.join("; ")));
    return box;
  }
  async function showChange(entryId) {
    // po wykonaniu zmiany: roznica od razu w rozmowie
    try {
      const data = await command({ command: "journal_changes", id: entryId });
      if (!(data.files || []).some((f) => f.diff)) return;
      const body = el("div", "body");
      body.appendChild(el("div", "caption", t("changes") + " #" + data.id));
      body.appendChild(changeBody(data));
      add("agent", body);
      if ($("tab-changes").hidden) { unseenChanges++; badge("changes-badge", unseenChanges); }
    } catch (_) { /* brak wpisu — nic nie pokazujemy */ }
  }
  async function loadChanges() {
    try {
      const data = await command({ command: "journal" });
      changesList.textContent = "";
      const entries = data.entries || [];
      if (!entries.length) { changesList.appendChild(el("div", "empty", t("noChanges"))); return; }
      entries.forEach((entry) => changesList.appendChild(changeCard(entry)));
    } catch (error) { changesList.textContent = ""; changesList.appendChild(el("div", "empty", error.message)); }
  }
  function changeCard(entry) {
    const card = el("div", "card-item");
    const head = el("div", "head");
    head.appendChild(el("b", "", "#" + entry.id));
    head.appendChild(el("span", "tag " + ({ failed: "del", restored: "warn", rolled_back: "warn", aborted: "warn" }[entry.status] || "add"), entry.status));
    card.appendChild(head);
    card.appendChild(el("div", "cmd", entry.summary.replace(/^#\w+\s*/, "")));
    const holder = el("div"); card.appendChild(holder);
    const foot = el("div", "foot");
    const toggle = el("button", "btn no small", t("showDiff"));
    toggle.addEventListener("click", async () => {
      if (holder.children.length) { holder.textContent = ""; toggle.textContent = t("showDiff"); return; }
      toggle.textContent = "…";
      try { holder.appendChild(changeBody(await command({ command: "journal_changes", id: entry.id }))); toggle.textContent = t("hideDiff"); }
      catch (error) { holder.appendChild(el("div", "meta", error.message)); toggle.textContent = t("showDiff"); }
    });
    foot.appendChild(toggle);
    if (entry.undoable) {
      const undo = el("button", "btn no small", t("undo"));
      undo.addEventListener("click", async () => {
        holder.textContent = "";
        try {
          const preview = await command({ command: "undo", id: entry.id });
          holder.appendChild(md(preview.preview || ""));
          if (!preview.undoable) return;
          const ask = el("div", "foot"), yes = el("button", "btn yes small", t("undoAsk")), no = el("button", "btn no small", t("no"));
          ask.appendChild(yes); ask.appendChild(no); holder.appendChild(ask);
          no.addEventListener("click", () => { holder.textContent = ""; });
          yes.addEventListener("click", async () => {
            ask.remove();
            try { const result = await command({ command: "undo", id: entry.id, execute: true }); toast(t("undoDone") + " #" + entry.id, result.text || ""); }
            catch (error) { toast(t("errorPrefix"), error.message); }
            loadChanges(); loadGraph(true);
          });
        } catch (error) { holder.appendChild(el("div", "meta", error.message)); }
      });
      foot.appendChild(undo);
    }
    card.appendChild(foot);
    return card;
  }

  // ---------------------------------------------------------------- alerty i zdarzenia na zywo
  const alertsList = $("alerts");
  const liveEvents = [];
  let activeAlerts = [], unseenAlerts = 0;
  function badge(id, count) { const node = $(id); node.hidden = !count; node.textContent = String(count); }
  function renderAlerts() {
    alertsList.textContent = "";
    if (!activeAlerts.length && !liveEvents.length) { alertsList.appendChild(el("div", "empty", t("noAlerts"))); return; }
    if (activeAlerts.length) {
      alertsList.appendChild(el("div", "section-title", t("active")));
      activeAlerts.forEach((alert) => {
        const card = el("div", "card-item alert-" + alert.severity);
        const head = el("div", "head");
        head.appendChild(el("span", "tag " + (alert.severity === "critical" ? "del" : "warn"), alert.severity));
        head.appendChild(el("b", "", alert.title));
        card.appendChild(head);
        if (alert.detail) card.appendChild(el("div", "meta", alert.detail));
        if (alert.history) card.appendChild(el("div", "meta", alert.history));
        const foot = el("div", "foot"), look = el("button", "btn no small", t("investigate"));
        look.addEventListener("click", () => { if (busy) return; switchPane("chat"); add("user", t("investigate") + ": " + alert.title); run({ command: "investigate", id: alert.id }, t("working")); });
        foot.appendChild(look); card.appendChild(foot);
        alertsList.appendChild(card);
      });
    }
    if (liveEvents.length) {
      alertsList.appendChild(el("div", "section-title", t("live")));
      liveEvents.forEach((event) => alertsList.appendChild(eventCard(event)));
    }
  }
  function eventTitle(event) {
    switch (event.type) {
      case "alert": return (event.state === "resolved" ? t("resolved") + " — " : "") + event.title;
      case "reminder": return (event.kind === "task" ? t("task") : t("reminder")) + ": " + event.text;
      case "routine": return t("routine") + " " + event.name + " — " + event.status;
      case "digest": case "welcome": return event.title || t("report");
      case "investigation": return t("investigation") + " — " + (event.title || "");
      case "approval": return t("approval") + ": " + (event.command || "");
      default: return event.type;
    }
  }
  function eventCard(event) {
    const card = el("div", "card-item" + (event.type === "alert" && event.state !== "resolved" ? " alert-" + event.severity : ""));
    const head = el("div", "head");
    head.appendChild(el("b", "", eventTitle(event)));
    card.appendChild(head);
    card.appendChild(el("div", "meta", event.at || ""));
    const text = event.report || event.detail || (event.sections || []).map((s) => s.title + "\n" + (s.lines || []).map((l) => "• " + l).join("\n")).join("\n\n");
    if (text) card.appendChild(el("div", "report", String(text).slice(0, 4000)));
    return card;
  }
  // Zdarzenia nie sa powtarzane przez backend: kto nie byl podlaczony w chwili publikacji (np. strona laczaca sie
  // ponownie po restarcie backendu), ten je traci. Dlatego przy starcie i po kazdym ponownym polaczeniu dobieramy
  // ostatnie zdarzenia z historii backendu ("recent") — bez duplikatow.
  const seenEvents = new Set();
  const eventKey = (event) => [event.type, event.at, event.id, event.key, event.title, event.text, event.name].join("|");
  function addEvent(event, announce) {
    const key = eventKey(event);
    if (seenEvents.has(key)) return false;
    seenEvents.add(key);
    liveEvents.unshift(event); liveEvents.splice(40);
    if (!announce) return true;
    if ($("tab-alerts").hidden) unseenAlerts++;
    toast(eventTitle(event), event.detail || (event.type === "reminder" ? "" : String(event.report || "").slice(0, 160)), () => switchTab("alerts"));
    return true;
  }
  async function loadAlerts(announce) {
    try {
      const data = await command({ command: "alerts" });
      activeAlerts = data.active || [];
      (data.recent || []).forEach((event) => addEvent(event, announce));
      renderAlerts(); badge("alerts-badge", activeAlerts.length + unseenAlerts);
    } catch (_) { /* pokazemy przy nastepnej probie */ }
  }
  let subscribedOnce = false;
  function onLiveEvent(event) {
    if (!event || event.type === "subscribed") {
      setLink(true);
      if (subscribedOnce) { loadAlerts(true); loadProviders(false); }     // po zerwaniu polaczenia: co nas ominelo
      subscribedOnce = true;
      return;
    }
    if (event.type === "error") { setLink(false, event.text); return; }
    if (event.type === "language") {      // jezyk zmieniony z innego kanalu (CLI, Telegram, druga karta)
      if (event.lang !== window.PipeI18n.lang) toast(t("languageChanged"), t("languageReload"), () => reloadInLanguage(""));
      return;
    }
    if (!addEvent(event, false)) return;
    if ($("tab-alerts").hidden) { unseenAlerts++; }
    toast(eventTitle(event), event.detail || (event.type === "reminder" ? "" : String(event.report || "").slice(0, 160)), () => switchTab("alerts"));
    if (event.type === "alert") { loadAlerts(); loadGraph(true); } else { renderAlerts(); badge("alerts-badge", activeAlerts.length + unseenAlerts); }
  }
  function connectEvents() {
    const source = new EventSource("/api/events");
    source.onmessage = (message) => { try { onLiveEvent(JSON.parse(message.data)); } catch (_) { /* niepelna ramka */ } };
    source.onerror = () => setLink(false, t("reconnecting"));
  }

  // ---------------------------------------------------------------- skille
  let skills = [];
  async function loadSkills() {
    try { skills = (await command({ command: "list_skills" })).skills || []; } catch (_) { return; }
    if (!$("tab-skills").hidden) renderSkills();
  }
  function runSkill(skill, args) {
    if (busy) return;
    switchPane("chat");
    if (pendingCard) pendingCard.supersede();
    add("user", "/" + (skill.command || skill.name) + (args ? " " + args : ""));
    run({ command: "run_skill", name: skill.name, args: args || "" }, t("runningSkill") + " " + skill.name + "…");
  }
  function renderSkills() {
    const list = $("skills");
    list.textContent = "";
    if (!skills.length) { list.appendChild(el("div", "empty", t("noSkills"))); return; }
    skills.forEach((skill, index) => {
      const card = el("div", "card-item skill");
      card.style.animationDelay = Math.min(index, 10) * 28 + "ms";
      const head = el("div", "head");
      head.appendChild(el("b", "", skill.name));
      if (skill.command) head.appendChild(el("span", "cmd-chip", "/" + skill.command));
      card.appendChild(head);
      card.appendChild(el("div", "desc", skill.description || ""));
      const fold = el("div", "fold"), inner = el("div");
      fold.appendChild(inner); card.appendChild(fold);
      const foot = el("div", "foot");
      const go = el("button", "btn run small", t("runSkill"));
      go.addEventListener("click", () => runSkill(skill, ""));
      const peek = el("button", "btn no small", t("showSkill"));
      peek.addEventListener("click", async () => {
        if (fold.classList.contains("open")) { fold.classList.remove("open"); peek.textContent = t("showSkill"); return; }
        if (!inner.children.length) {
          try { inner.appendChild(md((await command({ command: "skill", name: skill.name })).content || "")); }
          catch (error) { inner.appendChild(el("div", "body", error.message)); }
        }
        requestAnimationFrame(() => fold.classList.add("open"));
        peek.textContent = t("hideDiff");
      });
      foot.appendChild(go); foot.appendChild(peek);
      card.appendChild(foot);
      list.appendChild(card);
    });
  }

  // ---------------------------------------------------------------- providerzy LLM
  // Stan z backendu: { providers, active, chosen, can_edit } — klucze nigdy tu nie trafiaja.
  let llm = null;
  const pill = $("model-pill"), menu = $("model-menu"), menuList = $("model-menu-list"), menuGlow = $("model-menu-glow");
  function applyProviders(data) {
    const changed = llm && (llm.active.id !== data.active.id || llm.active.model !== data.active.model);
    llm = data;
    $("model-pill-badge").replaceChildren(providerBadge(data.active, true));
    $("model-pill-name").textContent = data.active.name;
    $("model-pill-model").textContent = data.active.model;
    pill.title = t("providerNow") + " " + data.active.name + " · " + data.active.model;
    pill.hidden = false;
    if (changed) { pill.classList.remove("swapped"); void pill.offsetWidth; pill.classList.add("swapped"); }
    return changed;
  }
  async function loadProviders(first) {
    try { applyProviders(await command({ command: "providers" })); } catch (_) { return; }   // starszy backend: bez przelacznika
    if (first && !llm.chosen && llm.can_edit) openSetup(true);
  }
  // Znaczek providera: jego logo (static/providers/<id>.svg) — wylacznie po to, zeby wskazac, z czyimi modelami
  // Pipe sie laczy; patrz static/providers/NOTICE.md. Wlasny provider albo brak pliku: monogram w kolorze z id.
  const LOGOS = new Set(["gemini", "openai", "anthropic", "openrouter", "groq", "deepseek", "mistral", "xai", "zai", "kimi",
    "together", "cerebras", "fireworks", "ollama"]);
  function monogram(badge, provider) {
    const id = String(provider.id || ""), name = String(provider.name || id || "?");
    let hash = 7;
    for (const ch of id || name) hash = (hash * 31 + ch.charCodeAt(0)) % 9973;
    badge.classList.remove("logo");
    badge.textContent = name.trim().charAt(0).toUpperCase();
    badge.style.setProperty("--hue", String(168 + (hash % 13) * 10));       // od morskiego po fiolet — w tonacji strony
  }
  function providerBadge(provider, small) {
    const badge = el("span", "pbadge" + (small ? " small" : ""));
    badge.setAttribute("aria-hidden", "true");
    if (!LOGOS.has(provider.id)) { monogram(badge, provider); return badge; }
    const image = el("img");
    image.alt = ""; image.decoding = "async"; image.draggable = false;
    image.addEventListener("error", () => monogram(badge, provider));
    image.src = "/static/providers/" + provider.id + ".svg";
    badge.classList.add("logo");
    badge.appendChild(image);
    return badge;
  }
  function announceProvider() {
    const text = t("switchedTo") + " " + llm.active.name + " · " + llm.active.model;
    if (messages.querySelector(".welcome")) toast(llm.active.name, text); else add("note", text);
  }

  // przelacznik w rozmowie: gotowi providerzy (z kluczem) + wejscie do ekranu wyboru
  let menuOpen = false, menuIndex = 0, menuItems = [];
  function openMenu() {
    if (!llm || menuOpen) return;
    closePalette();
    menuList.textContent = "";
    menuList.appendChild(el("div", "palette-group", t("providers")));
    menuItems = llm.providers.filter((p) => p.ready).map((provider) => ({ provider }));
    if (llm.can_edit) menuItems.push({ manage: true });
    menuItems.forEach((item, index) => {
      const row = el("button", "palette-item model-item" + (item.manage ? " manage" : ""));
      row.type = "button"; row.dataset.index = String(index); row.setAttribute("role", "option");
      const name = el("span", "name", item.manage ? "+" : "");
      if (!item.manage) { name.appendChild(providerBadge(item.provider, true)); name.appendChild(document.createTextNode(item.provider.name)); }
      row.appendChild(name);
      row.appendChild(el("span", "desc", item.manage ? t("manageProviders") : item.provider.model));
      row.appendChild(el("span", "hint", !item.manage && item.provider.active ? "✓" : ""));
      row.addEventListener("click", () => pickMenu(index));
      row.addEventListener("mousemove", () => { if (menuIndex !== index) { menuIndex = index; moveMenuGlow(); } });
      menuList.appendChild(row);
    });
    menuIndex = Math.max(0, menuItems.findIndex((item) => item.provider && item.provider.active));
    menu.style.bottom = (pill.offsetParent.clientHeight - pill.offsetTop + 8) + "px";
    menu.classList.remove("out"); menu.hidden = false; menuOpen = true;
    pill.setAttribute("aria-expanded", "true");
    menuGlow.style.transition = "none"; moveMenuGlow();
    requestAnimationFrame(() => { menuGlow.style.transition = ""; });
  }
  function moveMenuGlow() {
    const row = menuList.querySelector(`.palette-item[data-index="${menuIndex}"]`);
    menuList.querySelectorAll(".palette-item").forEach((node) => node.setAttribute("aria-selected", String(node === row)));
    if (!row) { menuGlow.style.opacity = "0"; return; }
    menuGlow.style.opacity = "1";
    menuGlow.style.height = row.offsetHeight + "px";
    menuGlow.style.transform = `translateY(${row.offsetTop + menuList.offsetTop}px)`;
  }
  function closeMenu() {
    if (!menuOpen) return;
    menuOpen = false;
    pill.setAttribute("aria-expanded", "false");
    menu.classList.add("out");
    setTimeout(() => { if (!menuOpen) { menu.hidden = true; menu.classList.remove("out"); } }, 140);
  }
  function pickMenu(index) {
    const item = menuItems[index];
    closeMenu();
    if (!item) return;
    if (item.manage) openSetup(false); else switchProvider(item.provider);
  }
  async function switchProvider(provider) {
    if (provider.active) return;
    if (!llm.can_edit) { toast(t("providers"), t("viewerProviders")); return; }
    if (busy) { toast(t("providers"), t("providerBusy")); return; }
    try { if (applyProviders(await command({ command: "provider_set", name: provider.id }))) announceProvider(); }
    catch (error) { toast(t("errorPrefix"), error.message); }
  }
  pill.addEventListener("click", () => (menuOpen ? closeMenu() : openMenu()));
  pill.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!menuOpen) { openMenu(); return; }
      menuIndex = (menuIndex + (event.key === "ArrowDown" ? 1 : menuItems.length - 1)) % menuItems.length;
      moveMenuGlow();
    } else if (menuOpen && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); pickMenu(menuIndex); }
    else if (menuOpen && event.key === "Escape") { event.preventDefault(); closeMenu(); }
  });
  document.addEventListener("mousedown", (event) => { if (menuOpen && !menu.contains(event.target) && !pill.contains(event.target)) closeMenu(); });

  // ekran wyboru: krok 1 — siatka providerow, krok 2 — klucz i model wybranego
  const setup = $("setup"), stepPick = $("step-pick"), stepKey = $("step-key");
  let setupFirst = false, setupBusy = false;
  function showStep(name) {
    const pick = name === "pick";
    stepPick.classList.toggle("away", !pick); stepKey.classList.toggle("away", pick);
    stepPick.inert = !pick; stepKey.inert = pick;
  }
  function openSetup(first) {
    if (!llm) return;
    setupFirst = !!first;
    closeMenu(); closePalette();
    $("setup-title").textContent = t(first ? "setupFirstTitle" : "setupTitle");
    $("setup-lead").textContent = t(first ? "setupFirstLead" : "setupLead");
    $("setup-hint").textContent = t("setupHint");
    $("setup-skip").textContent = first ? t("setupKeep") + " " + llm.active.name : t("setupClose");
    renderGrid(); showStep("pick");
    setup.classList.remove("out"); setup.hidden = false;
    requestAnimationFrame(() => { const card = $("provider-grid").querySelector("button"); if (card) card.focus({ preventScroll: true }); });
  }
  function closeSetup() {
    if (setup.hidden || setupBusy) return;
    setup.classList.add("out");
    setTimeout(() => { setup.hidden = true; setup.classList.remove("out"); stepKey.textContent = ""; input.focus(); }, 220);
  }
  function renderGrid() {
    const grid = $("provider-grid");
    grid.textContent = "";
    llm.providers.forEach((p, index) => {
      const card = el("button", "provider-card" + (p.active ? " active" : p.ready ? " ready" : ""));
      card.type = "button";
      card.style.animationDelay = Math.min(index, 14) * 24 + "ms";
      const head = el("div", "head");
      head.appendChild(providerBadge(p));
      head.appendChild(el("b", "", p.name));
      // etykieta tylko tam, gdzie cos mowi: uzywany / gotowy / lokalny — reszta po prostu czeka na klucz
      if (p.active || p.ready || !p.requires_key) head.appendChild(el("span", "tag " + (p.active ? "" : p.ready ? "add" : "mute"),
        t(p.active ? "pv_active" : p.ready ? "pv_ready" : "pv_local")));
      card.appendChild(head);
      if (p.ready && p.model) card.appendChild(el("div", "model", p.model));
      if (p.notes) card.appendChild(el("div", "note", p.notes));
      card.addEventListener("click", () => showProvider(p));
      grid.appendChild(card);
    });
  }
  function showProvider(p) {
    stepKey.textContent = "";
    let models = [], filter = "", ticket = 0;
    const back = el("button", "ghost back", "← " + t("back"));
    back.type = "button";
    back.addEventListener("click", () => showStep("pick"));
    stepKey.appendChild(back);
    const title = el("h2", "with-badge");
    title.appendChild(providerBadge(p)); title.appendChild(document.createTextNode(p.name));
    stepKey.appendChild(title);
    if (p.notes) stepKey.appendChild(el("p", "lead", p.notes));
    const form = el("form", "provider-form");
    form.autocomplete = "off"; form.noValidate = true;
    stepKey.appendChild(form);

    // --- klucz
    const keyInput = el("input");
    keyInput.type = "password"; keyInput.autocomplete = "off"; keyInput.spellcheck = false;
    keyInput.placeholder = t("keyPlaceholder"); keyInput.setAttribute("aria-label", t("apiKey"));
    const checkButton = el("button", "btn yes small", t("checkKey"));
    checkButton.type = "submit";
    const keyFold = el("div", "fold" + (p.has_key ? "" : " open")), keyInner = el("div", "fold-pad");
    if (p.requires_key) {
      form.appendChild(el("div", "field-label", t("apiKey")));
      if (p.has_key) {
        const saved = el("div", "saved");
        saved.appendChild(el("span", "", t(p.key_source === "env" ? "keyFromEnv" : "keySaved")));
        const change = el("button", "linkish", t("changeKey"));
        change.type = "button";
        change.addEventListener("click", () => { keyFold.classList.add("open"); change.remove(); keyInput.focus(); });
        saved.appendChild(change);
        if (p.key_source === "web") {
          const forget = el("button", "linkish danger", t("forgetKey"));
          forget.type = "button";
          forget.addEventListener("click", async () => {
            try { applyProviders(await command({ command: "provider_forget", name: p.id })); toast(t("keyForgotten"), p.name); renderGrid(); showStep("pick"); }
            catch (error) { setStatus("bad", error.message); }
          });
          saved.appendChild(forget);
        }
        form.appendChild(saved);
      }
      const field = el("div", "field");
      const reveal = el("button", "linkish", t("showKey"));
      reveal.type = "button";
      reveal.addEventListener("click", () => { const hide = keyInput.type === "text"; keyInput.type = hide ? "password" : "text"; reveal.textContent = t(hide ? "showKey" : "hideKey"); });
      field.appendChild(keyInput); field.appendChild(reveal); field.appendChild(checkButton);
      keyInner.appendChild(field);
      if (/^https:\/\//.test(p.key_url || "")) {
        const link = el("a", "linkish", t("whereKey") + " ↗");
        link.href = p.key_url; link.target = "_blank"; link.rel = "noopener noreferrer";
        keyInner.appendChild(link);
      }
      keyFold.appendChild(keyInner); form.appendChild(keyFold);
    }
    const status = el("div", "status");
    status.setAttribute("aria-live", "polite");
    form.appendChild(status);
    function setStatus(kind, text) { status.className = "status " + (kind || ""); status.textContent = text || ""; }

    // --- model
    const modelFold = el("div", "fold"), modelInner = el("div", "fold-pad");
    const modelLabel = el("div", "field-label", t("model")), count = el("span", "count");
    modelLabel.appendChild(count);
    const modelField = el("div", "field"), modelInput = el("input");
    modelInput.type = "text"; modelInput.autocomplete = "off"; modelInput.spellcheck = false;
    modelInput.placeholder = t("modelPlaceholder"); modelInput.setAttribute("aria-label", t("model"));
    modelField.appendChild(modelInput);
    const list = el("div", "model-list");
    modelInner.appendChild(modelLabel); modelInner.appendChild(modelField); modelInner.appendChild(list);
    modelFold.appendChild(modelInner); form.appendChild(modelFold);

    const foot = el("div", "sheet-foot"), label = t("useProvider") + " " + p.name;
    const use = el("button", "btn yes", label);
    use.type = "button"; use.disabled = true;
    foot.appendChild(el("div", "spacer")); foot.appendChild(use);
    form.appendChild(foot);
    const syncUse = () => { use.disabled = setupBusy || !modelFold.classList.contains("open") || !modelInput.value.trim(); };

    function renderModels() {
      list.textContent = "";
      const q = filter.toLowerCase(), chosen = modelInput.value.trim();
      const found = models.filter((m) => !q || m.toLowerCase().includes(q));
      found.slice(0, 80).forEach((m) => {
        const row = el("button", "model-row" + (m === chosen ? " on" : ""), m);
        row.type = "button";
        row.addEventListener("click", () => { modelInput.value = m; filter = ""; renderModels(); syncUse(); });
        list.appendChild(row);
      });
      if (models.length && !found.length) list.appendChild(el("div", "model-none", t("noModelMatch")));
      list.hidden = !models.length;
      const on = list.querySelector(".on");
      // Lista bywa rysowana w trakcie rozwijania bloku (wysokosc jeszcze rosnie), wiec liczymy od docelowej.
      const room = 208;
      if (on) list.scrollTop = on.offsetTop + on.offsetHeight <= room ? 0 : on.offsetTop - room / 2 + on.offsetHeight / 2;
    }
    function offerModels(note) {
      modelInput.value = [p.model, p.default_model].find((m) => m && (!models.length || models.includes(m))) || models[0] || "";
      filter = "";
      count.textContent = models.length ? models.length + " " + t("modelCount") : "";
      if (note) setStatus("", note);
      renderModels();
      modelFold.classList.add("open");
      syncUse();
    }
    async function check() {
      const key = keyInput.value.trim(), mine = ++ticket, stored = !key && (p.has_key || !p.requires_key);
      if (p.requires_key && !key && !p.has_key) { keyInput.focus(); return; }
      setStatus("busy", t("checking"));
      checkButton.disabled = true;
      try {
        const data = await command({ command: "provider_models", name: p.id, key });
        if (mine !== ticket) return;
        models = data.models || [];
        setStatus(key ? "ok" : "", key ? t("keyOk") : "");
        offerModels(models.length ? "" : t("modelsUnavailable"));
        if (key) modelInput.focus({ preventScroll: true });
      } catch (error) {
        if (mine !== ticket) return;
        setStatus("bad", error.message);
        // zapisany klucz: lista modeli to tylko wygoda — nazwe mozna wpisac recznie
        if (stored) { models = []; offerModels(); setStatus("bad", error.message + " " + t("modelsUnavailable")); }
      } finally { if (mine === ticket) checkButton.disabled = false; }
    }
    async function save() {
      const model = modelInput.value.trim(), key = keyInput.value.trim();
      if (!model || setupBusy || use.disabled) return;
      if (busy) { setStatus("bad", t("providerBusy")); return; }
      setupBusy = true; use.disabled = true; use.textContent = t("saving");
      let changed = false, done = false;
      try { changed = applyProviders(await command({ command: "provider_set", name: p.id, key, model })); done = true; }
      catch (error) { setStatus("bad", error.message); }
      setupBusy = false; use.textContent = label; syncUse();
      if (done) { closeSetup(); if (changed) announceProvider(); }
    }
    keyInput.addEventListener("input", () => { ticket++; checkButton.disabled = false; modelFold.classList.remove("open"); setStatus("", ""); syncUse(); });
    modelInput.addEventListener("input", () => { filter = modelInput.value.trim(); renderModels(); syncUse(); });
    modelInput.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); save(); } });
    form.addEventListener("submit", (event) => { event.preventDefault(); check(); });
    use.addEventListener("click", save);

    showStep("key");
    if (p.has_key || !p.requires_key) check(); else requestAnimationFrame(() => keyInput.focus({ preventScroll: true }));
  }
  $("setup-close").addEventListener("click", closeSetup);
  $("setup-close").setAttribute("aria-label", t("close"));
  $("setup-skip").addEventListener("click", async () => {
    if (setupFirst) { try { applyProviders(await command({ command: "provider_set", name: llm.active.id })); } catch (_) { /* zapytamy przy nastepnym wejsciu */ } }
    closeSetup();
  });
  setup.addEventListener("mousedown", (event) => { if (event.target === setup) closeSetup(); });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || setup.hidden) return;
    if (stepKey.classList.contains("away")) closeSetup(); else showStep("pick");
  });

  // ---------------------------------------------------------------- komendy "/"
  // [nazwa polska, alias angielski, klucz opisu, czy przyjmuje argumenty]
  const COMMANDS = [
    ["status", "status", "c_status"], ["raport", "report", "c_report"], ["zmiany", "changes", "c_changes", true],
    ["wykres", "chart", "c_chart", true], ["zdrowie", "health", "c_health"], ["audyt", "audit", "c_audit"],
    ["mapa", "map", "c_map"], ["server", "server", "c_server", true], ["katalogi", "directory", "c_directory"],
    ["skille", "skills", "c_skills"], ["alerty", "alerts", "c_alerts"], ["incydenty", "incidents", "c_incidents"],
    ["rutyny", "routines", "c_routines"], ["przypomnienia", "reminders", "c_reminders", true], ["cele", "targets", "c_targets"],
    ["vibe", "vibe", "c_vibe", true], ["dziennik", "journal", "c_journal"], ["cofnij", "undo", "c_undo", true],
    ["zgody", "approvals", "c_approvals"], ["mcp", "mcp", "c_mcp"], ["koszt", "cost", "c_cost"],
    ["historia", "history", "c_history"], ["providerzy", "providers", "c_provider", true], ["yolo", "yolo", "c_yolo", true], ["jezyk", "language", "c_language", true], ["aktualizuj", "update", "c_update", true], ["pomoc", "help", "c_help"],
  ];
  const english = window.PipeI18n.lang === "en";
  const shown = (entry) => (english ? entry[1] : entry[0]);
  const ALIASES = { provider: "providerzy" };      // stara nazwa z wczesniejszych wersji
  function findCommand(name) { name = ALIASES[name] || name; return COMMANDS.find((entry) => entry[0] === name || entry[1] === name); }
  const SCAN_WORDS = ["aktualizuj", "odswiez", "odśwież", "skanuj", "update", "refresh", "scan"];
  const lines = (items) => (items || []).map((item) => "- " + item).join("\n");

  // Jezyk Pipe jest wspolny dla calego agenta (prompty, raporty, komunikaty) — strona odswieza sie w nowym.
  function reloadInLanguage(note) {
    try { sessionStorage.setItem("pipe-session", session); sessionStorage.setItem("pipe-language-note", note || ""); } catch (_) { /* tryb prywatny */ }
    location.reload();
  }
  async function setLanguage(args) {
    if (busy) { toast(t("languageChanged"), t("languageBusy")); return; }
    try {
      const data = await command({ command: "language", args });
      if (args && data.lang && data.lang !== window.PipeI18n.lang) reloadInLanguage(data.text);
      else add("agent", md(data.text || ""));
    } catch (error) { add("agent error", md(t("errorPrefix") + ": " + error.message)); }
  }

  // /providerzy — ekran wyboru; /providerzy <id> [model] — przelaczenie od razu (provider musi miec klucz)
  async function providersCommand(args) {
    if (!llm) { add("agent error", md(t("offline"))); return; }
    const [wanted, model] = args.split(/\s+/).filter(Boolean);
    if (!wanted) { openSetup(false); return; }
    const q = wanted.toLowerCase(), found = llm.providers.find((p) => p.id === q || p.name.toLowerCase() === q);
    if (!found) { toast(t("providers"), t("unknownProvider") + " " + wanted); openSetup(false); return; }
    if (!found.ready || !llm.can_edit) { openSetup(false); showProvider(found); return; }
    if (found.active && (!model || model === found.model)) { toast(t("providers"), t("providerNow") + " " + found.name + " · " + found.model); return; }
    if (busy) { toast(t("providers"), t("providerBusy")); return; }
    try { if (applyProviders(await command({ command: "provider_set", name: found.id, model: model || "" }))) announceProvider(); }
    catch (error) { toast(t("errorPrefix"), error.message); }
  }

  // YOLO: zmiany w tej rozmowie bez pytania o TAK — stan zyje w sesji backendu, tu tylko znacznik
  const yoloChip = $("yolo-chip");
  function showYolo(on) { yoloChip.hidden = !on; yoloChip.title = t("yoloChip"); }
  async function loadYolo() {
    try { showYolo(!!(await command({ command: "yolo" })).yolo); } catch (_) { showYolo(false); }   // starszy backend
  }
  async function setYolo(args) {
    try {
      const data = await command({ command: "yolo", args });
      showYolo(!!data.yolo);
      add(data.yolo ? "agent error" : "agent", md(data.text || ""));
    } catch (error) { add("agent error", md(t("errorPrefix") + ": " + error.message)); }
  }
  yoloChip.addEventListener("click", () => { if (!busy) setYolo("off"); });

  const HANDLERS = {
    jezyk: (args) => setLanguage(args),
    yolo: (args) => setYolo(args),
    status: () => run({ command: "status" }, t("working")),
    aktualizuj: (args) => run({ command: "update", args }, t("working")),
    raport: () => dataMessage({ command: "digest" }, digestBody),
    zmiany: (args) => dataMessage({ command: "changes", args }, (d) => listing(d.text)),
    wykres: (args) => dataMessage({ command: "chart", args }, (d) => el("div", "caption", d.summary || "")),
    zdrowie: () => dataMessage({ command: "health" }, (d) => listing(d.text)),
    audyt: () => dataMessage({ command: "audit" }, (d) => listing(d.text)),
    incydenty: () => dataMessage({ command: "incidents" }, (d) => listing(d.text)),
    koszt: () => dataMessage({ command: "usage" }, (d) => listing(d.text)),
    katalogi: () => dataMessage({ command: "directory" }, (d) => listing(d.text, "DIRECTORY")),
    rutyny: () => dataMessage({ command: "routines" }, (d) => listing(lines(d.routines), t("c_routines"))),
    cele: () => dataMessage({ command: "targets" }, (d) => listing(lines(d.targets), t("c_targets"))),
    mcp: () => dataMessage({ command: "mcp_servers" }, (d) => listing(lines(d.servers), t("c_mcp"))),
    historia: () => dataMessage({ command: "history" }, (d) => plain((d.entries || []).join("\n"))),
    przypomnienia: (args) => {
      const parts = args.split(/\s+/), cancel = ["anuluj", "cancel", "usun", "remove"].includes((parts[0] || "").toLowerCase()) ? (parts[1] || "").replace("#", "") : "";
      return dataMessage({ command: "reminders", cancel }, (d) => listing(d.text));
    },
    vibe: (args) => dataMessage({ command: "vibe", args }, (d) => ("reset" in d ? md(d.reset ? t("vibeReset") : t("vibeNone")) : md(d.content || t("vibeEmpty")))),
    server: async (args) => {
      if (SCAN_WORDS.includes(args.toLowerCase())) return run({ command: "scan_server" }, t("working"));
      let content = "";
      try { content = (await command({ command: "server_md" })).content || ""; } catch (error) { add("agent error", md(error.message)); return; }
      if (content.trim()) add("agent", md(content)); else await run({ command: "scan_server" }, t("working"));
    },
    zgody: async () => {
      let pending = [];
      try { pending = (await command({ command: "approvals" })).pending || []; } catch (error) { add("agent error", md(error.message)); return; }
      if (!pending.length) { add("agent", md(t("noApprovals"))); return; }
      pending.forEach((item) => {
        const card = el("div", "confirm"), title = el("div", "title");
        title.appendChild(el("span", "tag warn", t("approval")));
        title.appendChild(document.createTextNode(item.requested_by || ""));
        card.appendChild(title);
        card.appendChild(md("`" + (item.target || "local") + "`: `" + String(item.command || "").replace(/`/g, "'") + "`" + (item.reason ? "\n\n" + item.reason : "") + (item.plan ? "\n\n" + item.plan : "")));
        const actions = el("div", "actions"), yes = el("button", "btn yes", t("yes")), no = el("button", "btn no", t("no"));
        actions.appendChild(yes); actions.appendChild(no); card.appendChild(actions);
        const decide = async (decision) => {
          actions.remove(); card.classList.add("done");
          try { const result = await command({ command: "approve", id: item.id, decision }); card.appendChild(el("div", "verdict", result.text || (decision ? t("approved") : t("rejected")))); }
          catch (error) { card.appendChild(el("div", "verdict", error.message)); }
        };
        yes.addEventListener("click", () => decide(true)); no.addEventListener("click", () => decide(false));
        add("agent", card);
      });
    },
    mapa: () => { switchTab("map"); loadGraph(false); },
    skille: () => switchTab("skills"),
    alerty: () => switchTab("alerts"),
    dziennik: () => switchTab("changes"),
    cofnij: () => switchTab("changes"),
    providerzy: (args) => providersCommand(args),
    pomoc: () => add("agent", listing(COMMANDS.map((entry) => "- /" + shown(entry) + " — " + t(entry[2])).join("\n")
      + (skills.some((s) => s.command) ? "\n" + t("skills") + ":\n" + skills.filter((s) => s.command).map((s) => "- /" + s.command + " — " + s.description).join("\n") : ""), t("commands"))),
  };
  const PANEL_ONLY = ["mapa", "skille", "alerty", "dziennik", "cofnij", "providerzy"];

  // Zwraca true, gdy tekst byl komenda (wbudowana albo skillem); false — idzie do agenta jako zwykla wiadomosc.
  async function slash(text) {
    const space = text.indexOf(" "), name = (space < 0 ? text.slice(1) : text.slice(1, space)).toLowerCase(), args = space < 0 ? "" : text.slice(space + 1).trim();
    const entry = findCommand(name);
    if (entry) {
      if (!PANEL_ONLY.includes(entry[0])) { if (pendingCard) pendingCard.supersede(); add("user", text); }
      await HANDLERS[entry[0]](args);
      return true;
    }
    const skill = skills.find((s) => s.command === name || s.name === name);
    if (skill) { runSkill(skill, args); return true; }
    return false;
  }

  // paleta: podpowiedzi po wpisaniu "/"
  const palette = $("palette"), paletteList = $("palette-list"), glow = $("palette-glow");
  let options = [], active = 0, paletteOpen = false;
  function paletteOptions(query) {
    const q = query.toLowerCase();
    const match = (...names) => !q || names.some((n) => n && n.toLowerCase().startsWith(q)) || (q.length > 1 && names.some((n) => n && n.toLowerCase().includes(q)));
    const commands = COMMANDS.filter((entry) => match(entry[0], entry[1]))
      .map((entry) => ({ group: "commands", name: shown(entry), desc: t(entry[2]), args: !!entry[3] }));
    const owned = skills.filter((s) => s.command && match(s.command, s.name))
      .map((s) => ({ group: "skills", name: s.command, desc: s.description, args: true }));
    // dokladne trafienia na gorze
    const rank = (o) => (o.name.toLowerCase() === q ? 0 : o.name.toLowerCase().startsWith(q) ? 1 : 2);
    return [...commands, ...owned].sort((a, b) => rank(a) - rank(b));
  }
  function updatePalette() {
    const value = input.value;
    if (!value.startsWith("/") || /\s/.test(value)) { closePalette(); return; }
    options = paletteOptions(value.slice(1));
    active = Math.min(active, Math.max(0, options.length - 1));
    paletteList.textContent = "";
    let group = "";
    options.forEach((option, index) => {
      if (option.group !== group && !value.slice(1)) { group = option.group; paletteList.appendChild(el("div", "palette-group", t(group))); }
      const row = el("button", "palette-item");
      row.type = "button"; row.dataset.index = String(index); row.setAttribute("role", "option");
      row.appendChild(el("span", "name", "/" + option.name));
      row.appendChild(el("span", "desc", option.desc || ""));
      row.appendChild(el("span", "hint", option.group === "skills" ? "skill" : ""));
      row.addEventListener("mousedown", (event) => { event.preventDefault(); choose(index, true); });
      row.addEventListener("mousemove", () => { if (active !== index) { active = index; moveGlow(); } });
      paletteList.appendChild(row);
    });
    if (!options.length) paletteList.appendChild(el("div", "palette-empty", t("noCommand")));
    else {
      const keys = el("div", "palette-keys");
      [["↑↓", "keyMove"], ["Enter", "keyRun"], ["Tab", "keyFill"], ["Esc", "keyClose"]].forEach(([key, label]) => {
        const item = el("span"); item.appendChild(el("kbd", "", key)); item.appendChild(document.createTextNode(t(label))); keys.appendChild(item);
      });
      paletteList.appendChild(keys);
    }
    if (!paletteOpen) { palette.classList.remove("out"); palette.hidden = false; paletteOpen = true; glow.style.transition = "none"; }
    moveGlow();
    requestAnimationFrame(() => { glow.style.transition = ""; });
  }
  function moveGlow() {
    const row = paletteList.querySelector(`.palette-item[data-index="${active}"]`);
    paletteList.querySelectorAll(".palette-item").forEach((node) => node.setAttribute("aria-selected", String(node === row)));
    if (!row) { glow.style.opacity = "0"; return; }
    glow.style.opacity = "1";
    glow.style.height = row.offsetHeight + "px";
    glow.style.transform = `translateY(${row.offsetTop + paletteList.offsetTop}px)`;
    const top = row.offsetTop + paletteList.offsetTop, bottom = top + row.offsetHeight;
    if (top < palette.scrollTop) palette.scrollTo({ top: top - 6, behavior: "smooth" });
    else if (bottom > palette.scrollTop + palette.clientHeight) palette.scrollTo({ top: bottom - palette.clientHeight + 6, behavior: "smooth" });
  }
  function closePalette() {
    if (!paletteOpen) return;
    paletteOpen = false; active = 0;
    palette.classList.add("out");
    setTimeout(() => { if (!paletteOpen) { palette.hidden = true; palette.classList.remove("out"); } }, 140);
  }
  function choose(index, runNow) {
    const option = options[index];
    if (!option) return;
    if (runNow) { send("/" + option.name); return; }
    input.value = "/" + option.name + " "; autosize(); closePalette(); input.focus();
  }
  function paletteKey(event) {
    if (!paletteOpen || !options.length) { if (paletteOpen && event.key === "Escape") { closePalette(); return true; } return false; }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      active = (active + (event.key === "ArrowDown" ? 1 : options.length - 1)) % options.length;
      moveGlow();
      return true;
    }
    if (event.key === "Tab") { event.preventDefault(); choose(active, false); return true; }
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); choose(active, true); return true; }
    if (event.key === "Escape") { event.preventDefault(); closePalette(); return true; }
    return false;
  }

  // ---------------------------------------------------------------- powloka
  function toast(title, text, onClick) {
    const node = el("div", "toast");
    node.appendChild(el("b", "", title));
    if (text) node.appendChild(document.createTextNode(String(text).slice(0, 220)));
    const close = () => { node.classList.add("out"); setTimeout(() => node.remove(), 300); };
    node.addEventListener("click", () => { if (onClick) onClick(); close(); });
    $("toasts").appendChild(node);
    setTimeout(close, 7000);
  }
  function switchTab(name) {
    ["map", "changes", "skills", "alerts"].forEach((tab) => { $("tab-" + tab).hidden = tab !== name; $("st-" + tab).classList.toggle("on", tab === name); });
    if (name === "skills") loadSkills();
    if (name === "changes") { unseenChanges = 0; badge("changes-badge", 0); loadChanges(); }
    if (name === "alerts") { unseenAlerts = 0; loadAlerts(); }
    if (name === "map") requestAnimationFrame(() => graph.fit(0));
    switchPane("stage");
  }
  function switchPane(name) {
    $("layout").dataset.pane = name;
    $("tab-chat").classList.toggle("on", name === "chat"); $("tab-stage").classList.toggle("on", name === "stage");
  }
  document.querySelectorAll(".stage-tabs [data-tab]").forEach((button) => button.addEventListener("click", () => switchTab(button.dataset.tab)));
  document.querySelectorAll("#mobile-switch [data-pane]").forEach((button) => button.addEventListener("click", () => switchPane(button.dataset.pane)));
  $("lightbox").addEventListener("click", () => { $("lightbox").hidden = true; });
  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("pipe-theme", theme); } catch (_) { /* tryb prywatny */ }
  }
  $("theme").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  $("lang").addEventListener("click", () => setLanguage(window.PipeI18n.lang === "en" ? "pl" : "en"));
  $("new-chat").addEventListener("click", () => {
    if (busy) return;
    session = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()));
    messages.textContent = ""; pendingCard = null; welcome();
    showYolo(false);                     // nowa rozmowa = nowa sesja, YOLO zostaje w starej
  });

  // teksty
  $("tab-chat").textContent = t("chat"); $("tab-stage").textContent = t("stage"); $("new-chat").textContent = t("newChat");
  $("st-skills").textContent = t("skills");
  $("st-map").textContent = t("map"); $("st-changes-label").textContent = t("changes"); $("st-alerts-label").textContent = t("alerts");
  $("theme").title = t("theme");
  $("lang").textContent = window.PipeI18n.lang.toUpperCase(); $("lang").title = t("language");
  $("follow-label").textContent = t("follow"); $("fit").title = t("fit"); $("refresh").title = t("refresh");
  input.placeholder = t("placeholder");
  $("server-name").textContent = config.server || "";

  welcome();
  if (languageNote) add("agent", md(languageNote));
  loadSkills();
  loadGraph(false);
  loadAlerts();
  loadProviders(true);
  loadYolo();
  connectEvents();
  input.focus();
})();
