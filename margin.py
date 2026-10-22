"""Margin calculation.

    margin = (open_price * lots * contract_size / instrument_leverage)
             * ( |profit| / |(open_price - close_price) * lots * contract_size| )

The first factor is the notional exposure divided by the instrument leverage,
expressed in the instrument's quote currency. The second factor is the implied
quote-currency -> account-currency rate, derived from the trade's own realised
profit. Both factors need a non-zero denominator, so the function returns a
reason string instead of a number when the trade cannot support the derivation.
"""


def compute_margin(open_price, close_price, lots, contract_size, leverage, profit):
    """(margin, reason). margin is None when it cannot be derived; reason says why."""
    missing = [name for name, value in (
        ("open price", open_price),
        ("close price", close_price),
        ("lots", lots),
        ("contract size", contract_size),
        ("instrument leverage", leverage),
        ("profit", profit),
    ) if value is None]
    if missing:
        return None, "missing " + ", ".join(missing)

    if leverage == 0:
        return None, "instrument leverage is 0"
    if lots == 0:
        return None, "lots is 0"
    if contract_size == 0:
        return None, "contract size is 0"

    price_move_value = (open_price - close_price) * lots * contract_size
    if price_move_value == 0:
        return None, "open price equals close price (no price move to scale by)"
    if profit == 0:
        return None, "profit is 0 (cannot derive currency conversion)"

    notional = open_price * lots * contract_size
    conversion = abs(profit) / abs(price_move_value)
    return (notional / leverage) * conversion, ""
