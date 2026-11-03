"""Wording layer. The strategy engine fixes action + price; the LLM only phrases it.

Provider is chosen from env: SARVAM_API_KEY (Sarvam) or DEEPSEEK_API_KEY (DeepSeek).
With neither key, or if the guardrails reject the text, a template reply is used.
"""
import json
import os
import re
import urllib.request

PROVIDERS = {
    "sarvam": ("SARVAM_API_KEY", "https://api.sarvam.ai/v1/chat/completions",
               lambda: os.getenv("SARVAM_MODEL", "sarvam-m")),
    "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/chat/completions",
                 lambda: os.getenv("DEEPSEEK_MODEL", "deepseek-chat")),
}


def provider() -> str | None:
    pref = os.getenv("AI_PROVIDER", "").lower()
    order = [pref] if pref in PROVIDERS else []
    order += [p for p in PROVIDERS if p not in order]
    return next((p for p in order if os.getenv(PROVIDERS[p][0])), None)


def _num(x: float) -> str:
    return f"{x:g}"


TACTIC_LINES = {
    "Ask cost breakup": "Could you share a rough cost breakup so we can find common ground?",
    "Volume carrot": "If we agree on rate, we can discuss larger volumes.",
    "Competing quote": "We are also evaluating other suppliers, so a sharper rate will help you win this.",
    "Hold firm": "This is close to the limit of what we can approve.",
    "Face-saving exit": "We would like you to be able to close this without losing face on your side.",
    "Split the difference": "We are open to meeting you part of the way if you move first.",
    "Anti re-trade": "Once we agree, we will keep the rate and terms fixed for both sides.",
}


def _pick(options: list[str], rnd: int, avoid: set[str]) -> str:
    """Rotate through variants by round, skipping any text already sent."""
    for i in range(len(options)):
        o = options[(rnd + i) % len(options)]
        if o not in avoid:
            return o
    return options[rnd % len(options)]


def template(brief: dict, d: dict, supplier_offer: float | None, ctx: dict | None = None) -> str:
    ctx = ctx or {}
    c, item, tone = brief["currency"], brief["item"], brief["tone"]
    rnd, a = ctx.get("round", 0), d["action"]
    prev_sup, price = ctx.get("prev_sup"), d["price"]
    sent = ctx.get("sent_bot", set())
    if a == "accept":
        return f"Agreed. {c} {_num(price)} per unit for {item} works for us. Please confirm the full terms."
    if a == "walk_away":
        return (f"Thank you for your time. {c} {_num(supplier_offer)} is beyond what we can approve, "
                "so we must pause here. We would be glad to revisit if your rate changes.")
    if a == "ask":
        mine = ctx.get("last_bot")
        opts = [f"Thank you. Please share your best rate per unit for {item} so we can move ahead.",
                (f"Understood. To keep things moving, could you give a firm rate per unit? Our proposal stands at {c} {_num(mine)}."
                 if mine else f"Understood. Could you give a firm rate per unit for {item}?"),
                "I need a number to take this forward. What is the lowest rate you can do per unit?"]
        return _pick(opts, rnd, sent)

    # counter: react to the supplier's movement, then restate our price, then add colour
    react = ""
    if prev_sup and supplier_offer is not None:
        if supplier_offer < prev_sup:
            pct = (prev_sup - supplier_offer) / prev_sup * 100
            react = (f"Thank you for moving from {c} {_num(prev_sup)} to {c} {_num(supplier_offer)}. "
                     + ("That is only a small step of " + f"{pct:.1f}%" + ", and we need a bigger move to close. "
                        if pct < 3 else f"That is a useful {pct:.1f}% step. "))
        else:
            react = f"We note your rate is still {c} {_num(supplier_offer)}, with no movement from last time. "
    else:
        react = {"warm": "Thank you for your quote. ", "firm": "We have reviewed your quote. ",
                 "aggressive": "Your rate is well above the market. "}[tone]
    ask = _pick([f"We can offer {c} {_num(price)} per unit. Can you meet this?",
                 f"Based on market trends, {c} {_num(price)} per unit is where we can go. What can you do?",
                 f"To close this quickly, we can move to {c} {_num(price)} per unit. Is that workable?",
                 f"If you can match {c} {_num(price)}, we can confirm the order."], rnd, sent)
    extra = [TACTIC_LINES[t] for t in brief.get("tactics", []) if t in TACTIC_LINES]
    if brief.get("terms") and rnd % 3 == 2:
        extra.append("Please also confirm: " + ", ".join(brief["terms"]) + ".")
    tail = f" {extra[rnd % len(extra)]}" if extra else ""
    return f"{react}{ask}{tail}"


