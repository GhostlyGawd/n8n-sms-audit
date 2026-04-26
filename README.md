# n8n SMS Audit

[![CI](https://github.com/GhostlyGawd/n8n-sms-audit/actions/workflows/ci.yml/badge.svg)](https://github.com/GhostlyGawd/n8n-sms-audit/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

> **Try it now without installing anything: https://ghostlygawd.github.io/n8n-sms-audit/**
> Paste your workflow JSON, see findings in your browser. Nothing uploaded — runs entirely client-side via Pyodide.

A single-file Python diagnostic that ingests an n8n workflow JSON export and produces a prioritized audit report — covering the failure modes that actually break n8n + Twilio + Airtable SMS pipelines in production.

No credentials required. No API calls. Pure static analysis on the workflow JSON.

## Quick start

```bash
python audit.py path/to/workflow.json                  # Markdown report
python audit.py path/to/workflow.json --format json    # JSON report
python audit.py path/to/workflow.json --fix > fixed.json   # Auto-remediate the mechanical fixes
```

Try it on the included samples:

```bash
python audit.py sample_workflow.json              # 4-node sample → 11 findings
python audit.py samples/clean.json                # 7-node clean flow → 0 findings
python audit.py samples/complex_multi_issue.json  # 6-node real-world mess → 12 findings
```

The `clean.json` sample is the reference for what a well-built workflow looks like — every rule passes.

## What it catches

| Severity | Check |
|----------|-------|
| Critical | Twilio node with no error branch — silent halt on 429/5xx |
| Critical | Hardcoded credential in node parameters |
| High | Twilio recipient not in E.164 format (Twilio error 21211) |
| High | Airtable node with no error branch |
| High | No global error workflow — failures go unnoticed |
| High | No deduplication before Twilio send — webhook retries cause duplicate SMS |
| Medium | Twilio sends without batching — 1 msg/sec default cap silently drops |
| Medium | Webhook with no Respond node — >30s workflows time out the caller |
| Medium | SMS body over 160 chars — multi-segment billing |
| Low | Airtable write without `typecast: true` |

## Sample output (excerpt)

```
# n8n Audit Report — `Customer Onboarding SMS Flow`

**Nodes scanned:** 4

**Findings by severity:**
- Critical: 2
- High:     5
- Medium:   3
- Low:      1

## 1. [CRITICAL] Twilio node has no error branch
**Category:** error_handling  •  **Node:** Send Welcome SMS

Node 'Send Welcome SMS' will halt the workflow on a 429/5xx and downstream
Airtable updates will be skipped, creating phantom 'pending' rows in your CRM.

**Recommended fix:**
Set the node's 'On Error' to 'Continue (using error output)' and route the
error branch to an Airtable update that flips status='failed' + records the
error message.
```

Each finding ships with a copy-pasteable fix.

## Auto-remediation (`--fix`)

```bash
python audit.py workflow.json --fix > workflow.fixed.json
```

Reads the workflow, runs the audit, and writes a remediated workflow JSON to stdout (a summary of applied fixes goes to stderr). The input file is never mutated.

**What `--fix` mechanically applies:**

| Finding | Mechanical fix |
|---------|---------------|
| Twilio/Airtable node has no error branch | Sets `onError: "continueErrorOutput"` on the node |
| Airtable write without typecast | Sets `parameters.options.typecast: true` |

**What `--fix` deliberately leaves to a human:**

- Hardcoded credentials → require knowing your credential names
- Non-E.164 phone numbers → require knowing your normalization rules
- Missing dedup → require knowing what to dedup on
- Missing global error workflow → require choosing the error-handler workflow ID
- Missing webhook Respond node → require choosing the response payload
- SMS body length / batching → require human judgment on copy and rate

The result: a workflow JSON you can re-import to n8n that has the safe, mechanical fixes already applied. Then you remediate the remaining findings by hand. Idempotent — running `--fix` on an already-fixed workflow is a no-op.

## What it deliberately does NOT do

- It does **not** connect to your Twilio or Airtable accounts. Audit-as-static-analysis means no credential exposure and no API spend.
- It does **not** modify your workflow file. Read-only.
- It does **not** catch logic-level bugs (wrong field names, business rules). It catches the structural anti-patterns that produce 90% of production tickets.

## Extending

Each rule is a function in `audit.py`, registered in the `CHECKS` list. Add a new rule by writing a function with signature `(report: Report, workflow: dict) -> None` and appending it. Add a corresponding test in `tests/test_audit.py` covering both the positive and negative case.

## Tests

```bash
python -m pytest tests -v
```

22 tests covering every rule (positive + negative path) plus a clean-workflow smoke test that fails if any rule starts producing false positives. CI runs across Python 3.10, 3.11, and 3.12.

## Browser preview server

For prospects who want to try the audit without cloning the repo:

```bash
python audit_server.py                 # http://localhost:8000
python audit_server.py --host 0.0.0.0 --port 8000   # public bind for cloud deploy
PORT=8000 python audit_server.py       # honors $PORT (Heroku/Railway/Fly)
```

A single-file HTTP server using only the Python stdlib — no Flask, no FastAPI, no dependencies. Renders a paste-and-audit form, runs the audit on the server, returns a styled HTML report. Deploys to any free PaaS in one click.

Health check at `/health` returns `ok`. Audit runs entirely on the server &mdash; no data is logged or stored.

## License

MIT. Use it freely. PRs welcome.
