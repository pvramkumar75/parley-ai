"""Parley - AI procurement negotiation desk. FastAPI backend + static front-end."""
import hmac
import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from . import ai, engine

STATIC = Path(__file__).parent / "static"
DB_PATH = Path(os.getenv("DATA_DIR", "data")) / "parley.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_lock = threading.Lock()
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.execute("CREATE TABLE IF NOT EXISTS neg (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
_db.commit()

app = FastAPI(title="Parley")
Strategy = Literal["boulware", "linear", "conceder"]


def load(nid: str) -> dict:
    with _lock:
        row = _db.execute("SELECT data FROM neg WHERE id=?", (nid,)).fetchone()
    if not row:
        raise HTTPException(404, "Negotiation not found")
    return json.loads(row[0])


def save(n: dict) -> None:
    with _lock:
        _db.execute("INSERT OR REPLACE INTO neg (id, data) VALUES (?, ?)", (n["id"], json.dumps(n)))
        _db.commit()


def check_token(n: dict, token: str) -> None:
    if not token or not hmac.compare_digest(n["buyer_token"], token):
        raise HTTPException(403, "Invalid buyer token")


class Brief(BaseModel):
    item: str = Field(min_length=1, max_length=120)
    category: str = "other"
    quantity: float = Field(1, gt=0)
    unit: str = "kg"
    needed_by: str = ""
    company: str = Field("", max_length=120)
    supplier_name: str = Field(min_length=1, max_length=120)
    contact: str = ""
    phone: str = ""
    email: str = ""
    currency: Literal["INR", "USD", "CNY", "EUR"] = "INR"
    quoted: float = Field(0, ge=0)
    target: float = Field(gt=0)
    ceiling: float = Field(gt=0)
    terms: list[str] = []
    tone: Literal["warm", "firm", "aggressive"] = "firm"
    tactics: list[str] = []
    instructions: str = Field("", max_length=1000)
    market_notes: str = Field("", max_length=2000)
    engine: Literal["ai", "rules"] = "ai"
    strategy: Strategy = "boulware"
    pacing: Literal["natural", "instant"] = "natural"
    hours: int = Field(48, ge=1, le=720)
    max_rounds: int = Field(10, ge=2, le=40)

    @model_validator(mode="after")
    def _order(self):
        if self.target > self.ceiling:
            raise ValueError("target must not exceed the walk-away ceiling")
        return self


PUBLIC_FIELDS = ["item", "category", "quantity", "unit", "needed_by", "supplier_name", "contact",
                 "currency", "terms"]


def view(n: dict, role: str) -> dict:
    now = time.time()
    msgs = [m for m in n["messages"] if m["visible_at"] <= now]
    pending = any(m["visible_at"] > now for m in n["messages"])
    status = n["status"]
    if status == "open" and now > n["expires_at"]:
        status = "expired"
    base = {"id": n["id"], "status": status, "expires_at": n["expires_at"], "pending": pending,
            "outcome": n.get("outcome"), "ai_provider": ai.provider() if n["brief"]["engine"] == "ai" else None}
    if role == "buyer":
        msgs_out = msgs
        base["brief"] = n["brief"]
        base["round"] = n["round"]
    else:
        msgs_out = [m for m in msgs if m["role"] in ("bot", "sup", "buyer")]
        base["brief"] = {k: n["brief"][k] for k in PUBLIC_FIELDS}
    base["messages"] = [{k: m[k] for k in ("id", "role", "text", "price", "ts")} for m in msgs_out]
    return base


def add_msg(n: dict, role: str, text: str, price=None, delay: float = 0.0) -> dict:
    ts = time.time()
    m = {"id": secrets.token_hex(4), "role": role, "text": text, "price": price,
         "ts": ts, "visible_at": ts + delay}
    n["messages"].append(m)
    return m


def extract_price(text: str, brief: dict, last: Optional[float]) -> Optional[float]:
    ref = last or brief["quoted"] or brief["target"]
    # "only 1% discount" / "5 percent off": a cut from their last rate, not a price
    pct = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent|per cent)", text, re.I)
    lone = len(re.findall(r"\d[\d,]*\.?\d*", text)) == 1    # "sorry 1% only"
    if pct and last and (lone or re.search(r"discount|off|less|reduc|lower|cut|down", text, re.I)):
        p = float(pct.group(1))
        if 0 < p < 60:
            return round(last * (1 - p / 100), 2)
    best = None
    for raw in re.findall(r"\d[\d,]*\.?\d*", text):
        try:
            v = float(raw.replace(",", "").rstrip("."))
        except ValueError:
            continue
        if ref * 0.3 <= v <= ref * 3 and (best is None or abs(v - ref) < abs(best - ref)):
            best = v
    return best


