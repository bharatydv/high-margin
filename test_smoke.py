"""Smoke tests for the pure logic — run: python test_smoke.py"""

import pandas as pd
import fields
from fpfx_api import _unwrap_object, _unwrap_records
from leverage_sheet import (Entry, _parse_worksheet, lookup_leverage,
                            parse_leverage, resolve_plan)
from margin import compute_margin
from overlap import (add_overlap_columns, closest_gap, format_gap,
                     overlaps, peak_concurrent)


def check(label, got, want):
    ok = got == want or (isinstance(want, float) and isinstance(got, float)
                         and abs(got - want) < 1e-6)
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    return ok


results = []

# --- margin formula ---------------------------------------------------------
# EURUSD 1 lot, open 1.10000, close 1.10500, profit 500 USD, leverage 100.
# notional 110000 / 100 = 1100 ; conversion = 500 / |(-0.005)*1*100000| = 1.0
margin, reason = compute_margin(1.10, 1.105, 1.0, 100000.0, 100.0, 500.0)
results.append(check("EURUSD margin", round(margin, 6), 1100.0))
results.append(check("EURUSD reason", reason, ""))

# EURGBP 1 lot, open 0.85000, close 0.85500, profit 630 USD (GBP->USD ~1.26).
# notional 85000 GBP / 30 = 2833.33 GBP ; conversion = 630 / 500 = 1.26 -> 3570 USD
margin, _ = compute_margin(0.85, 0.855, 1.0, 100000.0, 30.0, 630.0)
results.append(check("cross-currency conversion", round(margin, 2), 3570.0))

# XAUUSD 2 lots, open 2400, close 2390, profit -2000, leverage 20, contract 100.
# notional 2400*2*100 = 480000 / 20 = 24000 ; conversion = 2000/|(10)*2*100| = 1.0
margin, _ = compute_margin(2400.0, 2390.0, 2.0, 100.0, 20.0, -2000.0)
results.append(check("XAUUSD margin", round(margin, 6), 24000.0))

# a losing trade must give the same positive margin as its winning mirror
win, _ = compute_margin(1.10, 1.105, 1.0, 100000.0, 100.0, 500.0)
loss, _ = compute_margin(1.10, 1.095, 1.0, 100000.0, 100.0, -500.0)
results.append(check("sign symmetry", round(win, 6), round(loss, 6)))

# guards
results.append(check("breakeven price", compute_margin(1.1, 1.1, 1, 100000, 100, 5)[0], None))
results.append(check("zero profit", compute_margin(1.1, 1.2, 1, 100000, 100, 0)[0], None))
results.append(check("no leverage", compute_margin(1.1, 1.2, 1, 100000, None, 5)[0], None))
results.append(check("zero lots", compute_margin(1.1, 1.2, 0, 100000, 100, 5)[0], None))

# --- field lookup -----------------------------------------------------------
trade = {"openPrice": "1.10500", "close_price": 1.1, "Volume": "2.50",
         "netProfit": "-1,250.00", "account": {"plan": "Pro 100K"}}
results.append(check("openPrice", fields.to_float(fields.pick(trade, fields.OPEN_PRICE)), 1.105))
results.append(check("volume->lots", fields.to_float(fields.pick(trade, fields.LOTS)), 2.5))
results.append(check("comma profit", fields.to_float(fields.pick(trade, fields.PROFIT)), -1250.0))
results.append(check("nested plan", fields.pick(trade, fields.PLAN), "Pro 100K"))
results.append(check("parenthesised negative", fields.to_float("(300.50)"), -300.5))
results.append(check("blank", fields.to_float(""), None))

# --- payload unwrapping -----------------------------------------------------
results.append(check("bare list", len(_unwrap_records([{"a": 1}, {"a": 2}])), 2))
results.append(check("data wrapper", len(_unwrap_records({"data": [{"a": 1}]})), 1))
results.append(check("trades wrapper", len(_unwrap_records({"trades": [{"a": 1}, {"a": 2}]})), 2))
results.append(check("overview wrapper", _unwrap_object({"data": {"plan": "X"}}), {"plan": "X"}))
results.append(check("overview bare", _unwrap_object({"plan": "X"}), {"plan": "X"}))

