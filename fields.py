"""Tolerant field lookup.

The FPFX payload field names are not documented here, so every read goes through
a normalised key map (lowercase, alphanumerics only) and a list of candidate
names. Add a candidate to the lists below if a real payload uses another name.
"""

import re

_NORM = re.compile(r"[^a-z0-9]+")


def norm_key(key):
    return _NORM.sub("", str(key).lower())


# The overview payload ships the account's plaintext MT5 password, and trades
# carry the client's IP. Neither belongs on screen, so anything matching these
# is masked before a payload is displayed.
SENSITIVE = ("password", "passwd", "pwd", "secret", "token", "apikey",
             "authorization", "credential", "privatekey", "ipaddress", "email")


def is_sensitive(key):
    return any(marker in norm_key(key) for marker in SENSITIVE)


def redact(obj):
    """Deep copy with sensitive values masked. Safe to hand to st.json."""
    if isinstance(obj, dict):
        return {k: ("••• redacted •••" if is_sensitive(k) and obj[k] not in (None, "", 0)
                    else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def flatten(obj, prefix=""):
    """Flatten nested dicts into a single level so `account.balance` is reachable."""
    flat = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict):
                flat.update(flatten(value, f"{path}."))
            else:
                flat[path] = value
    return flat


def key_map(obj):
    """{normalised_key: value} for a record, including nested paths and leaf names."""
    mapping = {}
    for path, value in flatten(obj).items():
        mapping.setdefault(norm_key(path), value)
        mapping.setdefault(norm_key(path.split(".")[-1]), value)
    return mapping


def pick(record, candidates, default=None):
    """First candidate name present in the record, else default."""
    mapping = record if isinstance(record, dict) and "__normalised__" in record else key_map(record)
    for candidate in candidates:
        value = mapping.get(norm_key(candidate))
        if value not in (None, "", "-"):
            return value
    return default


def to_float(value, default=None):
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace(" ", "")
    if not text or text in {"-", "--", "n/a", "N/A"}:
        return default
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").lstrip("+")
    text = re.sub(r"[^0-9.\-eE]", "", text)
    try:
        number = float(text)
    except ValueError:
        return default
    return -number if negative else number


# --- candidate names, most specific first -----------------------------------

# Confirmed against the live payload: overview carries AccountType,
# StartingBalance, CurrentBalance, CurrentEquity, Login.
PLAN = ["AccountType", "account_type", "accountType", "plan", "plan_name",
        "planName", "planTitle", "PlanDescription", "program", "programName",
        "challenge_type", "challengeType", "product", "package"]

ACCOUNT_SIZE = ["StartingBalance", "starting_balance", "startingBalance",
                "account_size", "accountSize", "initial_balance", "initialBalance",
                "CurrentBalance", "balance", "account_balance", "accountBalance",
                "equity", "deposit"]

BALANCE = ["CurrentBalance", "current_balance", "balance", "account_balance",
           "accountBalance", "equity"]
EQUITY = ["CurrentEquity", "current_equity", "equity", "account_equity", "accountEquity"]
LOGIN = ["Login", "login_id", "loginId", "login", "account", "account_id",
         "accountId", "accountNumber"]
CURRENCY = ["currency", "account_currency", "accountCurrency", "ccy"]

SYMBOL = ["stdSymbol", "std_symbol", "standardSymbol", "standard_symbol",
          "symbol", "symbolName", "symbol_name", "tradeSymbol", "trade_symbol",
          "instrument", "instrument_name", "instrumentName", "instrumentSymbol",
          "ticker", "pair", "security", "asset", "market",
          "product", "product_name", "productName", "contract_name", "contractName"]

LOTS = ["lots", "lot", "volume", "size", "lot_size", "lotSize", "quantity", "qty",
        "trade_volume", "tradeVolume", "volume_lots", "volumeLots"]

OPEN_PRICE = ["open_price", "openPrice", "price_open", "priceOpen", "openingPrice",
              "opening_price", "entry_price", "entryPrice", "open", "price"]

CLOSE_PRICE = ["close_price", "closePrice", "price_close", "priceClose", "closingPrice",
               "closing_price", "exit_price", "exitPrice", "close"]

PROFIT = ["profit", "net_profit", "netProfit", "pnl", "pl", "profit_loss",
          "profitLoss", "gross_profit", "grossProfit", "result"]

CONTRACT_SIZE = ["contract_size", "contractSize", "contract", "lot_contract_size",
                 "contractsize", "unit_size", "unitSize", "trade_contract_size"]

SIDE = ["side", "direction", "buy_sell", "buySell", "trade_type", "tradeType",
        "order_type", "orderType", "cmd", "action", "type"]
OPEN_TIME = ["open_time", "openTime", "opened_at", "openedAt", "time_open", "date_open"]
CLOSE_TIME = ["close_time", "closeTime", "closed_at", "closedAt", "time_close", "date_close"]
TICKET = ["ticket", "trade_id", "tradeId", "order_id", "orderId", "deal", "id", "position_id"]
COMMISSION = ["commission", "commissions", "fee"]
SWAP = ["swap", "swaps", "rollover"]


# --- symbol fallback --------------------------------------------------------
# The trade payload carries the instrument, but under a key not in SYMBOL above.
# Rather than leave the column empty, scan the record for the one value that
# looks like a ticker. Keys that commonly hold non-ticker strings are excluded.

_SYMBOL_SHAPE = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,11}([._#\-/][A-Za-z0-9]{1,6})?$")

_NOT_SYMBOL_KEYS = ("ticket", "id", "login", "account", "plan", "program",
                    "currency", "ccy", "time", "date", "status", "comment",
                    "side", "direction", "cmd", "action", "type", "group",
                    "server", "platform", "email", "user", "state", "reason",
                    "phase", "step", "stage", "challenge", "product", "package")


def guess_symbol(record):
    """(symbol, source_key) for the most ticker-looking value, else (None, None)."""
    for path, value in flatten(record).items():
        leaf = norm_key(path.split(".")[-1])
        if any(bad in leaf for bad in _NOT_SYMBOL_KEYS):
            continue
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not _SYMBOL_SHAPE.match(text):
            continue
        letters = sum(c.isalpha() for c in text)
        if letters < 2 or text.isdigit():
            continue
        # a ticker is predominantly upper case in every feed we have seen
        if text.upper() != text:
            continue
        return text, path
    return None, None
