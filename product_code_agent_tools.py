"""
Product Code Navigator - BigQuery lookup tool.

Uses the SQL queries EXACTLY as provided (hardcoded full table references and
exact column names, including trailing spaces). Does NOT reuse helpers from the
main Agent and does NOT rebuild table names from BQ_DATASET.

Inputs (from the tech spec):
  * Product Stock Code
  * Item (stock) Description

Search order (stop at first hit):
  1. TEMP_PM  by Supplier_Part_Number LIKE %Product Stock Code%
  2. TEMP_PM  by Supplier_Part_Number LIKE %Item (stock) Description%
  3. TEMP_item_model by `producer_code ` LIKE %Product Stock Code%
     (or, if no stock code) by `name ` LIKE %Item (stock) Description%

If a query returns many rows, choose the row most similar to the
(code, description). Confidence:
  - exact match in TEMP_PM         -> High
  - exact match in TEMP_item_model -> Medium
  - only a similar (LIKE) match    -> Low
Recency: keep matches whose item_model_id is within the last 2 years, per
raw_item_model (item_model_id + delivery_date). Best effort.

Returns "code - name/desc" plus confidence.
"""

import difflib
import os
from datetime import datetime, timedelta

from google.cloud import bigquery
from google.adk.tools import ToolContext

# Project to bill/run the queries against (NOT used to build table names -
# the dataset+table are hardcoded in the SQL exactly as provided).
BQ_QUERY_PROJECT = os.getenv("BQ_PROJECT", "vf-ie-aib-prd-iei-cfa-lab")
RECENCY_YEARS = 2

_client = None


def _bq():
    global _client
    if _client is None:
        _client = bigquery.Client(project=BQ_QUERY_PROJECT)
    return _client


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


def _norm(s):
    return str(s or "").strip().upper()


def _esc(s):
    """Escape a value for inline use in a LIKE literal (single quotes)."""
    return str(s or "").replace("\\", "\\\\").replace("'", "\\'")


def _similar(a, b):
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _best_row(rows, code, desc):
    def score(r):
        blob = " ".join(str(v) for v in r.values())
        return max(_similar(blob, code), _similar(blob, desc))
    return max(rows, key=score) if rows else None


def _has_exact(rows, code, desc, fields):
    tc, td = _norm(code), _norm(desc)
    for r in rows:
        for f in fields:
            v = _norm(r.get(f))
            if v and (v == tc or (td and v == td)):
                return True
    return False


_RECENT_IDS_CACHE = {"loaded": False, "value": None}


def _recent_item_model_ids():
    """Set of item_model_id within the last 2 years, from raw_item_model.
    Returns None if it can't be read (so we don't hard-fail).

    CACHED for the life of the process: this scans a whole table, and the tool
    is called once per product line, so without caching a 10-line tech spec
    would scan raw_item_model 10 times. We fetch it once and reuse.
    """
    if _RECENT_IDS_CACHE["loaded"]:
        return _RECENT_IDS_CACHE["value"]

    sql = ("SELECT item_model_id, delivery_date FROM "
           "`vf-ie-datahub.vfie_dh_lake_customer_complex_fixed_s.raw_item_model`")
    try:
        rows = _rows(sql)
    except Exception as exc:  # noqa: BLE001
        print(f"[PCNAV] recency table unreadable, skipping recency: {exc}")
        _RECENT_IDS_CACHE.update(loaded=True, value=None)
        return None
    cutoff = datetime.utcnow() - timedelta(days=365 * RECENCY_YEARS)
    recent = set()
    for r in rows:
        mid, dd = r.get("item_model_id"), r.get("delivery_date")
        if not mid or not dd:
            continue
        parsed = None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%m/%d/%Y",
                    "%Y-%m-%d %H:%M:%S", "%d-%m-%Y"):
            try:
                parsed = datetime.strptime(str(dd).strip()[:19], fmt)
                break
            except ValueError:
                continue
        if parsed is None or parsed >= cutoff:
            recent.add(str(mid))  # unparseable -> keep (don't silently drop)
    _RECENT_IDS_CACHE.update(loaded=True, value=recent)
    print(f"[PCNAV] recency set cached: {len(recent)} recent item_model_ids")
    return recent


def _apply_recency(rows, recent_ids):
    if recent_ids is None or not rows:
        return rows, False
    id_field = next((c for c in ("item_model_id", "item_model_ID", "id")
                     if c in rows[0]), None)
    if not id_field:
        return rows, False
    kept = [r for r in rows if str(r.get(id_field)) in recent_ids]
    return (kept if kept else rows), True


def _confidence_reason(confidence, source, considered):
    """Human-readable explanation of why this confidence was assigned."""
    if confidence == "High":
        return f"Exact match found in Price Manager (TEMP_PM)."
    if confidence == "Medium":
        return f"Exact match found in item model catalogue (TEMP_item_model)."
    if confidence == "Low":
        where = source or "the catalogue"
        return (f"No exact match; closest similar record chosen from {where}"
                + (f" out of {considered} candidates." if considered else "."))
    return "No matching product code found in Price Manager or item model catalogue."


