"""
Browser-based preview server for the n8n SMS Audit toolkit.

A single-file HTTP server (stdlib only — no Flask, no FastAPI) that lets
prospects paste their n8n workflow JSON into a textarea and immediately see
a rendered audit report. Designed to deploy to Replit / Railway / Fly / a
free Render instance with zero configuration.

Usage:
    python audit_server.py                    # binds to 127.0.0.1:8000
    python audit_server.py --host 0.0.0.0 --port 8000   # for cloud deploy
    PORT=8000 python audit_server.py          # honors PORT env var (Heroku-style)

Open http://localhost:8000 in a browser, paste a workflow JSON, click Audit.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit import audit, render_markdown  # noqa: E402

MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB — n8n exports are typically <100 KB

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>n8n SMS Audit</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --fg: #1a1a1a; --bg: #fafafa; --accent: #0066cc; --border: #ddd;
    --critical: #c0392b; --high: #e67e22; --medium: #f1c40f; --low: #27ae60;
    --code-bg: #f4f4f4;
  }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         max-width: 960px; margin: 0 auto; padding: 2rem 1rem; color: var(--fg);
         background: var(--bg); line-height: 1.5; }
  h1 { margin-top: 0; font-size: 1.75rem; }
  h2 { font-size: 1.15rem; margin-top: 2rem; }
  .lede { color: #555; margin-bottom: 1.5rem; }
  textarea { width: 100%; height: 280px; font-family: ui-monospace, "SF Mono", Menlo,
             monospace; font-size: 13px; padding: 0.75rem; border: 1px solid var(--border);
             border-radius: 6px; background: white; resize: vertical; }
  button { background: var(--accent); color: white; border: 0; padding: 0.6rem 1.4rem;
           font-size: 14px; border-radius: 6px; cursor: pointer; margin-top: 0.75rem; }
  button:hover { background: #0052a3; }
  .meta { display: flex; gap: 1rem; flex-wrap: wrap; margin: 1rem 0; padding: 0.75rem;
          background: white; border: 1px solid var(--border); border-radius: 6px; }
  .pill { padding: 0.15rem 0.5rem; border-radius: 4px; font-size: 12px;
          font-weight: 600; color: white; }
  .pill.critical { background: var(--critical); }
  .pill.high     { background: var(--high); }
  .pill.medium   { background: #b8a409; }
  .pill.low      { background: var(--low); }
  .finding { background: white; border: 1px solid var(--border); border-left: 4px solid #aaa;
             border-radius: 6px; padding: 1rem 1.25rem; margin-bottom: 0.75rem; }
  .finding.critical { border-left-color: var(--critical); }
  .finding.high     { border-left-color: var(--high); }
  .finding.medium   { border-left-color: var(--medium); }
  .finding.low      { border-left-color: var(--low); }
  .finding h3 { margin: 0 0 0.5rem 0; font-size: 1rem; }
  .finding .cat { color: #666; font-size: 12px; font-family: ui-monospace, monospace; }
  .finding pre { background: var(--code-bg); padding: 0.6rem; border-radius: 4px;
                 white-space: pre-wrap; font-size: 12px; margin: 0.5rem 0 0; }
  .error { background: #fff3f3; border: 1px solid #ffaaaa; padding: 1rem;
           border-radius: 6px; color: #800; }
  footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--border);
           color: #777; font-size: 13px; }
  footer a { color: var(--accent); }
  .empty { background: white; border: 1px solid var(--border); padding: 2rem;
           text-align: center; border-radius: 6px; color: #555; }
</style>
</head>
<body>
<h1>n8n SMS Audit</h1>
<p class="lede">
  Paste an n8n workflow JSON export below. The audit runs locally on this server &mdash;
  no credentials sent, no API calls made, no data stored.
</p>
<form method="POST" action="/">
  <textarea name="workflow" placeholder='{"name": "...", "nodes": [...], "settings": {...}}'>{prefill}</textarea>
  <br>
  <button type="submit">Run Audit</button>
</form>
{result}
<footer>
  Open source: <a href="https://github.com/GhostlyGawd/n8n-sms-audit">github.com/GhostlyGawd/n8n-sms-audit</a> &middot;
  MIT licensed &middot;
  Need a deeper hands-on audit + remediation?
  <a href="mailto:hello@example.com?subject=n8n%20audit">Get in touch.</a>
</footer>
</body>
</html>"""


