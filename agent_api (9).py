"""FastAPI wrapper around the quote-to-order ADK agent."""

import asyncio
import json
import os
import re
import traceback

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

AGENT_MOCK = os.getenv("AGENT_MOCK", "0") == "1"

# The LLM proxy sometimes glitches with:
#   "responsible AI check not passed for content-moderation-Health with score of ..."
# On that error we retry: send a plain "hi" to the model first (fresh, benign
# turn to reset the moderation state), then re-run the workflow with the
# opportunity id in a brand-new session. Default: 3 retries (4 attempts total).
RAI_MAX_RETRIES = int(os.getenv("RAI_MAX_RETRIES", "3"))
_RAI_PATTERNS = ("responsible ai", "content-moderation", "content moderation", "rai check")

# Transient capacity/rate errors from the model backend (Vertex/proxy).
OVERLOAD_MAX_RETRIES = int(os.getenv("OVERLOAD_MAX_RETRIES", "5"))
_OVERLOAD_PATTERNS = (
    "resource_exhausted", "resource exhausted", "429",
    "overloaded", "queue_preempted", "prefill_queue_preempted",
    "too many retries", "unavailable", "try again later",
)


def _is_overloaded_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(p in msg for p in _OVERLOAD_PATTERNS)


def _is_rai_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(p in msg for p in _RAI_PATTERNS)


_SIZE_PATTERNS = (
    "constraint is too tall", "constraint-is-too-big", "invalid_argument",
    "request contains an invalid argument", "too large", "exceeds",
)


def _is_size_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(p in msg for p in _SIZE_PATTERNS)


# How many times to shrink the condense budget and re-run on a size error,
# and the shrink factor each time.
SIZE_MAX_RETRIES = int(os.getenv("SIZE_MAX_RETRIES", "4"))
SIZE_SHRINK_FACTOR = float(os.getenv("SIZE_SHRINK_FACTOR", "0.5"))
SIZE_MIN_BUDGET = int(os.getenv("SIZE_MIN_BUDGET", "12000"))


async def _say_hi():
    """Benign warm-up turn straight to the model (not through the workflow)."""
    try:
        from vf_quote_to_order_agent_configured.Agent.agent import client, gemini_model
        await client.aio.models.generate_content(
            model=gemini_model.model, contents="hi"
        )
    except Exception:  # noqa: BLE001 - warm-up is best effort
        pass


# ── Detect which flavour of agent we have ───────────────────────────
# Mirrors the RA agent_server.py pattern: if the agent module exposes a
# plain `run_agent(query)` function, call that directly (e.g. a stub or a
# non-ADK implementation). Otherwise, if it exposes `root_agent`, drive it
# through the standard ADK Runner + InMemorySessionService.
_AGENT_MODULE = None
_AGENT_MODE = "not-loaded"
_run_agent_fn = None
_root_agent = None
_runner = None
_session_service = None

if not AGENT_MOCK:
    try:
        import vf_quote_to_order_agent_configured.Agent.agent as _AGENT_MODULE
    except Exception as _exc:  # noqa: BLE001
        _AGENT_MODULE = None
        _AGENT_MODE = f"import-error: {_exc}"

    if _AGENT_MODULE is not None:
        _run_agent_fn = getattr(_AGENT_MODULE, "run_agent", None)
        _root_agent = getattr(_AGENT_MODULE, "root_agent", None)

        if _root_agent is not None and _run_agent_fn is None:
            from google.adk.runners import Runner
            from google.adk.sessions import InMemorySessionService

            _APP_NAME = "q2o"
            _session_service = InMemorySessionService()
            _runner = Runner(agent=_root_agent, app_name=_APP_NAME,
                              session_service=_session_service)
            _AGENT_MODE = f"adk:{getattr(_root_agent, 'name', 'root_agent')}"
        elif _run_agent_fn is not None:
            _AGENT_MODE = "function:run_agent"
        else:
            _AGENT_MODE = "error: agent.py must define run_agent(query) or root_agent"

