// Prosba o dostep do Pipe wystawionego publicznie (/web wlacz): kod na ekranie, zgoda administratora w rozmowie
// (Telegram, CLI, pipe web na innym urzadzeniu), potem ciasteczko urzadzenia i zwykla strona. Bez innerHTML.
(function () {
  "use strict";
  const config = JSON.parse(document.getElementById("pipe-config").textContent || "{}");
  const lang = config.lang === "en" ? "en" : "pl";
  const T = {
    pl: {
      title: "Wejście do Pipe",
      lead: "To urządzenie nie ma jeszcze dostępu. Poproś o niego, a ja zapytam w rozmowie z Pipe, czy je wpuścić.",
      ask: "Poproś o dostęp",
      again: "Poproś jeszcze raz",
      codeLead: "Sprawdź, czy ten sam kod widzisz w wiadomości od Pipe, i naciśnij tam „Wpuść”.",
      hint: "Wiadomość przyjdzie na Telegram, do pipe web na innym urządzeniu i do CLI. Możesz też napisać w Pipe: /web wpusc ",
      waiting: "Czekam na zgodę…",
      approved: "Wpuszczone. Otwieram Pipe…",
      denied: "Odmówiono dostępu.",
      expired: "Kod wygasł — poproś jeszcze raz.",
      unknown: "Prośba przepadła (np. restart Pipe) — poproś jeszcze raz.",
      error: "Nie udało się: ",
      foot: "Każde urządzenie wchodzi raz, potem Pipe pamięta je przez 30 dni od ostatniego użycia.",
    },
    en: {
      title: "Sign in to Pipe",
      lead: "This device has no access yet. Ask for it and I will ask in your Pipe chat whether to let it in.",
      ask: "Ask for access",
      again: "Ask again",
      codeLead: "Check that the message from Pipe shows the same code and press “Let in” there.",
      hint: "The message goes to Telegram, to pipe web on another device and to the CLI. You can also write in Pipe: /web approve ",
      waiting: "Waiting for approval…",
      approved: "Approved. Opening Pipe…",
      denied: "Access denied.",
      expired: "The code expired — ask again.",
      unknown: "The request is gone (e.g. Pipe restarted) — ask again.",
      error: "Failed: ",
      foot: "Each device signs in once, then Pipe remembers it for 30 days after its last use.",
    },
  }[lang];
  const $ = (id) => document.getElementById(id);
  document.documentElement.lang = lang;
  $("pair-server").textContent = config.server || "Pipe";
  $("pair-title").textContent = T.title;
  $("pair-lead").textContent = T.lead;
  $("pair-foot").textContent = T.foot;
  const button = $("pair-btn");
  button.textContent = T.ask;

  let timer = 0;
  function status(text, bad) {
    const el = $("pair-status");
    el.hidden = !text;
    el.textContent = text || "";
    el.classList.toggle("bad", !!bad);
  }
  function reset(text) {
    clearTimeout(timer);
    $("pair-code").hidden = true;
    $("pair-hint").hidden = true;
    $("pair-lead").textContent = T.lead;
    status(text, true);
    button.hidden = false;
    button.disabled = false;
    button.textContent = T.again;
  }
  async function post(path, body) {
    const response = await fetch(path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
      credentials: "same-origin",
    });
    let data = {};
    try { data = await response.json(); } catch (_) { /* pusta odpowiedz */ }
    if (!response.ok) throw new Error(data.error || "HTTP " + response.status);
    return data;
  }
  async function poll(pairing, deadline) {
    if (Date.now() > deadline) { reset(T.expired); return; }
    let data;
    try { data = await post("/api/pair/poll", { id: pairing.id, secret: pairing.secret }); }
    catch (_) { timer = setTimeout(() => poll(pairing, deadline), 4000); return; }     // chwilowy brak sieci
    if (data.status === "approved") {
      status(T.approved);
      setTimeout(() => location.replace("/"), 600);
    } else if (data.status === "pending") {
      timer = setTimeout(() => poll(pairing, deadline), 2000);
    } else {
      reset({ denied: T.denied, expired: T.expired }[data.status] || T.unknown);
    }
  }
  button.addEventListener("click", async () => {
    button.disabled = true;
    status("");
    let pairing;
    try { pairing = await post("/api/pair/start"); }
    catch (error) { reset(T.error + error.message); return; }
    $("pair-lead").textContent = T.codeLead;
    $("pair-code").textContent = pairing.code;
    $("pair-code").hidden = false;
    $("pair-hint").textContent = T.hint + pairing.code;
    $("pair-hint").hidden = false;
    button.hidden = true;
    status(T.waiting);
    poll(pairing, Date.now() + (pairing.expires_in || 300) * 1000);
  });
})();
