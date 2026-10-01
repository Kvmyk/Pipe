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
    loadGraph(true);
  }

  async function send(text) {
    text = text.trim();
    if (!text || busy) return;
    if (pendingCard) pendingCard.supersede();
    add("user", text);
    input.value = ""; autosize();
    await run({ message: text });
  }
  function autosize() { input.style.height = "auto"; input.style.height = Math.min(160, input.scrollHeight) + "px"; }
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(input.value); }
  });
  $("composer").addEventListener("submit", (event) => { event.preventDefault(); send(input.value); });

  // szybkie akcje
  const chips = [
    ["chipStatus", () => { add("user", t("chipStatus")); run({ command: "status" }, t("working")); }],
    ["chipChanges", async () => { add("user", t("chipChanges")); await dataMessage({ command: "changes" }, (d) => plain(d.text)); }],
    ["chipAudit", async () => { add("user", t("chipAudit")); await dataMessage({ command: "audit" }, (d) => plain(d.text)); }],
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
  async function loadAlerts() {
    try { const data = await command({ command: "alerts" }); activeAlerts = data.active || []; renderAlerts(); badge("alerts-badge", activeAlerts.length + unseenAlerts); }
    catch (_) { /* pokazemy przy nastepnej probie */ }
  }
  function onLiveEvent(event) {
    if (!event || event.type === "subscribed") { setLink(true); return; }
    if (event.type === "error") { setLink(false, event.text); return; }
    liveEvents.unshift(event); liveEvents.splice(40);
    if ($("tab-alerts").hidden) { unseenAlerts++; }
    toast(eventTitle(event), event.detail || (event.type === "reminder" ? "" : String(event.report || "").slice(0, 160)), () => switchTab("alerts"));
    if (event.type === "alert") { loadAlerts(); loadGraph(true); } else { renderAlerts(); badge("alerts-badge", activeAlerts.length + unseenAlerts); }
  }
  function connectEvents() {
    const source = new EventSource("/api/events");
    source.onmessage = (message) => { try { onLiveEvent(JSON.parse(message.data)); } catch (_) { /* niepelna ramka */ } };
    source.onerror = () => setLink(false, t("reconnecting"));
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
    ["map", "changes", "alerts"].forEach((tab) => { $("tab-" + tab).hidden = tab !== name; $("st-" + tab).classList.toggle("on", tab === name); });
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
  $("new-chat").addEventListener("click", () => {
    if (busy) return;
    session = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()));
    messages.textContent = ""; pendingCard = null; welcome();
  });

  // teksty
  $("tab-chat").textContent = t("chat"); $("tab-stage").textContent = t("stage"); $("new-chat").textContent = t("newChat");
  $("st-map").textContent = t("map"); $("st-changes-label").textContent = t("changes"); $("st-alerts-label").textContent = t("alerts");
  $("theme").title = t("theme");
  $("follow-label").textContent = t("follow"); $("fit").title = t("fit"); $("refresh").title = t("refresh");
  input.placeholder = t("placeholder");
  $("server-name").textContent = config.server || "";

  welcome();
  loadGraph(false);
  loadAlerts();
  connectEvents();
  input.focus();
})();
