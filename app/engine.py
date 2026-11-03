"""Strategy layer: concession curves, accept/counter/walk decisions, NegMAS forecast."""
EXPONENTS = {"boulware": 4.0, "linear": 1.0, "conceder": 0.25}


def planned_price(target: float, ceiling: float, rnd: int, max_rounds: int, strategy: str) -> float:
    t = min(1.0, rnd / max(1, max_rounds - 1))
    return target + (ceiling - target) * t ** EXPONENTS[strategy]


def opening_anchor(quoted: float, target: float) -> float:
    gap = quoted - target if quoted > target else 0
    a = target - gap * 0.2 if gap > 0 else target * 0.94
    a = min(max(a, target * 0.80), target * 0.99)
    if quoted > 0:
        a = min(a, quoted * 0.97)
    return round(a, 2)


def decide(brief: dict, supplier_offer: float | None, rnd: int, last_bot: float | None) -> dict:
    target, ceiling = brief["target"], brief["ceiling"]
    mr, strat = brief["max_rounds"], brief["strategy"]
    if supplier_offer is None:
        return {"action": "ask", "price": None}
    planned = planned_price(target, ceiling, rnd, mr, strat)
    if rnd == 0:
        planned = min(planned, opening_anchor(brief.get("quoted") or supplier_offer, target))
    if supplier_offer <= max(planned, target):
        return {"action": "accept", "price": supplier_offer}
    if rnd >= mr - 1 and supplier_offer > ceiling:
        return {"action": "walk_away", "price": None}
    price = max(planned, last_bot or 0)
    price = min(price, supplier_offer * 0.995, ceiling)
    return {"action": "counter", "price": round(price, 2)}


def curve(target: float, ceiling: float, rounds: int, strategy: str) -> list[float]:
    return [round(planned_price(target, ceiling, i, rounds, strategy), 2) for i in range(rounds)]


def zopa(quoted: float, target: float, ceiling: float) -> dict:
    inside = min(quoted, ceiling)
    return {
        "zopa_exists": quoted <= ceiling,
        "gap_to_ceiling": round(quoted - ceiling, 2),
        "suggested_counter": round(target + 0.25 * (inside - target), 2),
        "max_savings_pct": round((quoted - target) / quoted * 100, 2),
    }


def simulate(quoted, target, ceiling, buyer_strategy, supplier_strategy, supplier_floor, rounds) -> dict:
    from negmas import AspirationNegotiator, MappingUtilityFunction, SAOMechanism, make_issue

    floor = supplier_floor if supplier_floor else target
    lo, hi = min(floor, target), max(quoted, ceiling)
    if lo >= hi:
        return {"agreed": False, "price": None, "rounds_used": 0, "savings_vs_quote_pct": None}
    issues = [make_issue((lo, hi), name="price")]
    span = hi - lo
    b = MappingUtilityFunction(lambda o: max(0.0, (hi - o[0]) / span), issues=issues,
                               reserved_value=max(0.0, (hi - ceiling) / span))
    s = MappingUtilityFunction(lambda o: max(0.0, (o[0] - lo) / span), issues=issues,
                               reserved_value=max(0.0, (floor - lo) / span))
    m = SAOMechanism(issues=issues, n_steps=rounds)
    m.add(AspirationNegotiator(aspiration_type=buyer_strategy, name="buyer"), ufun=b)
    m.add(AspirationNegotiator(aspiration_type=supplier_strategy, name="supplier"), ufun=s)
    st = m.run()
    ag = st.agreement[0] if st.agreement else None
    return {
        "agreed": ag is not None,
        "price": None if ag is None else round(float(ag), 2),
        "savings_vs_quote_pct": None if ag is None else round((quoted - ag) / quoted * 100, 2),
        "rounds_used": st.step,
    }
