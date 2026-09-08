Clear. Let me update the diagram: remove the ingestion pipeline, add the Validation Agent running in parallel with the Product Code Navigator, and give it an LLM call through RAI.I moved RAI to a temporary position that's outside the backend lane. Let me place RAI and Gemini properly — below the agent stack, inside the backend lane — and add the validation agent's LLM edge.This looks right. The ingestion pipeline is gone, the Validation Agent (yellow) sits **side by side with the Product Code Navigator** showing they run in parallel after Retrieval, and RAI + Gemini are now cleanly below the agent lane inside the backend project. No overlaps, no dangling edges. Let me export.Diagram updated — ingestion pipeline removed, Validation Agent added running in parallel with the Product Code Navigator, both feeding BSP Extraction. Verified: no overlaps, no dangling edges.

Here's the updated prompt for their AI tool:

---

**PROMPT:**

Create a low-level architecture diagram for a "Quote-to-Order" multi-agent application running on Google Cloud Platform. Produce it as an editable draw.io (mxfile XML) diagram. Use a clean, professional style suitable for an executive audience: rounded rectangles, dashed grouping lanes for cloud projects, numbered arrows, and a legend. No emojis.

**Layout: left to right in four zones.**

**Zone 1 — User (outside the cloud boundary, far left):**
- Actor labelled: `G Suite User (OAuth2 / OIDC SSO)`

**Zone 2 — UI project.** Dashed lane labelled `Project: vf-ie-aib-prd-iei-cfa-lab`, containing one box:
- `Streamlit UI (Cloud Run)` — sub-lines: `Order Entry · Processing · Validation · Results`, `HTTPS via AGENT_API_URL`

**Zone 3 — Backend / AI project.** Dashed lane labelled `Project: vf-ie-aib-prd-iei-cfa-lab — Backend / AI layer (Cloud Run)`, containing:

(a) A box `FastAPI Service (agent_api)` listing these endpoints:
```
POST /process/{opp}    · run workflow
GET  /status/{opp}     · live progress
GET  /result/{opp}     · shaped result
POST /log              · session log (on Begin)
POST /feedback/{opp}   · raise request
GET  /feedback/{opp}   · list requests
GET  /exists/{opp}     · opportunity folder check
GET  /health
retry: 429 backoff · size auto-shrink
```

(b) An inner lane `ADK 2.x Orchestration — root_agent Workflow` showing this flow: **Retrieval → (Validation Agent and Product Code Navigator run IN PARALLEL) → BSP Extraction → Email Drafter**. Draw the two parallel agents side by side on the same row, with arrows from Retrieval branching to both, and arrows from both converging into BSP Extraction. Agent boxes:
1. `Retrieval Agent` / `qto_retrieval_agent` / `tool: get_opportunity_data` / `→ output: retrieval_summary`
2. `Product Code Navigator` / `product_code_agent` / `tool: resolve_product_code` / `deterministic BigQuery lookup` / `→ output: resolved_product_codes` — **shade light green**
3. `Validation Agent` / `validation_agent` / `runs validation checks` / `→ output: validation_report` — **shade light yellow**
4. `BSP Extraction Agent` / `bsp_extraction_agent` / `fills sales order header + line items` / `→ output: bsp_order`
5. `Email Drafter Agent` / `email_agent` / `tool: read_validation_file` / `→ output: email_draft`

(c) Two boxes below the agent lane, still inside the backend project:
- `Responsible AI Layer Proxy` — `ID-token (OIDC) auth`, `pre/post checks · governs every model call` — **shade light red**
- `Vertex AI — Gemini` — `vertexai=true genai client`, `region: europe-west1`

**Zone 4 — Data project.** Dashed lane labelled `Project: vf-ie-datahub — Data layer`, containing two boxes:
- `GCS` — `source docs (PDF/Word/Excel) + parsed VBOP JSON + validation.json`, `gs://vfie-dh-customer-fixed`, `gs://vfie-dh-customer-complex-fixed`, `app logs: quote-to-order-app-logs/`
- `BigQuery (BSP & EVO)` — `dataset: vfie_dh_lake_customer_complex_fixed_s`, `read: TEMP_PM, TEMP_item_model, raw_item_model`, `write: sales order + rows`, `access: dataEditor + jobUser`

**Connections — numbered arrows in this exact order:**
1. User → Streamlit UI: `1. Begin (Opportunity ID)`
2. Streamlit UI → FastAPI: `2. GET /exists/{opp}`
3. FastAPI → GCS: `3. check processed_files/{opp}/` (dashed)
4. Streamlit UI → FastAPI: `4. POST /log (session, user, datetime)`
5. FastAPI → GCS: `5. write app log`
6. Streamlit UI → FastAPI: `6. POST /process/{opp}`
7. FastAPI → Retrieval Agent: `7. invoke workflow`
8. Retrieval Agent ↔ GCS: `8. read VBOP / validation.json` (bidirectional)
9. Retrieval Agent ↔ Responsible AI Proxy: `9. interpret docs (LLM)` (bidirectional, **red dashed**)
10. Validation Agent ↔ Responsible AI Proxy: `10. run validation checks (LLM)` (bidirectional, **red dashed**)
11. Responsible AI Proxy ↔ Vertex AI Gemini: `11. pre/post checks · call model` (bidirectional, **red dashed**)
12. Product Code Navigator ↔ BigQuery: `12. deterministic lookup (SQL)` (bidirectional, **solid black — NOT a model call**)
13. BSP Extraction Agent ↔ Responsible AI Proxy: `13. assemble order (LLM)` (bidirectional, **red dashed**)
14. Email Drafter Agent ↔ Responsible AI Proxy: `14. draft email (LLM) · reads validation.json` (bidirectional, **red dashed**)
15. ADK Workflow → FastAPI: `15. return result (order, rows, email)`
16. FastAPI → BigQuery: `16. save sales order + rows`
17. Streamlit UI → FastAPI: `17. POST /feedback/{opp} (raise request)`
18. FastAPI → GCS: `18. save feedback → validation.json + update app log`

**Legend (bottom):** `solid = call/data · red dashed = governed LLM call (via RAI → Gemini)`

**Important constraints:**
- Every LLM call must visibly pass through the Responsible AI Layer Proxy before reaching Gemini — no agent connects to Gemini directly.
- The Validation Agent and Product Code Navigator must clearly appear to run **in parallel** (same row, branching from Retrieval, converging into BSP Extraction).
- The Product Code Navigator's BigQuery lookup must be visually distinct from LLM calls (solid, not red dashed) because it is deterministic, not model-based.
- Ensure no boxes overlap and arrow labels remain readable.

---

Same three checks as before when reviewing their AI's output: no agent bypassing the RAI proxy, the BigQuery lookup not styled as an LLM call, and the parallel branch actually rendered as parallel (AI tools often flatten it into a sequential chain).
