"""Turn raw trade records into the display table. No Streamlit here, so it is
importable and testable on its own."""

import pandas as pd

import fields
from leverage_sheet import lookup_entry, lookup_leverage
from margin import compute_margin

COLUMN_ORDER = ["Ticket", "Instrument", "Asset Class", "Side", "Lots",
                "Open Price", "Close Price", "Open Time", "Close Time",
                "Profit", "Commission", "Swap", "Plan", "Contract Size",
                "Instrument Leverage", "Margin", "Margin %", "Overlapping Trades",
                "Combined Margin", "Notes"]

_EPOCH_MIN = 946_684_800      # 2000-01-01
_EPOCH_MAX = 4_102_444_800    # 2100-01-01


def as_datetime(value):
    """Convert epoch seconds/ms/us or ISO format strings to naive UTC timestamp.
    
    Handles:
    - Epoch integers (seconds, milliseconds, microseconds)
    - ISO format strings (2026-09-01T10:00:00.000)
    - Non-datetime values pass through as-is
    """
    if value is None:
        return value
    
    # Try converting as string first (ISO format)
    if isinstance(value, str):
        try:
            return pd.to_datetime(value, utc=True).tz_localize(None)
        except (ValueError, TypeError):
            pass  # Not a valid ISO format, try as epoch
    
    # Try converting as numeric epoch
    number = fields.to_float(value)
    if number is None or number <= 0:
        return value
    
    # Integer units, not float division, so 1785281282460 stays .460 and does not
    # pick up binary-float noise.
    for unit, scale in (("us", 1_000_000), ("ms", 1_000), ("s", 1)):
        if _EPOCH_MIN * scale <= number <= _EPOCH_MAX * scale:
            return pd.to_datetime(int(number), unit=unit, utc=True).tz_localize(None)
    
    return value


def is_time_overlap(open_time_1, close_time_1, open_time_2, close_time_2):
    """Check if two trades overlap in time.
    
    Returns True if there is ANY overlap between the two time windows.
    
    Formula: Overlap = (Open B < Close A) AND (Close B > Open A)
    
    This correctly handles ALL cases:
    - Case 1: B opens and closes inside A → (10:15 < 11:00) AND (10:45 > 10:00) ✓
    - Case 2: B opens inside A but closes after → (10:30 < 11:00) AND (11:30 > 10:00) ✓
    - Case 3: B opens before A but closes inside → (10:00 < 11:30) AND (11:00 > 10:30) ✓
    - Case 4: B completely covers A → (10:00 < 11:00) AND (12:00 > 10:30) ✓
    - Case 5: No overlap → (10:40 < 10:30) ✗ (fails first condition)
    
    Non-datetime values are skipped.
    """
    # Skip if either trade lacks time data
    if not isinstance(open_time_1, pd.Timestamp) or not isinstance(close_time_1, pd.Timestamp):
        return False
    if not isinstance(open_time_2, pd.Timestamp) or not isinstance(close_time_2, pd.Timestamp):
        return False
    
    # Trades overlap if:
    # - Trade B opens before Trade A closes: open_time_2 < close_time_1
    # - Trade B closes after Trade A opens: close_time_2 > open_time_1
    # This condition is symmetric and handles all partial overlaps
    overlap = open_time_2 < close_time_1 and close_time_2 > open_time_1
    
    return overlap


def get_overlap_type(open_time_1, close_time_1, open_time_2, close_time_2):
    """Classify the type of overlap between two trades.
    
    Returns one of:
    - "fully_contained": Trade 2 fully opens and closes during Trade 1
    - "partial_open": Trade 2 opens during Trade 1 but closes after
    - "partial_close": Trade 2 opens before Trade 1 but closes during
    - "crossing": Trade 2 partially overlaps on both sides
    - "none": No overlap
    """
    if not is_time_overlap(open_time_1, close_time_1, open_time_2, close_time_2):
        return "none"
    
    opens_inside = open_time_1 <= open_time_2 <= close_time_1
    closes_inside = open_time_1 <= close_time_2 <= close_time_1
    
    if opens_inside and closes_inside:
        return "fully_contained"
    elif opens_inside and not closes_inside:
        return "partial_open"
    elif not opens_inside and closes_inside:
        return "partial_close"
    else:
        return "crossing"


def find_overlapping_trades(row_index, rows_data):
    """Find all trades that overlap with the trade at row_index.
    
    Returns a list of indices of overlapping trades (excluding row_index itself).
    """
    if not rows_data or row_index >= len(rows_data):
        return []
    
    current_row = rows_data[row_index]
    open_time_1 = current_row["Open Time"]
    close_time_1 = current_row["Close Time"]
    
    overlapping = []
    for i, other_row in enumerate(rows_data):
        if i == row_index:
            continue
        
        open_time_2 = other_row["Open Time"]
        close_time_2 = other_row["Close Time"]
        
        if is_time_overlap(open_time_1, close_time_1, open_time_2, close_time_2):
            overlapping.append(i)
    
    return overlapping


def calculate_combined_margin(row_indices, rows_data):
    """Calculate combined margin for overlapping trades.
    
    Sums the margins of all trades in the group.
    Skips trades with no margin (None values).
    """
    combined = 0.0
    for idx in row_indices:
        if idx < len(rows_data):
            margin = rows_data[idx].get("Margin")
            if margin is not None and isinstance(margin, (int, float)):
                combined += margin
    
    return combined if combined > 0 else None


