"""Tests for the n8n SMS audit rules.

Each test feeds a minimal synthetic workflow into a single check and asserts
the expected finding fires (and does not fire on the negative case). Tests
target the public surface only — they never poke private helpers.
"""

from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit import audit, apply_auto_fixes, Report  # noqa: E402


def _wf(*nodes, settings=None, name="t"):
    return {"name": name, "nodes": list(nodes), "settings": settings or {}}


def _has_finding(report: Report, *, category: str, severity: str | None = None) -> bool:
    return any(
        f.category == category and (severity is None or f.severity == severity)
        for f in report.findings
    )


# ---------- Twilio checks ----------------------------------------------------

def test_twilio_no_error_branch_fires_critical():
    wf = _wf({
        "id": "1", "name": "Send", "type": "n8n-nodes-base.twilio",
        "parameters": {"to": "+15551234567", "message": "hi"},
    })
    r = audit(wf)
    assert _has_finding(r, category="error_handling", severity="critical")


def test_twilio_with_continue_on_fail_clears_error_branch_finding():
    wf = _wf({
        "id": "1", "name": "Send", "type": "n8n-nodes-base.twilio",
        "parameters": {"to": "+15551234567", "message": "hi"},
        "continueOnFail": True,
    })
    r = audit(wf)
    twilio_error_findings = [
        f for f in r.findings
        if f.category == "error_handling" and f.node == "Send"
    ]
    assert not twilio_error_findings


def test_non_e164_recipient_flagged_high():
    wf = _wf({
        "id": "1", "name": "Send", "type": "n8n-nodes-base.twilio",
        "parameters": {"to": "5551234567", "message": "hi"},
        "continueOnFail": True,
    })
    r = audit(wf)
    assert _has_finding(r, category="phone_format", severity="high")


def test_e164_recipient_does_not_trigger():
    wf = _wf({
        "id": "1", "name": "Send", "type": "n8n-nodes-base.twilio",
        "parameters": {"to": "+15551234567", "message": "hi"},
        "continueOnFail": True,
    })
    r = audit(wf)
    assert not _has_finding(r, category="phone_format")


def test_templated_recipient_does_not_trigger():
    wf = _wf({
        "id": "1", "name": "Send", "type": "n8n-nodes-base.twilio",
        "parameters": {"to": "{{$json.phone}}", "message": "hi"},
        "continueOnFail": True,
    })
    r = audit(wf)
    assert not _has_finding(r, category="phone_format")


def test_long_sms_body_flagged_medium():
    body = "x" * 200
    wf = _wf({
        "id": "1", "name": "Send", "type": "n8n-nodes-base.twilio",
        "parameters": {"to": "+15551234567", "message": body},
        "continueOnFail": True,
    })
    r = audit(wf)
    assert _has_finding(r, category="cost", severity="medium")


def test_short_sms_body_does_not_trigger_cost():
    wf = _wf({
        "id": "1", "name": "Send", "type": "n8n-nodes-base.twilio",
        "parameters": {"to": "+15551234567", "message": "hi"},
        "continueOnFail": True,
    })
    r = audit(wf)
    assert not _has_finding(r, category="cost")


# ---------- Airtable checks --------------------------------------------------

def test_airtable_no_error_branch_high():
    wf = _wf({
        "id": "1", "name": "Get", "type": "n8n-nodes-base.airtable",
        "parameters": {"operation": "list"},
    })
    r = audit(wf)
    assert _has_finding(r, category="error_handling", severity="high")


def test_airtable_create_without_typecast_low():
    wf = _wf({
        "id": "1", "name": "Create", "type": "n8n-nodes-base.airtable",
        "parameters": {"operation": "create"},
        "continueOnFail": True,
    })
    r = audit(wf)
    assert _has_finding(r, category="data_integrity", severity="low")


def test_airtable_with_typecast_does_not_trigger_low():
    wf = _wf({
        "id": "1", "name": "Create", "type": "n8n-nodes-base.airtable",
        "parameters": {"operation": "create", "options": {"typecast": True}},
        "continueOnFail": True,
    })
    r = audit(wf)
    assert not _has_finding(r, category="data_integrity")


# ---------- Cross-cutting checks ---------------------------------------------

def test_no_global_error_workflow_high():
    wf = _wf({"id": "1", "name": "n", "type": "n8n-nodes-base.set"})
    r = audit(wf)
    assert _has_finding(r, category="observability", severity="high")


def test_global_error_workflow_clears_finding():
    wf = _wf(
        {"id": "1", "name": "n", "type": "n8n-nodes-base.set"},
        settings={"errorWorkflow": "wf-error-handler"},
    )
    r = audit(wf)
    assert not _has_finding(r, category="observability")


