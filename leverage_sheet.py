"""Read the (plan, instrument) -> instrument_leverage table from Google Sheets.

The sheet is private, so access goes through a service account. Share the sheet
with the service account's client_email as a Viewer, then point
GOOGLE_APPLICATION_CREDENTIALS (or credentials.json in this folder) at its key.

The live sheet has one tab per plan family (Prime / Pro / Direct), each with
`Instrument Name | Asset Class | Leverage`. Two other layouts are also handled:

  pertab | instrument | leverage |            -> plan comes from the worksheet title
  long   | instrument | plan   | leverage |   -> keyed by both columns
  wide   | instrument | Plan A | Plan B   |   -> plan comes from the column header

Leverage differs per plan for the same instrument (AUDCAD is 50 on Prime but 100
on Pro), so lookup NEVER falls back across plans -- an unresolved plan yields no
margin and an explanatory note rather than a plausible wrong number. The only
exception is a sheet that defines a single plan, where there is nothing to
confuse.
"""

import os
import re
from collections import namedtuple

import gspread
from google.oauth2.service_account import Credentials

SHEET_ID = "12xwxKQkvHpK_iIg1Mc7CtC3p-LoTOfeV4xDf_jIxOuI"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

Entry = namedtuple("Entry", "leverage plan instrument asset_class")

_INSTRUMENT_HEADERS = {"instrument", "instruments", "instrumentname", "symbol",
                       "symbols", "symbolname", "stdsymbol", "ticker", "pair",
                       "pairs", "security", "market", "name", "assetname"}
_LEVERAGE_HEADERS = {"leverage", "instrumentleverage", "lev", "leverages",
                     "maxleverage", "instrumentlev"}
_PLAN_HEADERS = {"plan", "plans", "planname", "program", "account", "accounttype",
                 "challenge", "challengetype", "product", "package", "phase"}
_ASSET_CLASS_HEADERS = {"assetclass", "asset", "class", "category", "assettype",
                        "instrumenttype", "type", "group"}

_ALNUM = re.compile(r"[^a-z0-9]+")


class LeverageError(RuntimeError):
    pass


def norm(text):
    return _ALNUM.sub("", str(text).lower())


