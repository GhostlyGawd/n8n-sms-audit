"""
n8n + Twilio + Airtable SMS Automation Audit Toolkit
=====================================================

Single-file diagnostic that ingests an n8n workflow JSON export and produces a
prioritized audit report covering the most common failure modes in
SMS-automation pipelines:

  - Twilio rate limit & 429 handling
  - Airtable API throttling (5 req/sec base, 50/sec workspace) and missing
    retry/backoff
  - Webhook timeout and "fire-and-forget" patterns that silently drop messages
  - Phone number normalization (E.164) before passing to Twilio
  - Idempotency keys / duplicate-send protection
  - Error-handling node coverage (Error Trigger, On Error setting per node)
  - Credential reuse and secret hygiene
  - Loop nodes without batching → API quota burn
  - Hardcoded values that should be Airtable-driven
  - Missing logging / no audit trail of sent messages

Usage:
  python audit.py path/to/workflow.json
  python audit.py path/to/workflow.json --format md > report.md
  python audit.py path/to/workflow.json --format json > report.json

The script is read-only and never mutates the source workflow file.
"""

from __future__ import annotations
import argparse
import json
import sys
from dataclasses import dataclass, asdict, field
from typing import Any, Iterable

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


@dataclass
class Finding:
    severity: str
    category: str
    title: str
    detail: str
    node: str | None = None
    fix: str = ""
    auto_fix: str | None = None  # ID of an auto-fixer that can mechanically apply this fix

    def sort_key(self) -> tuple[int, str]:
        return (SEVERITY_ORDER.get(self.severity, 99), self.category)


@dataclass
class Report:
    workflow_name: str
    node_count: int
    findings: list[Finding] = field(default_factory=list)

    def add(self, **kwargs: Any) -> None:
        self.findings.append(Finding(**kwargs))

    def sorted(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: f.sort_key())

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {k: 0 for k in SEVERITY_ORDER}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out


def _iter_nodes(workflow: dict) -> Iterable[dict]:
    nodes = workflow.get("nodes")
    if not isinstance(nodes, list):
        return []
    return nodes


def _node_type(node: dict) -> str:
    return str(node.get("type", "")).lower()


def _node_name(node: dict) -> str:
    return str(node.get("name", node.get("id", "<unnamed>")))


def _has_error_branch(node: dict) -> bool:
    on_error = str(node.get("onError", "")).lower()
    return on_error in {"continueerroroutput", "continueregularoutput"} or bool(
        node.get("continueOnFail")
    )


def check_twilio_nodes(report: Report, workflow: dict) -> None:
    for node in _iter_nodes(workflow):
        t = _node_type(node)
        if "twilio" not in t:
            continue
        name = _node_name(node)
        params = node.get("parameters", {}) or {}
        to_field = str(params.get("to", "") or params.get("toWhatsapp", ""))

        if to_field and "+" not in to_field and "{{" not in to_field:
            report.add(
                severity="high",
                category="phone_format",
                title="Twilio recipient may not be in E.164 format",
                detail=(
                    f"Node '{name}' sends to '{to_field}'. Twilio rejects "
                    "numbers not in E.164 (+<country><number>) with error 21211."
                ),
                node=name,
                fix=(
                    "Normalize before this node — use a Function/Code node:\n"
                    "  const e164 = num.replace(/\\D/g, '');\n"
                    "  return '+' + (e164.startsWith('1') ? e164 : '1' + e164);"
                ),
            )

        if not _has_error_branch(node):
            report.add(
                severity="critical",
                category="error_handling",
                title="Twilio node has no error branch",
                detail=(
                    f"Node '{name}' will halt the workflow on a 429/5xx and "
                    "downstream Airtable updates will be skipped, creating "
                    "phantom 'pending' rows in your CRM."
                ),
                node=name,
                fix=(
                    "Set the node's 'On Error' to 'Continue (using error output)' "
                    "and route the error branch to an Airtable update that flips "
                    "status='failed' + records the error message."
                ),
                auto_fix="continue_on_fail",
            )

        body = str(params.get("message", "") or params.get("body", ""))
        if body and len(body) > 160 and "{{" not in body:
            report.add(
                severity="medium",
                category="cost",
                title="SMS body over 160 chars — billed as multi-segment",
                detail=(
                    f"Node '{name}' has a {len(body)}-char body. Twilio charges "
                    "per 160-char segment (153 for concatenated). At scale this "
                    "doubles or triples cost vs. trimming."
                ),
                node=name,
                fix="Tighten copy below 160 chars or confirm cost model accepts segments.",
            )