# --- leverage parsing and sheet layouts -------------------------------------
results.append(check("1:100", parse_leverage("1:100"), 100.0))
results.append(check("plain 30", parse_leverage("30"), 30.0))
results.append(check("blank leverage", parse_leverage(""), None))

wide = _parse_worksheet("Leverage", [
    ["Instrument", "Pro 100K", "Evaluation 50K"],
    ["EURUSD", "1:100", "1:50"],
    ["XAUUSD", "1:20", "1:10"],
])
results.append(check("wide layout", wide[("pro100k", "eurusd")][0], 100.0))
results.append(check("wide layout 2", wide[("evaluation50k", "xauusd")][0], 10.0))

long = _parse_worksheet("Sheet1", [
    ["Plan", "Symbol", "Leverage"],
    ["Pro 100K", "EURUSD", "1:100"],
    ["Pro 100K", "US30", "1:20"],
])
results.append(check("long layout", long[("pro100k", "us30")][0], 20.0))

pertab = _parse_worksheet("Evaluation", [["Instrument", "Leverage"], ["GBPUSD", "1:30"]])
results.append(check("per-tab layout", pertab[("evaluation", "gbpusd")][0], 30.0))

# the live layout: one tab per plan, Instrument Name | Asset Class | Leverage
live = {}
for tab, lev in (("Prime", "50"), ("Pro", "100"), ("Direct", "50")):
    live.update(_parse_worksheet(tab, [
        ["Instrument Name", "Asset Class", "Leverage"],
        ["AUDCAD", "FX Majors & Minors", lev],
        ["XAUUSD", "Metals", "10" if tab != "Pro" else "20"],
    ]))
results.append(check("live tabs parsed", len(live), 6))
results.append(check("live prime leverage", live[("prime", "audcad")].leverage, 50.0))
results.append(check("live pro leverage", live[("pro", "audcad")].leverage, 100.0))
results.append(check("asset class captured", live[("prime", "audcad")].asset_class,
                     "FX Majors & Minors"))
results.append(check("asset class not read as instrument",
                     live[("prime", "audcad")].instrument, "AUDCAD"))

# --- plan routing -----------------------------------------------------------
results.append(check("plan '2-Step Prime Funded'", resolve_plan(live, "2-Step Prime Funded")[0],
                     "prime"))
results.append(check("plan 'Pro Funded'", resolve_plan(live, "Pro Funded")[0], "pro"))
results.append(check("plan '1-Step Direct'", resolve_plan(live, "1-Step Direct")[0], "direct"))
results.append(check("unknown plan unresolved", resolve_plan(live, "Gold Tier")[0], None))
results.append(check("blank plan unresolved", resolve_plan(live, "")[0], None))
results.append(check("single-plan sheet needs no plan", resolve_plan(pertab, "")[0], "evaluation"))

# --- lookup: correct tab, and never across plans ----------------------------
results.append(check("prime routes to prime", lookup_leverage(live, "Prime 100K", "AUDCAD")[0], 50.0))
results.append(check("pro routes to pro", lookup_leverage(live, "Pro 100K", "AUDCAD")[0], 100.0))
results.append(check("direct routes to direct",
                     lookup_leverage(live, "Direct 50K", "AUDCAD")[0], 50.0))
results.append(check("suffix variant", lookup_leverage(live, "Prime", "AUDCAD.r")[0], 50.0))
results.append(check("no cross-plan fallback", lookup_leverage(live, "Gold Tier", "AUDCAD")[0], None))
results.append(check("miss on tab", lookup_leverage(live, "Prime", "NOPE")[0], None))
results.append(check("wide layout still routes", lookup_leverage(wide, "Pro 100K", "EURUSD")[0],
                     100.0))


# --- epoch timestamps (values taken from a real payload) --------------------
from transform import as_datetime, build_rows

results.append(check("epoch ms", str(as_datetime(1785281282460)), "2026-07-28 23:28:02.460000"))
results.append(check("epoch ms as string", str(as_datetime("1785248081584")),
                     "2026-07-28 14:14:41.584000"))