app = FastAPI(title="Q2O Agent API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory result cache: opp_id -> result dict
RESULTS: dict = {}

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


def _metrics_add_bq_read(opp_id: str, bytes_billed: int):
    m = METRICS.get(opp_id)
    if not m:
        return
    m["bq_bytes_read"] += int(bytes_billed or 0)
    m["bq_queries"] += 1


def _metrics_add_bq_write(opp_id: str, rows: int, approx_bytes: int = 0):
    m = METRICS.get(opp_id)
    if not m:
        return
    m["bq_rows_written"] += int(rows or 0)
    m["bq_bytes_written"] += int(approx_bytes or 0)


# ── Cost rates (set from env; defaults are placeholders - set your real rates)
# All rates in your billing currency.
RATE_IN_PER_1M    = float(os.getenv("RATE_IN_PER_1M", "0"))     # Gemini input  $/1M tokens
RATE_OUT_PER_1M   = float(os.getenv("RATE_OUT_PER_1M", "0"))    # Gemini output $/1M tokens
RATE_VCPU_SEC     = float(os.getenv("RATE_VCPU_SEC", "0"))      # Cloud Run $/vCPU-second
RATE_GIB_SEC      = float(os.getenv("RATE_GIB_SEC", "0"))       # Cloud Run $/GiB-second
RATE_BQ_PER_TIB   = float(os.getenv("RATE_BQ_PER_TIB", "0"))    # BigQuery $/TiB scanned
RUN_VCPU          = float(os.getenv("RUN_VCPU", "2"))           # Cloud Run vCPUs configured
RUN_GIB           = float(os.getenv("RUN_GIB", "4"))            # Cloud Run memory GiB
COST_DIR          = os.getenv("COST_DIR", "cost_reports")       # where to write the file


def _metrics_finish_and_print(opp_id: str):
    """Print the run's cost drivers and write a per-run cost report file
    showing the equation and cost per service."""
    import time as _t
    m = METRICS.get(opp_id)
    if not m:
        return {}
    m["duration_sec"] = round(_t.time() - m["start"], 2)
    mb_read = m["bq_bytes_read"] / (1024 * 1024)
    tib_read = m["bq_bytes_read"] / (1024 ** 4)

    print(
        "[cost] opp=%s | duration=%.2fs | tokens in=%d out=%d total=%d | "
        "bq_queries=%d bq_read=%.2f MB | bq_rows_written=%d (~%.2f KB)"
        % (opp_id, m["duration_sec"], m["input_tokens"], m["output_tokens"],
           m["total_tokens"], m["bq_queries"], mb_read,
           m["bq_rows_written"], m["bq_bytes_written"] / 1024.0)
    )

    # ---- compute cost per service ----
    cost_in   = m["input_tokens"]  / 1_000_000 * RATE_IN_PER_1M
    cost_out  = m["output_tokens"] / 1_000_000 * RATE_OUT_PER_1M
    cost_cpu  = m["duration_sec"] * RUN_VCPU * RATE_VCPU_SEC
    cost_mem  = m["duration_sec"] * RUN_GIB * RATE_GIB_SEC
    cost_bq   = tib_read * RATE_BQ_PER_TIB
    total     = cost_in + cost_out + cost_cpu + cost_mem + cost_bq
    m["cost"] = {"gemini_input": cost_in, "gemini_output": cost_out,
                 "cloudrun_cpu": cost_cpu, "cloudrun_mem": cost_mem,
                 "bigquery_queries": cost_bq, "total": total}

    lines = [
        f"Opportunity: {opp_id}",
        "=" * 72,
        "",
        "MEASURED PER RUN",
        f"  duration                : {m['duration_sec']:.2f} s",
        f"  input tokens            : {m['input_tokens']:,}",
        f"  output tokens           : {m['output_tokens']:,}",
        f"  BigQuery queries        : {m['bq_queries']}",
        f"  BigQuery bytes scanned  : {mb_read:.2f} MB",
        f"  sales order rows written: {m['bq_rows_written']}",
        "",
        "RATES USED",
        f"  Gemini input   : {RATE_IN_PER_1M} per 1M tokens",
        f"  Gemini output  : {RATE_OUT_PER_1M} per 1M tokens",
        f"  Cloud Run vCPU : {RATE_VCPU_SEC} per vCPU-second  (config: {RUN_VCPU} vCPU)",
        f"  Cloud Run mem  : {RATE_GIB_SEC} per GiB-second    (config: {RUN_GIB} GiB)",
        f"  BigQuery       : {RATE_BQ_PER_TIB} per TiB scanned",
        "",
        "COST PER SERVICE",
        "-" * 72,
        f"Gemini (input)      = {m['input_tokens']:,} / 1,000,000 x {RATE_IN_PER_1M}"
        f"  = {cost_in:.6f}",
        f"Gemini (output)     = {m['output_tokens']:,} / 1,000,000 x {RATE_OUT_PER_1M}"
        f"  = {cost_out:.6f}",
        f"Cloud Run (CPU)     = {m['duration_sec']:.2f}s x {RUN_VCPU} vCPU x {RATE_VCPU_SEC}"
        f"  = {cost_cpu:.6f}",
        f"Cloud Run (memory)  = {m['duration_sec']:.2f}s x {RUN_GIB} GiB x {RATE_GIB_SEC}"
        f"  = {cost_mem:.6f}",
        f"BigQuery (queries)  = {tib_read:.8f} TiB x {RATE_BQ_PER_TIB}"
        f"  = {cost_bq:.6f}",
        f"BigQuery (writes)   = inserts are not billed per row"
        f"  = 0.000000",
        f"Cloud Storage       = immaterial for this run"
        f"  = 0.000000",
        "-" * 72,
        f"TOTAL PER OPPORTUNITY = {total:.6f}",
        "",
    ]
    if not any([RATE_IN_PER_1M, RATE_OUT_PER_1M, RATE_VCPU_SEC, RATE_GIB_SEC, RATE_BQ_PER_TIB]):
        lines.append("NOTE: rates are 0 - set RATE_IN_PER_1M, RATE_OUT_PER_1M, "
                     "RATE_VCPU_SEC, RATE_GIB_SEC, RATE_BQ_PER_TIB env vars.")
    report = "\n".join(lines)

    try:
        os.makedirs(COST_DIR, exist_ok=True)
        path = os.path.join(COST_DIR, f"{opp_id}_cost.txt")
        with open(path, "w") as fh:
            fh.write(report)
        print(f"[cost] report written: {path}")
    except Exception as exc:  # noqa: BLE001
        print(f"[cost] could not write report: {exc}")

    return m

# ── Live processing progress ──────────────────────────────────────────────
# Keyed by opportunity id, updated as each ADK node completes, and read by the
# UI's processing page via GET /status/{opp}. This is best-effort and never
# affects the run itself - if it fails, the run still completes normally.
PROGRESS: dict = {}

# The ordered pipeline steps we surface to the user. Each entry maps an ADK
# node/agent name to a friendly label. "key" is the technical detail shown
# alongside the friendly label.
PIPELINE_STEPS = [
    {"key": "qto_retrieval_agent",  "label": "Retrieving opportunity documents"},
    {"key": "product_code_agent",   "label": "Resolving product codes"},
    {"key": "bsp_extraction_agent", "label": "Assembling the sales order"},
    {"key": "email_agent",          "label": "Drafting the summary email"},
]


def _progress_init(opp_id: str):
    """Start a fresh progress record: all steps pending."""
    PROGRESS[opp_id] = {
        "opportunity_id": opp_id,
        "status": "running",
        "steps": [
            {"key": s["key"], "label": s["label"], "state": "pending"}
            for s in PIPELINE_STEPS
        ],
    }


def _progress_mark(opp_id: str, node_name: str, state: str):
    """Mark the step matching node_name as 'active' or 'done'. Unknown nodes
    are ignored so the store never breaks on an unexpected event."""
    rec = PROGRESS.get(opp_id)
    if not rec:
        return
    for step in rec["steps"]:
        if step["key"] == node_name:
            step["state"] = state
            break


def _progress_finish(opp_id: str, ok: bool = True):
    rec = PROGRESS.get(opp_id)
    if not rec:
        return
    rec["status"] = "done" if ok else "error"
    if ok:
        for step in rec["steps"]:
            if step["state"] != "done":
                step["state"] = "done"

REQUIRED_LABELS = {
    "R1_po_signed_contract": "PO / signed contract",
    "R2_quote_to_customer": "Quote to customer",
    "R3_vendor_quote_clear_instruction": "Quote from vendor and clear instruction what vendor to order from",
    "R4_site_id": "Site ID",
    "R5_site_contact_info": "Site contact information",
    "R6_summary_sold_designed_hld": "Summary of what has been sold and/or Design / HLD if Solution Design involved",
    "R7_summary_content_complete": "Summary to include: services supplied, deployment, engineering asks/requirements, leadtime",
}

CHECK_LABELS = {
    "C1_leadtime_aligned_kpis": "If Leadtime provided to customer, is it aligned to our Product KPIs",
    "C2_circuit_order_form_attached": "Is the Circuit Order Form attached?",
    "C3_po_quote_attached_signed": "Is the PO/Quote attached and is it signed?",
    "C4_vendor_clear": "Is it clear what vendor the order is for?",
    "C5_third_party_vendor_docs": "Third Party usage: (Eir Fibre, Eir UG, EIL and SAB, Enet, Siro, Ripplecom/Host) quotes attached?",
    "C6_infra_orders_product_speed": "Are the correct Infra orders attached (Sales Enablement) and are they correct regarding product and speed etc?",
    "C7_site_info_correct": "Is the site information correct Site ID, Site Name, Site Address?",
    "C8_site_contact_details": "Is the site contact details provided, site contact name, email and site contact mobile number?",
    "C9_summary_matches_design_form": "Does the summary/design of the order match the design and order form attached?",
    "C10_correct_customer_account": "Are they on the correct customer account/company name so they are to be billed correctly?",
    "C11_lan_ip_for_dia": "Has the customers LAN IP details been provided for DIA?",
    "C12_public_ip_range_approved": "Customer Public address range(s) request details - has this been approved?",
    "C13_standard_offering_slash30": "Customer requesting IP address ranges from customers - standard offering is /30",
    "C14_kit_router_fulfilment": "Is the Kit correct, is the router compatible to the product being ordered and is the fulfilment run rate or ship back correct?",
    "C15_power_antenna_cables_licences": "Are all the power supplies, antenna's, extension cables, Licences requested, on the order?",
    "C16_third_party_install_in_quote": "Is the 3rd party installation included in the quote?",
    "C17_contract_term_matches": "Is the contract term provided and does it match the order?",
    "C18_bend_info_correct": "Has the B-End information if required been filled out and is it correct?",
    "C19_qos_af_ef_correct": "Has the QoS (Quality of Service) if required been filled out and is it correct? AF and EF % On order form",
    "C20_cease_details_included": "If services are to be ceased after the new service is completed, the cease details must be added to the request.",
    "C21_upgrade_downgrade_stated": "If the order is an Upgrade/Downgrade this must be clearly stated on the order.",
    "C22_referenced_files_attached": "If there are any comments to please refer to a certain email or file, please make sure it is attached.",
    "C23_vendor_quote_still_valid": "Is the vendor quote still valid (not out of date)?",
    "C24_vf_quote_to_customer_attached": "Is the VF Quote to Customer attached?",
    "C25_leadtime_indicated_to_customer": "What Leadtime (if any) have you indicated to the customer",
    "C26_vendor_quotations_attached": "Are the Vendor Quotations attached?",
    "C27_tech_spec_commercial_approval_margin": "Tech spec includes commercial approval incl. margin (pricing tool / EDRA / commercial manager email)?",
}


BASE_CHECK_LABELS = {
    "B1_customer_name_in_bsp": "Customer name exists / matches in BSP (else raise MDG)",
    "B2_delivery_address_in_bsp": "Delivery address exists in BSP (else raise BSP Team)",
    "B3_billing_entity_matches": "Billing entity name matches bill_to_name in BSP (else raise MDG)",
    "B4_opp_ref_matches_vbop": "Opportunity ref in tech spec matches VBOP",
    "B5_po_number_consistent": "PO number consistent across VBOP / customer PO (or email confirmation)",
    "B6_cost_less_than_sell": "Vendor PO cost price < sell price (Vodafone quote & customer PO)",
    "B7_product_codes_in_docs": "Every tech spec product code present in vendor quote (or name found)",
    "B8_product_codes_in_bsp_mdm": "Every tech spec product code exists in BSP via MDM (else raise MDM)",
    "B9_quantities_align": "Quantities align across all files (vendor quote may be higher)",
    "B10_vendor_quote_not_expired": "Vendor quote still valid (within 30 days, not expired)",
    "B11_contact_details_complete": "Contact name & details in tech spec are complete",
    "B13_billing_address_in_bsp": "Billing address present in documents and exists in BSP (else raise BSP Team)",
}

CIRCUIT_CHECK_LABELS = {
    "X1_contract_term_present": "Contract term present (circuit order form / tech spec)",
    "X2_premise_id_siro": "Premise ID present (required for SIRO vendors)",
    "X3_service_type_matches": "Service type present and matches supporting docs",
    "X4_speed_matches": "Speed present and matches customer order form",
    "X5_product_name_type_exist": "Product name and type exist",
    "X6_service_address_in_bsp": "Service address present and matches BSP (else raise BSP Team)",
    "X7_po_matches_vbop": "PO number in circuit order form matches VBOP",
    "X8_billing_freq_aligned": "Billing frequency present and aligned",
    "X9_po_type_recurring": "PO type set to Recurring",
}

def _parse_email_draft(email_draft):
    """Normalise the email agent's output into {subject, body}."""
    if not email_draft:
        return None
    parsed = _parse_json_block(email_draft) if isinstance(email_draft, str) else email_draft
    if isinstance(parsed, dict):
        subject = parsed.get("subject") or parsed.get("Subject") or ""
        body = parsed.get("body") or parsed.get("Body") or ""
        if subject or body:
            return {"subject": str(subject), "body": str(body)}
    # fall back: treat the whole thing as the body
    if isinstance(email_draft, str) and email_draft.strip():
        return {"subject": "Quote-to-order summary", "body": email_draft.strip()}
    return None


def _parse_json_block(text):
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, str):
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


