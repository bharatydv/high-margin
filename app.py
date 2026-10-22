"""High Margin — enter a login id, get every trade with a derived Margin column."""

import pandas as pd
import streamlit as st

import fields
from fpfx_api import (ApiError, fetch_all_trades, fetch_overview, fetch_payouts,
                      latest_approved_date)
from leverage_sheet import LeverageError, load_leverage_table
from transform import COLUMN_ORDER, build_rows
from overlap import add_overlap_columns, OVERLAP_COLUMNS

# Fixed settings — deliberately not user-facing controls.
THRESHOLD_PCT = 70.0
DEFAULT_CONTRACT_SIZE = 100_000.0
YELLOW = "background-color: #FFE680; color: #1a1a1a"
PEACH = "background-color: #FFD0B0; color: #1a1a1a"

# The compact view: one row per trade, only the inputs to the margin formula.
SUMMARY_COLUMNS = ["Trade ID", "Instrument Name", "Lot size", "Contract size", "Side", "Symbols",
                   "Open Price", "Open Time", "Close Time", "Account Size", "Instrument Leverage",
                   "Margin (%)", "Overlaps", "Overlapping Trades", "Combined Margin", "Combined Margin %"]

st.set_page_config(page_title="High Margin", page_icon="📊", layout="wide")


def _first_series(df, name):
    """Return a plain 1-D Series for a column name, even if duplicated.

    When columns are duplicated (by name) df[name] returns a DataFrame of
    every matching column. Take the leftmost column; if there were
    duplicates, keep only that first copy.
    """
    matches = [c for c in df.columns if c == name]
    if not matches:
        return None
    first = matches[0]
    if len(matches) > 1:
        keep = [c for c in df.columns if c != name or c is first]
        df = df[keep]
    series = df[first]
    if isinstance(series, pd.DataFrame):
        series = series.iloc[:, 0]
    return series


def _as_plain_list(series):
    """pandas Series -> Python list (works regardless of pandas version)."""
    if series is None:
        return None
    try:
        return list(series.values)
    except AttributeError:
        return list(series)


def build_highlight_mask(frame):
    """Which rows to highlight — TRUE for:
      1. rows whose own Margin % (or Margin (%), whichever is present)
         exceeds THRESHOLD_PCT;
      2. rows whose Combined Margin % exceeds THRESHOLD_PCT;
      3. rows that OVERLAP any row already flagged by rules 1–2.
    This guarantees that when two trades' combined exposure is over the
    line BOTH trades are highlighted, not just whichever one had the bigger
    stand-alone margin.
    """
    n = len(frame)
    mask = [False] * n
    if n == 0:
        return mask

    margin_pct_series = None
    for col in ("Margin %", "Margin (%)"):
        if col in frame.columns:
            margin_pct_series = _first_series(frame, col)
            if margin_pct_series is not None:
                break
    margin_pct = list(margin_pct_series) if margin_pct_series is not None else [pd.NA] * n

    combined_pct = None
    if "Combined Margin %" in frame.columns:
        s = _first_series(frame, "Combined Margin %")
        if s is not None:
            combined_pct = list(s)

    for i in range(n):
        v = margin_pct[i]
        if pd.notna(v) and v > THRESHOLD_PCT:
            mask[i] = True
        if combined_pct is not None:
            u = combined_pct[i]
            if pd.notna(u) and u > THRESHOLD_PCT:
                mask[i] = True

    peers_col = None
    if "Overlapping Trades" in frame.columns:
        s = _first_series(frame, "Overlapping Trades")
        if s is not None:
            peers_col = list(s)
    ticket_col = None
    for col in ("Ticket", "Trade ID"):
        if col in frame.columns:
            s = _first_series(frame, col)
            if s is not None:
                ticket_col = list(s)
                break

    if peers_col is not None and ticket_col is not None:
        ticket_to_row = {}
        for i, t in enumerate(ticket_col):
            if t is None or (isinstance(t, float) and pd.isna(t)):
                continue
            k = str(t)
            if k not in ticket_to_row:
                ticket_to_row[k] = []
            ticket_to_row[k].append(i)

        already_flagged = [i for i, b in enumerate(mask) if b]
        for flagged_row in already_flagged:
            peers_text = peers_col[flagged_row]
            if not peers_text:
                continue
            for ticket_str in str(peers_text).split(","):
                t = ticket_str.strip()
                if not t or t.startswith("+"):
                    continue
                target_indices = ticket_to_row.get(t)
                if target_indices:
                    for tgt in target_indices:
                        mask[tgt] = True
    return mask


