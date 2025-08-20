/* ======================================================================
   Ankify.AI — Front-end logic (static/app.js)
   Wires buttons, reads inputs, calls Flask endpoints, handles downloads.
   Endpoints expected:
     GET  /healthz
     GET  /usage
     GET  /admin/subscriptions?email=...
     POST /api/checkout
     POST /api/billing-portal
     POST /build-apkg
   ====================================================================== */

console.log("✅ Ankify.AI app.js loaded");

// Global error logger to surface silent JS errors
window.addEventListener("error", (e) => {
  // eslint-disable-next-line no-console
  console.error("JS runtime error:", e?.error || e?.message || e);
});

// -------------- Shorthand selectors --------------
const $  = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// -------------- Elements --------------
const apiStatus      = $("#apiStatus span");
const usagePill      = $("#usagePill");
const statusEl       = $("#status");
const signedInPill   = $("#signedInPill");

const emailInput     = $("#userEmail");
const subscribeBtn   = $("#subscribeBtn");
const manageBtn      = $("#manageBillingBtn");

const inputText      = $("#inputText");
const deckTitleEl    = $("#deckTitle");
const minCardsEl     = $("#minCards");
const modesBox       = $("#modesBox");
const yieldSlider    = $("#yieldSlider");
const yieldBadge     = $("#yieldBadge");
const yieldHint      = $("#yieldHint");
const wordsPerChunk  = $("#wordsPerChunk");

const buildBtn       = $("#buildBtn");
const checkUsageBtn  = $("#checkUsageBtn");

// -------------- Status helpers --------------
function setStatus(msg, ok = null) {
  if (!statusEl) return;
  statusEl.textContent = msg || "";
  statusEl.className = "status";
  if (ok === true)  statusEl.classList.add("ok");
  if (ok === false) statusEl.classList.add("err");
}

// -------------- Yield UI --------------
function updateYieldUI() {
  if (!yieldSlider || !yieldBadge || !yieldHint) return;
  const v = Number(yieldSlider.value) / 100.0; // 0..1
  let label, desc;
  if (v <= 0.01)         { label = "Exhaustive";        desc = "Attempt one card per fact (two-pass)."; }
  else if (v <= 0.25)    { label = "Broad coverage";    desc = "More granular facts and definitions."; }
  else if (v <= 0.6)     { label = "Balanced coverage"; desc = "Mix of key concepts and coverage."; }
  else if (v < 1.0)      { label = "High-yield focus";  desc = "Primarily highest-yield material."; }
  else                   { label = "Only highest-yield";desc = "Strictly the essentials."; }
  yieldBadge.textContent = label;
  yieldHint.textContent  = "0 = exhaustive (attempt one card per fact). 1 = only highest-yield essentials. " + desc;
}

// -------------- Modes helper --------------
function getSelectedModes() {
  return $$('#modesBox input[type="checkbox"]:checked').map(cb => cb.value);
}

// -------------- Health & Usage --------------
async function refreshHealth() {
  try {
    const r = await fetch("/healthz", { cache: "no-store" });
    apiStatus.textContent = r.ok ? "ok" : "unavailable";
  } catch {
    apiStatus.textContent = "unavailable";
  }
}

async function refreshUsage() {
  try {
    const r = await fetch("/usage", { cache: "no-store" });
    if (!r.ok) throw new Error("usage failed");
    const j = await r.json();
    const used = typeof j.cards_used === "number" ? j.cards_used : "?";
    const cap  = typeof j.cap === "number" ? j.cap : "?";
    const mon  = j.month || "?";
    usagePill.textContent = `Usage: ${used} / ${cap} (month: ${mon})`;
  } catch {
    usagePill.textContent = "Usage: unavailable";
  }
}

// -------------- Subscription helpers --------------
function setSignedInPill(email) {
  if (!signedInPill) return;
  if (email) {
    signedInPill.textContent = `Signed in as ${email} — Active`;
    signedInPill.classList.add("ok");
    signedInPill.style.display = "";
  } else {
    signedInPill.textContent = "";
    signedInPill.classList.remove("ok");
    signedInPill.style.display = "none";
  }
}

function showSubscribe(show) {
  if (!subscribeBtn) return;
  if (show) {
    subscribeBtn.classList.remove("hidden");
    subscribeBtn.style.display = "";
  } else {
    subscribeBtn.classList.add("hidden");
    subscribeBtn.style.display = "none";
  }
}

async function checkSubscription(email) {
  if (!email) {
    setSignedInPill(null);
    showSubscribe(true);
    return;
  }
  try {
    const r = await fetch(`/admin/subscriptions?email=${encodeURIComponent(email)}`, { cache: "no-store" });
    if (!r.ok) { setSignedInPill(null); showSubscribe(true); return; }
    const j = await r.json();
    const active = j.found &&
                   (j.status === "active" || j.status === "trialing") &&
                   Number(j.current_period_end || 0) * 1000 >= Date.now();
    if (active) {
      setSignedInPill(email);
      showSubscribe(false);
    } else {
      setSignedInPill(null);
      showSubscribe(true);
    }
  } catch {
    setSignedInPill(null);
    showSubscribe(true);
  }
}

// -------------- Local storage for email --------------
function restoreEmail() {
  try {
    const saved = localStorage.getItem("ankify_email");
    if (saved) emailInput.value = saved;
  } catch { /* ignore */ }
}
function persistEmail() {
  try {
    const v = (emailInput.value || "").trim().toLowerCase();
    if (v) localStorage.setItem("ankify_email", v);
  } catch { /* ignore */ }
}