# Checks that only apply to CIRCUIT orders. For non-circuit (e.g. Kit) orders
# these are dropped from the response entirely, so the UI never shows them and
# they never count towards failures.
CIRCUIT_ONLY_KEYS = {
    "R4_site_id",                      # Site ID - circuit orders only
    "C2_circuit_order_form_attached",  # Circuit Order Form
    "C5_third_party_vendor_docs",      # eir / enet / SIRO / Ripplecom quotes
    "C6_infra_orders_product_speed",   # infra orders, product & speed
    "C11_lan_ip_for_dia",              # DIA LAN IP
    "C12_public_ip_range_approved",    # public IP ranges
    "C13_standard_offering_slash30",   # /30 standard offering
    "C18_bend_info_correct",           # B-End: EIL, SAB, eir exchange, enet NNI
    "C19_qos_af_ef_correct",           # QoS AF / EF %
}


def _feedback_index(saved_status):
    """Build {check_key: feedback_record} from a previously saved
    validation_status.json (any of its check sections)."""
    idx = {}
    if not isinstance(saved_status, dict):
        return idx
    for section in ("criteria", "checks", "base_checks", "circuit_checks"):
        for c in saved_status.get(section, []) or []:
            if isinstance(c, dict) and c.get("key") and c.get("feedback"):
                idx[c["key"]] = c["feedback"]
    return idx