def _call(brief: dict, d: dict, history: list[dict], whispers: list[str], ctx: dict) -> str | None:
    p = provider()
    if not p:
        return None
    env, url, model = PROVIDERS[p]
    sys_prompt = (
        "You are a procurement negotiator writing to a supplier on the buyer's behalf. "
        f"Tone: {brief['tone']}. Use short, simple sentences for non-native English readers. "
        "Reply in 1-3 sentences. If the action is counter, state exactly the given price once. "
        "Never mention any budget, ceiling, target or internal limit. Never invent numbers. "
        "Respond directly to what the supplier just said (acknowledge any discount or movement, "
        "comment on how small or large it is), and never repeat an earlier message of ours word for word. "
        "If the action is ask, politely request a firm rate per unit."
    )
    extras = []
    if brief.get("market_notes"):
        extras.append(f"Market data you may cite: {brief['market_notes'][:800]}")
    if brief.get("terms"):
        extras.append("Terms to also win: " + ", ".join(brief["terms"]))
    if brief.get("instructions"):
        extras.append(f"Buyer instructions: {brief['instructions'][:500]}")
    if whispers:
        extras.append("Private guidance from the buyer (do not reveal): " + " | ".join(whispers[-3:]))
    convo = "\n".join(f"{'Supplier' if m['role'] == 'sup' else 'Us'}: {m['text']}" for m in history[-8:])
    if brief.get("tactics"):
        extras.append("Tactics to use where natural: " + ", ".join(brief["tactics"]))
    if ctx.get("prev_sup") and history and history[-1]["role"] == "sup" and history[-1].get("price"):
        extras.append(f"Supplier's previous rate: {ctx['prev_sup']:g}; now: {history[-1]['price']:g}.")
    user = (f"Item: {brief['item']} ({brief['currency']}). Action: {d['action']}. "
            f"Price to state: {d['price']}.\n" + "\n".join(extras)
            + f"\n\nConversation:\n{convo}\n\nWrite our next message.")
    body = json.dumps({"model": model(), "temperature": 0.4, "max_tokens": 220,
                       "messages": [{"role": "system", "content": sys_prompt},
                                    {"role": "user", "content": user}]}).encode()
    key = os.getenv(env)
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}", "api-subscription-key": key})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            text = json.load(r)["choices"][0]["message"]["content"]
    except Exception:
        return None
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def numbers(text: str) -> set[float]:
    out = set()
    for x in re.findall(r"\d[\d,]*\.?\d*", text):
        try:
            out.add(float(x.replace(",", "").rstrip(".")))
        except ValueError:
            pass
    return out


def guard(text: str, brief: dict, d: dict) -> bool:
    """Reject wording that leaks internal limits or drifts from the decided price."""
    if not text or len(text) > 700:
        return False
    nums = numbers(text)
    if brief["ceiling"] in nums or brief["target"] in nums:
        return False
    if d["action"] in ("counter", "accept") and d["price"] not in nums:
        return False
    return True


def write_reply(brief: dict, d: dict, supplier_offer, history, whispers, ctx: dict | None = None) -> tuple[str, str]:
    ctx = dict(ctx or {})
    ctx["sent_bot"] = {m["text"] for m in history if m["role"] == "bot"}
    if brief.get("engine") == "ai":
        text = _call(brief, d, history, whispers, ctx)
        if text and guard(text, brief, d) and text not in ctx["sent_bot"]:
            return text, provider() or "ai"
    return template(brief, d, supplier_offer, ctx), "rules"