def opening_message(b: dict) -> str:
    """Introduction sent as soon as the supplier opens the link.

    States the bot's opening offer (below the real target), never the target or ceiling.
    """
    who = b["contact"].strip() or b["supplier_name"]
    me = b["company"].strip() or "the buyer"
    c = b["currency"]
    intro = (f"Hello {who}, I am an AI assistant negotiating on behalf of {me}. I am trained to negotiate "
             "with you logically, considering market trends and cost modelling. ")
    if b["quoted"]:
        offer = engine.opening_anchor(b["quoted"], b["target"])
        body = (f"Thank you for your quote of {c} {b['quoted']:g}. However, we find it on the higher side. "
                f"Our proposed price is {c} {offer:g} per unit")
    else:
        body = "Please share your best rate per unit so we can move ahead"
    if b["terms"]:
        body += ", and the terms we are looking for are: " + ", ".join(b["terms"])
    return intro + body + ". Can we work towards this?"


# ---------- API ----------
@app.get("/api/health")
def health():
    return {"ok": True, "ai_provider": ai.provider()}


@app.post("/api/negotiations")
def create(b: Brief):
    nid, token = secrets.token_urlsafe(6), secrets.token_urlsafe(16)
    n = {"id": nid, "buyer_token": token, "brief": b.model_dump(), "messages": [], "round": 0,
         "status": "open", "created_at": time.time(), "expires_at": time.time() + b.hours * 3600,
         "last_bot": None, "last_sup": b.quoted or None, "outcome": None}
    add_msg(n, "system", f"Negotiation with {b.supplier_name} opened for {b.item}.")
    add_msg(n, "bot", opening_message(n["brief"]), engine.opening_anchor(b.quoted, b.target) if b.quoted else None)
    if b.quoted:
        n["last_bot"] = engine.opening_anchor(b.quoted, b.target)
    save(n)
    return {"id": nid, "buyer_token": token, "link": f"/n/{nid}"}


@app.get("/api/negotiations/{nid}")
def get(nid: str, role: str = "supplier", token: str = ""):
    n = load(nid)
    if role == "buyer":
        check_token(n, token)
    return view(n, "buyer" if role == "buyer" else "supplier")


class Batch(BaseModel):
    items: list[dict]


@app.post("/api/batch")
def batch(b: Batch):
    out = []
    for it in b.items[:100]:
        try:
            n = load(it["id"])
            check_token(n, it.get("token", ""))
            v = view(n, "buyer")
            out.append({k: v[k] for k in ("id", "status", "expires_at", "outcome", "round")}
                       | {"item": n["brief"]["item"], "supplier": n["brief"]["supplier_name"],
                          "currency": n["brief"]["currency"], "last_sup": n["last_sup"],
                          "last_bot": n["last_bot"], "target": n["brief"]["target"]})
        except (HTTPException, KeyError):
            continue
    return {"items": out}


class MsgReq(BaseModel):
    kind: Literal["sup", "sup_accept", "buyer_manual", "buyer_whisper"]
    text: str = Field("", max_length=1500)
    price: Optional[float] = Field(None, gt=0)
    token: str = ""


