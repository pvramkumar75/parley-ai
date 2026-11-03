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


def template(brief: dict, d: dict, supplier_offer: float | None) -> str:
    c, item, tone = brief["currency"], brief["item"], brief["tone"]
    a = d["action"]
    if a == "accept":
        return f"Agreed. {c} {_num(d['price'])} per unit for {item} works for us. Please confirm the full terms."
    if a == "walk_away":
        return (f"Thank you for your time. {c} {_num(supplier_offer)} is beyond what we can approve, "
                "so we must pause here. We would be glad to revisit if your rate changes.")
    if a == "ask":
        return f"Thank you. Please share your best rate per unit for {item} so we can move ahead."
    opener = {"warm": "Thank you for your quote.", "firm": "We have reviewed your quote.",
              "aggressive": "Your rate is well above the market."}[tone]
    return f"{opener} For {item}, we can offer {c} {_num(d['price'])} per unit. Can you meet this?"


def _call(brief: dict, d: dict, history: list[dict], whispers: list[str]) -> str | None:
    p = provider()
    if not p:
        return None
    env, url, model = PROVIDERS[p]
    sys_prompt = (
        "You are a procurement negotiator writing to a supplier on the buyer's behalf. "
        f"Tone: {brief['tone']}. Use short, simple sentences for non-native English readers. "
        "Reply in 1-3 sentences. If the action is counter, state exactly the given price once. "
        "Never mention any budget, ceiling, target or internal limit. Never invent numbers."
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


def write_reply(brief: dict, d: dict, supplier_offer, history, whispers) -> tuple[str, str]:
    if brief.get("engine") == "ai":
        text = _call(brief, d, history, whispers)
        if text and guard(text, brief, d):
            return text, provider() or "ai"
    return template(brief, d, supplier_offer), "rules"
