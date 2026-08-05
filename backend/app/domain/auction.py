"""Uniform clearing-price auction settlement with a full audit trail."""
from __future__ import annotations
import hashlib
from dataclasses import dataclass, field

from .rules import RULE_VERSION


def _tiebreak(seed, bidder_id) -> int:
    h = hashlib.blake2b(f"{seed}|{bidder_id}".encode(), digest_size=8).digest()
    return 100000 + int.from_bytes(h, "big") % 900000


def settle(seats: int, bids: list[dict], cap: float | None = None, seed="default") -> dict:
    """Settle one course.

    Ranking: bid descending, then tie-break ascending (the official rule).
    Clearing price: lowest winning bid; zero if any seat goes unfilled.
    """
    if seats is None or seats < 0:
        raise ValueError(f"seats must be >= 0, got {seats}")
    cap = float("inf") if cap is None else float(cap)

    rejected, eligible = [], []
    for i, b in enumerate(bids or []):
        bid_id = b.get("id", f"bidder{i}")
        v = b.get("bid")
        if b.get("eligible") is False:
            rejected.append({"id": bid_id, "bid": v, "reason": b.get("reason", "marked ineligible")}); continue
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v != v or v in (float("inf"), float("-inf")):
            rejected.append({"id": bid_id, "bid": v, "reason": "bid is not a finite number"}); continue
        if v < 0:
            rejected.append({"id": bid_id, "bid": v, "reason": "negative bid"}); continue
        if float(v) != int(v):
            rejected.append({"id": bid_id, "bid": v,
                             "reason": "bid must be a whole number (AUC.INTEGER_BIDS)"}); continue
        if v > cap:
            rejected.append({"id": bid_id, "bid": v, "reason": f"bid exceeds cap of {cap:g} (AUC.MAX_BID)"}); continue
        tb = b.get("tie_break")
        eligible.append({"id": bid_id, "bid": int(v),
                         "tie_break": int(tb) if tb is not None else _tiebreak(seed, bid_id)})

    ranked = sorted(eligible, key=lambda x: (-x["bid"], x["tie_break"], str(x["id"])))
    n_win = min(seats, len(ranked))
    winners, losers = ranked[:n_win], ranked[n_win:]
    unfilled = seats - n_win

    if not winners:
        clearing = 0
    elif unfilled > 0:
        clearing = 0
    else:
        clearing = winners[-1]["bid"]

    results = []
    tot_charged = tot_refunded = tot_bid = 0
    for w in winners:
        charged = min(clearing, w["bid"])
        refund = w["bid"] - charged
        tot_charged += charged; tot_refunded += refund; tot_bid += w["bid"]
        results.append({"id": w["id"], "bid": w["bid"], "tie_break": w["tie_break"],
                        "won": True, "charged": charged, "refunded": refund})
    for l in losers:
        tot_refunded += l["bid"]; tot_bid += l["bid"]
        results.append({"id": l["id"], "bid": l["bid"], "tie_break": l["tie_break"],
                        "won": False, "charged": 0, "refunded": l["bid"]})

    return {
        "seats": seats, "seats_unfilled": unfilled, "clearing_price": clearing,
        "winners": [w["id"] for w in winners], "losers": [l["id"] for l in losers],
        "rejected": rejected,
        "ranked": [{"id": r["id"], "bid": r["bid"], "tie_break": r["tie_break"]} for r in ranked],
        "results": results,
        "totals": {"bid": tot_bid, "charged": tot_charged, "refunded": tot_refunded},
        "conservation_ok": abs((tot_charged + tot_refunded) - tot_bid) < 1e-9,
        "rule_version": RULE_VERSION, "seed": str(seed),
        "rules_applied": ["AUC.CLEARING_PRICE", "AUC.TIEBREAK", "AUC.REFUND",
                          "AUC.MAX_BID", "AUC.INTEGER_BIDS"],
    }