def _confidence_index(resolved_codes):
    """Build a lookup {key -> navigator entry} so we can stamp confidence,
    producer code, name, and reasoning onto line items deterministically.
    Keys: the full 'resolved' string, the code part, and the raw
    product_stock_code, all upper/stripped."""
    parsed = _parse_json_block(resolved_codes)
    entries = []
    if isinstance(parsed, dict):
        entries = parsed.get("resolved_codes") or parsed.get("resolved") or []
    elif isinstance(parsed, list):
        entries = parsed
    idx = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        resolved = str(e.get("resolved", "")).strip().upper()
        code_part = resolved.split(" - ", 1)[0].strip() if resolved else ""
        psc = str(e.get("product_stock_code", "")).strip().upper()
        for k in (resolved, code_part, psc):
            if k:
                idx.setdefault(k, e)
    return idx


def _split_item(item):
    """Split a 'code - name' string into (producer_code, name)."""
    s = str(item or "").strip()
    if " - " in s:
        code, name = s.split(" - ", 1)
        return code.strip(), name.strip()
    return s, ""


def _stamp_confidence(order, resolved_codes):
    """Populate each line item's confidence, producer_code, name, and
    confidence_reason from the navigator result. Matches by the resolved
    string, the item, or the sku. Falls back to splitting the item string."""
    idx = _confidence_index(resolved_codes)
    lines = (order or {}).get("line_items") or []
    for li in lines:
        if not isinstance(li, dict):
            continue
        cand = [
            str(li.get("item", "")).strip().upper(),
            str(li.get("item", "")).split(" - ", 1)[0].strip().upper(),
            str(li.get("sku", "")).strip().upper(),
        ]
        entry = next((idx[c] for c in cand if c and c in idx), None)

        # confidence: keep a real one the extractor set, else take navigator's
        existing = str(li.get("confidence") or "").strip()
        if not (existing and existing.lower() not in ("", "not specified", "none")):
            li["confidence"] = (entry or {}).get("confidence") or existing or "None"

        # producer_code + name: prefer navigator's, else split the item string
        code_split, name_split = _split_item(li.get("item"))
        pc = (entry or {}).get("code") or code_split
        nm = (entry or {}).get("name") or name_split
        if not str(li.get("producer_code") or "").strip() or str(li.get("producer_code")).strip().lower() == "not specified":
            li["producer_code"] = pc or "Not Specified"
        if not str(li.get("name") or "").strip() or str(li.get("name")).strip().lower() == "not specified":
            li["name"] = nm or "Not Specified"

        # reasoning for the confidence
        if not str(li.get("confidence_reason") or "").strip():
            li["confidence_reason"] = (entry or {}).get("confidence_reason") or ""
    return order