@st.cache_data(show_spinner=False, ttl=300)
def cached_account(login_id):
    overview, overview_raw = fetch_overview(login_id)
    trades, trades_raw = fetch_all_trades(login_id)
    return overview, overview_raw, trades, trades_raw


@st.cache_data(show_spinner=False, ttl=300)
def cached_payouts(login_id):
    return fetch_payouts(login_id)


@st.cache_data(show_spinner=False, ttl=60)
def cached_leverage_table():
    return load_leverage_table()


def style_table(frame, percent_column):
    """Yellow-fill rows over the threshold (or overlapping such rows),
    and format the numeric columns."""
    highlight_mask = build_highlight_mask(frame)

    def highlight(row):
        i = row.name
        hit = 0 <= i < len(highlight_mask) and highlight_mask[i]
        color = YELLOW if hit else ""
        return [color for _ in row]

    formats = {
        "Lots": "{:,.2f}", "Lot size": "{:,.2f}",
        "Open Price": "{:,.5f}", "Close Price": "{:,.5f}",
        "Profit": "{:,.2f}", "Commission": "{:,.2f}", "Swap": "{:,.2f}",
        "Contract Size": "{:,.0f}", "Contract size": "{:,.0f}",
        "Account Size": "{:,.0f}", "Instrument Leverage": "{:,.0f}",
        "Overlaps": "{:,.0f}",
        "Margin": "{:,.2f}", "Margin %": "{:,.2f}%", "Margin (%)": "{:,.2f}%",
        "Combined Margin": "{:,.2f}", "Combined Margin %": "{:,.2f}%",
    }
    return (frame.style
            .apply(highlight, axis=1)
            .format({k: v for k, v in formats.items() if k in frame.columns}, na_rep="—"))


def csv_export_frame(frame):
    """Return a CSV-ready copy with stable date and percentage formatting."""
    exported = frame.copy()

    for column in ("Open Time", "Close Time"):
        if column not in exported.columns:
            continue
        parsed = pd.to_datetime(exported[column], errors="coerce")
        formatted = parsed.dt.strftime("%d-%m-%Y %H:%M:%S")
        exported[column] = formatted.where(parsed.notna(), exported[column])

    for column in ("Margin %", "Margin (%)", "Combined Margin %"):
        if column in exported.columns:
            exported[column] = exported[column].map(
                lambda value: f"{value:.2f}%" if pd.notna(value) else ""
            )
    
    for column in ("Combined Margin",):
        if column in exported.columns:
            exported[column] = exported[column].map(
                lambda value: f"{value:.2f}" if pd.notna(value) else ""
            )
    return exported


def selectable_table(frame, percent_column, key, filename, height):
    """Render a table with multi-row selection and selected/all CSV downloads."""
    event = st.dataframe(
        style_table(frame, percent_column),
        use_container_width=True,
        height=height,
        hide_index=True,
        on_select="rerun",
        selection_mode="multi-row",
        key=key,
    )

    picked = list(event.selection.rows) if event and event.selection else []
    chosen = frame.iloc[picked] if picked else frame.iloc[0:0]
    csv_chosen = csv_export_frame(chosen)
    csv_all = csv_export_frame(frame)

    left, right, note = st.columns([1, 1, 3])
    left.download_button(
        f"Download selected ({len(chosen)})",
        csv_chosen.to_csv(index=False).encode("utf-8"),
        file_name=f"{filename}_selected.csv",
        mime="text/csv",
        disabled=chosen.empty,
        key=f"{key}_dl_selected",
        use_container_width=True,
    )
    right.download_button(
        f"Download all ({len(frame)})",
        csv_all.to_csv(index=False).encode("utf-8"),
        file_name=f"{filename}.csv",
        mime="text/csv",
        key=f"{key}_dl_all",
        use_container_width=True,
    )
    note.caption("Tick the checkboxes to pick any number of rows, then download just those."
                 if chosen.empty else f"{len(chosen)} row(s) selected.")
    return chosen