def test_hardcoded_credential_critical():
    wf = _wf({
        "id": "1", "name": "Send", "type": "n8n-nodes-base.twilio",
        "parameters": {
            "to": "+15551234567",
            "message": "hi",
            "authToken": "AC_literal_secret_value_here",
        },
        "continueOnFail": True,
    })
    r = audit(wf)
    assert _has_finding(r, category="security", severity="critical")


def test_credential_via_expression_does_not_fire():
    wf = _wf({
        "id": "1", "name": "Send", "type": "n8n-nodes-base.twilio",
        "parameters": {
            "to": "+15551234567",
            "message": "hi",
            "authToken": "={{$credentials.twilio.authToken}}",
        },
        "continueOnFail": True,
    })
    r = audit(wf)
    assert not _has_finding(r, category="security")


def test_idempotency_missing_when_twilio_present_high():
    wf = _wf({
        "id": "1", "name": "Send", "type": "n8n-nodes-base.twilio",
        "parameters": {"to": "+15551234567", "message": "hi"},
        "continueOnFail": True,
    })
    r = audit(wf)
    assert _has_finding(r, category="idempotency", severity="high")


def test_idempotency_present_via_dedup_node_clears():
    wf = _wf(
        {"id": "1", "name": "DedupByMessageId",
         "type": "n8n-nodes-base.removeDuplicates"},
        {"id": "2", "name": "Send", "type": "n8n-nodes-base.twilio",
         "parameters": {"to": "+15551234567", "message": "hi"},
         "continueOnFail": True},
    )
    r = audit(wf)
    assert not _has_finding(r, category="idempotency")


def test_webhook_without_respond_node_medium():
    wf = _wf(
        {"id": "1", "name": "WH", "type": "n8n-nodes-base.webhook"},
        {"id": "2", "name": "Send", "type": "n8n-nodes-base.twilio",
         "parameters": {"to": "+15551234567", "message": "hi"},
         "continueOnFail": True},
    )
    r = audit(wf)
    assert _has_finding(r, category="reliability", severity="medium")


def test_webhook_with_respond_node_clears_reliability():
    wf = _wf(
        {"id": "1", "name": "WH", "type": "n8n-nodes-base.webhook"},
        {"id": "2", "name": "Resp", "type": "n8n-nodes-base.respondToWebhook"},
        {"id": "3", "name": "Send", "type": "n8n-nodes-base.twilio",
         "parameters": {"to": "+15551234567", "message": "hi"},
         "continueOnFail": True},
    )
    r = audit(wf)
    assert not _has_finding(r, category="reliability")


def test_unbatched_twilio_with_airtable_medium():
    wf = _wf(
        {"id": "1", "name": "List", "type": "n8n-nodes-base.airtable",
         "parameters": {"operation": "list"},
         "continueOnFail": True},
        {"id": "2", "name": "Send", "type": "n8n-nodes-base.twilio",
         "parameters": {"to": "+15551234567", "message": "hi"},
         "continueOnFail": True},
    )
    r = audit(wf)
    assert _has_finding(r, category="rate_limits", severity="medium")


def test_batched_twilio_clears_rate_limits():
    wf = _wf(
        {"id": "1", "name": "List", "type": "n8n-nodes-base.airtable",
         "parameters": {"operation": "list"}, "continueOnFail": True},
        {"id": "2", "name": "Batch", "type": "n8n-nodes-base.splitInBatches"},
        {"id": "3", "name": "Send", "type": "n8n-nodes-base.twilio",
         "parameters": {"to": "+15551234567", "message": "hi"},
         "continueOnFail": True},
    )
    r = audit(wf)
    assert not _has_finding(r, category="rate_limits")


# ---------- Smoke tests ------------------------------------------------------

def test_clean_workflow_produces_zero_findings():
    """A workflow that respects all the rules should produce no findings."""
    wf = {
        "name": "Clean Flow",
        "settings": {"errorWorkflow": "wf-error-handler"},
        "nodes": [
            {"id": "1", "name": "WH", "type": "n8n-nodes-base.webhook"},
            {"id": "2", "name": "Resp",
             "type": "n8n-nodes-base.respondToWebhook"},
            {"id": "3", "name": "Dedup",
             "type": "n8n-nodes-base.removeDuplicates"},
            {"id": "4", "name": "List", "type": "n8n-nodes-base.airtable",
             "parameters": {"operation": "list"}, "continueOnFail": True},
            {"id": "5", "name": "Batch",
             "type": "n8n-nodes-base.splitInBatches"},
            {"id": "6", "name": "Send", "type": "n8n-nodes-base.twilio",
             "parameters": {"to": "+15551234567", "message": "hi"},
             "continueOnFail": True},
            {"id": "7", "name": "Update", "type": "n8n-nodes-base.airtable",
             "parameters": {"operation": "update",
                            "options": {"typecast": True}},
             "continueOnFail": True},
        ],
    }
    r = audit(wf)
    assert r.findings == [], (
        "Clean workflow should produce no findings, got: "
        + ", ".join(f"{f.severity}/{f.category}" for f in r.findings)
    )