def _shape_result(opp_id, validation, order, saved_status=None, resolved_codes=None,
                  validation_file=None, email_draft=None):
    """Convert agent output into the UI shape.

    validation_file: the parsed validation.json read from GCS. Its "report"
        section supplies the checks (required_information / checks /
        bsp_base_checks), its "validation" section the overall status/reasoning,
        and its "feedbacks" the UI-written raised requests (a SEPARATE list, not
        per-check).
    resolved_codes: the Product Code Navigator's resolved_product_codes, used to
        stamp each line item's confidence deterministically.
    """
    # Validation now comes from validation.json's "report" (not an agent).
    vfile = validation_file or {}
    report = vfile.get("report") or {}
    v = report or (_parse_json_block(validation) or {})
    o = _parse_json_block(order) or {}
    feedbacks = vfile.get("feedbacks") or []
    # The extractor no longer uses a constrained output_schema (it produced a
    # decoding state machine too large for the serving layer). Validate/coerce
    # the parsed JSON against BSPOrderTemplate here instead - best effort, so a
    # minor deviation never blocks the result.
    try:
        from vf_quote_to_order_agent_configured.Agent.models import BSPOrderTemplate  # type: ignore
        if isinstance(o, dict):
            o = BSPOrderTemplate(**o).model_dump()
    except Exception:  # noqa: BLE001 - keep the raw parsed dict if coercion fails
        pass

    # Stamp confidence onto line items from the navigator result (reliable,
    # independent of whether the extractor copied it).
    if isinstance(o, dict):
        o = _stamp_confidence(o, resolved_codes)

    def items(section, labels):
        out = []
        data = v.get(section, {}) or {}

        # report arrays are compact: ["C1","Y|N","comment"] (feedback is NOT
        # here anymore - it lives in the separate feedbacks list).
        short_index = {}   # short id -> (result, notes)
        if isinstance(data, list):
            for row in data:
                if not row:
                    continue
                key = str(row[0]).strip()
                res = str(row[1]).strip().upper() if len(row) > 1 else "N"
                note = row[2] if len(row) > 2 else ""
                short_index[key.upper()] = (res, note)

        for key, label in labels.items():
            short = key.split("_", 1)[0].upper()   # "C1_leadtime..." -> "C1"
            if short_index:
                res_t, note = short_index.get(short, ("N", ""))
                res = str(res_t).strip().upper()
            else:
                entry = data.get(key, {}) or {}
                res = str(entry.get("result", "N")).strip().upper()
                note = entry.get("notes", "")
            out.append({
                "key": key,
                "label": label,
                "ok": res == "Y",
                "notes": note or ("Not verified" if res != "Y" else ""),
            })
        return out

    order_category = str(v.get("order_category") or o.get("order_category") or "").strip()
    is_circuit = order_category.lower() == "circuit" or ("circuit_checks" in v)

    criteria = items("required_information", REQUIRED_LABELS)
    checks = items("checks", CHECK_LABELS)
    base_checks = items("bsp_base_checks", BASE_CHECK_LABELS)
    circuit_checks = items("circuit_checks", CIRCUIT_CHECK_LABELS) if is_circuit else []

    # Non-circuit orders: drop circuit-specific checks entirely.
    if not is_circuit:
        criteria = [c for c in criteria if c["key"] not in CIRCUIT_ONLY_KEYS]
        checks = [c for c in checks if c["key"] not in CIRCUIT_ONLY_KEYS]

    failed = [i for i in criteria + checks + base_checks + circuit_checks if not i["ok"]]

    validation_summary = vfile.get("validation") or {}

    return {
        "status": "ok",
        "opportunity_id": opp_id,
        "matched_folder": v.get("matched_folder", ""),
        "order_category": order_category or "Not Specified",
        "is_circuit": is_circuit,
        "criteria": criteria,
        "checks": checks,
        "base_checks": base_checks,
        "circuit_checks": circuit_checks,
        "failed_count": len(failed),
        "validation": validation_summary,   # {status, reasoning, comments, date}
        "feedbacks": feedbacks,             # UI-written raised requests (separate list)
        "overall": v.get("overall", {}),
        "order": o,
        "email_draft": _parse_email_draft(email_draft),
        "raw_validation": v,
    }


_MOCK_BASE_FAIL = {"B1_customer_name_in_bsp": "raise_mdg", "B8_product_codes_in_bsp_mdm": "raise_mdm",
                   "B13_billing_address_in_bsp": "raise_bsp"}
_MOCK_CIRCUIT_FAIL = {"X2_premise_id_siro": "raise_bsp"}

MOCK_VALIDATION = {
    "matched_folder": "4234-opp-DEMO",
    "order_category": "Circuit",
    "required_information": {k: {"result": ("N" if k == "R4_site_id" else "Y"),
                                 "notes": ("Missing required value" if k == "R4_site_id" else "Found in documents")}
                             for k in REQUIRED_LABELS},
    "checks": {k: {"result": ("N" if k == "C2_circuit_order_form_attached" else "Y"),
                   "notes": ("Document not found in payload" if k == "C2_circuit_order_form_attached" else "Verified")}
               for k in CHECK_LABELS},
    "bsp_base_checks": {k: {"result": ("N" if k in _MOCK_BASE_FAIL else "Y"),
                            "notes": ("No acceptable match in BSP" if k in _MOCK_BASE_FAIL else "Matched in BSP"),
                            "action": _MOCK_BASE_FAIL.get(k, "none")}
                        for k in BASE_CHECK_LABELS},
    "circuit_checks": {k: {"result": ("N" if k in _MOCK_CIRCUIT_FAIL else "Y"),
                           "notes": ("SIRO premise id missing" if k in _MOCK_CIRCUIT_FAIL else "Present and matches"),
                           "action": _MOCK_CIRCUIT_FAIL.get(k, "none")}
                       for k in CIRCUIT_CHECK_LABELS},
    "overall": {"ready_for_order": "N",
                "blocking_issues": ["Site ID missing", "Circuit Order Form not attached",
                                    "Customer not in BSP", "Product code not in MDM", "SIRO premise id missing"],
                "needs_human_review": []},
}