# --- load -------------------------------------------------------------------

st.title("📊 High Margin")
st.caption("Enter a login id to pull the account overview and every trade, with a "
           "derived margin per trade.")

login_id = st.text_input("Login ID", placeholder="e.g. 500123").strip()
if not login_id:
    st.info("Enter a login id above to load the account.")
    st.stop()

if st.button("Refresh leverage data", help="Reload the latest values from Google Sheets."):
    cached_leverage_table.clear()

# A missing leverage table is not fatal: the trades and every other column still
# render, with Margin blank and the reason in Notes.
try:
    leverage_table, worksheet_titles = cached_leverage_table()
except LeverageError as exc:
    st.warning(f"Leverage sheet unavailable — Margin cannot be computed. {exc}")
    leverage_table, worksheet_titles = {}, []

try:
    with st.spinner(f"Loading account {login_id}…"):
        overview, overview_raw, trades, trades_raw = cached_account(login_id)
except ApiError as exc:
    st.error(str(exc))
    st.stop()

try:
    payouts, _ = cached_payouts(login_id)
except ApiError as exc:
    payouts = []
    st.warning(f"Payout history unavailable — latest approval date cannot be shown. {exc}")

if not trades:
    st.warning("The all-trades endpoint returned no trade records for this login id.")

plan = fields.pick(overview, fields.PLAN)
account_size = fields.to_float(fields.pick(overview, fields.ACCOUNT_SIZE))
balance = fields.to_float(fields.pick(overview, fields.BALANCE))
equity = fields.to_float(fields.pick(overview, fields.EQUITY))
currency = fields.pick(overview, fields.CURRENCY) or ""
latest_payout = latest_approved_date(payouts)

cols = st.columns(4)
cols[0].metric("Login", fields.pick(overview, fields.LOGIN) or login_id)
cols[1].metric("Plan", plan or "—")
cols[2].metric("Trades", len(trades))
cols[3].metric("Latest Payout Approved Date",
               latest_payout.strftime("%d %b %Y") if latest_payout else "—")

if not plan:
    st.warning("No plan field found in the overview payload, so no leverage tab can be "
               "selected and Margin stays blank. Check the raw payload below and add "
               "the real field name to `PLAN` in fields.py.")

frame = build_rows(trades, plan, leverage_table, DEFAULT_CONTRACT_SIZE)

if account_size:
    frame["Margin %"] = frame["Margin"] / account_size * 100
else:
    frame["Margin %"] = pd.NA
    st.warning("No account size found in the overview payload, so Margin % and the "
               f"{THRESHOLD_PCT:.0f}% highlight cannot be computed.")

# Add overlap detection and combined margin calculation
frame = add_overlap_columns(frame, account_size=account_size)

# Dedupe column list while preserving order — duplicates come from
# COLUMN_ORDER and OVERLAP_COLUMNS sharing some names, and pandas
# frame[[dup, dup]] creates two same-named columns (later indexed as a
# multi-column DataFrame, which breaks every subsequent ["col"] access).
seen, ordered_cols = set(), []
for col in COLUMN_ORDER + OVERLAP_COLUMNS:
    if col in frame.columns and col not in seen:
        ordered_cols.append(col)
        seen.add(col)
frame = frame[ordered_cols]


# --- margin summary ---------------------------------------------------------

n = len(frame)
summary = pd.DataFrame(index=range(n))
for col_in_frame, col_in_summary in [
    ("Ticket", "Trade ID"),
    ("Instrument", "Instrument Name"),
    ("Lots", "Lot size"),
    ("Contract Size", "Contract size"),
    ("Side", "Side"),
    ("Instrument", "Symbols"),
    ("Open Price", "Open Price"),
    ("Open Time", "Open Time"),
    ("Close Time", "Close Time"),
    ("Instrument Leverage", "Instrument Leverage"),
    ("Margin %", "Margin (%)"),
    ("Overlaps", "Overlaps"),
    ("Overlapping Trades", "Overlapping Trades"),
    ("Combined Margin", "Combined Margin"),
    ("Combined Margin %", "Combined Margin %"),
]:
    s = _first_series(frame, col_in_frame)
    values = _as_plain_list(s) if s is not None else (
        [0] * n if col_in_summary == "Overlaps"
        else [""] * n if col_in_summary == "Overlapping Trades"
        else [None] * n
    )
    summary[col_in_summary] = values
