// Scena reklamy: rysuje dowolna chwile filmu z timeline.json (edit.py) i surfaces.json (surfaces.py). render.py wola
// seek(t) klatka po klatce, wiec nic tu nie zalezy od zegara przegladarki — kazda klatka jest powtarzalna.
"use strict";

const params = new URLSearchParams(location.search);
const FORMAT = params.get("f") || "f45";
document.body.classList.add(FORMAT);

const $ = (id) => document.getElementById(id);
let TL = null, SF = null;

// --- krzywe ---------------------------------------------------------------------------------------------------
function bezier(x1, y1, x2, y2) {
  const cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx;
  const cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
  const x = (s) => ((ax * s + bx) * s + cx) * s, y = (s) => ((ay * s + by) * s + cy) * s;
  const dx = (s) => (3 * ax * s + 2 * bx) * s + cx;
  return (t) => {
    if (t <= 0) return 0;
    if (t >= 1) return 1;
    let s = t;
    for (let i = 0; i < 8; i++) { const d = dx(s); if (Math.abs(d) < 1e-6) break; s -= (x(s) - t) / d; }
    return y(Math.min(1, Math.max(0, s)));
  };
}
const easeCam = bezier(0.55, 0, 0.12, 1);   // szybki start, dlugie hamowanie — bez odbic
const easeIn = bezier(0.16, 1, 0.3, 1);     // wejscie (expo out)
const easeOut = bezier(0.7, 0, 0.84, 0);    // wyjscie
const clamp01 = (v) => Math.min(1, Math.max(0, v));
const lerp = (a, b, k) => a + (b - a) * k;
const show = (el, on) => { el.style.visibility = on ? "visible" : "hidden"; };

function sceneAt(t) {
  return TL.scenes.find((s) => t >= s.start && t < s.end) || TL.scenes[TL.scenes.length - 1];
}

// wejscie powierzchni na cieciu: krotkie przyblizenie 1.05 -> 1, potem wolny, staly najazd do konca ujecia
// (nic nie stoi w miejscu; kamera w przegladarce ma wlasny ruch, wiec tam bez najazdu)
function punch(el, t, start, end, drift) {
  const k = easeIn(clamp01((t - start) / 0.45));
  const slow = drift ? 0.035 * clamp01((t - start) / Math.max(0.5, end - start)) : 0;
  el.style.opacity = clamp01((t - start) / 0.06);
  el.style.transform = `scale(${(1.05 - 0.05 * k) * (1 + slow)})`;
}

// --- przegladarka: klatki + kamera ----------------------------------------------------------------------------
function trackAt(scene, name, t) {
  const samples = (TL.tracks[scene.name] || []).filter((s) => s[1] && s[1][name]);
  if (!samples.length) return null;
  if (t <= samples[0][0]) return samples[0][1][name];
  for (let i = 1; i < samples.length; i++) {
    if (t <= samples[i][0]) {
      const [t0, a] = samples[i - 1], [t1, b] = samples[i], k = (t - t0) / (t1 - t0 || 1);
      return a[name].map((v, j) => lerp(v, b[name][j], k));
    }
  }
  return samples[samples.length - 1][1][name];
}

function resolve(v, axis, scene, t, viewW) {
  if (typeof v === "number") return v;
  const r = trackAt(scene, v.track, t);
  if (!r) return axis === "x" ? TL.viewport.width / 2 : TL.viewport.height / 2;
  const win = $("window"), viewH = viewW * win.clientHeight / win.clientWidth, pad = v.pad || 0;
  const [x, y, w, h] = r;
  if (axis === "x") return v.edge === "left" ? x - pad + viewW / 2 : x + w / 2;
  if (v.edge === "top") return y - pad + viewH / 2;
  if (v.edge === "bottom") return y + h + pad - viewH / 2;
  // follow: krotka wymiana na srodku kadru, dluga — jej koniec (najnowsze kroki) przy dolnej krawedzi
  if (v.edge === "follow") return h + 2 * pad <= viewH ? y + h / 2 : y + h + pad - viewH / 2;
  return y + h / 2;
}

