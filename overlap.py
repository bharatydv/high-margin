"""Concurrent exposure — which trades were live at the same time, and what their
margins add up to while they overlap.

Two trades overlap when each one opens before the other closes:

    open_b < close_a  AND  close_b > open_a

That single test covers every arrangement: B sitting inside A, B straddling
either end of A, and B covering A entirely. It is also the test that gets right
the case a naive "did B open between A's open and close?" check gets wrong — a B
that opened *before* A and was still running when A opened is concurrent
exposure, even though its open time falls outside A's window.

The comparisons are strict, so trades that merely touch — B closing at the exact
instant A opens — are not concurrent: there is no moment at which both are live.

No Streamlit here, so it is importable and testable on its own.
"""

import numpy as np
import pandas as pd

import fields
from transform import as_datetime

OVERLAP_COLUMNS = ["Overlaps", "Overlapping Trades", "Combined Margin", "Combined Margin %"]

# A trade with no close time is still running. Nothing outlives Timestamp.max,
# so it overlaps everything opened after it, which is the intent.
_STILL_OPEN = pd.Timestamp.max
_TICKET_PREVIEW = 8


def _as_timestamp(value):
    """Anything the payload might carry -> naive Timestamp, or NaT for 'not set'."""
    if value is None:
        return pd.NaT
    if isinstance(value, str) and not value.strip():
        return pd.NaT
    # A close time of 0 (or negative) is the feed saying "not set" for a trade
    # that is still open. Read literally it is 1970, which would hand the trade a
    # decades-long window that overlaps nearly everything.
    number = None if isinstance(value, pd.Timestamp) else fields.to_float(value)
    if number is not None and number <= 0:
        return pd.NaT
    stamp = pd.to_datetime(as_datetime(value), errors="coerce")
    if isinstance(stamp, pd.Timestamp) and stamp.tzinfo is not None:
        stamp = stamp.tz_convert("UTC").tz_localize(None)
    return stamp


def _interval(open_value, close_value):
    """(start, end) for a trade's live window, or (None, None) if it cannot be placed."""
    start = _as_timestamp(open_value)
    if pd.isna(start):
        return None, None
    end = _as_timestamp(close_value)
    if pd.isna(end):
        end = _STILL_OPEN
    if end < start:      # reversed timestamps: the window is still the span between them
        start, end = end, start
    return start, end


def overlaps(open_a, close_a, open_b, close_b):
    """True when A and B were both live at some shared instant."""
    start_a, end_a = _interval(open_a, close_a)
    start_b, end_b = _interval(open_b, close_b)
    if start_a is None or start_b is None:
        return False
    return start_b < end_a and end_b > start_a


def _series(frame, name, default=None):
    """A column if present, else a same-length column of `default`."""
    if name in frame.columns:
        return frame[name]
    return pd.Series([default] * len(frame), index=frame.index)


def _intervals(frame):
    """[(start, end)] per row, start None where the trade cannot be placed."""
    return [_interval(open_value, close_value)
            for open_value, close_value in zip(_series(frame, "Open Time"),
                                               _series(frame, "Close Time"))]


def _format_tickets(values):
    shown = [str(v) for v in values[:_TICKET_PREVIEW]]
    extra = len(values) - len(shown)
    return ", ".join(shown) + (f" +{extra} more" if extra > 0 else "")


