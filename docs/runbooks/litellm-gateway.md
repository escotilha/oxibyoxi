# LiteLLM gateway runbook — Mac Mini key provisioning

This runbook walks the operator through connecting oxi to the LiteLLM proxy
already running on the operator's Mac Mini (as set up by the local-inference
skill), provisioning per-role virtual keys with individual budget caps, and
rotating keys when needed.

---

## Prerequisites

| Requirement | Check |
|---|---|
| LiteLLM proxy running on Mac Mini | `curl http://<tailscale-hostname>:4000/health` returns `{"status":"healthy"}` |
| Tailscale connected on both machines | `tailscale status` shows the Mac Mini peer |
| LiteLLM master key available | Set during your local-inference skill setup |
| `OXI_GATEWAY_BASE_URL` known | The Tailscale MagicDNS URL, e.g. `http://mac-mini.tail1234.ts.net:4000` |

If the `/health` check fails, see
[§ Fallback behaviour when the gateway is unreachable](#fallback-behaviour-when-the-gateway-is-unreachable)
before continuing.

---

## Step 1 — Discover the gateway URL

Find your Mac Mini's Tailscale hostname:

```bash
tailscale status | grep mac-mini
# 100.x.y.z   mac-mini   ...   active
```

Then confirm MagicDNS resolves it:

```bash
curl http://mac-mini.tail<id>.ts.net:4000/health
# {"status":"healthy","litellm_version":"..."}
```

Export the URL for the steps below:

```bash
export OXI_GATEWAY_BASE_URL=http://mac-mini.tail<id>.ts.net:4000
```

Add this to your shell profile (`.zshrc` / `.bashrc`) so it persists across
sessions.

---

## Step 2 — Provision a virtual key

oxi uses three per-role virtual keys so the gateway can apply individual
budget limits. Create them in order:

### 2a — heartbeat key (`oxi-heartbeat`)

Used by the `heartbeat-triage` role. Set a tight daily budget (the role
makes short, cheap calls).

```bash
curl -s -X POST "${OXI_GATEWAY_BASE_URL}/key/generate" \
  -H "Authorization: Bearer <your-litellm-master-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "oxi-heartbeat",
    "max_budget": 1.0,
    "budget_duration": "1d",
    "metadata": {"created_by": "oxi-t2-36"}
  }' | jq -r '.key'
```

Copy the returned `sk-...` value and export it:

```bash
export OXI_GATEWAY_KEY_HEARTBEAT=sk-<returned-value>
```

### 2b — classifier key (`oxi-classifier`)

Used by the `classifier` role. A moderate daily budget.

```bash
curl -s -X POST "${OXI_GATEWAY_BASE_URL}/key/generate" \
  -H "Authorization: Bearer <your-litellm-master-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "oxi-classifier",
    "max_budget": 2.0,
    "budget_duration": "1d",
    "metadata": {"created_by": "oxi-t2-36"}
  }' | jq -r '.key'
```

```bash
export OXI_GATEWAY_KEY_CLASSIFIER=sk-<returned-value>
```

### 2c — summary key (`oxi-summary`)

Used by the `summary` role. A moderate daily budget.

```bash
curl -s -X POST "${OXI_GATEWAY_BASE_URL}/key/generate" \
  -H "Authorization: Bearer <your-litellm-master-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "oxi-summary",
    "max_budget": 2.0,
    "budget_duration": "1d",
    "metadata": {"created_by": "oxi-t2-36"}
  }' | jq -r '.key'
```

```bash
export OXI_GATEWAY_KEY_SUMMARY=sk-<returned-value>
```

---

## Step 3 — Verify oxi can reach the gateway

Run a reconciliation-only tick (no Claude spend, no real inference):

```bash
oxi v3 tick --times 1
```

To confirm the gateway health check specifically:

```bash
OXI_INFERENCE_OFFLINE=0 python -m pytest oxi-core/tests/test_litellm_gateway_smoke.py -v
```

Expected:

```text
PASSED test_litellm_gateway_smoke.py::test_gateway_health_reachable
```

---

## Step 4 — Persist env vars

Add the following to your shell profile and any process supervisor (launchd
plist, systemd unit, etc.) that starts the oxi engine:

```bash
# LiteLLM gateway (oxi T2-36)
export OXI_GATEWAY_BASE_URL=http://mac-mini.tail<id>.ts.net:4000
export OXI_GATEWAY_KEY_HEARTBEAT=sk-...
export OXI_GATEWAY_KEY_CLASSIFIER=sk-...
export OXI_GATEWAY_KEY_SUMMARY=sk-...
```

> **Security note:** treat these keys like passwords. Do not commit them to
> version control. If you use a secrets manager (1Password, Vault, etc.),
> inject them from there instead.

---

## Rotating a key

To rotate `oxi-heartbeat` (repeat for the other roles as needed):

### 1 — Generate the replacement key

```bash
curl -s -X POST "${OXI_GATEWAY_BASE_URL}/key/generate" \
  -H "Authorization: Bearer <your-litellm-master-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "oxi-heartbeat-new",
    "max_budget": 1.0,
    "budget_duration": "1d"
  }' | jq -r '.key'
```

### 2 — Update the env var

```bash
export OXI_GATEWAY_KEY_HEARTBEAT=sk-<new-value>
```

Restart the oxi engine process to pick up the new key.

### 3 — Delete the old key

First, retrieve the key hash of the old key:

```bash
curl -s "${OXI_GATEWAY_BASE_URL}/key/info?key_alias=oxi-heartbeat" \
  -H "Authorization: Bearer <your-litellm-master-key>" | jq -r '.info.token'
```

Then delete it:

```bash
curl -s -X DELETE "${OXI_GATEWAY_BASE_URL}/key/delete" \
  -H "Authorization: Bearer <your-litellm-master-key>" \
  -H "Content-Type: application/json" \
  -d '{"keys": ["<old-token-hash>"]}'
```

### 4 — Rename the new alias

```bash
curl -s -X POST "${OXI_GATEWAY_BASE_URL}/key/update" \
  -H "Authorization: Bearer <your-litellm-master-key>" \
  -H "Content-Type: application/json" \
  -d '{"key": "sk-<new-value>", "key_alias": "oxi-heartbeat"}'
```

---

## Adjusting a budget cap

To raise the daily budget for `oxi-classifier` to $5:

```bash
curl -s -X POST "${OXI_GATEWAY_BASE_URL}/key/update" \
  -H "Authorization: Bearer <your-litellm-master-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "oxi-classifier",
    "max_budget": 5.0,
    "budget_duration": "1d"
  }'
```

The change takes effect immediately — no restart required.

---

## Viewing usage

List all oxi keys and their current spend:

```bash
curl -s "${OXI_GATEWAY_BASE_URL}/key/list?include_team_keys=false" \
  -H "Authorization: Bearer <your-litellm-master-key>" \
  | jq '.keys[] | select(.key_alias | startswith("oxi-")) | {alias: .key_alias, spent: .spend, budget: .max_budget}'
```

---

## Fallback behaviour when the gateway is unreachable

oxi's heartbeat-triage path (T2-38) probes the gateway's `/health` endpoint
before each inference call. If the probe times out or returns a non-2xx
response:

- The triage step **disables itself** for that tick and logs a
  `heartbeat.gateway_unreachable` warning.
- `heartbeat.py` continues running its non-LLM reaping logic (stale task
  detection, ledger writes) as before T2-36.
- No crash, no alarm — the operator sees the warning in `oxi status` and
  the dashboard's event feed.

To silence the probe during planned gateway maintenance, set:

```bash
export OXI_INFERENCE_OFFLINE=1
```

This also skips the CI smoke test that checks `/health`.

---

## Environment variable reference

| Variable | Purpose | Example value |
|---|---|---|
| `OXI_GATEWAY_BASE_URL` | LiteLLM proxy base URL (no trailing slash) | `http://mac-mini.tail1234.ts.net:4000` |
| `OXI_GATEWAY_KEY_HEARTBEAT` | Virtual key for the `heartbeat-triage` role | `sk-abc123...` |
| `OXI_GATEWAY_KEY_CLASSIFIER` | Virtual key for the `classifier` role | `sk-def456...` |
| `OXI_GATEWAY_KEY_SUMMARY` | Virtual key for the `summary` role | `sk-ghi789...` |
| `OXI_INFERENCE_OFFLINE` | Set to `1` to skip gateway probes (offline / maintenance) | `1` |

---

## Related

- `oxi-core/src/oxi_core/defaults/inference.yaml` — gateway provider block
- `docs/runbooks/install.md` — initial oxi install
- T2-38 — heartbeat-triage wiring to the InferenceGateway
