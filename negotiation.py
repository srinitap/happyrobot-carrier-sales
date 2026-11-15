MAX_ROUNDS = 3
FINAL_BELOW_MAX = 20     
REJECTED = "rejected"    
ACCEPTED = "accepted"    

def new_state(board_rate, max_rate):
    negotiating = board_rate < max_rate
    return {
        "board_rate": board_rate,
        "max_rate": max_rate,
        "mode": "negotiate" if negotiating else "fixed",
        "last_offer": board_rate if negotiating else max_rate,
        "round": 0,
        "status": "open",      
        "agreed_rate": None,
        "history": [],         
    }


def round_offer(state, rnd):
    start, max_rate = state["board_rate"], state["max_rate"]
    gap = max(max_rate - start, 0)
    last = state["last_offer"]
    if rnd >= MAX_ROUNDS:
        return min(max(max_rate - FINAL_BELOW_MAX, last + 1), max_rate)
    return int(start + gap * rnd / MAX_ROUNDS)   


def decide(state, carrier_ask=None):
    if state["status"] == "agreed":
        return "accept", state["agreed_rate"]
    if state["status"] == "failed":
        return "end", None

    last = state["last_offer"]

    if carrier_ask == ACCEPTED:
        return _agree(state, last)

    rejected = carrier_ask == REJECTED

    if carrier_ask is None:
        if state["round"] == 0:
            result = "offer"
        elif state["mode"] == "fixed":
            result = "hold"
        else:
            result = "final_offer" if state["round"] >= MAX_ROUNDS else "counter"
        return result, last

    if not rejected:
        # A wildly low number was probably misheard
        if carrier_ask < state["board_rate"] * 0.5:
            return "confirm_rate", None
        # They asked for our current offer or less then take it
        if carrier_ask <= last:
            return _agree(state, carrier_ask)

    if state["mode"] == "fixed":
        return _fixed_price(state, carrier_ask)

    # After the final offer, accept anything up to the max, otherwise no deal
    if state["round"] >= MAX_ROUNDS:
        if not rejected and carrier_ask <= state["max_rate"]:
            return _agree(state, carrier_ask)
        return _fail(state, carrier_ask)

    state["round"] += 1
    rnd = state["round"]
    offer = round_offer(state, rnd)
    if not rejected and carrier_ask <= offer:
        return _agree(state, carrier_ask)   

    state["last_offer"] = offer
    result = "final_offer" if rnd >= MAX_ROUNDS else "counter"
    state["history"].append((rnd, carrier_ask, result, offer))
    return result, offer


def _fixed_price(state, carrier_ask):
    # Board rate is at/above the max, offer only the max
    # Opening -> hold -> final_offer -> end
    state["round"] += 1
    if state["round"] == 1:
        result = "hold"
    elif state["round"] == 2:
        result = "final_offer"
    else:
        return _fail(state, carrier_ask)
    state["history"].append((state["round"], carrier_ask, result, state["last_offer"]))
    return result, state["last_offer"]


def _agree(state, rate):
    rate = int(rate)
    state["status"] = "agreed"
    state["agreed_rate"] = rate
    state["history"].append((state["round"], rate, "accept", rate))
    return "accept", rate


def _fail(state, carrier_ask):
    state["status"] = "failed"
    state["history"].append((state["round"], carrier_ask, "end", None))
    return "end", None