function cameraAt(scene, t) {
  const keys = TL.camera[scene.name];
  if (!keys) return null;
  const at = (key) => [key[0], resolve(key[1], "x", scene, t, key[3]), resolve(key[2], "y", scene, t, key[3]), key[3]];
  if (t <= keys[0][0]) return at(keys[0]);
  for (let i = 1; i < keys.length; i++) {
    if (t <= keys[i][0]) {
      const a = at(keys[i - 1]), b = at(keys[i]);
      const k = easeCam(clamp01((t - a[0]) / (b[0] - a[0])));
      return [t, lerp(a[1], b[1], k), lerp(a[2], b[2], k), Math.exp(lerp(Math.log(a[3]), Math.log(b[3]), k))];
    }
  }
  return at(keys[keys.length - 1]);
}

function applyCamera(cam) {
  const win = $("window"), W = win.clientWidth, H = win.clientHeight;
  const vw = TL.viewport.width, vh = TL.viewport.height;
  let [, cx, cy, viewW] = cam;
  const zoom = W / viewW, viewH = H / zoom;
  cx = viewW >= vw ? vw / 2 : Math.min(vw - viewW / 2, Math.max(viewW / 2, cx));
  cy = viewH >= vh ? vh / 2 : Math.min(vh - viewH / 2, Math.max(viewH / 2, cy));
  $("ui").style.transform = `translate(${W / 2 - cx * zoom}px, ${H / 2 - cy * zoom}px) scale(${zoom / TL.scale})`;
  lastCam = { W, H, cx, cy, zoom };
}

// --- kursor: punkt w sledzonym elemencie nagrania -> wspolrzedne okna wedlug biezacej kamery ----------------------
let lastCam = null;
function cursorPoint(scene, t, target) {
  const r = trackAt(scene, target.track, t);
  if (!r) return null;
  return [r[0] + r[2] * target.fx + target.dx, r[1] + r[3] * target.fy + target.dy];
}

function applyCursor(t, scene) {
  const el = $("cursor"), keys = TL.cursor || [];
  let a = null, b = null;
  for (let i = 0; i < keys.length - 1; i++) {
    if (t >= keys[i].t && t <= keys[i + 1].t && keys[i + 1].t - keys[i].t < 3) { a = keys[i]; b = keys[i + 1]; break; }
  }
  const visible = scene.kind === "web" && a && lastCam;
  el.style.visibility = visible ? "visible" : "hidden";
  if (!visible) return;
  const pa = cursorPoint(scene, t, a.target), pb = cursorPoint(scene, t, b.target);
  if (!pa || !pb) { el.style.visibility = "hidden"; return; }
  const k = easeCam(clamp01((t - a.t) / (b.t - a.t)));
  const x = lerp(pa[0], pb[0], k), y = lerp(pa[1], pb[1], k);
  const { W, H, cx, cy, zoom } = lastCam;
  el.style.left = `${W / 2 + (x - cx) * zoom}px`;
  el.style.top = `${H / 2 + (y - cy) * zoom}px`;
  // pojawienie sie i znikniecie: krotkie wygaszenie na pierwszym i ostatnim punkcie serii
  const first = keys.indexOf(a) === 0 || keys[keys.indexOf(a) - 1].t < a.t - 3 || a.t - keys[keys.indexOf(a) - 1].t > 3;
  el.style.opacity = first ? clamp01((t - a.t) / 0.15) : 1;
  // klikniecie: wcisniecie (lekkie zmniejszenie) i rozchodzacy sie krag dokladnie w chwili kliku (dzwiek w score.py)
  const click = keys.find((c) => c.click && t >= c.t - 0.02 && t < c.t + 0.45);
  const since = click ? t - click.t : 1;
  el.querySelector("svg").style.transform = `scale(${click && since < 0.12 ? 0.86 : 1})`;
  const ring = el.querySelector("i");
  ring.style.opacity = click && since >= 0 ? 0.9 * (1 - clamp01(since / 0.4)) : 0;
  ring.style.transform = `scale(${0.3 + 0.9 * easeIn(clamp01(since / 0.4))})`;
}

let currentSrc = "";
async function applyWeb(t, scene) {
  const frame = TL.frames[Math.min(TL.frames.length - 1, Math.max(0, Math.round(t * TL.fps)))];
  if (!frame) return;
  const src = "/cap/" + frame;
  if (src !== currentSrc) {
    const img = $("ui");
    img.src = src;
    currentSrc = src;
    await img.decode();
  }
  const cam = cameraAt(scene, t);
  if (cam) applyCamera(cam);
}

