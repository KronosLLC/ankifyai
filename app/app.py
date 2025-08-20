# app/app.py
import os
import re
import json
import time
import sqlite3
import hashlib
import tempfile
from datetime import datetime
from typing import List, Dict, Any

from flask import Flask, request, send_file, send_from_directory, jsonify, Response
import genanki
import requests
import stripe

# ───────────────────────── Env: Stripe ─────────────────────────
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")               # sk_test_... or sk_live_...
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID")                # price_...
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")    # whsec_...

# ───────────────────────── Paths & Flask ─────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))   # .../AnkifyAI/app
APP_ROOT = os.path.dirname(BASE_DIR)                    # .../AnkifyAI
STATIC_DIR = os.path.join(APP_ROOT, "static")           # .../AnkifyAI/static
DATA_DIR = os.path.join(APP_ROOT, "data")               # .../AnkifyAI/data
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(
    __name__,
    static_folder=STATIC_DIR,
    static_url_path="/static"
)
# Safety: cap request body (~2 MB) to avoid OOM on tiny instances
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

@app.route("/")
def index():
    # Serves static/client.html (must exist at AnkifyAI/static/client.html)
    return send_from_directory(app.static_folder, "client.html")

# ───────────────────────── Subscription DB (SQLite) ─────────────────────────
# Option B: default to a portable, project-local path and ensure the directory exists
DB_PATH = os.environ.get("SUBS_DB_PATH", os.path.join(DATA_DIR, "subscriptions.db"))

def _db():
    dbdir = os.path.dirname(DB_PATH)
    if dbdir and not os.path.exists(dbdir):
        os.makedirs(dbdir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _init_db():
    with _db() as conn:
        conn.execute("""
          CREATE TABLE IF NOT EXISTS subscriptions (
            email TEXT PRIMARY KEY,
            status TEXT,                 -- active, trialing, past_due, canceled, incomplete, unpaid
            current_period_end INTEGER,  -- epoch seconds
            customer_id TEXT,
            subscription_id TEXT,
            updated_at INTEGER
          );
        """)
        conn.commit()
_init_db()

def upsert_subscription(email, status, cpe, customer_id=None, subscription_id=None):
    email = (email or "").strip().lower()
    now = int(time.time())
    with _db() as conn:
        conn.execute("""
          INSERT INTO subscriptions(email, status, current_period_end, customer_id, subscription_id, updated_at)
          VALUES (?, ?, ?, ?, ?, ?)
          ON CONFLICT(email) DO UPDATE SET
            status=excluded.status,
            current_period_end=excluded.current_period_end,
            customer_id=COALESCE(excluded.customer_id, customer_id),
            subscription_id=COALESCE(excluded.subscription_id, subscription_id),
            updated_at=excluded.updated_at;
        """, (email, (status or ""), int(cpe or 0), customer_id, subscription_id, now))
        conn.commit()

def get_subscription(email):
    email = (email or "").strip().lower()
    with _db() as conn:
        cur = conn.execute("SELECT * FROM subscriptions WHERE email=?", (email,))
        return cur.fetchone()

def is_active(sub_row):
    if not sub_row:
        return False
    status = (sub_row["status"] or "").lower()
    cpe = int(sub_row["current_period_end"] or 0)
    if status not in ("active", "trialing"):
        return False
    return cpe >= int(time.time())

# (Optional) small admin peek endpoint for testing; remove or protect later.
@app.get("/admin/subscriptions")
def admin_subs():
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return ("Provide ?email=", 400)
    row = get_subscription(email)
    if not row:
        return jsonify({"email": email, "found": False})
    return jsonify({
        "email": email,
        "status": row["status"],
        "current_period_end": row["current_period_end"],
        "customer_id": row["customer_id"],
        "subscription_id": row["subscription_id"],
        "found": True
    })

# ───────────────────────── Stripe: Checkout + Portal + Webhook ─────────────────────────
@app.post("/api/checkout")
def api_checkout():
    """
    Body: { "email": "you@example.com" }
    Returns: { "url": "https://checkout.stripe.com/..." }
    """
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return ("Email required", 400)

    host = request.host_url.rstrip("/")
    success_url = f"{host}/?subscribed=1"
    cancel_url  = f"{host}/?canceled=1"

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            customer_email=email,
            allow_promotion_codes=False
        )
        # pre-create row as 'incomplete' (status will update via webhook)
        upsert_subscription(email, "incomplete", None, None, None)
        return jsonify({"url": session.url})
    except Exception as e:
        return (f"Checkout error: {e}", 400)