results.append(check("epoch seconds", str(as_datetime(1785281282)), "2026-07-28 23:28:02"))
results.append(check("non-epoch passthrough", as_datetime("2026-01-05 10:00"), "2026-01-05 10:00"))
results.append(check("none passthrough", as_datetime(None), None))
results.append(check("small int not a date", as_datetime(42), 42))

# --- symbol fallback --------------------------------------------------------
results.append(check("guess from tradeInstrument",
                     fields.guess_symbol({"ticket": "M0088_1785", "tradeInstrument": "XAUUSD"})[0],
                     "XAUUSD"))
results.append(check("guess skips currency",
                     fields.guess_symbol({"currency": "USD", "secName": "EURUSD"})[0],
                     "EURUSD"))
results.append(check("guess keeps broker suffix",
                     fields.guess_symbol({"sym": "US30.CASH"})[0], "US30.CASH"))
results.append(check("guess skips plan text",
                     fields.guess_symbol({"plan": "2-Step Prime Funded", "x": "GBPJPY"})[0],
                     "GBPJPY"))
results.append(check("guess finds nothing",
                     fields.guess_symbol({"ticket": "X1", "comment": "tp hit"})[0], None))
results.append(check("explicit key beats guess",
                     fields.pick({"stdSymbol": "GBPUSD", "tradeInstrument": "WRONG"},
                                 fields.SYMBOL), "GBPUSD"))

# --- end-to-end row build, using the real screenshot trade ------------------
frame = build_rows(
    [{"ticket": "W583093554253050", "stdSymbol": "XAUUSD", "side": "Sell",
      "volume": 0.70, "openPrice": 4047.51, "closePrice": 4052.36,
      "openTime": 1785222334986, "closeTime": 1785225973644,
      "profit": -339.50, "commission": -4.20, "contractSize": 100},
     {"ticket": "NOLEV", "stdSymbol": "NOPE", "volume": 1, "openPrice": 1.1,
      "closePrice": 1.2, "profit": 100, "contractSize": 100000}],
    "2-Step Prime Funded", live, 100000.0)

results.append(check("row count", len(frame), 2))
results.append(check("instrument", frame.loc[0, "Instrument"], "XAUUSD"))
results.append(check("asset class column", frame.loc[0, "Asset Class"], "Metals"))
results.append(check("contract size from trade", frame.loc[0, "Contract Size"], 100.0))
results.append(check("leverage from Prime tab", frame.loc[0, "Instrument Leverage"], 10.0))
# notional 4047.51*0.7*100 = 283325.7 / 10 = 28332.57 ; conversion 339.5/339.5 = 1
results.append(check("margin", round(frame.loc[0, "Margin"], 2), 28332.57))

# the same trade on a Pro account must use leverage 20 and halve the margin
pro = build_rows([{"stdSymbol": "XAUUSD", "volume": 0.70, "openPrice": 4047.51,
                   "closePrice": 4052.36, "profit": -339.50, "contractSize": 100}],
                 "Pro 100K", live, 100000.0)
results.append(check("pro leverage", pro.loc[0, "Instrument Leverage"], 20.0))
results.append(check("pro margin halves", round(pro.loc[0, "Margin"], 2), 14166.29))
results.append(check("no note on good row", frame.loc[0, "Notes"], ""))
results.append(check("open time formatted", str(frame.loc[0, "Open Time"]),
                     "2026-07-28 07:05:34.986000"))
results.append(check("unmatched leverage -> no margin",
                     bool(pd.isna(frame.loc[1, "Margin"])), True))
results.append(check("unmatched leverage note",
                     "not on the 'prime' tab" in frame.loc[1, "Notes"], True))


# --- real payload shape (field names from the live API; values are fake) ----
overview = {"Login": 900001, "AccountType": "2-Step Prime (Phase 2) Assessment",
            "StartingBalance": 2000.0, "CurrentBalance": 1806.71,
            "CurrentEquity": 1806.71, "Password": "fake-pass-123",
            "PlanDescription": "2-Step Prime C-2 2000 MT5 Bi-Weekly"}