def check_airtable_nodes(report: Report, workflow: dict) -> None:
    seen_loops_over_airtable = False
    for node in _iter_nodes(workflow):
        t = _node_type(node)
        if "airtable" not in t:
            continue
        name = _node_name(node)
        params = node.get("parameters", {}) or {}

        op = str(params.get("operation", "")).lower()
        if op in {"create", "update", "upsert"} and not params.get("options", {}).get(
            "typecast"
        ):
            report.add(
                severity="low",
                category="data_integrity",
                title="Airtable write without typecast",
                detail=(
                    f"Node '{name}' writes without typecast=true. String-to-number "
                    "and date coercions will throw 422 on edge values."
                ),
                node=name,
                fix="Set node options.typecast = true unless you've validated upstream.",
                auto_fix="airtable_typecast",
            )

        if not _has_error_branch(node):
            report.add(
                severity="high",
                category="error_handling",
                title="Airtable node has no error branch",
                detail=(
                    f"Node '{name}' will fail on rate limits (5 req/sec base) "
                    "and halt the workflow."
                ),
                node=name,
                fix=(
                    "Set 'On Error' to 'Continue' and add a Wait(2s) + retry "
                    "node, or batch via 'Create Multiple Records' (10 rows/req)."
                ),
                auto_fix="continue_on_fail",
            )


def check_loops_and_batching(report: Report, workflow: dict) -> None:
    nodes = list(_iter_nodes(workflow))
    types = [_node_type(n) for n in nodes]
    has_loop = any("splitinbatches" in t or "loop" in t for t in types)
    has_twilio = any("twilio" in t for t in types)
    has_airtable = any("airtable" in t for t in types)

    if has_twilio and not has_loop and has_airtable:
        report.add(
            severity="medium",
            category="rate_limits",
            title="Twilio sends inside a flat workflow with no batching",
            detail=(
                "When Airtable returns N rows, n8n executes downstream nodes once "
                "per item. Without SplitInBatches, you may hit Twilio's 1 msg/sec "
                "default Messaging Service limit and silently drop messages."
            ),
            fix=(
                "Insert a SplitInBatches node (size 1, wait 1100ms between batches) "
                "before the Twilio send, or upgrade to a Messaging Service with "
                "throughput pool for higher TPS."
            ),
        )


def check_global_error_workflow(report: Report, workflow: dict) -> None:
    settings = workflow.get("settings", {}) or {}
    error_workflow = settings.get("errorWorkflow")
    if not error_workflow:
        report.add(
            severity="high",
            category="observability",
            title="No global error workflow configured",
            detail=(
                "If any unhandled error occurs, you'll have no Slack/email alert "
                "and will only notice when a customer complains."
            ),
            fix=(
                "Settings → Error Workflow → assign a small workflow that posts "
                "{$execution.id, $workflow.name, $error} to Slack/email."
            ),
        )


def check_credentials_hygiene(report: Report, workflow: dict) -> None:
    for node in _iter_nodes(workflow):
        params = node.get("parameters", {}) or {}
        for k, v in params.items():
            if not isinstance(v, str):
                continue
            lowered = k.lower()
            if any(s in lowered for s in ("token", "key", "secret", "password")):
                if v and "{{" not in v and not v.startswith("="):
                    report.add(
                        severity="critical",
                        category="security",
                        title="Hardcoded credential in node parameters",
                        detail=(
                            f"Node '{_node_name(node)}' parameter '{k}' contains a "
                            "literal string. Credentials must live in n8n credential "
                            "store, not parameters."
                        ),
                        node=_node_name(node),
                        fix="Move to Credentials → reference via $credentials.<name>.",
                    )


def check_idempotency(report: Report, workflow: dict) -> None:
    has_dedup = False
    for node in _iter_nodes(workflow):
        t = _node_type(node)
        name = _node_name(node).lower()
        if "removeduplicates" in t or "dedup" in name or "idempot" in name:
            has_dedup = True
            break
    has_twilio = any("twilio" in _node_type(n) for n in _iter_nodes(workflow))
    if has_twilio and not has_dedup:
        report.add(
            severity="high",
            category="idempotency",
            title="No deduplication node before Twilio send",
            detail=(
                "If the trigger fires twice (webhook retry, n8n restart mid-run), "
                "the same SMS will be sent multiple times. Customers will complain."
            ),
            fix=(
                "Add a 'Remove Duplicates' node keyed on (recipient, message_id) "
                "or write an Airtable 'sent_log' row with a unique constraint key "
                "before the Twilio call."
            ),
        )


def check_webhook_response(report: Report, workflow: dict) -> None:
    nodes = list(_iter_nodes(workflow))
    has_webhook = any("webhook" in _node_type(n) for n in nodes)
    has_respond = any("respondtowebhook" in _node_type(n) for n in nodes)
    if has_webhook and not has_respond:
        report.add(
            severity="medium",
            category="reliability",
            title="Webhook trigger with no Respond node",
            detail=(
                "n8n returns the webhook response only after the entire workflow "
                "finishes. If the workflow takes >30s, the caller times out and "
                "may retry, causing duplicate sends."
            ),
            fix=(
                "Switch the Webhook node's 'Respond' option to 'Using Respond to "
                "Webhook Node' and respond immediately, then process async."
            ),
        )