// -------------- Stripe: Subscribe & Manage Billing --------------
async function subscribe() {
  const email = (emailInput.value || "").trim().toLowerCase();
  if (!email) {
    setStatus("Enter your email before subscribing.", false);
    statusEl?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    return;
  }
  persistEmail();
  setStatus("Creating checkout session…");

  try {
    const r = await fetch("/api/checkout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email })
    });
    if (!r.ok) {
      const t = await r.text();
      setStatus(`Checkout error: ${t}`, false);
      return;
    }
    const j = await r.json();
    if (j.url) {
      window.location = j.url; // to Stripe Checkout
    } else {
      setStatus("Unexpected checkout response.", false);
    }
  } catch (err) {
    setStatus(`Checkout failed: ${err}`, false);
  }
}

async function manageBilling() {
  const email = (emailInput.value || "").trim().toLowerCase();
  if (!email) {
    setStatus("Enter your email to manage billing.", false);
    statusEl?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    return;
  }
  persistEmail();
  setStatus("Opening billing portal…");
  try {
    const r = await fetch("/api/billing-portal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email })
    });
    if (!r.ok) {
      const t = await r.text();
      setStatus(`Billing portal error: ${t}`, false);
      return;
    }
    const j = await r.json();
    if (j.url) {
      window.location = j.url; // to Stripe Portal
    } else {
      setStatus("Unexpected portal response.", false);
    }
  } catch (err) {
    setStatus(`Billing portal request failed: ${err}`, false);
  }
}

// -------------- Build .apkg --------------
async function buildDeck() {
  const email = (emailInput.value || "").trim().toLowerCase();
  if (!email) {
    setStatus("Enter your email (must match your subscription).", false);
    statusEl?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    return;
  }
  persistEmail();

  const text = (inputText.value || "").trim();
  if (!text) {
    setStatus("Please paste some text.", false);
    return;
  }

  const title       = (deckTitleEl.value || "Ankify.AI Deck").trim();
  const minCards    = Math.max(0, parseInt(minCardsEl.value || "0", 10));
  const modes       = getSelectedModes();
  if (modes.length === 0) {
    setStatus("Select at least one card type.", false);
    return;
  }
  const yLevel      = Number(yieldSlider.value) / 100.0;
  const chunkWords  = Math.max(300, parseInt(wordsPerChunk.value || "700", 10));

  const payload = {
    email,
    deck_title: title,
    text,
    yield_level: yLevel,
    modes,
    approx_cards: minCards,     // floor in balanced mode
    words_per_chunk: chunkWords // affects both pipelines
  };

  setStatus("Building deck… this may take a moment.");
  try {
    const r = await fetch("/build-apkg", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    if (!r.ok) {
      const msg = await r.text();
      setStatus(`Error: ${msg}`, false);
      // Re-check subscription in case it's inactive
      await checkSubscription(email);
      return;
    }

    // Download the .apkg file
    const blob = await r.blob();
    const cd   = r.headers.get("Content-Disposition") || "";
    const match = cd.match(/filename\*=UTF-8''([^;]+)|filename="?([^"]+)"?/i);
    let filename = "AnkifyAI.apkg";
    if (match) {
      filename = decodeURIComponent(match[1] || match[2]).replace(/[/\\]/g, "_");
    } else {
      filename = title.replace(/\s+/g, "_") + ".apkg";
    }

    const url = URL.createObjectURL(blob);
    const a   = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);

    setStatus(`Deck built: ${filename}`, true);
    await refreshUsage();
    await checkSubscription(email);
  } catch (err) {
    setStatus(`Request failed: ${err}`, false);
    await checkSubscription(email);
  }
}

// -------------- Query params (Stripe return) --------------
function getQuery() {
  const q = {};
  (location.search || "").replace(/^\?/, "").split("&").forEach(kv => {
    if (!kv) return;
    const [k, v] = kv.split("=");
    q[decodeURIComponent(k || "")] = decodeURIComponent(v || "");
  });
  return q;
}

// -------------- Init --------------
function initEventHandlers() {
  // Yield slider
  if (yieldSlider) {
    yieldSlider.addEventListener("input", updateYieldUI);
    updateYieldUI();
  }

  // Buttons
  subscribeBtn?.addEventListener("click", subscribe);
  manageBtn?.addEventListener("click", manageBilling);
  buildBtn?.addEventListener("click", buildDeck);
  checkUsageBtn?.addEventListener("click", refreshUsage);

  // Email persistence & subscription check
  emailInput?.addEventListener("change", () => {
    persistEmail();
    const v = (emailInput.value || "").trim().toLowerCase();
    checkSubscription(v);
  });
  emailInput?.addEventListener("blur", () => {
    persistEmail();
    const v = (emailInput.value || "").trim().toLowerCase();
    checkSubscription(v);
  });
}

async function init() {
  restoreEmail();
  await refreshHealth();
  await refreshUsage();

  const email = (emailInput?.value || "").trim().toLowerCase();
  if (email) await checkSubscription(email);

  // If returning from Stripe (?subscribed=1 or ?canceled=1), refresh status then clean URL
  const qp = getQuery();
  if (qp.subscribed === "1" || qp.canceled === "1") {
    if (email) {
      setStatus(qp.subscribed === "1" ? "Subscription updated. Checking status…" : "Checkout canceled.");
      await checkSubscription(email);
    }
    if (history.replaceState) history.replaceState(null, "", location.pathname);
  }
}

// Since client.html uses `defer`, DOM is ready when this runs,
// but we still guard with DOMContentLoaded for safety in other hosts.
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => {
    initEventHandlers();
    init().catch(err => console.error("Init error:", err));
  });
} else {
  initEventHandlers();
  init().catch(err => console.error("Init error:", err));
}