def annotate_overlapping_trades(rows_data):
    """Add overlap information and combined margin to rows.
    
    Modifies rows in-place to add:
    - "Overlapping Trades": comma-separated list of overlapping trade tickets
    - "Combined Margin": sum of margins for overlapping trades
    - Updated Notes with overlap details
    
    Works directly with the rows list without sorting.
    """
    # Initialize all rows with empty overlap fields
    for row in rows_data:
        row["Overlapping Trades"] = ""
        row["Combined Margin"] = None
    
    # For each row, find overlaps with other rows
    for i, current_row in enumerate(rows_data):
        current_open = current_row.get("Open Time")
        current_close = current_row.get("Close Time")
        
        # Skip if current trade lacks time data
        if not isinstance(current_open, pd.Timestamp) or not isinstance(current_close, pd.Timestamp):
            continue
        
        overlapping_indices = []
        
        # Check all other trades for overlap
        for j, other_row in enumerate(rows_data):
            if i == j:
                continue
            
            other_open = other_row.get("Open Time")
            other_close = other_row.get("Close Time")
            
            # Check if times overlap
            if is_time_overlap(current_open, current_close, other_open, other_close):
                overlapping_indices.append(j)
        
        # If overlaps found, update the row
        if overlapping_indices:
            # Build ticket list
            overlapping_tickets = []
            for j in overlapping_indices:
                ticket = rows_data[j].get("Ticket")
                if ticket:
                    overlapping_tickets.append(str(ticket))
            
            current_row["Overlapping Trades"] = ", ".join(overlapping_tickets)
            
            # Calculate combined margin (include current trade + all overlapping)
            all_indices = [i] + overlapping_indices
            combined_margin = calculate_combined_margin(all_indices, rows_data)
            current_row["Combined Margin"] = combined_margin
            
            # Add overlap details to notes
            overlap_notes = []
            for j in overlapping_indices:
                other_row = rows_data[j]
                other_ticket = other_row.get("Ticket", "unknown")
                other_open = other_row.get("Open Time")
                other_close = other_row.get("Close Time")
                
                overlap_type = get_overlap_type(current_open, current_close, other_open, other_close)
                overlap_notes.append(f"Overlaps with #{other_ticket} ({overlap_type})")
            
            if overlap_notes:
                current_notes = current_row.get("Notes", "")
                if current_notes:
                    current_row["Notes"] = current_notes + "; " + "; ".join(overlap_notes)
                else:
                    current_row["Notes"] = "; ".join(overlap_notes)


def build_rows(trades, plan, leverage_table, fallback_contract_size):
    rows = []
    for trade in trades:
        record = fields.key_map(trade)
        record["__normalised__"] = True

        instrument = fields.pick(record, fields.SYMBOL)
        instrument_note = ""
        if not instrument:
            instrument, source_key = fields.guess_symbol(trade)
            if instrument:
                instrument_note = f"instrument inferred from '{source_key}'"

        open_price = fields.to_float(fields.pick(record, fields.OPEN_PRICE))
        close_price = fields.to_float(fields.pick(record, fields.CLOSE_PRICE))
        lots = fields.to_float(fields.pick(record, fields.LOTS))
        profit = fields.to_float(fields.pick(record, fields.PROFIT))

        contract_size = fields.to_float(fields.pick(record, fields.CONTRACT_SIZE))
        contract_source = "trade"
        if contract_size is None:
            contract_size = fallback_contract_size
            contract_source = "default"

        trade_plan = fields.pick(record, fields.PLAN) or plan
        leverage, leverage_note = lookup_leverage(leverage_table, trade_plan, instrument)
        entry = lookup_entry(leverage_table, trade_plan, instrument)

        margin, reason = compute_margin(open_price, close_price, lots,
                                        contract_size, leverage, profit)

        notes = [n for n in (
            reason,
            instrument_note,
            leverage_note if leverage is None else "",
            "contract size defaulted" if contract_source == "default" else "",
        ) if n]

        rows.append({
            "Ticket": fields.pick(record, fields.TICKET),
            "Instrument": instrument,
            "Asset Class": entry.asset_class if entry else "",
            "Side": fields.pick(record, fields.SIDE),
            "Lots": lots,
            "Open Price": open_price,
            "Close Price": close_price,
            "Open Time": as_datetime(fields.pick(record, fields.OPEN_TIME)),
            "Close Time": as_datetime(fields.pick(record, fields.CLOSE_TIME)),
            "Profit": profit,
            "Commission": fields.to_float(fields.pick(record, fields.COMMISSION)),
            "Swap": fields.to_float(fields.pick(record, fields.SWAP)),
            "Plan": trade_plan,
            "Contract Size": contract_size,
            "Instrument Leverage": leverage,
            "Margin": margin,
            "Notes": "; ".join(notes),
        })
    
    # Annotate overlapping trades and combined margins
    annotate_overlapping_trades(rows)
    
    # Create DataFrame
    df = pd.DataFrame(rows)
    
    # Sort by Open Time (ascending, oldest first)
    df = df.sort_values("Open Time", na_position="last").reset_index(drop=True)
    
    return df