def test_sample_workflow_produces_expected_findings():
    """The shipped sample should always trigger the headline issues."""
    sample_path = Path(__file__).resolve().parents[1] / "sample_workflow.json"
    import json
    wf = json.loads(sample_path.read_text(encoding="utf-8"))
    r = audit(wf)
    counts = r.counts()
    assert counts["critical"] >= 2, f"expected ≥2 criticals, got {counts}"
    assert counts["high"] >= 4, f"expected ≥4 highs, got {counts}"
    assert _has_finding(r, category="security", severity="critical")
    assert _has_finding(r, category="phone_format", severity="high")


# ---------- Auto-fix mode ----------------------------------------------------

def test_apply_auto_fixes_does_not_mutate_input():
    wf = _wf({
        "id": "1", "name": "Send", "type": "n8n-nodes-base.twilio",
        "parameters": {"to": "+15551234567", "message": "hi"},
    })
    original = {**wf, "nodes": [dict(n) for n in wf["nodes"]]}
    r = audit(wf)
    apply_auto_fixes(wf, r.findings)
    # Original wf untouched
    assert wf["nodes"][0].get("onError") is None
    assert wf == original


def test_apply_auto_fixes_adds_continue_on_fail_to_twilio():
    wf = _wf({
        "id": "1", "name": "Send", "type": "n8n-nodes-base.twilio",
        "parameters": {"to": "+15551234567", "message": "hi"},
    })
    r = audit(wf)
    fixed, applied = apply_auto_fixes(wf, r.findings)
    assert fixed["nodes"][0].get("onError") == "continueErrorOutput"
    assert any(a["fix"] == "continue_on_fail" and a["node"] == "Send" for a in applied)


def test_apply_auto_fixes_adds_typecast_to_airtable_write():
    wf = _wf({
        "id": "1", "name": "Create Row", "type": "n8n-nodes-base.airtable",
        "parameters": {"operation": "create"},
        "continueOnFail": True,
    })
    r = audit(wf)
    fixed, applied = apply_auto_fixes(wf, r.findings)
    assert fixed["nodes"][0]["parameters"]["options"]["typecast"] is True
    assert any(a["fix"] == "airtable_typecast" for a in applied)


def test_apply_auto_fixes_idempotent():
    wf = _wf({
        "id": "1", "name": "Send", "type": "n8n-nodes-base.twilio",
        "parameters": {"to": "+15551234567", "message": "hi"},
    })
    r1 = audit(wf)
    fixed_once, applied_first = apply_auto_fixes(wf, r1.findings)
    r2 = audit(fixed_once)
    fixed_twice, applied_second = apply_auto_fixes(fixed_once, r2.findings)
    # Second run should detect the fix is already in place and apply nothing
    # (for the auto-fixable subset)
    error_handling_findings_second_run = [
        f for f in r2.findings
        if f.category == "error_handling" and f.node == "Send"
    ]
    assert error_handling_findings_second_run == []
    assert fixed_twice["nodes"][0].get("onError") == "continueErrorOutput"


def test_apply_auto_fixes_preserves_unrelated_node_fields():
    wf = _wf({
        "id": "1", "name": "Send", "type": "n8n-nodes-base.twilio",
        "parameters": {
            "to": "+15551234567",
            "message": "hi",
            "customField": "preserve me",
        },
        "position": [100, 200],
        "typeVersion": 2,
    })
    r = audit(wf)
    fixed, _ = apply_auto_fixes(wf, r.findings)
    fixed_node = fixed["nodes"][0]
    assert fixed_node["parameters"]["customField"] == "preserve me"
    assert fixed_node["position"] == [100, 200]
    assert fixed_node["typeVersion"] == 2


def test_apply_auto_fixes_only_touches_findings_with_auto_fix():
    """Findings without an auto_fix ID (e.g. phone_format, cost) should not
    be touched by apply_auto_fixes."""
    wf = _wf({
        "id": "1", "name": "Send", "type": "n8n-nodes-base.twilio",
        "parameters": {"to": "5551234567", "message": "x" * 200},
        "continueOnFail": True,
    })
    r = audit(wf)
    fixed, applied = apply_auto_fixes(wf, r.findings)
    # phone_format and cost findings have no auto_fix => no changes applied
    assert applied == []
    assert fixed["nodes"][0]["parameters"]["to"] == "5551234567"
    assert fixed["nodes"][0]["parameters"]["message"] == "x" * 200
