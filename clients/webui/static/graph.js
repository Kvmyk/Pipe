// Schemat na zywo: uklad warstwowy, kamera (przesuwanie / zoom), plynne przejscia miedzy poziomami,
// stany wezlow (agent pracuje / czeka na zgode) i impulsy plynace po polaczeniach.
// Wszystko w SVG, animacje na transform/opacity + jedna petla requestAnimationFrame.
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const W = 190, H = 58, COL = 286, ROW = 84;
  const ICONS = {
    server: "M5 5h14v6H5zM5 13h14v6H5zM8.5 8h.01M8.5 16h.01",
    cluster: "M12 3l7.8 4.5v9L12 21l-7.8-4.5v-9zM12 8.2v7.6M8.6 10l6.8 4M15.4 10l-6.8 4",
    project: "M12 4l8 4-8 4-8-4zM4 12l8 4 8-4M4 16l8 4 8-4",
    app: "M12 3l8 4.5v9L12 21l-8-4.5v-9zM12 12l8-4.5M12 12L4 7.5M12 12v9",
    db: "M5 6c0-1.7 3.1-3 7-3s7 1.3 7 3-3.1 3-7 3-7-1.3-7-3zM5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3",
    proxy: "M4 12h6M14 6h6M14 18h6M10 12c2.4 0 1.6-6 4-6M10 12c2.4 0 1.6 6 4 6",
    internet: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c3 3.4 3 14.6 0 18M12 3c-3 3.4-3 14.6 0 18",
    service: "M9 3v5M15 3v5M7 8h10v4a5 5 0 0 1-10 0zM12 17v4",
    folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
    external: "M14 4h6v6M20 4l-9 9M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4",
  };
  const easeInOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
  const easeOut = (t) => 1 - Math.pow(1 - t, 3);
  const reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function svg(tag, attrs, cls) {
    const node = document.createElementNS(NS, tag);
    if (cls) node.setAttribute("class", cls);
    for (const key in attrs || {}) node.setAttribute(key, attrs[key]);
    return node;
  }
  function clip(text, max) {
    text = String(text || "");
    return text.length > max ? text.slice(0, max - 1) + "…" : text;
  }

  // --- uklad: warstwy od zrodel (internet) w prawo, wezly bez polaczen w osobnym bloku ---
  function layout(view) {
    const nodes = view.nodes, edges = view.edges;
    const ids = new Set(nodes.map((n) => n.id));
    const links = edges.filter((e) => ids.has(e.from) && ids.has(e.to));
    const incoming = new Map(), outgoing = new Map();
    nodes.forEach((n) => { incoming.set(n.id, []); outgoing.set(n.id, []); });
    links.forEach((e) => { incoming.get(e.to).push(e.from); outgoing.get(e.from).push(e.to); });
    const level = new Map();
    const connected = nodes.filter((n) => incoming.get(n.id).length || outgoing.get(n.id).length);
    connected.forEach((n) => level.set(n.id, 0));
    for (let pass = 0; pass < nodes.length + 1; pass++) {
      let changed = false;
      links.forEach((e) => {
        const next = level.get(e.from) + 1;
        if (next > level.get(e.to) && next <= nodes.length) { level.set(e.to, next); changed = true; }
      });
      if (!changed) break;
    }
    const columns = [];
    connected.forEach((n) => { (columns[level.get(n.id)] = columns[level.get(n.id)] || []).push(n); });
    const kindOrder = { internet: 0, server: 1, proxy: 2, project: 3, app: 4, cluster: 4, db: 5, service: 6, external: 7, folder: 8 };
    const byKind = (a, b) => (kindOrder[a.kind] ?? 5) - (kindOrder[b.kind] ?? 5) || a.label.localeCompare(b.label);
    const yOf = new Map();
    const compact = columns.filter(Boolean);
    compact.forEach((column, index) => {
      if (index === 0) column.sort(byKind);
      else {
        const bary = (n) => {
          const ys = incoming.get(n.id).map((id) => yOf.get(id)).filter((y) => y !== undefined);
          return ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : 1e6;
        };
        column.sort((a, b) => bary(a) - bary(b) || byKind(a, b));
      }
      column.forEach((n, row) => yOf.set(n.id, row));
    });
    const isolated = nodes.filter((n) => !level.has(n.id)).sort(byKind);
    const tallest = Math.max(1, ...compact.map((c) => c.length));
    const perColumn = Math.max(3, Math.min(6, Math.max(tallest, Math.ceil(Math.sqrt(isolated.length * 1.4)))));
    for (let i = 0; i < isolated.length; i += perColumn) compact.push(isolated.slice(i, i + perColumn));
    const height = Math.max(1, ...compact.map((c) => c.length));
    const pos = new Map();
    compact.forEach((column, cx) => {
      const offset = ((height - column.length) * ROW) / 2;
      column.forEach((n, row) => pos.set(n.id, { x: cx * COL, y: offset + row * ROW, col: cx }));
    });
    return { pos, links, width: Math.max(1, compact.length) * COL - (COL - W), height: height * ROW - (ROW - H) };
  }

  function edgePath(a, b) {
    const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2;
    if (x2 > x1) {
      const bend = Math.max(36, (x2 - x1) * 0.5);
      return `M${x1},${y1} C${x1 + bend},${y1} ${x2 - bend},${y2} ${x2},${y2}`;
    }
    const lift = Math.max(60, Math.abs(y2 - y1) * 0.5 + 46);   // polaczenie wstecz albo w tej samej kolumnie
    return `M${x1},${y1} C${x1 + 70},${y1 - lift} ${x2 - 70},${y2 - lift} ${x2},${y2}`;
  }

  function create(options) {
    const root = options.svg;
    const camera = root.querySelector("#camera");
    const gEdges = root.querySelector("#edges"), gNodes = root.querySelector("#nodes"), gPulses = root.querySelector("#pulses");
    const t = options.t;
    let data = null, viewId = null, current = null;      // current: { view, layout, nodeEls, edgeEls }
    let cam = { x: 0, y: 0, k: 1 }, camAnim = null, selected = null, busy = false;
    let states = new Map();                               // globalny id wezla -> "active" | "waiting"
    const pulses = [];
    let lastSpawn = 0, rafId = 0;

    camera.style.transformOrigin = "0 0";
    function applyCamera() { camera.style.transform = `translate(${cam.x}px, ${cam.y}px) scale(${cam.k})`; }
    function size() { const r = root.getBoundingClientRect(); return { w: Math.max(200, r.width), h: Math.max(160, r.height) }; }

    function fitTransform(scaleMul) {
      const { w, h } = size();
      const pad = 34, lw = current.layout.width, lh = current.layout.height;
      let k = Math.min((w - pad * 2) / lw, (h - pad * 2) / lh, 1.12);
      k = Math.max(0.3, k) * (scaleMul || 1);
      return { k, x: (w - lw * k) / 2, y: (h - lh * k) / 2 };
    }
    function moveCamera(target, ms, ease) {
      // Karta w tle nie dostaje klatek animacji — wtedy ustawiamy kamere od razu, zeby przejscie nie utknelo w polowie.
      if (reduced || !ms || document.hidden) {
        const pending = camAnim && camAnim.resolve;
        cam = { ...target }; camAnim = null; applyCamera();
        if (pending) pending();
        return Promise.resolve();
      }
      return new Promise((resolve) => {
        if (camAnim && camAnim.resolve) camAnim.resolve();
        const anim = { from: { ...cam }, to: target, start: performance.now(), ms, ease: ease || easeInOut, resolve };
        camAnim = anim;
        loop();
        // bezpiecznik: gdyby klatki przestaly przychodzic w trakcie (karta schowana), konczymy ruch sami
        setTimeout(() => { if (camAnim === anim) { cam = { ...target }; camAnim = null; applyCamera(); resolve(); } }, ms + 250);
      });
    }

    // --- petla animacji: kamera + impulsy na polaczeniach ---
    function loop() { if (!rafId) rafId = requestAnimationFrame(frame); }
    function frame(now) {
      rafId = 0;
      let again = false;
      if (camAnim) {
        const p = Math.min(1, (now - camAnim.start) / camAnim.ms), e = camAnim.ease(p);
        cam.x = camAnim.from.x + (camAnim.to.x - camAnim.from.x) * e;
        cam.y = camAnim.from.y + (camAnim.to.y - camAnim.from.y) * e;
        cam.k = camAnim.from.k + (camAnim.to.k - camAnim.from.k) * e;
        applyCamera();
        if (p >= 1) { const done = camAnim.resolve; camAnim = null; if (done) done(); } else again = true;
      }
      if (current && !reduced) {
        const live = current.edgeEls.filter((e) => e.live);
        if (live.length && now - lastSpawn > 620) {
          lastSpawn = now;
          live.forEach((e) => {
            const dot = svg("circle", { r: 3.6 }, "pulse");
            gPulses.appendChild(dot);
            pulses.push({ dot, path: e.path, length: e.path.getTotalLength(), start: now, ms: 900 });
          });
        }
        for (let i = pulses.length - 1; i >= 0; i--) {
          const pulse = pulses[i], p = (now - pulse.start) / pulse.ms;
          if (p >= 1 || !pulse.path.isConnected) { pulse.dot.remove(); pulses.splice(i, 1); continue; }
          const point = pulse.path.getPointAtLength(pulse.length * easeInOut(p));
          pulse.dot.setAttribute("cx", point.x); pulse.dot.setAttribute("cy", point.y);
          pulse.dot.style.opacity = String(Math.min(1, Math.min(p, 1 - p) * 5));
        }
        if (live.length || pulses.length) again = true;
      }
      if (again) loop();
    }

    // --- rysowanie ---
    function nodeEl(node) {
      const outer = svg("g", { "data-id": node.id }, "node-pos");
      outer.style.transition = reduced ? "" : "transform .55s cubic-bezier(.22,.8,.24,1), opacity .3s";
      const g = svg("g", {}, "node");
      g.appendChild(svg("rect", { x: -3, y: -3, width: W + 6, height: H + 6, rx: 9 }, "ring"));
      g.appendChild(svg("rect", { x: 0, y: 0, width: W, height: H, rx: 6 }, "card"));
      g.appendChild(svg("rect", { x: 13, y: H / 2 - 15, width: 30, height: 30, rx: 6 }, "ico-bg"));
      const icon = svg("path", { d: ICONS[node.kind] || ICONS.app, transform: `translate(${28 - 9.6}, ${H / 2 - 9.6}) scale(.8)` }, "ico");
      g.appendChild(icon);
      g.appendChild(svg("text", { x: 53, y: 25 }, "title"));
      g.appendChild(svg("text", { x: 53, y: 42 }, "sub"));
      g.appendChild(svg("circle", { cx: W - 12, cy: 12, r: 3.5 }, "state"));
      if (node.opens) g.appendChild(svg("path", { d: `M${W - 21},${H / 2 + 5} l5,5 -5,5` }, "opens"));
      const title = svg("title"); g.appendChild(title);
      outer.appendChild(g);
      return outer;
    }
    function updateNode(outer, node, p) {
      const g = outer.firstChild;
      outer.style.transform = `translate(${p.x}px, ${p.y}px)`;
      const keep = ["active", "waiting", "selected", "enter", "flash-ok", "flash-bad"].filter((c) => g.classList.contains(c));
      g.setAttribute("class", ["node", "kind-" + node.kind, "state-" + (node.state || "ok"), ...keep].join(" "));
      g.querySelector(".title").textContent = clip(node.label, node.opens ? 15 : 17);
      g.querySelector(".sub").textContent = clip(node.sub, 19);
      g.querySelector("title").textContent = node.label + (node.sub ? " — " + node.sub : "");
    }

    function render(animateEnter) {
      const view = data.views[viewId];
      const lay = layout(view);
      const old = current && current.viewId === viewId ? current.nodeEls : new Map();
      if (!current || current.viewId !== viewId) { gNodes.textContent = ""; }
      gEdges.textContent = "";
      pulses.splice(0).forEach((p) => p.dot.remove());
      const nodeEls = new Map();
      view.nodes.forEach((node) => {
        const p = lay.pos.get(node.id);
        let outer = old.get(node.id);
        const fresh = !outer;
        if (fresh) {
          outer = nodeEl(node);
          gNodes.appendChild(outer);
          if (!reduced && (animateEnter || current)) {
            outer.firstChild.classList.add("enter");
            outer.firstChild.style.animationDelay = (p.col * 45 + (p.y / ROW) * 14) + "ms";
            setTimeout(() => { outer.firstChild.classList.remove("enter"); outer.firstChild.style.animationDelay = ""; }, 900);
          }
          const was = outer.style.transition; outer.style.transition = "none";
          updateNode(outer, node, p);
          void outer.getBoundingClientRect(); outer.style.transition = was;
        } else updateNode(outer, node, p);
        nodeEls.set(node.id, outer);
      });
      old.forEach((outer, id) => {
        if (!nodeEls.has(id)) { outer.style.opacity = "0"; setTimeout(() => outer.remove(), 320); }
      });
      const edgeEls = lay.links.map((edge) => {
        const path = svg("path", { d: edgePath(lay.pos.get(edge.from), lay.pos.get(edge.to)), "marker-end": "url(#arrow)" },
          "edge kind-" + (edge.kind || "link") + (animateEnter ? " enter" : ""));
        gEdges.appendChild(path);
        let label = null;
        if (edge.label) {
          const mid = path.getPointAtLength(path.getTotalLength() / 2);
          label = svg("text", { x: mid.x, y: mid.y - 6, "text-anchor": "middle" }, "edge-label" + (animateEnter ? " enter" : ""));
          label.textContent = clip(edge.label, 30);
          gEdges.appendChild(label);
        }
        return { edge, path, label, live: false };
      });
      current = { viewId, view, layout: lay, nodeEls, edgeEls };
      applyStates();
      if (selected && nodeEls.has(selected)) nodeEls.get(selected).firstChild.classList.add("selected");
      options.onView && options.onView(viewId, view);
    }

    // --- stany: ktory wezel w biezacym widoku odpowiada dzialaniu ---
    function depth(id) { return id === "fleet" ? 0 : id === "host" ? 1 : 2; }
    function pathOf(id) { return (data && data.index[id]) || (id === "host" ? ["host", "host"] : null); }
    function inView(id, view) {
      const path = pathOf(id);
      if (!path) return null;
      const d = depth(view);
      if (d === 0) return path[0];
      if (d === 1) return path.length > 1 && path[0] === "host" ? path[1] : null;
      return path[1] === view ? (path[2] || null) : null;
    }
    function deepestView(id) {
      const path = pathOf(id);
      if (!path || !data) return null;
      if (path.length >= 3 && data.views[path[1]]) return path[1];
      if (path.length >= 2 && path[0] === "host") return "host";
      return data.views.fleet && data.views.fleet.nodes.length > 1 ? "fleet" : "host";
    }
    function applyStates() {
      if (!current) return;
      const local = new Map();
      states.forEach((state, id) => {
        const node = inView(id, viewId);
        if (!node) return;
        if (state === "active" || !local.has(node)) local.set(node, state);
      });
      let hostBusy = false;
      current.nodeEls.forEach((outer, id) => {
        const g = outer.firstChild, state = local.get(id);
        g.classList.toggle("active", state === "active");
        g.classList.toggle("waiting", state === "waiting");
      });
      if (depth(viewId) === 1 && local.has("host")) hostBusy = local.get("host");
      root.parentElement.dataset.busy = hostBusy || "";
      current.edgeEls.forEach((e) => {
        const live = local.get(e.edge.to) === "active";
        e.live = live;
        e.path.classList.toggle("live", live);
      });
      loop();
    }

    // --- przejscia miedzy poziomami ---
    async function show(next, opts) {
      opts = opts || {};
      if (!data || !data.views[next] || busy) return false;
      if (next === viewId) { if (opts.fit) fit(); return true; }
      const deeper = !viewId || depth(next) > depth(viewId);
      const previous = viewId;
      busy = true;
      if (current && !reduced && !opts.instant) {
        // wyjscie: w glab — najazd na kliknięty wezel; w gore — oddalenie
        const anchor = deeper ? current.layout.pos.get(inViewNodeFor(next) || "") : null;
        const { w, h } = size();
        const k = cam.k * (deeper ? 1.7 : 0.62);
        const target = anchor
          ? { k, x: w / 2 - (anchor.x + W / 2) * k, y: h / 2 - (anchor.y + H / 2) * k }
          : { k, x: w / 2 - (w / 2 - cam.x) * (k / cam.k), y: h / 2 - (h / 2 - cam.y) * (k / cam.k) };
        camera.style.transition = "opacity .2s ease"; camera.style.opacity = "0";
        await moveCamera(target, 230, (t) => t * t);
      }
      viewId = next;
      selected = null;
      render(true);
      const end = fitTransform();
      if (!reduced && !opts.instant) {
        let start;
        const back = !deeper && current.layout.pos.get(inViewNodeFor(previous) || "");
        if (back) {                                         // powrot: startujemy z najazdu na wezel, z ktorego wyszlismy
          const { w, h } = size(), k = end.k * 1.6;
          start = { k, x: w / 2 - (back.x + W / 2) * k, y: h / 2 - (back.y + H / 2) * k };
        } else {
          const { w, h } = size(), k = end.k * (deeper ? 0.8 : 1.25);
          start = { k, x: w / 2 - (current.layout.width * k) / 2, y: h / 2 - (current.layout.height * k) / 2 };
        }
        cam = start; applyCamera();
        void camera.getBoundingClientRect();
        camera.style.transition = "opacity .34s ease"; camera.style.opacity = "1";
        await moveCamera(end, 460, easeOut);
      } else { camera.style.opacity = "1"; cam = end; applyCamera(); }
      busy = false;
      return true;
    }
    // wezel w biezacym widoku, ktory "otwiera" podany widok (albo go reprezentuje)
    function inViewNodeFor(otherView) {
      if (!current) return null;
      const node = current.view.nodes.find((n) => n.opens === otherView);
      return node ? node.id : null;
    }

    function fit(ms) { if (current) return moveCamera(fitTransform(), ms === undefined ? 420 : ms, easeInOut); }
    // `covered` — szerokosc (px) zaslonieta z prawej przez panel szczegolow: wezel ma zostac w widocznej czesci
    function focus(id, ms, covered) {
      if (!current) return;
      const p = current.layout.pos.get(id);
      if (!p) return;
      const { w, h } = size();
      const free = Math.max(160, w - (covered || 0));
      const k = Math.max(cam.k, Math.min(1, fitTransform().k * 1.25));
      const cx = (p.x + W / 2) * cam.k + cam.x, cy = (p.y + H / 2) * cam.k + cam.y;
      const half = (W / 2) * cam.k;
      const visible = cx - half > 12 && cx + half < free - 12 && cy > h * 0.12 && cy < h * 0.88;
      if (visible && Math.abs(k - cam.k) < 0.02) return;
      return moveCamera({ k, x: free / 2 - (p.x + W / 2) * k, y: h / 2 - (p.y + H / 2) * k }, ms || 520, easeInOut);
    }

    // --- interakcja ---
    let drag = null;
    root.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      drag = { x: event.clientX, y: event.clientY, cx: cam.x, cy: cam.y, moved: false, target: event.target };
      root.setPointerCapture(event.pointerId);
    });
    root.addEventListener("pointermove", (event) => {
      if (!drag) return;
      const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
      if (!drag.moved && Math.hypot(dx, dy) < 5) return;
      if (!drag.moved) { drag.moved = true; root.classList.add("dragging"); camAnim = null; options.onUserMove && options.onUserMove(); }
      cam.x = drag.cx + dx; cam.y = drag.cy + dy; applyCamera();
    });
    const release = (event) => {
      if (!drag) return;
      const was = drag; drag = null;
      root.classList.remove("dragging");
      try { root.releasePointerCapture(event.pointerId); } catch (_) { /* juz zwolniony */ }
      if (was.moved) return;
      const holder = was.target.closest ? was.target.closest(".node-pos") : null;
      if (!holder) { select(null); options.onSelect && options.onSelect(null); return; }
      const node = current.view.nodes.find((n) => n.id === holder.dataset.id);
      if (!node) return;
      options.onUserMove && options.onUserMove();
      if (node.opens && data.views[node.opens] && node.opens !== viewId) { options.onOpen && options.onOpen(node); return; }
      select(node.id);
      options.onSelect && options.onSelect(node);
    };
    root.addEventListener("pointerup", release);
    root.addEventListener("pointercancel", release);
    root.addEventListener("wheel", (event) => {
      event.preventDefault();
      if (!current) return;
      const rect = root.getBoundingClientRect();
      const mx = event.clientX - rect.left, my = event.clientY - rect.top;
      const factor = Math.exp(-event.deltaY * (event.ctrlKey ? 0.012 : 0.0016));
      const base = camAnim ? camAnim.to : cam;
      const k = Math.min(2.4, Math.max(0.25, base.k * factor));
      const ratio = k / base.k;
      options.onUserMove && options.onUserMove();
      moveCamera({ k, x: mx - (mx - base.x) * ratio, y: my - (my - base.y) * ratio }, 140, easeOut);
    }, { passive: false });
    root.addEventListener("dblclick", (event) => { if (!event.target.closest(".node-pos")) fit(); });
    if (window.ResizeObserver) new ResizeObserver(() => { if (current && !drag && !busy) fit(0); }).observe(root);

    function select(id) {
      selected = id;
      if (current) current.nodeEls.forEach((outer, key) => outer.firstChild.classList.toggle("selected", key === id));
    }

    return {
      setData(next) {
        data = next;
        if (!viewId || !data.views[viewId]) { viewId = data.root; current = null; render(true); cam = fitTransform(); applyCamera(); camera.style.opacity = "1"; }
        else render(false);
      },
      get view() { return viewId; },
      get data() { return data; },
      show, fit, focus, select, inView, deepestView,
      setStates(next) { states = next; applyStates(); },
      flash(id, ok) {
        const local = current && inView(id, viewId), outer = local && current.nodeEls.get(local);
        if (!outer) return;
        const cls = ok ? "flash-ok" : "flash-bad";
        outer.firstChild.classList.remove(cls); void outer.getBoundingClientRect(); outer.firstChild.classList.add(cls);
        setTimeout(() => outer.firstChild.classList.remove(cls), 950);
      },
      node(view, id) { return data && data.views[view] ? data.views[view].nodes.find((n) => n.id === id) : null; },
      label(id) {
        if (!data) return id;
        for (const key in data.views) { const n = data.views[key].nodes.find((x) => x.id === id); if (n) return n.label; }
        return id === "host" ? data.hostname : id.replace(/^[a-z]+:/, "");
      },
    };
  }

  window.PipeGraph = { create };
})();