results.append(check("real plan", fields.pick(overview, fields.PLAN),
                     "2-Step Prime (Phase 2) Assessment"))
results.append(check("real account size",
                     fields.to_float(fields.pick(overview, fields.ACCOUNT_SIZE)), 2000.0))
results.append(check("real balance",
                     fields.to_float(fields.pick(overview, fields.BALANCE)), 1806.71))
results.append(check("real equity",
                     fields.to_float(fields.pick(overview, fields.EQUITY)), 1806.71))
results.append(check("real login", fields.pick(overview, fields.LOGIN), 900001))
results.append(check("account size is starting, not current",
                     fields.to_float(fields.pick(overview, fields.ACCOUNT_SIZE)) != 1806.71, True))

real_trade = {"stdSymbol": "XAUUSD", "trdSymbol": "XAUUSD", "side": "Buy", "lots": 0.04,
              "openPrice": 4234.95, "closePrice": 4235.27, "openTime": 1781229265000,
              "closeTime": 1781229381000, "profit": 1.28, "commission": -0.24,
              "contractSize": 100, "ticket": 5309758, "swap": 0.0,
              "lastIpAddress": "203.0.113.24"}
results.append(check("real symbol", fields.pick(real_trade, fields.SYMBOL), "XAUUSD"))
results.append(check("real side", fields.pick(real_trade, fields.SIDE), "Buy"))
results.append(check("real lots", fields.to_float(fields.pick(real_trade, fields.LOTS)), 0.04))
results.append(check("real contract size",
                     fields.to_float(fields.pick(real_trade, fields.CONTRACT_SIZE)), 100.0))

# --- redaction --------------------------------------------------------------
red = fields.redact(overview)
results.append(check("password masked", red["Password"], "••• redacted •••"))
results.append(check("no plaintext leak", "fake-pass-123" in str(red), False))
results.append(check("non-sensitive untouched", red["CurrentBalance"], 1806.71))
results.append(check("ip masked", fields.redact(real_trade)["lastIpAddress"],
                     "••• redacted •••"))
results.append(check("nested redaction",
                     fields.redact({"a": {"apiKey": "x"}})["a"]["apiKey"], "••• redacted •••"))
results.append(check("list redaction",
                     fields.redact([{"token": "t"}])[0]["token"], "••• redacted •••"))
results.append(check("is_sensitive password", fields.is_sensitive("Password"), True))
results.append(check("is_sensitive lots", fields.is_sensitive("lots"), False))

# --- time overlap -----------------------------------------------------------
# Trades overlap when open_b < close_a AND close_b > open_a. Every arrangement
# of two live windows, using the reference times from the spec.

def at(hhmm):
    return pd.Timestamp(f"2026-01-01 {hhmm}")


A = (at("10:00"), at("11:00"))

results.append(check("B inside A", overlaps(*A, at("10:15"), at("10:45")), True))
results.append(check("B opens inside A, closes after",
                     overlaps(*A, at("10:30"), at("11:30")), True))
results.append(check("B opens before A, closes inside",
                     overlaps(at("10:30"), at("11:30"), at("10:00"), at("11:00")), True))
results.append(check("B covers A entirely",
                     overlaps(at("10:30"), at("11:00"), at("10:00"), at("12:00")), True))
results.append(check("identical windows", overlaps(*A, *A), True))
results.append(check("disjoint — B after A",
                     overlaps(at("10:00"), at("10:30"), at("10:40"), at("11:00")), False))
results.append(check("disjoint — B before A",
                     overlaps(at("10:40"), at("11:00"), at("10:00"), at("10:30")), False))
results.append(check("touching endpoints are not concurrent",
                     overlaps(*A, at("09:00"), at("10:00")), False))
results.append(check("overlap is symmetric",
                     overlaps(at("10:30"), at("11:30"), *A),
                     overlaps(*A, at("10:30"), at("11:30"))))
results.append(check("open trade overlaps a later one",
                     overlaps(at("10:00"), None, at("14:00"), at("15:00")), True))
results.append(check("no open time cannot be placed",
                     overlaps(None, at("11:00"), *A), False))

