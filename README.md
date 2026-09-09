Here's the exact change to `subAgent/product_code_agent/tools.py`. Only the `_rows` function is modified; everything else is added new.

**Replace your existing `_rows` function with this:**

```python
def _rows(sql):
    """Run SQL and return a list of plain dicts (all values stringified).

    Also records the bytes billed for the query so per-run BigQuery cost can
    be measured. Prints one line per query and accumulates a module total.
    """
    out = []
    job = _bq().query(sql)
    for row in job:
        out.append({k: ("" if v is None else str(v)) for k, v in dict(row).items()})
    # ---- cost metrics: bytes billed for this query ----
    try:
        billed = getattr(job, "total_bytes_billed", None) or 0
        processed = getattr(job, "total_bytes_processed", None) or 0
        global _BQ_BYTES_TOTAL, _BQ_QUERY_COUNT
        _BQ_BYTES_TOTAL += int(billed)
        _BQ_QUERY_COUNT += 1
        print("[cost][bq] query #%d billed=%.2f MB processed=%.2f MB (run total %.2f MB)"
              % (_BQ_QUERY_COUNT, billed / 1048576.0, processed / 1048576.0,
                 _BQ_BYTES_TOTAL / 1048576.0))
    except Exception:  # noqa: BLE001 - metrics never break a query
        pass
    return out
```

**Then add these three new items** anywhere after it (e.g. right below):

```python
# Running totals for BigQuery cost metrics (reset per run via bq_metrics_reset).
_BQ_BYTES_TOTAL = 0
_BQ_QUERY_COUNT = 0


def bq_metrics_reset():
    """Reset the BigQuery byte counters at the start of a run."""
    global _BQ_BYTES_TOTAL, _BQ_QUERY_COUNT
    _BQ_BYTES_TOTAL = 0
    _BQ_QUERY_COUNT = 0


def bq_metrics_get():
    """Return (bytes_billed, query_count) accumulated since the last reset."""
    return _BQ_BYTES_TOTAL, _BQ_QUERY_COUNT
```

That's the whole change to that file.

**The only real difference in `_rows`:** it used to iterate `_bq().query(sql)` directly and throw away the job object. Now it keeps the job in a variable (`job = _bq().query(sql)`) so it can read `total_bytes_billed` after iterating. Functionally identical for callers — same return value, same behaviour — just with the byte accounting added.

Two notes:
- If your colleagues renamed `_rows` or `_bq`, adapt those names accordingly; the pattern is just "keep the job object, read `total_bytes_billed` from it."
- The metrics are in a `try/except`, so if anything about the job object differs in their version, queries still work normally — you'd just lose the byte print.


-------


Here are the exact changes to `agent_api.py`. Four insertion points.

---

### 1. Add the metrics store + helpers

Find `RESULTS: dict = {}` and add this **right after it**:

```python
# ── Per-run cost metrics ──────────────────────────────────────────────────
# Captures the cost drivers for one opportunity run so you can compute cost
# per run: duration, model tokens (in/out), and BigQuery bytes read/written.
# Printed as a single summary line at the end of /process.
METRICS: dict = {}


def _metrics_init(opp_id: str):
    import time as _t
    METRICS[opp_id] = {
        "start": _t.time(),
        "duration_sec": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "bq_bytes_read": 0,
        "bq_queries": 0,
        "bq_bytes_written": 0,
        "bq_rows_written": 0,
    }


def _metrics_add_tokens(opp_id: str, prompt: int, candidates: int, total: int):
    m = METRICS.get(opp_id)
    if not m:
        return
    m["input_tokens"] += int(prompt or 0)
    m["output_tokens"] += int(candidates or 0)
    m["total_tokens"] += int(total or (prompt or 0) + (candidates or 0))


def _metrics_add_bq_write(opp_id: str, rows: int, approx_bytes: int = 0):
    m = METRICS.get(opp_id)
    if not m:
        return
    m["bq_rows_written"] += int(rows or 0)
    m["bq_bytes_written"] += int(approx_bytes or 0)


def _metrics_finish_and_print(opp_id: str):
    """Print one summary line with everything needed to price the run."""
    import time as _t
    m = METRICS.get(opp_id)
    if not m:
        return {}
    m["duration_sec"] = round(_t.time() - m["start"], 2)
    mb_read = m["bq_bytes_read"] / (1024 * 1024)
    print(
        "[cost] opp=%s | duration=%.2fs | tokens in=%d out=%d total=%d | "
        "bq_queries=%d bq_read=%.2f MB | bq_rows_written=%d (~%.2f KB)"
        % (opp_id, m["duration_sec"], m["input_tokens"], m["output_tokens"],
           m["total_tokens"], m["bq_queries"], mb_read,
           m["bq_rows_written"], m["bq_bytes_written"] / 1024.0)
    )
    return m
```

---

### 2. Capture tokens in the ADK event loop

In `_run_agent`, inside the `async for _event in _runner.run_async(...)` loop, add this **at the end of the loop body** (after the existing `_progress_mark` logic):

```python
            # ---- cost metrics: accumulate model token usage per run ----
            try:
                um = getattr(_event, "usage_metadata", None)
                if um is not None:
                    _metrics_add_tokens(
                        opp_id,
                        getattr(um, "prompt_token_count", 0) or 0,
                        getattr(um, "candidates_token_count", 0) or 0,
                        getattr(um, "total_token_count", 0) or 0,
                    )
            except Exception:  # noqa: BLE001 - metrics never break a run
                pass
```

---

### 3. Initialise metrics at the start of `/process`

Find `_progress_init(opp_id)` in the `process` function and add **after it**:

```python
    _metrics_init(opp_id)
    try:
        from vf_quote_to_order_agent_configured.subAgent.product_code_agent.tools import (  # type: ignore
            bq_metrics_reset,
        )
        bq_metrics_reset()
    except Exception:  # noqa: BLE001
        pass
```

---

### 4. Collect and print at the end of `/process`

Find these two lines near the end of `process`:
```python
    RESULTS[opp_id] = result
    _progress_finish(opp_id, ok=(result.get("status") != "error"))
```

Add this **immediately after them** (before `return result`):

```python
    # Record the sales-order rows this run produced.
    try:
        _rows_out = (result.get("order") or {}).get("line_items") or []
        _approx = len(json.dumps(_rows_out)) if _rows_out else 0
        _metrics_add_bq_write(opp_id, len(_rows_out), _approx)
    except Exception:  # noqa: BLE001
        pass
    # Pull BigQuery bytes from the navigator's counters.
    try:
        from vf_quote_to_order_agent_configured.subAgent.product_code_agent.tools import (  # type: ignore
            bq_metrics_get,
        )
        _bytes, _qcount = bq_metrics_get()
        m = METRICS.get(opp_id)
        if m:
            m["bq_bytes_read"] = _bytes
            m["bq_queries"] = _qcount
    except Exception:  # noqa: BLE001
        pass
    _metrics_finish_and_print(opp_id)
```

---

That's all four. Everything is additive — no existing logic changes, and every block is wrapped in `try/except` so metrics can't break a run.

One dependency worth noting: **steps 3 and 4 import `bq_metrics_reset` / `bq_metrics_get` from the navigator's tools.py** — the functions from the previous message. If those aren't added there, the imports fail silently (caught by the `except`) and you'll still get duration and tokens, just `bq_read=0.00 MB`. So both files need their changes for the BigQuery number to appear.