def _render_findings_html(report) -> str:
    counts = report.counts()
    pills = "".join(
        f'<span class="pill {sev}">{sev.upper()}: {counts.get(sev, 0)}</span>'
        for sev in ("critical", "high", "medium", "low")
    )
    meta = (
        f'<div class="meta">'
        f'<strong>Workflow:</strong> {html.escape(report.workflow_name)}'
        f' &middot; <strong>{report.node_count}</strong> nodes scanned'
        f' &middot; {pills}'
        f'</div>'
    )
    if not report.findings:
        return meta + (
            '<div class="empty">No issues detected by the configured rule set. '
            'This does not mean the workflow is correct &mdash; it means none of the '
            'known anti-patterns triggered.</div>'
        )
    blocks = []
    for i, f in enumerate(report.sorted(), 1):
        node_label = (
            f' &middot; Node: <code>{html.escape(f.node)}</code>' if f.node else ""
        )
        blocks.append(
            f'<div class="finding {f.severity}">'
            f'<h3>{i}. <span class="pill {f.severity}">{f.severity.upper()}</span> '
            f'{html.escape(f.title)}</h3>'
            f'<div class="cat">Category: <code>{html.escape(f.category)}</code>{node_label}</div>'
            f'<p>{html.escape(f.detail)}</p>'
            f'<strong>Recommended fix:</strong>'
            f'<pre>{html.escape(f.fix or "(no automated fix suggestion)")}</pre>'
            f'</div>'
        )
    return meta + "".join(blocks)


def _render_page(prefill: str = "", result_html: str = "") -> str:
    return PAGE_TEMPLATE.replace("{prefill}", html.escape(prefill)).replace(
        "{result}", result_html
    )


class AuditHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 — stdlib signature
        sys.stderr.write(f"[audit-server] {format % args}\n")

    def _respond(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._respond(HTTPStatus.OK, _render_page())
            return
        if self.path == "/health":
            self._respond(HTTPStatus.OK, "ok", "text/plain; charset=utf-8")
            return
        self._respond(HTTPStatus.NOT_FOUND, "<h1>404</h1>")

    def do_POST(self):
        if self.path != "/":
            self._respond(HTTPStatus.NOT_FOUND, "<h1>404</h1>")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._respond(
                HTTPStatus.BAD_REQUEST,
                _render_page(result_html='<div class="error">Request body missing or too large.</div>'),
            )
            return
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        form = parse_qs(raw, keep_blank_values=True)
        workflow_text = (form.get("workflow") or [""])[0].strip()
        if not workflow_text:
            self._respond(
                HTTPStatus.OK,
                _render_page(result_html='<div class="error">Paste a workflow JSON first.</div>'),
            )
            return
        try:
            workflow = json.loads(workflow_text)
        except json.JSONDecodeError as e:
            self._respond(
                HTTPStatus.OK,
                _render_page(
                    prefill=workflow_text,
                    result_html=f'<div class="error">Invalid JSON: {html.escape(str(e))}</div>',
                ),
            )
            return
        if not isinstance(workflow, dict) or "nodes" not in workflow:
            self._respond(
                HTTPStatus.OK,
                _render_page(
                    prefill=workflow_text,
                    result_html=(
                        '<div class="error">That parses as JSON but does not look like an '
                        'n8n workflow export (expected an object with a "nodes" key).</div>'
                    ),
                ),
            )
            return
        report = audit(workflow)
        self._respond(
            HTTPStatus.OK,
            _render_page(prefill=workflow_text, result_html=_render_findings_html(report)),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Browser-based n8n SMS audit preview.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), AuditHandler)
    sys.stderr.write(
        f"[audit-server] listening on http://{args.host}:{args.port}\n"
        f"[audit-server] press ctrl-c to stop\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[audit-server] shutting down\n")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