# A(100) and B(200) overlap; C(400) is disjoint from both.
frame = pd.DataFrame([
    {"Ticket": "A", "Open Time": at("10:00"), "Close Time": at("11:00"), "Margin": 100.0,
     "Notes": ""},
    {"Ticket": "B", "Open Time": at("10:30"), "Close Time": at("11:30"), "Margin": 200.0,
     "Notes": ""},
    {"Ticket": "C", "Open Time": at("12:00"), "Close Time": at("13:00"), "Margin": 400.0,
     "Notes": ""},
])
combined = add_overlap_columns(frame, account_size=1000.0)
results.append(check("A combines with B", combined.loc[0, "Combined Margin"], 300.0))
results.append(check("B combines with A", combined.loc[1, "Combined Margin"], 300.0))
results.append(check("C overlaps nothing", combined.loc[2, "Overlaps"], 0))
results.append(check("lone trade has no combined margin",
                     pd.isna(combined.loc[2, "Combined Margin"]), True))
results.append(check("combined margin %", combined.loc[0, "Combined Margin %"], 30.0))
results.append(check("peer tickets listed", combined.loc[0, "Overlapping Trades"], "B"))
results.append(check("own margin untouched", combined.loc[2, "Margin"], 400.0))

# Three-way: A+B+C all live at 10:45, so each carries the full 700.
three = pd.DataFrame([
    {"Ticket": "A", "Open Time": at("10:00"), "Close Time": at("11:00"), "Margin": 100.0,
     "Notes": ""},
    {"Ticket": "B", "Open Time": at("10:15"), "Close Time": at("10:50"), "Margin": 200.0,
     "Notes": ""},
    {"Ticket": "C", "Open Time": at("10:40"), "Close Time": at("12:00"), "Margin": 400.0,
     "Notes": ""},
])
triple = add_overlap_columns(three, account_size=1000.0)
results.append(check("three-way combined", triple.loc[0, "Combined Margin"], 700.0))
results.append(check("three-way count", triple.loc[0, "Overlaps"], 2))
peak = peak_concurrent(three)
results.append(check("peak concurrent margin", peak["margin"], 700.0))
results.append(check("peak trade count", len(peak["tickets"]), 3))

# A overlaps B and C, but B and C never meet each other: the per-trade sum is
# A's own concurrent exposure, while the true peak is only ever two trades.
chain = pd.DataFrame([
    {"Ticket": "A", "Open Time": at("10:00"), "Close Time": at("12:00"), "Margin": 100.0,
     "Notes": ""},
    {"Ticket": "B", "Open Time": at("10:10"), "Close Time": at("10:20"), "Margin": 200.0,
     "Notes": ""},
    {"Ticket": "C", "Open Time": at("11:00"), "Close Time": at("11:30"), "Margin": 400.0,
     "Notes": ""},
])
chained = add_overlap_columns(chain, account_size=1000.0)
results.append(check("B does not meet C", chained.loc[1, "Overlaps"], 1))
results.append(check("peak is the worst pair, not the chain",
                     peak_concurrent(chain)["margin"], 500.0))

# A trade with no margin is excluded from the sum and flagged, not silently dropped.
partial = add_overlap_columns(pd.DataFrame([
    {"Ticket": "A", "Open Time": at("10:00"), "Close Time": at("11:00"), "Margin": 100.0,
     "Notes": ""},
    {"Ticket": "B", "Open Time": at("10:30"), "Close Time": at("11:30"), "Margin": None,
     "Notes": "missing profit"},
]), account_size=1000.0)
results.append(check("partial combined excludes the blank",
                     partial.loc[0, "Combined Margin"], 100.0))
results.append(check("partial combined is flagged",
                     "combined margin is partial" in partial.loc[0, "Notes"], True))
results.append(check("blank margin gives no combined",
                     pd.isna(partial.loc[1, "Combined Margin"]), True))
results.append(check("existing note kept",
                     partial.loc[1, "Notes"].startswith("missing profit"), True))