@app.post("/api/billing-portal")
def billing_portal():
    """
    Body: { "email": "you@example.com" }
    Returns: { "url": "https://billing.stripe.com/session/..." }
    """
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return ("Email required", 400)

    row = get_subscription(email)
    if not row or not row["customer_id"]:
        return ("No Stripe customer found for this email. Subscribe first.", 404)

    return_url = request.host_url.rstrip("/") + "/"
    try:
        session = stripe.billing_portal.Session.create(
            customer=row["customer_id"],
            return_url=return_url
        )
        return jsonify({"url": session.url})
    except Exception as e:
        return (f"Billing portal error: {e}", 400)

@app.post("/api/stripe/webhook")
def stripe_webhook():
    payload = request.data
    sig = request.headers.get("Stripe-Signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return Response(f"Webhook signature error: {e}", status=400)

    et = event["type"]
    obj = event["data"]["object"]

    # Checkout completed → we learn email + customer/sub ids
    if et == "checkout.session.completed":
        email = ((obj.get("customer_details") or {}).get("email") or "").lower()
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")
        if email:
            upsert_subscription(email, "incomplete", None, customer_id, subscription_id)

    # Subscription lifecycle updates → status + current_period_end
    if et in ("customer.subscription.created", "customer.subscription.updated"):
        customer_id = obj.get("customer")
        status = obj.get("status")  # active, trialing, past_due, canceled, unpaid, incomplete
        cpe = obj.get("current_period_end")   # epoch seconds
        if customer_id:
            try:
                cust = stripe.Customer.retrieve(customer_id)
                email = (cust.get("email") or "").lower()
                if email:
                    upsert_subscription(email, status, cpe, customer_id, obj.get("id"))
            except Exception:
                pass

    if et == "customer.subscription.deleted":
        customer_id = obj.get("customer")
        if customer_id:
            try:
                cust = stripe.Customer.retrieve(customer_id)
                email = (cust.get("email") or "").lower()
                if email:
                    upsert_subscription(email, "canceled", int(time.time()), customer_id, obj.get("id"))
            except Exception:
                pass

    return {"ok": True}

# ───────────────────────── Usage (monthly cap via JSON file) ─────────────────────────
USAGE_PATH = os.path.join(DATA_DIR, "adaptive_profile.json")
DEFAULT_CAP_CARDS_PER_MONTH = int(os.environ.get("OPENAI_MONTHLY_CAP", "50000"))
USAGE_SCHEMA_VERSION = 1  # bump if schema changes

def _usage_defaults() -> dict:
    return {
        "version": USAGE_SCHEMA_VERSION,
        "month": datetime.utcnow().strftime("%Y-%m"),
        "cards_used": 0,
        "cap": DEFAULT_CAP_CARDS_PER_MONTH,
    }

def _validate_and_patch_usage(data: dict) -> dict:
    base = _usage_defaults()
    if not isinstance(data, dict):
        return base
    out = {**base, **{k: v for k, v in data.items() if k in base}}

    # month rollover
    current_month = datetime.utcnow().strftime("%Y-%m")
    if out.get("month") != current_month:
        out["month"] = current_month
        out["cards_used"] = 0

    # numeric sanity
    try:
        out["cards_used"] = int(out.get("cards_used", 0))
    except Exception:
        out["cards_used"] = 0
    try:
        out["cap"] = int(out.get("cap", DEFAULT_CAP_CARDS_PER_MONTH))
    except Exception:
        out["cap"] = DEFAULT_CAP_CARDS_PER_MONTH

    out["cards_used"] = max(0, out["cards_used"])
    out["cap"] = max(0, out["cap"])
    out["version"] = USAGE_SCHEMA_VERSION
    return out

def _save_usage_atomic(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="usage_", suffix=".json", dir=DATA_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, USAGE_PATH)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass

def load_usage() -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(USAGE_PATH):
        data = _usage_defaults()
        _save_usage_atomic(data)
        return data
    try:
        with open(USAGE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = {}
    data = _validate_and_patch_usage(raw)
    if raw != data:
        _save_usage_atomic(data)
    return data

def save_usage(data: dict) -> None:
    _save_usage_atomic(_validate_and_patch_usage(data))

@app.get("/usage")
def usage_endpoint():
    return load_usage()

@app.post("/usage/reset")
def usage_reset():
    body = request.get_json(silent=True) or {}
    cap = body.get("cap")
    data = _usage_defaults()
    if isinstance(cap, int) and cap >= 0:
        data["cap"] = cap
    save_usage(data)
    return {"ok": True, "usage": data}

# ───────────────────────── Modes ─────────────────────────
MODE_ALIASES = {
    "Basic Recall (Q/A)": {"basic recall (q/a)", "recall", "basic", "qa", "q/a"},
    "Fill in the Blank":  {"fill in the blank", "fill-in-the-blank", "cloze"},
    "Mechanism (Why/How)":{"mechanism", "why", "how", "why/how"},
    "Scenario":           {"scenario", "case", "vignette"},
}
CANONICAL_MODES = list(MODE_ALIASES.keys())

def normalize_modes(modes_in: List[str]) -> List[str]:
    if not modes_in:
        return ["Basic Recall (Q/A)"]
    out = []
    for m in modes_in:
        ml = (m or "").strip().lower()
        added = False
        for canon, aliases in MODE_ALIASES.items():
            if ml == canon.lower() or ml in aliases:
                out.append(canon); added = True; break
        if not added:
            out.append("Basic Recall (Q/A)")
    # de-dupe keep order
    seen, cleaned = set(), []
    for m in out:
        if m not in seen:
            cleaned.append(m); seen.add(m)
    return cleaned

# ───────────────────────── OpenAI (mini only) ─────────────────────────
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL   = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()  # mini by default
OPENAI_URL     = "https://api.openai.com/v1/chat/completions"

# ───────────────────────── Text helpers ─────────────────────────
def clean_text(text: str) -> str:
    CITATION_BRACKETS = re.compile(r"\[\s*\d+\s*\]")
    MULTI_SPACE = re.compile(r"\s+")
    text = CITATION_BRACKETS.sub("", text)
    text = text.replace("\u00a0", " ")
    text = MULTI_SPACE.sub(" ", text).strip()
    return text

def chunk_by_words(text: str, target_words=700, overlap=120) -> List[str]:
    words = clean_text(text).split()
    if not words:
        return []
    chunks = []
    i = 0
    n = len(words)
    while i < n:
        chunk = " ".join(words[i:i+target_words])
        if chunk.strip():
            chunks.append(chunk)
        step = max(1, target_words - overlap)
        i += step
    return chunks

# ───────────────────────── Yield → Density & Targets ─────────────────────────
def cards_per_1000_words(yield_level: float) -> float:
    y = max(0.0, min(1.0, yield_level))
    min_density = 6.0
    max_density = 18.0
    return min_density + (1.0 - y) * (max_density - min_density)

def estimate_total_cards(raw_text: str, approx_cards: int, yield_level: float) -> int:
    total_words = len(clean_text(raw_text).split())
    auto = int(round((total_words / 1000.0) * cards_per_1000_words(yield_level)))
    return max(approx_cards, auto)

# ───────────────────────── JSON parsing helper ─────────────────────────
def parse_json_array(s: str):
    try:
        return json.loads(s)
    except Exception:
        start = s.find('['); end = s.rfind(']') + 1
        if start != -1 and end > start:
            return json.loads(s[start:end])
        raise

# ───────────────────────── Fact extraction & card building ─────────────────────────
def normalize_fact(f: str) -> str:
    t = clean_text(f).lower()
    t = re.sub(r"\s*\.\s*$", "", t)
    return t

def ai_extract_facts(chunk_text: str, max_facts: int) -> List[Dict[str, Any]]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set in environment.")

    sys = "You extract atomic, testable, non-overlapping factual statements from study text. Output STRICT JSON only."
    usr = f"""
STUDY TEXT:
<<<{chunk_text}>>>

TASK:
- Return up to {max_facts} atomic facts as a JSON ARRAY of objects, each {{"fact": "..."}}.
- A fact must be a single, self-contained statement (definition, key property, threshold, mechanism step, cause→effect).
- Preserve quantitative values (cutoffs, doses, triads, first-line choices).
- Avoid redundancies; split long sentences into multiple atomic facts where appropriate.
- Use concise, neutral language. No citations.

RETURN: STRICT JSON ARRAY ONLY.
"""
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": sys},
            {"role": "user", "content": usr}
        ],
        "temperature": 0.1,
        "max_tokens": 2200,
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    r = requests.post(OPENAI_URL, headers=headers, json=payload, timeout=120)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    items = parse_json_array(content)
    out = []
    for it in items:
        if isinstance(it, dict):
            fact = (it.get("fact") or "").strip()
        elif isinstance(it, str):
            fact = it.strip()
        else:
            fact = ""
        if fact:
            out.append({"fact": fact})
    return out

def ai_cards_from_facts(facts: List[Dict[str, Any]], modes: List[str]) -> List[Dict[str, Any]]:
    if not facts:
        return []
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set in environment.")

    banned = ["What does this mean", "Which of the following", "True or False", "T/F", "Select all that apply"]
    facts_json = json.dumps(facts[:50], ensure_ascii=False)

    sys = "You turn atomic facts into precise Anki flashcards. Output STRICT JSON only."
    usr = f"""
FACTS (JSON):
{facts_json}

MODES ALLOWED: {', '.join(modes)}

CONVERT each fact into exactly one flashcard object:
{{"front": "...", "back": "...", "mode": "<one of {CANONICAL_MODES}>"}}

RULES:
- Front must cue the exact fact with a specific question (avoid vague stems; avoid {banned}).
- Answers concise (≈5–35 words).
- If "Fill in the Blank", put one/two key terms in {{c1::...}} on the FRONT; explanation on the back.
- Stay faithful to the fact; no hallucinations.
- Return STRICT JSON ARRAY ONLY.
"""
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": sys},
            {"role": "user", "content": usr}
        ],
        "temperature": 0.2,
        "max_tokens": 1800,
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    r = requests.post(OPENAI_URL, headers=headers, json=payload, timeout=120)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    items = parse_json_array(content)

    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        front = (it.get("front") or "").strip()
        back  = (it.get("back") or "").strip()
        mode  = normalize_modes([it.get("mode", "Basic Recall (Q/A)")])[0]
        if front and back:
            out.append({"front": front, "back": back, "mode": mode})
    return out