MOCK_ORDER = {
    "order_title": "Enterprise_New_eir_DIA_TechCorp_SITE-12345",
    "order_type": "New", "order_category": "Circuit",
    "external_reference_number": "DEMO",
    "company_code": "VF-IE-001", "bill_to_party": "TechCorp Ltd", "ship_to_party": "TechCorp Ltd",
    "line_items": [
        {"quantity": 1, "item": "Dedicated Internet Access 100Mbps", "sku": "TEL-DIA-100",
         "location": "TechCorp Ltd", "price": "450.00", "charge_type": "Recurring",
         "recurring_period": "Monthly", "fulfilment": "DIA-100-S"},
        {"quantity": 1, "item": "SLA", "sku": "SLA", "location": "TechCorp Ltd",
         "price": "0.00", "charge_type": "Recurring", "recurring_period": "Monthly", "fulfilment": "SLA"},
        {"quantity": 1, "item": "ECS-INSTALL", "sku": "ECS-INSTALL", "location": "TechCorp Ltd",
         "price": "350.00", "charge_type": "One Off", "recurring_period": "Not Applicable", "fulfilment": "H202"},
        {"quantity": 1, "item": "ECS-INTSAndConfigIRL", "sku": "ECS-INTSAndConfigIRL", "location": "TechCorp Ltd",
         "price": "500.00", "charge_type": "One Off", "recurring_period": "Not Applicable", "fulfilment": "H202"},
    ],
}


async def _run_agent(opp_id: str):
    """Run the workflow (in whichever mode was detected) and return
    (validation_report, bsp_order). A brand-new session is created on every
    call, so calling this again after `_say_hi()` naturally starts fresh."""
    if _AGENT_MODE.startswith("adk:"):
        import uuid

        from google.genai import types

        session = await _session_service.create_session(
            app_name="q2o", user_id="ui", session_id=str(uuid.uuid4())
        )
        msg = types.Content(role="user", parts=[types.Part(text=f"{opp_id}")])

        # Track which node is active from the ADK event stream. Each event
        # carries its author (the agent that produced it); when the author
        # changes, the previous node is done and the new one is active.
        _current = {"node": None}
        async for _event in _runner.run_async(
            user_id="ui", session_id=session.id, new_message=msg
        ):
            author = getattr(_event, "author", None)
            if author and author != _current["node"]:
                if _current["node"]:
                    _progress_mark(opp_id, _current["node"], "done")
                _progress_mark(opp_id, author, "active")
                _current["node"] = author
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
        if _current["node"]:
            _progress_mark(opp_id, _current["node"], "done")

        session = await _session_service.get_session(
            app_name="q2o", user_id="ui", session_id=session.id
        )
        state = session.state or {}
        return (state.get("validation_report"), state.get("bsp_order"),
                state.get("opportunity_prefix", ""),
                state.get("existing_validation_status"),
                state.get("resolved_product_codes"),
                state.get("email_draft"))

    if _AGENT_MODE == "function:run_agent":
        result = _run_agent_fn(opp_id)
        if asyncio.iscoroutine(result):
            result = await result
        # Expect run_agent to return either (validation, order) or a single
        # JSON-ish payload containing both keys.
        if isinstance(result, tuple) and len(result) == 2:
            return result[0], result[1], "", None, None, None
        parsed = _parse_json_block(result) or {}
        return (parsed.get("validation_report"), parsed.get("bsp_order"),
                "", None, parsed.get("resolved_product_codes"),
                parsed.get("email_draft"))

    raise RuntimeError(f"Agent not available ({_AGENT_MODE})")


@app.get("/health")
def health():
    return {"ok": True, "mock": AGENT_MOCK, "mode": _AGENT_MODE, "rai_max_retries": RAI_MAX_RETRIES}


@app.get("/exists/{opp_id}")
def exists(opp_id: str):
    """Check whether an opportunity has been processed - i.e. a folder named
    after the opportunity id exists under the processed_files prefix in GCS.
    The UI calls this before starting so it can show a helpful alert instead of
    running the pipeline on a missing opportunity."""
    if AGENT_MOCK:
        return {"opportunity_id": opp_id, "exists": True}
    try:
        from vf_quote_to_order_agent_configured.Agent.tools import (  # type: ignore
            opportunity_exists,
        )
        res = opportunity_exists(opp_id)
        if res.get("status") != "error":
            print(f"[exists] {opp_id}: exists={res.get('exists')} ({res.get('path')})")
        return res
    except Exception as exc:  # noqa: BLE001
        return {"opportunity_id": opp_id, "exists": False,
                "status": "error", "message": str(exc)}


