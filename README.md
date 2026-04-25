# n8n SMS Audit

A single-file Python diagnostic that ingests an n8n workflow JSON export and produces a prioritized audit report — covering the failure modes that actually break n8n + Twilio + Airtable SMS pipelines in production.

No credentials required. No API calls. Pure static analysis on the workflow JSON.

## Quick start

```bash
python audit.py path/to/workflow.json                  # Markdown report
python audit.py path/to/workflow.json --format json    # JSON report
```

Try it on the included sample:

```bash
python audit.py sample_workflow.json
```

You should see ~11 findings on the 4-node sample, ranging from a hardcoded Twilio auth token to silent-drop rate-limit issues.

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

## What it deliberately does NOT do

- It does **not** connect to your Twilio or Airtable accounts. Audit-as-static-analysis means no credential exposure and no API spend.
- It does **not** modify your workflow file. Read-only.
- It does **not** catch logic-level bugs (wrong field names, business rules). It catches the structural anti-patterns that produce 90% of production tickets.

## Extending

Each rule is a function in `audit.py`, registered in the `CHECKS` list. Add a new rule by writing a function with signature `(report: Report, workflow: dict) -> None` and appending it.

## License

MIT. Use it freely. PRs welcome.