def format_gap(delta):
    """A Timedelta as a short human phrase."""
    seconds = int(abs(delta).total_seconds())
    if seconds < 60:
        return f"{seconds} second{'' if seconds == 1 else 's'}"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def add_overlap_columns(frame, account_size=None):
    """Add Overlaps / Overlapping Trades / Combined Margin / Combined Margin %.

    Combined Margin is a trade's own margin plus the margin of every trade live
    at the same time — the exposure the account actually carried while that
    trade was open. A trade that overlaps nothing is left blank rather than set
    to its own margin: with no simultaneous exposure there is nothing to
    combine, and a number there would read as though there were.
    """
    frame = frame.copy()
    count = len(frame)
    if count == 0:
        frame["Overlaps"] = pd.Series(dtype="int64")
        frame["Overlapping Trades"] = pd.Series(dtype="object")
        frame["Combined Margin"] = pd.Series(dtype="float64")
        frame["Combined Margin %"] = pd.Series(dtype="float64")
        return frame

    notes = [[] for _ in range(count)]
    starts, ends = [], []
    for position, (start, end) in enumerate(_intervals(frame)):
        if start is None:
            notes[position].append("no open time — not placed on the concurrency timeline")
        elif end == _STILL_OPEN:
            notes[position].append("no close time — treated as still open")
        elif end == start:
            notes[position].append("opens and closes at the same instant — overlaps nothing")
        starts.append(start)
        ends.append(end)

    start_array = pd.to_datetime(pd.Series(starts, dtype="object")).to_numpy(dtype="datetime64[ns]")
    end_array = pd.to_datetime(pd.Series(ends, dtype="object")).to_numpy(dtype="datetime64[ns]")
    placed = ~pd.isna(start_array)

    margins = pd.to_numeric(_series(frame, "Margin"), errors="coerce").to_numpy(dtype="float64")
    tickets = ["" if pd.isna(t) else t for t in _series(frame, "Ticket", "")]

    counts = np.zeros(count, dtype="int64")
    combined = np.full(count, np.nan)
    peers = [""] * count

    for position in range(count):
        if not placed[position]:
            continue
        # the overlap test itself, run against every other trade at once
        mask = placed & (start_array < end_array[position]) & (end_array > start_array[position])
        mask[position] = False
        found = np.flatnonzero(mask)
        counts[position] = found.size
        if found.size == 0:
            continue
        peers[position] = _format_tickets([tickets[i] for i in found])
        peer_margins = margins[found]
        known = ~np.isnan(peer_margins)
        unknown = int((~known).sum())
        if unknown:
            notes[position].append(
                f"{unknown} overlapping trade(s) have no margin — combined margin is partial")
        if not np.isnan(margins[position]):
            combined[position] = margins[position] + peer_margins[known].sum()

    frame["Overlaps"] = counts
    frame["Overlapping Trades"] = peers
    frame["Combined Margin"] = combined
    frame["Combined Margin %"] = combined / account_size * 100 if account_size else pd.NA

    if "Notes" in frame.columns:
        existing = frame["Notes"].fillna("").astype(str)
        frame["Notes"] = ["; ".join(([old] if old else []) + new)
                          for old, new in zip(existing, notes)]
    return frame


def peak_concurrent(frame):
    """The single worst moment: the largest sum of margins live at the same instant.

    {'margin', 'time', 'tickets', 'unknown'} — 'unknown' counts trades live at
    that moment carrying no margin, so the peak is a floor, not a cap.
    """
    empty = {"margin": None, "time": None, "tickets": [], "unknown": 0}
    if frame.empty:
        return empty

    margins = pd.to_numeric(_series(frame, "Margin"), errors="coerce")
    tickets = _series(frame, "Ticket", "")

    events = []
    for position, (start, end) in enumerate(_intervals(frame)):
        if start is None or end == start:
            continue
        # closes sort before opens at the same instant, so a trade closing exactly
        # as another opens is never counted as concurrent
        events.append((end, 0, position))
        events.append((start, 1, position))
    if not events:
        return empty
    events.sort(key=lambda event: (event[0], event[1]))

    live, running, best = set(), 0.0, dict(empty)
    for moment, is_open, position in events:
        value = margins.iloc[position]
        if is_open:
            live.add(position)
            if pd.notna(value):
                running += float(value)
            if best["margin"] is None or running > best["margin"]:
                best = {"margin": running,
                        "time": moment,
                        "tickets": [tickets.iloc[i] for i in sorted(live)],
                        "unknown": sum(1 for i in live if pd.isna(margins.iloc[i]))}
        else:
            live.discard(position)
            if pd.notna(value):
                running -= float(value)
    return best


def closest_gap(frame):
    """(gap, earlier_ticket, later_ticket) for the two trades that came nearest to
    overlapping, or None if fewer than two trades can be placed.

    Only interesting when nothing overlaps at all: it is the evidence that a
    column of blank combined margins is the account's own shape — a strictly
    one-at-a-time trader — rather than a calculation that failed to run.
    """
    tickets = _series(frame, "Ticket", "")
    placed = [(start, end, tickets.iloc[position])
              for position, (start, end) in enumerate(_intervals(frame))
              if start is not None]
    if len(placed) < 2:
        return None

    placed.sort(key=lambda item: item[0])
    best = None
    reach, holder = placed[0][1], placed[0][2]
    for start, end, ticket in placed[1:]:
        gap = start - reach
        if best is None or gap < best[0]:
            best = (gap, holder, ticket)
        if end > reach:
            reach, holder = end, ticket
    return best