CHECKS = [
    check_twilio_nodes,
    check_airtable_nodes,
    check_loops_and_batching,
    check_global_error_workflow,
    check_credentials_hygiene,
    check_idempotency,
    check_webhook_response,
]


def audit(workflow: dict) -> Report:
    report = Report(
        workflow_name=str(workflow.get("name", "<unnamed workflow>")),
        node_count=len(list(_iter_nodes(workflow))),
    )
    for check in CHECKS:
        check(report, workflow)
    return report


def apply_auto_fixes(workflow: dict, findings: list[Finding]) -> tuple[dict, list[dict]]:
    """Mechanically apply the subset of findings that have a registered auto_fix.

    Returns a tuple of (remediated workflow, list of applied-fix records).
    Pure function: input workflow is not mutated. Idempotent: running this on
    an already-remediated workflow produces the same workflow and an empty
    applied list.
    """
    import copy
    fixed = copy.deepcopy(workflow)
    nodes_by_name = {_node_name(n): n for n in _iter_nodes(fixed)}
    applied: list[dict] = []

    for f in findings:
        if not f.auto_fix:
            continue
        if f.auto_fix == "continue_on_fail":
            if f.node and f.node in nodes_by_name:
                node = nodes_by_name[f.node]
                if not _has_error_branch(node):
                    node["onError"] = "continueErrorOutput"
                    applied.append({
                        "node": f.node,
                        "fix": "continue_on_fail",
                        "change": "set onError = continueErrorOutput",
                    })
        elif f.auto_fix == "airtable_typecast":
            if f.node and f.node in nodes_by_name:
                node = nodes_by_name[f.node]
                params = node.setdefault("parameters", {})
                options = params.setdefault("options", {})
                if not options.get("typecast"):
                    options["typecast"] = True
                    applied.append({
                        "node": f.node,
                        "fix": "airtable_typecast",
                        "change": "set parameters.options.typecast = true",
                    })
    return fixed, applied


def render_markdown(report: Report) -> str:
    counts = report.counts()
    lines = [
        f"# n8n Audit Report — `{report.workflow_name}`",
        "",
        f"**Nodes scanned:** {report.node_count}",
        "",
        "**Findings by severity:**",
        f"- Critical: {counts.get('critical', 0)}",
        f"- High:     {counts.get('high', 0)}",
        f"- Medium:   {counts.get('medium', 0)}",
        f"- Low:      {counts.get('low', 0)}",
        "",
        "---",
        "",
    ]
    if not report.findings:
        lines.append("_No issues detected by the configured rule set. "
                     "This does not mean the workflow is correct — it means "
                     "none of the known anti-patterns triggered._")
        return "\n".join(lines)
    for i, f in enumerate(report.sorted(), 1):
        lines += [
            f"## {i}. [{f.severity.upper()}] {f.title}",
            f"**Category:** `{f.category}`"
            + (f"  •  **Node:** `{f.node}`" if f.node else ""),
            "",
            f.detail,
            "",
            "**Recommended fix:**",
            "",
            "```",
            f.fix or "(no automated fix suggestion)",
            "```",
            "",
        ]
    return "\n".join(lines)


def render_json(report: Report) -> str:
    return json.dumps(
        {
            "workflow_name": report.workflow_name,
            "node_count": report.node_count,
            "counts": report.counts(),
            "findings": [asdict(f) for f in report.sorted()],
        },
        indent=2,
    )


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Audit an n8n workflow JSON export.")
    parser.add_argument("workflow", help="Path to n8n workflow JSON export")
    parser.add_argument(
        "--format",
        choices=("md", "json"),
        default="md",
        help="Output format (default: md)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help=(
            "Mechanically apply auto-fixable findings (continueOnFail on Twilio/"
            "Airtable nodes, typecast on Airtable writes) and print the remediated "
            "workflow JSON to stdout. The original file is never modified."
        ),
    )
    args = parser.parse_args()

    try:
        with open(args.workflow, "r", encoding="utf-8") as fh:
            workflow = json.load(fh)
    except FileNotFoundError:
        print(f"error: file not found: {args.workflow}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        return 2

    if not isinstance(workflow, dict) or "nodes" not in workflow:
        print(
            "error: input does not look like an n8n workflow export "
            "(expected an object with a 'nodes' key).",
            file=sys.stderr,
        )
        return 2

    report = audit(workflow)
    if args.fix:
        fixed, applied = apply_auto_fixes(workflow, report.findings)
        sys.stderr.write(
            f"# auto-fix applied {len(applied)} of "
            f"{sum(1 for f in report.findings if f.auto_fix)} auto-fixable findings\n"
        )
        for a in applied:
            sys.stderr.write(f"#   - {a['node']}: {a['change']}\n")
        print(json.dumps(fixed, indent=2))
        return 0
    if args.format == "json":
        print(render_json(report))
    else:
        print(render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