// --- telefon: Telegram ----------------------------------------------------------------------------------------
const phoneNodes = [];
function buildPhone() {
  const feed = $("feed");
  TL.phone.msgs.forEach((m) => {
    const el = document.createElement("div");
    if (m.kind === "kbd") {
      el.className = "kbd";
      m.buttons.forEach((label) => { const b = document.createElement("span"); b.textContent = label; el.appendChild(b); });
    } else {
      el.className = "msg-tg";
    }
    el.style.display = "none";
    feed.appendChild(el);
    phoneNodes.push({ m, el, html: null });
  });
}

function messageHtml(m, t) {
  // tresc z surfaces.json (tg_format); wiadomosc postepu zmienia sie w czasie (bot edytuje ja na biezaco)
  let key = m.html;
  if (m.states) for (const s of m.states) if (t >= s.at) key = s.html;
  let value = SF.telegram;
  for (const p of key.split(".")) value = Array.isArray(value) ? value[Number(p)] : value[p];
  return value + `<span class="time">${m.time || "03:41"}</span>`;
}

function applyPhone(t) {
  const ph = TL.phone;
  phoneNodes.forEach((node) => {
    const { m, el } = node;
    const on = t >= m.at && !(m.until && t >= m.until);
    el.style.display = on ? "" : "none";
    if (!on) return;
    if (m.kind !== "kbd") {
      const html = messageHtml(m, t);
      if (html !== node.html) { el.innerHTML = html; node.html = html; }
    }
    const k = easeIn(clamp01((t - m.at) / 0.3));
    el.style.opacity = clamp01((t - m.at) / 0.08);
    el.style.transform = `translateY(${(1 - k) * 0.7}em) scale(${0.94 + 0.06 * k})`;
  });
  // dotkniecie przycisku: palec pojawia sie i wciska dokladnie w chwili tap.at (dzwiek w score.py w tym samym czasie)
  const tap = ph.tap, touch = $("touch");
  const kbd = phoneNodes.find((n) => n.m.kind === "kbd");
  const button = kbd && kbd.el.style.display !== "none" && kbd.el.children[tap.button];
  if (button) button.classList.toggle("pressed", t >= tap.at && t < tap.at + 0.3);
  const vis = !!button && t >= tap.at - 0.5 && t < tap.at + 0.35;
  show(touch, vis);
  if (vis) {
    const phone = $("phone"), pr = phone.getBoundingClientRect(), br = button.getBoundingClientRect();
    const sx = pr.width / phone.offsetWidth;          // telefon moze byc w trakcie przyblizenia
    const x = (br.left + br.width * 0.42 - pr.left) / sx, y = (br.top + br.height * 0.6 - pr.top) / sx;
    const come = easeIn(clamp01((t - (tap.at - 0.5)) / 0.4));
    const press = t < tap.at ? 1 : t < tap.at + 0.12 ? 0.82 : 1;
    touch.style.left = `${x + (1 - come) * 40}px`;
    touch.style.top = `${y + (1 - come) * 60}px`;
    touch.style.opacity = t < tap.at + 0.1 ? come : 1 - clamp01((t - tap.at - 0.1) / 0.25);
    touch.style.transform = `scale(${press})`;
  }
}

// --- terminal: stany z prawdziwego CLI ------------------------------------------------------------------------
let ttyKey = "";
function applyTerm(t) {
  let state = null;
  for (const s of TL.term.states) if (t >= s.at) state = s;
  if (!state) return;
  const data = SF.terminal[state.index];
  const caret = '<span class="caret"></span>';
  let html = data.html.replace(/\n+$/, "\n");
  if (data.prompt !== undefined) html += `<b>›</b> ${data.prompt}${caret}`;
  else if (state.final) html += `\n<b>›</b> ${caret}`;
  const key = state.index + (state.final ? "f" : "");
  if (key !== ttyKey) { $("tty").innerHTML = html; ttyKey = key; }
}

// --- duze slowa -----------------------------------------------------------------------------------------------
let wordsScene = "";
function applyWords(t, scene) {
  const box = $("words"), block = TL.words[scene.name];
  if (!block) return;
  if (wordsScene !== scene.name) {
    box.innerHTML = "";
    block.forEach((item) => {
      const row = document.createElement("div");
      row.className = "w";
      const span = document.createElement("span");
      span.textContent = item.text;
      row.appendChild(span);
      box.appendChild(row);
    });
    wordsScene = scene.name;
  }
  [...box.children].forEach((row, i) => {
    const item = block[i], span = row.firstElementChild;
    const k = easeIn(clamp01((t - item.at) / 0.4));
    span.style.transform = `translateY(${(1 - k) * 105}%)`;
    span.style.visibility = t >= item.at ? "visible" : "hidden";
    const next = block[i + 1];
    span.style.color = next && t >= next.at ? "var(--dim)" : "var(--ink)";
  });
}

