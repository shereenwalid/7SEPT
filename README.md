You can test entirely with the API — no UI needed. The UI just calls these same endpoints.

Here's the sequence for a full run on port 8081:

**1. Health check** (confirm it's up)
```bash
curl -s http://localhost:8081/health
```

**2. Check the opportunity exists**
```bash
curl -s http://localhost:8081/exists/OPP-0007063044
```
Expect `{"exists": true, "path": "gs://..."}`.

**3. Write the session log** (what the Begin button does)
```bash
curl -s -X POST http://localhost:8081/log \
  -H "Content-Type: application/json" \
  -d '{"session_id":"s-test001","opp_id":"OPP-0007063044"}'
```

**4. Run the pipeline** — this is the one that produces the cost report
```bash
curl -s -X POST http://localhost:8081/process/OPP-0007063044
```
This blocks until the run finishes (could be a couple of minutes). Watch the backend terminal — you'll see the `[cost][bq]` lines per query and the final `[cost] opp=... duration=... tokens in=... out=...` summary, plus `[cost] report written: cost_reports/OPP-0007063044_cost.txt`.

**5. Read the cost report**
```bash
cat cost_reports/OPP-0007063044_cost.txt
```

**Optional extras:**
```bash
# live progress (run in another terminal while step 4 is going)
curl -s http://localhost:8081/status/OPP-0007063044

# the shaped result
curl -s http://localhost:8081/result/OPP-0007063044

# raise a request (tests feedback + log update)
curl -s -X POST http://localhost:8081/feedback/OPP-0007063044 \
  -H "Content-Type: application/json" \
  -d '{"check_key":"C24_vf_quote_to_customer_attached","option":"raise_sales","comment":"test","session_id":"s-test001"}'
```

Realistically **steps 2 and 4 are all you need** for the cost measurement — step 4 alone triggers the tokens, duration, and BigQuery bytes.

Two things to be aware of:
- **Set the rate env vars before starting** the server, or the cost report will show zeros (it'll warn you in the file). The measured numbers — duration, tokens, MB — appear regardless, so you can always do the arithmetic yourself afterwards.
- **`AGENT_MOCK` must be off** (unset or `0`) for a real run. In mock mode you'll get sample data with no real tokens or BigQuery queries, so the cost report would be meaningless.

Run it against 2–3 opportunities of different sizes and you'll have your low/typical/high range.