@app.post("/process/{opp_id}")
async def process(opp_id: str, request: Request = None):
    _progress_init(opp_id)
    _metrics_init(opp_id)
    try:
        from vf_quote_to_order_agent_configured.subAgent.product_code_agent.tools import (  # type: ignore
            bq_metrics_reset,
        )
        bq_metrics_reset()
    except Exception:  # noqa: BLE001
        pass
    # Capture the authenticated user from the Cloud Run / IAP header if present.
    # Falls back to 'unknown' when auth isn't enforced in front of the service.
    _user = "unknown"
    try:
        if request is not None:
            _user = (request.headers.get("X-Goog-Authenticated-User-Email")
                     or request.headers.get("x-goog-authenticated-user-email")
                     or "unknown")
            # header format is often "accounts.google.com:user@domain" - strip prefix
            if ":" in _user:
                _user = _user.split(":", 1)[1]
    except Exception:  # noqa: BLE001
        _user = "unknown"
    try:
        if AGENT_MOCK:
            # Walk the steps so the processing page animates in mock mode too.
            for s in PIPELINE_STEPS:
                _progress_mark(opp_id, s["key"], "active")
                await asyncio.sleep(1)
                _progress_mark(opp_id, s["key"], "done")
            result = _shape_result(opp_id, MOCK_VALIDATION, MOCK_ORDER,
                                   email_draft={
                                       "subject": f"Action Required: Billing Discrepancies Identified - {opp_id}",
                                       "body": ("Dear Billing Team,\n\nDuring our automated reconciliation review of "
                                                f"{opp_id}, the following discrepancies were identified that require "
                                                "attention before invoicing:\n\n1. PRICING VARIANCE - SKU-7821 "
                                                "(Enterprise Router X500)\n   - Contracted Price: 1,200.00\n   - Invoiced "
                                                "Price: 1,250.00\n   - Variance: +50.00 per unit (4.2%)\n\n2. DELIVERY "
                                                "SHORTFALL - SKU-3344 (Network Switch 48-Port)\n   - Ordered: 5 units\n   "
                                                "- Delivered: 4 units\n   - Pending: 1 unit\n\nPlease review and confirm "
                                                "the appropriate action.\n\nRegards,\nQuote-to-Order Assistant")
                                   })
        else:
            attempt = 0
            overload_attempt = 0
            size_attempt = 0
            while True:
                try:
                    validation, order, gcs_prefix, saved_status, resolved_codes, email_draft = await _run_agent(opp_id)
                    break
                except Exception as exc:  # noqa: BLE001
                    # Input too big for the model: shrink the condense budget and re-run.
                    # get_opportunity_data reads CONDENSE_TOTAL_BUDGET live from env,
                    # so lowering it here makes the next run send a smaller payload.
                    if _is_size_error(exc) and size_attempt < SIZE_MAX_RETRIES:
                        size_attempt += 1
                        cur = int(os.getenv("CONDENSE_TOTAL_BUDGET", "120000"))
                        new_budget = max(SIZE_MIN_BUDGET, int(cur * SIZE_SHRINK_FACTOR))
                        os.environ["CONDENSE_TOTAL_BUDGET"] = str(new_budget)
                        print(f"[size retry {size_attempt}/{SIZE_MAX_RETRIES}] input too big - "
                              f"shrinking payload budget {cur} -> {new_budget} and re-running")
                        continue
                    # Transient capacity/429: back off and retry (model queue overloaded)
                    if _is_overloaded_error(exc) and overload_attempt < OVERLOAD_MAX_RETRIES:
                        overload_attempt += 1
                        wait = min(2 ** overload_attempt, 30)  # 2,4,8,16,30s
                        print(f"[overload retry {overload_attempt}/{OVERLOAD_MAX_RETRIES}] "
                              f"429/RESOURCE_EXHAUSTED - backing off {wait}s then retrying")
                        await asyncio.sleep(wait)
                        continue
                    if _is_rai_error(exc) and attempt < RAI_MAX_RETRIES:
                        attempt += 1
                        print(f"[RAI retry {attempt}/{RAI_MAX_RETRIES}] {exc} "
                              f"- sending 'hi' then re-sending opportunity id "
                              f"in a fresh session")
                        await _say_hi()
                        await asyncio.sleep(1)
                        continue
                    raise
            # Validation is now a FILE in the opportunity folder, not an agent.
            try:
                from vf_quote_to_order_agent_configured.Agent.tools import (  # type: ignore
                    read_validation_file,
                )
                vfile = read_validation_file(opp_id)
                if vfile.get("status") != "success":
                    print(f"[process] validation.json: {vfile.get('status')} - {vfile.get('message','')}")
            except Exception as _vexc:  # noqa: BLE001
                print(f"[process] could not read validation.json: {_vexc}")
                vfile = {}

            result = _shape_result(opp_id, validation, order, saved_status=saved_status,
                                   resolved_codes=resolved_codes, validation_file=vfile,
                                   email_draft=email_draft)
            result["gcs_prefix"] = gcs_prefix
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        msg = str(exc)
        if _is_size_error(exc):
            msg = ("The opportunity documents were too large for the model even after "
                   "automatic compaction. Lower CONDENSE_TOTAL_BUDGET further, or the "
                   "biggest file needs cleaning at source. "
                   f"[{msg[:200]}]")
        elif _is_overloaded_error(exc):
            msg = ("The model service is temporarily overloaded (429 / RESOURCE_EXHAUSTED). "
                   "This is a transient capacity issue - please try again in a moment. "
                   f"[{msg[:200]}]")
        result = {"status": "error", "opportunity_id": opp_id, "message": msg,
                  "criteria": [], "checks": [], "failed_count": -1, "order": {}}

    RESULTS[opp_id] = result
    _progress_finish(opp_id, ok=(result.get("status") != "error"))
    # Record the sales-order rows this run produced (what gets written to
    # BigQuery). Row count is the write driver; BigQuery inserts are not
    # charged per row, so this is for volume tracking, not cost.
    try:
        _rows = (result.get("order") or {}).get("line_items") or []
        _approx = len(json.dumps(_rows)) if _rows else 0
        _metrics_add_bq_write(opp_id, len(_rows), _approx)
    except Exception:  # noqa: BLE001
        pass
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
    return result