# ───────────────────────── Balanced (single-pass) generator ─────────────────────────
def ai_generate_batch(chunk_text: str, n_cards: int, modes: List[str], yield_level: float) -> List[Dict[str, Any]]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set in environment.")

    banned_stems = [
        "What does this mean", "Which of the following", "All of the following",
        "True or False", "T/F", "Select all that apply"
    ]

    sys = "You convert study text into high-quality Anki flashcards. Output STRICT JSON only."
    usr = f"""
You will read STUDY TEXT and produce up to {n_cards} high-quality Anki flashcards.

STUDY TEXT (already cleaned of numeric citations like [61]):
<<<{chunk_text}>>>

MODES ALLOWED: {', '.join(modes)}

YIELD LEVEL: {yield_level} (0 = broad coverage, 1 = only highest-yield)
INTERPRETATION:
- If yield is low (≤0.3): prefer coverage; include granular facts, definitions, thresholds, lists.
- If yield is mid (~0.5): mix coverage with key concepts.
- If yield is high (≥0.8): choose only the highest-yield mechanistic/diagnostic/first-line facts.

REQUIREMENTS:
- Return STRICT JSON ARRAY. Each item: {{"front": "...", "back": "...", "mode": "<one of {CANONICAL_MODES}>"}}
- Questions MUST be specific; avoid vague stems like "What does this mean", "True/False", "Which of the following".
- Answers concise (≈5–35 words) and factual.
- For "Fill in the Blank", put Anki cloze like {{c1::term}} in the FRONT; explanations can go in the back.
- Avoid duplicates; avoid trivial rephrasings.
- Distribute cards across distinct ideas present in the text (do not over-focus on a single sentence).
"""
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": sys},
            {"role": "user", "content": usr}
        ],
        "temperature": 0.2,
        "max_tokens": 1800,
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    r = requests.post(OPENAI_URL, headers=headers, json=payload, timeout=90)
    r.raise_for_status()
    data = r.json()
    content = data["choices"][0]["message"]["content"]

    cards = parse_json_array(content)

    cleaned: List[Dict[str, Any]] = []
    seen_pairs = set()
    for c in cards:
        if not isinstance(c, dict):
            continue
        front = (c.get("front") or "").strip()
        back  = (c.get("back") or "").strip()
        mode_raw = (c.get("mode") or "").strip()
        mode = normalize_modes([mode_raw])[0]
        if not front or not back:
            continue
        sig = (front, back)
        if sig in seen_pairs:
            continue
        seen_pairs.add(sig)
        cleaned.append({"front": front, "back": back, "mode": mode})
        if len(cleaned) >= n_cards:
            break
    return cleaned