@app.post("/api/negotiations/{nid}/message")
def message(nid: str, r: MsgReq):
    n = load(nid)
    if n["status"] != "open" or time.time() > n["expires_at"]:
        raise HTTPException(410, "This negotiation is closed")
    brief = n["brief"]
    if r.kind.startswith("buyer"):
        check_token(n, r.token)
        if not r.text.strip():
            raise HTTPException(422, "Message is empty")
        add_msg(n, "whisper" if r.kind == "buyer_whisper" else "buyer", r.text.strip(), r.price)
        save(n)
        return {"ok": True}

    # supplier side
    if r.kind == "sup_accept":
        if n["last_bot"] is None:
            raise HTTPException(409, "No offer from the buyer to accept yet")
        add_msg(n, "sup", f"I accept {brief['currency']} {n['last_bot']:g}.", n["last_bot"])
        n["status"], n["outcome"] = "concluded", {"agreed_price": n["last_bot"], "by": "supplier_accept",
                                                  "concluded_at": time.time()}
        add_msg(n, "system", "Supplier accepted the current offer. Awaiting buyer to finalise terms.")
        save(n)
        return {"ok": True}

    text = r.text.strip()
    if not text and r.price is None:
        raise HTTPException(422, "Write a message or quote a rate")
    prev_sup = n["last_sup"]
    price = r.price or extract_price(text, brief, prev_sup)
    text = text or f"My rate is {brief['currency']} {price:g}."
    add_msg(n, "sup", text, price)
    if price is not None:
        n["last_sup"] = price
    d = engine.decide(brief, price, n["round"], n["last_bot"])
    history = [m for m in n["messages"] if m["role"] in ("bot", "sup", "buyer")]
    whispers = [m["text"] for m in n["messages"] if m["role"] == "whisper"]
    reply, source = ai.write_reply(brief, d, price, history, whispers,
                                   {"round": n["round"], "prev_sup": prev_sup, "last_bot": n["last_bot"]})
    delay = random.uniform(3, 10) if brief["pacing"] == "natural" else 0.0
    add_msg(n, "bot", reply, d["price"], delay)
    if price is not None:
        n["round"] += 1
    if d["action"] == "counter":
        n["last_bot"] = d["price"]
    elif d["action"] == "accept":
        n["last_bot"] = d["price"]
        n["outcome"] = {"agreed_price": d["price"], "by": "bot_accept"}
    n["last_action"], n["last_source"] = d["action"], source
    save(n)
    return {"ok": True, "action": d["action"]}


class ExtendReq(BaseModel):
    hours: int = Field(24, ge=1, le=720)
    token: str


@app.post("/api/negotiations/{nid}/extend")
def extend(nid: str, r: ExtendReq):
    n = load(nid)
    check_token(n, r.token)
    n["expires_at"] = max(time.time(), n["expires_at"]) + r.hours * 3600
    if n["status"] == "expired":
        n["status"] = "open"
    save(n)
    return {"expires_at": n["expires_at"]}


class ConcludeReq(BaseModel):
    token: str
    agreed_price: float = Field(gt=0)
    price_validity: str = ""
    delivery: str = ""
    payment_terms: str = ""
    packing: str = ""
    freight: str = ""
    quality: str = ""


@app.post("/api/negotiations/{nid}/conclude")
def conclude(nid: str, r: ConcludeReq):
    n = load(nid)
    check_token(n, r.token)
    o = r.model_dump(exclude={"token"})
    o["concluded_at"] = time.time()
    q = n["brief"]["quoted"]
    o["savings_pct"] = round((q - r.agreed_price) / q * 100, 2) if q else None
    n["outcome"], n["status"] = o, "concluded"
    add_msg(n, "system", f"Negotiation concluded at {n['brief']['currency']} {r.agreed_price:g}.")
    save(n)
    return {"outcome": o}


class ForecastReq(BaseModel):
    quoted: float = Field(gt=0)
    target: float = Field(gt=0)
    ceiling: float = Field(gt=0)
    buyer_strategy: Strategy = "boulware"
    supplier_strategy: Strategy = "linear"
    supplier_floor: Optional[float] = Field(None, gt=0)
    rounds: int = Field(10, ge=2, le=40)

    @model_validator(mode="after")
    def _order(self):
        if self.target > self.ceiling:
            raise ValueError("target must not exceed ceiling")
        return self


@app.post("/api/forecast")
def forecast(r: ForecastReq):
    return {
        "zopa": engine.zopa(r.quoted, r.target, r.ceiling),
        "curves": {s: engine.curve(r.target, r.ceiling, r.rounds, s) for s in engine.EXPONENTS},
        "anchor": engine.opening_anchor(r.quoted, r.target),
        "simulation": engine.simulate(r.quoted, r.target, r.ceiling, r.buyer_strategy,
                                      r.supplier_strategy, r.supplier_floor, max(r.rounds * 3, 20)),
    }


# ---------- pages ----------
@app.get("/n/{nid}")
def supplier_room(nid: str):
    return FileResponse(STATIC / "room.html")


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