@app.get("/status/{opp_id}")
def status(opp_id: str):
    """Live processing progress for the UI's processing page. Returns the
    ordered step list, each marked pending / active / done. Safe to poll."""
    rec = PROGRESS.get(opp_id)
    if not rec:
        # No run started yet (or already cleared) - return a neutral pending set.
        return {
            "opportunity_id": opp_id,
            "status": "pending",
            "steps": [
                {"key": s["key"], "label": s["label"], "state": "pending"}
                for s in PIPELINE_STEPS
            ],
        }
    return rec


@app.get("/result/{opp_id}")
def result(opp_id: str):
    return RESULTS.get(opp_id, {"status": "missing", "opportunity_id": opp_id})

class LogItem(BaseModel):
    session_id: str
    opp_id: str = ""


@app.post("/log")
async def write_log(item: LogItem, request: Request = None):
    """Write the initial app log for a session when the user clicks
    'Begin Order Creation Process'. Captures session id, user (from Cloud Run
    auth header if present), opportunity id, and datetime. Feedback is added
    later via /feedback."""
    _user = "unknown"
    try:
        if request is not None:
            _user = (request.headers.get("X-Goog-Authenticated-User-Email")
                     or request.headers.get("x-goog-authenticated-user-email")
                     or "unknown")
            if ":" in _user:
                _user = _user.split(":", 1)[1]
    except Exception:  # noqa: BLE001
        _user = "unknown"

    if AGENT_MOCK:
        return {"status": "success", "session_id": item.session_id, "user": _user,
                "opp_id": item.opp_id, "mock": True}
    try:
        from vf_quote_to_order_agent_configured.Agent.tools import save_app_log  # type: ignore
        res = save_app_log(session_id=item.session_id, opp_id=item.opp_id,
                           user=_user, feedback=[])
        return {"status": res.get("status"), "session_id": item.session_id,
                "user": _user, "opp_id": item.opp_id, "path": res.get("path")}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": str(exc)}


class FeedbackItem(BaseModel):
    check_key: str         # e.g. C1, B8, X2 - the check this request relates to
    option: str            # raise_bsp | raise_mdm | raise_mdg | manual_correction | raise_sales | ...
    comment: str = ""
    by: str = "ui"
    id: str = ""           # optional client id; server generates one if empty
    session_id: str = ""   # the app session id, so the app log can be updated


@app.post("/feedback/{opp_id}")
async def save_feedback(opp_id: str, item: FeedbackItem):
    """Add a feedback (a raised request) to validation.json's SEPARATE
    'feedbacks' array. Feedback is inserted ONLY from the UI - the agent's
    report/check rows are never modified. The feedbacks list is what the
    frontend shows.
    """
    import datetime as _dt
    import uuid as _uuid

    record = {
        "id": item.id or f"f-{_uuid.uuid4().hex[:8]}",
        "check_key": item.check_key,
        "option": item.option,
        "comment": item.comment,
        "by": item.by,
        "date": _dt.datetime.utcnow().isoformat() + "Z",
    }

    # keep the in-memory result's feedbacks in sync so the UI updates immediately
    result = RESULTS.get(opp_id)
    if result is not None:
        fbs = result.setdefault("feedbacks", [])
        fbs.append(record)

    if AGENT_MOCK:
        return {"status": "success", "opportunity_id": opp_id, "feedback": record,
                "feedbacks": (result or {}).get("feedbacks", [record])}

    # Persist: read validation.json, append to feedbacks, write it back.
    try:
        from vf_quote_to_order_agent_configured.Agent.tools import (  # type: ignore
            read_validation_file, save_feedbacks,
        )
        current = read_validation_file(opp_id)
        existing = current.get("feedbacks", []) if current.get("status") == "success" else []
        existing.append(record)
        saved = save_feedbacks(opp_id, existing)
        if saved.get("status") == "success":
            if result is not None:
                result["feedbacks"] = existing
            # Update the run's app log with the current feedback list.
            try:
                from vf_quote_to_order_agent_configured.Agent.tools import (  # type: ignore
                    save_app_log,
                )
                save_app_log(session_id=(item.session_id or opp_id), opp_id=opp_id,
                             feedback=[record], append=True)
            except Exception as _log_exc:  # noqa: BLE001
                print(f"[app_log] feedback update failed: {_log_exc}")
            return {"status": "success", "opportunity_id": opp_id,
                    "feedback": record, "feedbacks": existing, "path": saved.get("path")}
        return {"status": "error", "opportunity_id": opp_id,
                "message": saved.get("message", "save failed"), "feedback": record}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "opportunity_id": opp_id,
                "message": f"Failed to save feedback: {exc}", "feedback": record}


@app.get("/feedback/{opp_id}")
def get_feedback(opp_id: str):
    """Return the current feedbacks list for an opportunity (from validation.json)."""
    if AGENT_MOCK:
        return {"status": "success", "opportunity_id": opp_id,
                "feedbacks": RESULTS.get(opp_id, {}).get("feedbacks", [])}
    try:
        from vf_quote_to_order_agent_configured.Agent.tools import read_validation_file  # type: ignore
        current = read_validation_file(opp_id)
        return {"status": "success", "opportunity_id": opp_id,
                "feedbacks": current.get("feedbacks", []) if current.get("status") == "success" else []}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "opportunity_id": opp_id, "message": str(exc)}