def parse_leverage(value):
    """'1:100' -> 100.0, '100' -> 100.0, '' -> None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        text = text.split(":")[-1]
    text = text.replace(",", "").replace("x", "").replace("X", "").strip()
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number > 0 else None


def _credentials(credentials_path=None):
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [credentials_path,
                  os.path.join(here, "service_account.json"),
                  os.path.join(here, "credentials.json"),
                  os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")]

    # An explicit path that does not exist is a typo worth reporting, not skipping.
    if credentials_path and not os.path.exists(credentials_path):
        raise LeverageError(f"Service account key not found at {credentials_path}")

    for path in candidates:
        if path and os.path.exists(path):
            return Credentials.from_service_account_file(path, scopes=SCOPES)

    tried = ", ".join(p for p in candidates[1:] if p)
    raise LeverageError(
        "No service account key found (looked at: " + tried + "). Save the JSON key "
        "as credentials.json in this folder, then share the sheet with the service "
        "account's client_email as Viewer."
    )


def _header_row(rows):
    """Index of the first row that looks like a header, or None."""
    for index, row in enumerate(rows[:15]):
        cells = [norm(c) for c in row if str(c).strip()]
        if len(cells) < 2:
            continue
        if any(c in _INSTRUMENT_HEADERS for c in cells):
            return index
    return None


def _parse_worksheet(title, rows):
    """{(plan_norm, instrument_norm): Entry}"""
    table = {}
    header_index = _header_row(rows)
    if header_index is None:
        return table

    header = rows[header_index]
    normed = [norm(c) for c in header]

    instrument_col = next((i for i, c in enumerate(normed) if c in _INSTRUMENT_HEADERS), None)
    if instrument_col is None:
        return table
    leverage_col = next((i for i, c in enumerate(normed) if c in _LEVERAGE_HEADERS), None)
    plan_col = next((i for i, c in enumerate(normed) if c in _PLAN_HEADERS), None)
    asset_col = next((i for i, c in enumerate(normed)
                      if c in _ASSET_CLASS_HEADERS and i != instrument_col), None)

    # Remaining labelled columns are per-plan leverage columns (the "wide" layout).
    known = {instrument_col, leverage_col, plan_col, asset_col}
    reserved = (_INSTRUMENT_HEADERS | _LEVERAGE_HEADERS | _PLAN_HEADERS
                | _ASSET_CLASS_HEADERS)
    plan_columns = [i for i, c in enumerate(normed)
                    if c and i not in known and c not in reserved]

    def cell(row, index):
        return row[index] if index is not None and index < len(row) else ""

    for row in rows[header_index + 1:]:
        instrument = str(cell(row, instrument_col)).strip()
        if not instrument:
            continue
        instrument_key = norm(instrument)
        asset_class = str(cell(row, asset_col)).strip() if asset_col is not None else ""

        if leverage_col is not None:
            leverage = parse_leverage(cell(row, leverage_col))
            if leverage is not None:
                plan_label = str(cell(row, plan_col)).strip() if plan_col is not None else title
                table[(norm(plan_label), instrument_key)] = Entry(
                    leverage, plan_label, instrument, asset_class)

        for column in plan_columns:
            leverage = parse_leverage(cell(row, column))
            if leverage is None:
                continue
            plan_label = str(header[column]).strip()
            table[(norm(plan_label), instrument_key)] = Entry(
                leverage, plan_label, instrument, asset_class)

    return table


def load_leverage_table(credentials_path=None, sheet_id=SHEET_ID):
    """(table, worksheet_titles). Raises LeverageError on access problems."""
    client = gspread.authorize(_credentials(credentials_path))
    try:
        spreadsheet = client.open_by_key(sheet_id)
    except gspread.exceptions.APIError as exc:
        raise LeverageError(
            f"Could not open sheet {sheet_id}: {exc}. Share it with the service "
            f"account's client_email as Viewer."
        ) from exc

    table = {}
    titles = []
    any_data = False
    for worksheet in spreadsheet.worksheets():
        titles.append(worksheet.title)
        try:
            rows = worksheet.get_all_values()
        except gspread.exceptions.APIError:
            continue
        if any(any(str(c).strip() for c in row) for row in rows):
            any_data = True
        table.update(_parse_worksheet(worksheet.title, rows))

    if not table:
        tabs = ", ".join(titles)
        if not any_data:
            raise LeverageError(
                f"Sheet '{spreadsheet.title}' ({sheet_id}) is empty — tab(s) {tabs} "
                "contain no data. Add the instrument/leverage table, or point the app "
                "at the spreadsheet that actually holds it."
            )
        raise LeverageError(
            f"Sheet '{spreadsheet.title}' has data but no recognisable instrument "
            f"column in tab(s) {tabs}. Expected a header cell like Instrument/Symbol "
            "alongside either a Leverage column or one column per plan."
        )
    return table, titles


def _instrument_variants(instrument):
    """Match 'EURUSD.r', 'EURUSD#', 'EURUSDm' against a plain 'EURUSD' row."""
    base = norm(instrument)
    variants = [base]
    for suffix in ("r", "m", "c", "pro", "ecn", "raw", "std", "micro", "cash", "spot"):
        if base.endswith(suffix) and len(base) > len(suffix) + 2:
            variants.append(base[: -len(suffix)])
    return variants


def plan_keys(table):
    """Distinct normalised plan keys held in the table."""
    return sorted({key[0] for key in table})


def resolve_plan(table, plan):
    """Map an account plan onto a sheet tab. (plan_key, note); plan_key None if unresolved.

    'Prime Funded 2-Step' -> the 'Prime' tab. Matching is by whole token first,
    then by substring with the longest tab name winning, so 'Prime' is never
    beaten by a shorter accidental hit.
    """
    available = plan_keys(table)
    if not available:
        return None, "leverage sheet is empty"
    if len(available) == 1:
        return available[0], ""
    if not plan:
        return None, ("no plan on the account, and the sheet defines several ("
                      + ", ".join(available) + ")")

    plan_norm = norm(plan)
    tokens = {t for t in _ALNUM.split(str(plan).lower()) if t}

    exact = [key for key in available if key in tokens]
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:
        return None, f"plan {plan!r} matches more than one tab ({', '.join(exact)})"

    partial = sorted((key for key in available if key and key in plan_norm),
                     key=len, reverse=True)
    if partial:
        return partial[0], ""

    return None, (f"plan {plan!r} does not match any sheet tab ("
                  + ", ".join(available) + ")")


def lookup_leverage(table, plan, instrument):
    """(leverage, note). leverage is None when it cannot be resolved without guessing.

    Never falls back to another plan's leverage: the same instrument carries
    different leverage per plan, so a fallback would be a wrong number rather
    than a missing one.
    """
    if not table:
        return None, "leverage sheet unavailable"
    if not instrument:
        return None, "no instrument on trade"

    plan_key, note = resolve_plan(table, plan)
    if plan_key is None:
        return None, note

    for variant in _instrument_variants(instrument):
        entry = table.get((plan_key, variant))
        if entry is not None:
            return entry.leverage, ""

    return None, f"instrument {instrument!r} not on the {plan_key!r} tab"


def lookup_entry(table, plan, instrument):
    """The full Entry for a trade, or None. Used for the Asset Class column."""
    if not table or not instrument:
        return None
    plan_key, _ = resolve_plan(table, plan)
    if plan_key is None:
        return None
    for variant in _instrument_variants(instrument):
        entry = table.get((plan_key, variant))
        if entry is not None:
            return entry
    return None