def _result(code, name, confidence, source, recency_checked, considered):
    return {
        "status": "success",
        "resolved": f"{code} - {name}",   # always code - name/desc
        "code": code,
        "name": name,
        "confidence": confidence,          # High | Medium | Low | None
        "confidence_reason": _confidence_reason(confidence, source, considered),
        "source_table": source,
        "recency_checked": recency_checked,
        "candidates_considered": considered,
    }


def resolve_product_code(product_stock_code: str, description: str,
                         tool_context: ToolContext) -> dict:
    """Resolve a tech-spec product to its final BSP item code ("code - name/desc")
    with a confidence level, using the fixed BigQuery queries.

    Args:
        product_stock_code: Product Stock Code from the tech spec ("" if none).
        description: Item (stock) Description from the tech spec.
    """
    code = (product_stock_code or "").strip()
    desc = (description or "").strip()
    recent = _recent_item_model_ids()

    # ---- 1. TEMP_PM by Supplier_Part_Number LIKE %Product Stock Code% ----
    if code:
        sql = ("select * from "
               "`vf-ie-datahub.vfie_dh_lake_customer_complex_fixed_s.TEMP_PM` "
               f"WHERE UPPER(Supplier_Part_Number) LIKE '%{_esc(code).upper()}%'")
        try:
            rows = _rows(sql)
        except Exception as exc:  # noqa: BLE001
            print(f"[PCNAV ERROR] TEMP_PM(code): {type(exc).__name__}: {exc}")
            return {"status": "error", "message": f"{type(exc).__name__}: {exc}",
                    "resolved": f"{code} - {desc}", "confidence": "None"}
        rows, checked = _apply_recency(rows, recent)
        if rows:
            best = _best_row(rows, code, desc)
            exact = _has_exact(rows, code, desc,
                               ["Supplier_Part_Number", "Material_Description"])
            c = best.get("Supplier_Part_Number") or code
            n = best.get("Material_Description") or desc
            return _result(c, n, "High" if exact else "Low", "TEMP_PM",
                           checked, len(rows))

    # ---- 2. TEMP_PM by Supplier_Part_Number LIKE %Item (stock) Description% ----
    if desc:
        sql = ("select * from "
               "`vf-ie-datahub.vfie_dh_lake_customer_complex_fixed_s.TEMP_PM` "
               f"WHERE UPPER(Supplier_Part_Number) LIKE '%{_esc(desc).upper()}%'")
        try:
            rows = _rows(sql)
        except Exception as exc:  # noqa: BLE001
            print(f"[PCNAV ERROR] TEMP_PM(desc): {type(exc).__name__}: {exc}")
            rows = []
        rows, checked = _apply_recency(rows, recent)
        if rows:
            best = _best_row(rows, code, desc)
            exact = _has_exact(rows, code, desc,
                               ["Supplier_Part_Number", "Material_Description"])
            c = best.get("Supplier_Part_Number") or code
            n = best.get("Material_Description") or desc
            return _result(c, n, "High" if exact else "Low", "TEMP_PM",
                           checked, len(rows))

    # ---- 3. TEMP_item_model ----
    #    if we have a stock code -> by `producer_code `
    #    else                    -> by `name `
    if code:
        sql = ("select * from "
               "`vf-ie-datahub.vfie_dh_lake_customer_complex_fixed_s.TEMP_item_model` "
               f"WHERE UPPER(`producer_code `) LIKE '%{_esc(code).upper()}%'")
        match_fields = ["producer_code ", "producer_code", "name ", "name"]
    elif desc:
        sql = ("select * from "
               "`vf-ie-datahub.vfie_dh_lake_customer_complex_fixed_s.TEMP_item_model` "
               f"WHERE UPPER(`name `) LIKE '%{_esc(desc).upper()}%'")
        match_fields = ["producer_code ", "producer_code", "name ", "name"]
    else:
        return _result(code, desc, "None", None, recent is not None, 0)

    try:
        rows = _rows(sql)
    except Exception as exc:  # noqa: BLE001
        print(f"[PCNAV ERROR] TEMP_item_model: {type(exc).__name__}: {exc}")
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}",
                "resolved": f"{code} - {desc}", "confidence": "None"}
    rows, checked = _apply_recency(rows, recent)
    if rows:
        best = _best_row(rows, code, desc)
        exact = _has_exact(rows, code, desc, match_fields)
        # read producer_code / name allowing the trailing-space variants
        c = (best.get("producer_code ") or best.get("producer_code") or code)
        n = (best.get("name ") or best.get("name") or desc)
        return _result(c, n, "Medium" if exact else "Low",
                       "TEMP_item_model", checked, len(rows))

    # nothing found anywhere
    return _result(code, desc, "None", None, recent is not None, 0)