# Still-open trade: no close time, so it runs on and overlaps what follows.
still = add_overlap_columns(pd.DataFrame([
    {"Ticket": "A", "Open Time": at("10:00"), "Close Time": None, "Margin": 100.0,
     "Notes": ""},
    {"Ticket": "B", "Open Time": at("15:00"), "Close Time": at("16:00"), "Margin": 200.0,
     "Notes": ""},
]), account_size=1000.0)
results.append(check("open trade overlaps later trade", still.loc[0, "Overlaps"], 1))
results.append(check("open trade noted",
                     "still open" in still.loc[0, "Notes"], True))

results.append(check("empty frame is safe",
                     len(add_overlap_columns(pd.DataFrame(), account_size=1000.0)), 0))
results.append(check("no account size leaves combined % blank",
                     pd.isna(add_overlap_columns(frame).loc[0, "Combined Margin %"]), True))

# Epoch milliseconds, as the real payload carries them, must place on the timeline too.
epoch = add_overlap_columns(pd.DataFrame([
    {"Ticket": "A", "Open Time": pd.Timestamp("2026-06-08 12:34:25"),
     "Close Time": pd.Timestamp("2026-06-08 12:36:21"), "Margin": 100.0, "Notes": ""},
    {"Ticket": "B", "Open Time": pd.Timestamp("2026-06-08 12:35:00"),
     "Close Time": pd.Timestamp("2026-06-08 12:40:00"), "Margin": 50.0, "Notes": ""},
]), account_size=1000.0)
results.append(check("second-resolution overlap", epoch.loc[0, "Combined Margin"], 150.0))

# A trade the feed reports as still open sends closeTime back as 0. Read as an
# epoch that is 1970, which would hand the trade a decades-long window that
# overlaps nearly every other trade on the account.
results.append(check("zero close time is 'still open', not 1970",
                     overlaps(at("10:00"), 0, at("09:00"), at("09:30")), False))
results.append(check("zero close time still overlaps what follows",
                     overlaps(at("10:00"), 0, at("14:00"), at("15:00")), True))
results.append(check("negative close time is 'still open'",
                     overlaps(at("10:00"), -1, at("09:00"), at("09:30")), False))
zeroed = add_overlap_columns(pd.DataFrame([
    {"Ticket": "OPEN", "Open Time": at("10:00"), "Close Time": 0, "Margin": 100.0, "Notes": ""},
    {"Ticket": "EARLIER", "Open Time": at("08:00"), "Close Time": at("09:00"),
     "Margin": 200.0, "Notes": ""},
]), account_size=1000.0)
results.append(check("open trade does not reach backwards", zeroed.loc[0, "Overlaps"], 0))
results.append(check("zero close time reads as still open",
                     "still open" in zeroed.loc[0, "Notes"], True))

# Epoch milliseconds straight from the payload, not pre-converted.
results.append(check("raw epoch ms overlap",
                     overlaps(1781229265000, 1781229385000, 1781229300000, 1781229500000), True))
results.append(check("raw epoch ms disjoint",
                     overlaps(1781229265000, 1781229385000, 1781239300000, 1781239500000), False))

# The evidence shown when an account never holds two trades at once.
gap, earlier, later = closest_gap(pd.DataFrame([
    {"Ticket": "A", "Open Time": at("10:00"), "Close Time": at("10:30")},
    {"Ticket": "B", "Open Time": at("10:32"), "Close Time": at("11:00")},
    {"Ticket": "C", "Open Time": at("14:00"), "Close Time": at("15:00")},
]))
results.append(check("closest gap", gap, pd.Timedelta(minutes=2)))
results.append(check("closest gap tickets", (earlier, later), ("A", "B")))
results.append(check("closest gap needs two trades",
                     closest_gap(pd.DataFrame([{"Ticket": "A", "Open Time": at("10:00"),
                                                "Close Time": at("10:30")}])), None))
results.append(check("gap wording seconds", format_gap(pd.Timedelta(seconds=2)), "2 seconds"))
results.append(check("gap wording singular", format_gap(pd.Timedelta(seconds=1)), "1 second"))
results.append(check("gap wording minutes", format_gap(pd.Timedelta(seconds=99)), "1m 39s"))
results.append(check("gap wording hours", format_gap(pd.Timedelta(hours=5, minutes=7)), "5h 7m"))

print()
print(f"{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