summary["Account Size"] = [account_size] * n
summary = summary[SUMMARY_COLUMNS]

flagged = int((frame["Margin %"] > THRESHOLD_PCT).sum()) if account_size else 0

combined_in_frame = None
if "Combined Margin %" in frame.columns:
    _s = _first_series(frame, "Combined Margin %")
    if _s is not None:
        combined_in_frame = _s
combined_above = int((combined_in_frame > THRESHOLD_PCT).sum()) if combined_in_frame is not None and account_size else 0

combined_in_summary = None
if "Combined Margin %" in summary.columns:
    _s2 = _first_series(summary, "Combined Margin %")
    if _s2 is not None:
        combined_in_summary = _s2
summary_combined_above = (int((combined_in_summary > THRESHOLD_PCT).sum())
                         if combined_in_summary is not None and account_size else 0)

_hl = build_highlight_mask(frame)
highlighted_total = sum(1 for _b in _hl if _b)

summary_hl = build_highlight_mask(summary)
summary_highlighted = sum(1 for _b in summary_hl if _b)

st.subheader(
    f"Margin summary — {len(summary)} rows, {flagged} own margin above {THRESHOLD_PCT:.0f}%, "
    f"{summary_combined_above} combined (simultaneous) above {THRESHOLD_PCT:.0f}%"
)
st.caption(
    f"Yellow = own Margin % > {THRESHOLD_PCT:.0f}% OR Combined Margin % > {THRESHOLD_PCT:.0f}% "
    f"OR overlaps any row already over the line. "
    f"{summary_highlighted} row(s) highlighted in total."
)
selectable_table(summary, "Margin (%)", key="summary",
                 filename=f"margin_summary_{login_id}", height=360)

# --- full trades ------------------------------------------------------------

st.subheader(
    f"Trades — {len(frame)} rows, {flagged} own margin above {THRESHOLD_PCT:.0f}%, "
    f"{combined_above} combined (simultaneous) above {THRESHOLD_PCT:.0f}%"
)
st.caption(
    f"Yellow rows = own Margin % > {THRESHOLD_PCT:.0f}% OR Combined Margin % > {THRESHOLD_PCT:.0f}% "
    f"OR overlaps any row already above the threshold. "
    f"{highlighted_total} row(s) highlighted in total."
)
selectable_table(frame, "Margin %", key="trades",
                 filename=f"trades_{login_id}", height=560)

# --- diagnostics ------------------------------------------------------------

unresolved = frame[frame["Margin"].isna()]
if not unresolved.empty:
    with st.expander(f"{len(unresolved)} trades without a margin — why"):
        st.dataframe(unresolved[["Ticket", "Instrument", "Notes"]],
                     use_container_width=True, hide_index=True)

if trades:
    empty_columns = [c for c in ("Instrument", "Lots", "Open Price", "Close Price", "Profit")
                     if c in frame.columns and frame[c].isna().all()]
    with st.expander("Field diagnostics — every key in the first trade record",
                     expanded=bool(empty_columns)):
        if empty_columns:
            st.warning("These columns came back empty for every trade: "
                       + ", ".join(empty_columns)
                       + ". Find the right key below and add it to the matching list "
                         "in fields.py.")
        st.dataframe(
            pd.DataFrame([{"key": k,
                           "value": "••• redacted •••" if fields.is_sensitive(k)
                                    else str(v)[:120]}
                          for k, v in sorted(fields.flatten(trades[0]).items())]),
            use_container_width=True, height=420, hide_index=True,
        )

with st.expander("Raw payloads and leverage sheet"):
    st.caption("Worksheets read: " + (", ".join(worksheet_titles) or "none")
               + " · passwords and IPs are masked.")
    st.write("**Overview**")
    st.json(fields.redact(overview_raw), expanded=False)
    st.write("**All trades** (first record)")
    st.json(fields.redact(trades_raw if not trades else trades[0]), expanded=False)
