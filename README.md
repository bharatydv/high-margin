# High Margin

Streamlit app: enter a login id, get the account overview plus every trade with a
derived **Margin** column, and trades using more than 70% of the account size
shaded yellow.

## Data flow

1. `GET https://fdlapiprod.fpfxtech.io/get/account/overview/{login_id}` — the plan
   and the account size.
2. `GET https://fdlapiprod.fpfxtech.io/get/account/all-trades/{login_id}` — every trade.
3. Google Sheet `12xwxKQkvHpK_iIg1Mc7CtC3p-LoTOfeV4xDf_jIxOuI` — `instrument_leverage`
   keyed by **plan** and **instrument**.

## Margin

```
margin = (open_price * lots * contract_size / instrument_leverage)
         * ( |profit| / |(open_price - close_price) * lots * contract_size| )
```

The first factor is notional exposure over instrument leverage, in the
instrument's quote currency. The second is the implied quote → account currency
rate, backed out of the trade's own realised profit. A trade with no price move
or zero profit gives no usable rate, so those rows show a blank margin and the
reason in **Notes** rather than a wrong number.

## Concurrent exposure

Margin is per trade, but risk is not: two trades that were live at the same time
put both margins on the account at once. Trades are matched on **time overlap**,
not on whether one opened inside another's window:

```
A and B overlap  <=>  open_b < close_a  AND  close_b > open_a
```

That one test covers every arrangement — B inside A, B opening inside A and
closing after it, B opening before A and closing inside it, and B covering A
entirely. It also catches the case the naive "did B open between A's open and
close?" check misses: a B that opened *before* A and was still running when A
opened is simultaneous exposure, even though its open time sits outside A's
window.

Comparisons are strict, so trades that merely touch (B closes at the instant A
opens) are not concurrent — there is no moment at which both are live.

| column | meaning |
|---|---|
| `Overlaps` | how many other trades were live at the same time |
| `Overlapping Trades` | their tickets |
| `Combined Margin` | this trade's margin plus the margin of every trade overlapping it |
| `Combined Margin %` | combined margin over account size |

A trade that overlaps nothing gets a **blank** combined margin rather than a copy
of its own: with no simultaneous exposure there is nothing to combine, and a
number there would read as though there were.

The **Concurrent exposure** section also reports the **peak simultaneous margin** —
the single worst instant, found by sweeping every open and close in order. This
is not the same as the largest `Combined Margin`: if B and C both overlap A but
never each other, A's row sums all three, while the true peak is only ever two
trades at once.

Edge cases are noted per row rather than guessed at: a trade with no close time
is treated as still open (so it overlaps everything after it), one with no open
time is left off the timeline, and an overlapping trade with no margin is
excluded from the sum with the row marked as partial.

## Setup

```powershell
pip install -r requirements.txt
```

**Google Sheets access** — the sheet is private, so it is read with a service account:

1. Google Cloud Console → create a project → enable the **Google Sheets API**.
2. Create a **service account**, then a **JSON key** for it. Save it as
   `credentials.json` in this folder (or point `GOOGLE_APPLICATION_CREDENTIALS`
   at it, or type the path in the app sidebar).
3. Open the sheet → **Share** → paste the service account's `client_email`
   → **Viewer**. Without this step the API returns 403.

```powershell
streamlit run app.py
```

## Sheet layout and plan routing

The live sheet has one tab per plan family — **Prime**, **Pro**, **Direct** —
each with `Instrument Name | Asset Class | Leverage` and 77 instruments.

The account's plan selects the tab: a plan of `2-Step Prime Funded` reads the
**Prime** tab only. Matching is on whole tokens first, then longest substring, so
`Prime` never loses to a shorter accidental hit. Leverage is then keyed on
`Instrument Name`, matched against the trade's `stdSymbol`.

**Lookup never falls back across plans.** The same instrument carries different
leverage per plan (AUDCAD is 50 on Prime and Direct, 100 on Pro), so borrowing
another plan's row would yield a wrong margin rather than a missing one. If the
plan cannot be resolved to a tab, the row gets no margin and the Notes column
says why.

Two other layouts are also handled, in case the sheet is restructured:

| shape | looks like |
|---|---|
| long | `Plan \| Instrument \| Leverage` |
| wide | `Instrument \| Pro 100K \| Evaluation 50K` — plan from the column header |

Leverage cells may be `1:100` or `100`. Broker suffixes (`EURUSD.r`, `EURUSDm`)
match a plain `EURUSD` row. Unmatched instruments are listed in the "trades
without a margin" expander.

## Highlighting

`Margin %` = margin ÷ account size × 100. Rows above the sidebar threshold
(default 70%) are filled yellow. The basis and threshold are both adjustable in
the sidebar; use **Manual value** if the overview payload carries no account size.

## Adjusting field names

The FPFX payload field names were not available when this was written, so every
read goes through candidate lists in `fields.py` (matched case- and
punctuation-insensitively, nested paths included). If a column comes out empty,
open **Raw payloads** at the bottom of the app, find the real key, and add it to
the matching list in `fields.py`.

## Tests

```powershell
python test_smoke.py
```

Covers the margin formula (including a cross-currency case and every guard),
field extraction, payload unwrapping, and all three sheet layouts.