// --- napisy ---------------------------------------------------------------------------------------------------
function buildCaptions() {
  const box = $("captions");
  TL.captions.forEach((cap) => {
    const el = document.createElement("div");
    el.className = "cap";
    cap.lines = cap.lines.map((line, i) => (typeof line === "string" ? { text: line, at: cap.start + i * 0.07 } : line));
    cap.lines.forEach((line) => {
      const row = document.createElement("span");
      row.className = "line";
      const inner = document.createElement("span");
      inner.innerHTML = line.text;            // tylko nasze znaczniki <b>/<g>/<m> z edit.py
      row.appendChild(inner);
      el.appendChild(row);
    });
    cap.el = el;
    box.appendChild(el);
  });
}

function applyCaptions(t) {
  TL.captions.forEach((cap) => {
    const rows = cap.el.querySelectorAll(".line > span");
    let any = false;
    rows.forEach((span, i) => {
      const inAt = cap.lines[i].at, outAt = cap.end - 0.22 + i * 0.03;
      let off = null;
      if (t >= inAt && t <= outAt + 0.22) {
        off = t < outAt ? (1 - easeIn(clamp01((t - inAt) / 0.5))) * 108 : -easeOut(clamp01((t - outAt) / 0.22)) * 108;
      }
      span.style.visibility = off === null ? "hidden" : "visible";
      if (off !== null) { span.style.transform = `translateY(${off}%)`; any = true; }
    });
    cap.el.style.visibility = any ? "visible" : "hidden";
  });
}

// --- zegar, marka, plansza --------------------------------------------------------------------------------------
function applyHook(t, scene) {
  if (scene.kind !== "hook") return;
  const page = $("hook").querySelector(".page");
  page.style.transform = `scale(${1 + 0.05 * easeCam(clamp01((t - scene.start) / (scene.end - scene.start)))})`;
}

function applyBrand(t) {
  $("brand").style.opacity = 1 - clamp01((t - (TL.end - 0.3)) / 0.3);
}

function applyEnd(t) {
  const end = $("end"), start = TL.end;
  show(end, t >= start);
  if (t < start) return;
  const lock = end.querySelector(".lockup");
  const k = easeIn(clamp01((t - start) / 0.9));
  lock.style.opacity = clamp01((t - start) / 0.15);
  lock.style.transform = `scale(${1.12 - 0.12 * k})`;
  [[".l1", 0.35], [".l2", 0.5], [".l3", 1.0], [".l4", 1.12]].forEach(([sel, delay]) => {
    const p = easeIn(clamp01((t - start - delay) / 0.6));
    end.querySelector(sel + " > span").style.transform = `translateY(${(1 - p) * 108}%)`;
  });
}

// --- klatka ---------------------------------------------------------------------------------------------------
const SURFACES = { hook: "hook", web: "window", phone: "phone", term: "term", words: "words" };
async function seek(t) {
  const scene = sceneAt(t);
  Object.entries(SURFACES).forEach(([kind, id]) => $(id).classList.toggle("on", scene.kind === kind));
  const id = SURFACES[scene.kind];
  if (id) {
    // koniec ujecia = koniec ostatniej sceny tej samej grupy (to samo "enter")
    const group = TL.scenes.filter((s) => s.enter === scene.enter && s.kind === scene.kind);
    punch($(id), t, scene.enter, group[group.length - 1].end, scene.kind !== "web");
  }
  if (scene.kind === "web") await applyWeb(t, scene);
  applyCursor(t, scene);
  if (scene.kind === "phone") applyPhone(t);
  if (scene.kind === "term") applyTerm(t);
  if (scene.kind === "words") applyWords(t, scene);
  applyHook(t, scene);
  applyBrand(t);
  applyCaptions(t);
  applyEnd(t);
  return true;
}
window.seek = seek;

window.ready = (async () => {
  TL = await (await fetch("/timeline.json")).json();
  SF = await (await fetch("/surfaces.json")).json();
  buildCaptions();
  buildPhone();
  await document.fonts.ready;
  await Promise.all(["400", "500", "600"].map((w) => document.fonts.load(`${w} 40px "Plex Sans"`)));
  await Promise.all(["400", "500"].map((w) => document.fonts.load(`${w} 40px "Plex Mono"`)));
  return true;
})();