# ───────────────────────── Adaptive per-call sizing ─────────────────────────
def choose_call_size(yield_level: float, remaining_overall: int, remaining_for_chunk: int) -> int:
    # Base per-call size from yield (0→12, 1→6)
    base = int(round(12 - 6 * max(0.0, min(1.0, yield_level))))
    base = max(3, min(16, base))
    if remaining_overall > 100:
        base = min(20, base + 4)
    return max(1, min(base, remaining_overall, remaining_for_chunk))

# ───────────────────────── Build .apkg (now gated by subscription) ─────────────────────────
@app.post("/build-apkg")
def build_apkg():
    """
    JSON body:
    {
      "email": "you@example.com",          # REQUIRED for subscription gate
      "deck_title": "My Deck",
      "text": "Paste your study text here...",
      "yield_level": 0.6,                  # 0..1; 0 → exhaustive coverage
      "modes": ["Basic Recall (Q/A)","Fill in the Blank","Mechanism (Why/How)","Scenario"],
      "approx_cards": 40,                  # used only for balanced mode as a floor
      "words_per_chunk": 700               # rough chunk size by words
    }
    - Uses ONLY gpt-4o-mini for AI generation.
    - Enforces monthly cap via data/adaptive_profile.json.
    - Requires active Stripe subscription for the provided email.
    """
    data = request.get_json(silent=True) or {}

    # Subscription gate
    email = (data.get("email") or "").strip().lower()
    if not email:
        return ("Email is required", 400)
    sub = get_subscription(email)
    if not is_active(sub):
        return ("Subscription inactive. Please subscribe to continue.", 402)

    deck_title   = (data.get("deck_title") or "AnkifyAI Deck").strip()
    raw_text     = data.get("text") or ""
    yield_level  = float(data.get("yield_level", 0.6))
    modes        = normalize_modes(data.get("modes") or ["Basic Recall (Q/A)"])
    approx_cards = int(data.get("approx_cards", 40))
    words_chunk  = max(300, int(data.get("words_per_chunk", 700)))

    if not OPENAI_API_KEY:
        return ("Server missing OPENAI_API_KEY environment variable.", 500)

    usage = load_usage()
    remaining_cap = max(0, usage["cap"] - usage["cards_used"])
    if remaining_cap <= 0:
        return ("Monthly card limit reached. Try again next month or increase your cap.", 402)

    # Prepare Anki models
    model_id_base = int(hashlib.sha1(deck_title.encode("utf-8")).hexdigest(), 16) % (10**8)

    basic_model = genanki.Model(
        model_id_base,
        'Basic (AnkifyAI)',
        fields=[{'name': 'Question'}, {'name': 'Answer'}],
        templates=[{
            'name': 'Card 1',
            'qfmt': '{{Question}}',
            'afmt': '{{FrontSide}}<hr id="answer">{{Answer}}'
        }],
    )

    cloze_model = genanki.Model(
        model_id_base + 1,
        'Fill in the Blank (AnkifyAI)',
        fields=[{'name': 'Text'}, {'name': 'Extra'}],
        templates=[{
            'name': 'Fill in the Blank',
            'qfmt': '{{cloze:Text}}',
            'afmt': '{{cloze:Text}}<hr id="answer">{{Extra}}'
        }],
        model_type=genanki.Model.CLOZE
    )

    deck_id = (model_id_base + 5) % (10**10)
    deck = genanki.Deck(deck_id, deck_title)

    # ───── Yield=0 → Exhaustive pipeline
    if yield_level <= 0.0 + 1e-9:
        chunks = chunk_by_words(raw_text, target_words=words_chunk, overlap=120)
        all_facts_normed = set()
        all_facts: List[str] = []

        # Generous facts per chunk: scale with chunk length (≈110 facts / 1k words)
        for ch in chunks:
            ch_words = len(clean_text(ch).split())
            max_facts = max(40, int(round((ch_words / 1000.0) * 110)))
            try:
                facts = ai_extract_facts(ch, max_facts=max_facts)
            except Exception as e:
                return (f"AI extraction error: {e}", 500)

            for fobj in facts:
                ftxt = (fobj.get("fact") or "").strip()
                if not ftxt:
                    continue
                key = normalize_fact(ftxt)
                if key not in all_facts_normed:
                    all_facts_normed.add(key)
                    all_facts.append(ftxt)

        if not all_facts:
            return ("No extractable facts found. Try smaller words_per_chunk.", 422)

        allowed_total = min(len(all_facts), remaining_cap)

        # Convert facts → cards in batches of 50
        cards_added = 0
        i = 0
        while i < allowed_total:
            batch_facts = [{"fact": f} for f in all_facts[i:i+50]]
            try:
                new_cards = ai_cards_from_facts(batch_facts, modes)
            except Exception as e:
                return (f"AI cardization error: {e}", 500)

            actually_added = 0
            for c in new_cards:
                if cards_added >= allowed_total:
                    break
                mode = c.get("mode", "Basic Recall (Q/A)")
                front = c["front"]; back = c["back"]
                if not front or not back:
                    continue
                if mode == "Fill in the Blank":
                    note = genanki.Note(model=cloze_model, fields=[front, back])
                else:
                    note = genanki.Note(model=basic_model, fields=[front, back])
                deck.add_note(note)
                cards_added += 1
                actually_added += 1

            if actually_added > 0:
                usage["cards_used"] += actually_added
                save_usage(usage)

            i += 50

        if cards_added == 0:
            return ("No cards generated from extracted facts.", 422)

    else:
        # ───── Balanced pipeline (density-driven), auto call sizing, multi-call per chunk
        dynamic_total = estimate_total_cards(raw_text, approx_cards, yield_level)
        target_cards_total = min(dynamic_total, remaining_cap)
        if target_cards_total <= 0:
            return ("No remaining card quota.", 402)

        chunks = chunk_by_words(raw_text, target_words=words_chunk, overlap=120)
        cards_added = 0

        # Per-chunk targets from density
        chunk_words = [len(clean_text(c).split()) for c in chunks]
        density = cards_per_1000_words(yield_level)
        chunk_targets = [max(1, int(round((w / 1000.0) * density))) for w in chunk_words]

        # Keep calling within each chunk until its quota is met; then optional sweep
        for idx, chunk in enumerate(chunks):
            target_for_chunk = chunk_targets[idx]
            produced_for_chunk = 0

            while produced_for_chunk < target_for_chunk and cards_added < target_cards_total:
                remaining_overall = target_cards_total - cards_added
                remaining_for_chunk = target_for_chunk - produced_for_chunk
                n_this_call = choose_call_size(yield_level, remaining_overall, remaining_for_chunk)

                try:
                    batch_cards = ai_generate_batch(chunk, n_this_call, modes, yield_level)
                except Exception as e:
                    return (f"AI generation error: {e}", 500)

                actually_added = 0
                for c in batch_cards:
                    if cards_added >= target_cards_total:
                        break
                    mode = c.get("mode", "Basic Recall (Q/A)")
                    front = c["front"]; back = c["back"]
                    if not front or not back:
                        continue
                    if mode == "Fill in the Blank":
                        note = genanki.Note(model=cloze_model, fields=[front, back])
                    else:
                        note = genanki.Note(model=basic_model, fields=[front, back])
                    deck.add_note(note)
                    cards_added += 1
                    produced_for_chunk += 1
                    actually_added += 1

                if actually_added > 0:
                    usage["cards_used"] += actually_added
                    save_usage(usage)
                else:
                    break

        # Optional round-robin sweep to use any leftover budget
        while cards_added < target_cards_total:
            made_progress = False
            for chunk in chunks:
                if cards_added >= target_cards_total:
                    break
                n_this_call = choose_call_size(yield_level, target_cards_total - cards_added, target_cards_total - cards_added)
                try:
                    batch_cards = ai_generate_batch(chunk, n_this_call, modes, yield_level)
                except Exception as e:
                    return (f"AI generation error: {e}", 500)

                actually_added = 0
                for c in batch_cards:
                    if cards_added >= target_cards_total:
                        break
                    mode = c.get("mode", "Basic Recall (Q/A)")
                    front = c["front"]; back = c["back"]
                    if not front or not back:
                        continue
                    if mode == "Fill in the Blank":
                        note = genanki.Note(model=cloze_model, fields=[front, back])
                    else:
                        note = genanki.Note(model=basic_model, fields=[front, back])
                    deck.add_note(note)
                    cards_added += 1
                    actually_added += 1

                if actually_added > 0:
                    usage["cards_used"] += actually_added
                    save_usage(usage)
                    made_progress = True
            if not made_progress:
                break

        if cards_added == 0:
            return ("No cards generated. Try lowering yield or providing more text.", 422)

    # Build and return .apkg
    fd, temp_path = tempfile.mkstemp(suffix=".apkg")
    os.close(fd)
    genanki.Package(deck).write_to_file(temp_path)
    filename = f"{deck_title.replace(' ', '_')}.apkg"
    return send_file(temp_path, as_attachment=True, download_name=filename)

# ───────────────────────── Health ─────────────────────────
@app.get("/healthz")
def healthz():
    return {"ok": True}

# ───────────────────────── Main ─────────────────────────
if __name__ == "__main__":
    # Ensure folders exist
    os.makedirs(DATA_DIR, exist_ok=True)
    print("AnkifyAI starting…")
    print("Open UI:   http://localhost:8020/")
    print("Health:    http://localhost:8020/healthz")
    print("Usage:     http://localhost:8020/usage")
    app.run(host="0.0.0.0", port=8020, debug=True, use_reloader=False)