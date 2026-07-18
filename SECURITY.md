# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** via GitHub's
["Report a vulnerability"](../../security/advisories/new) (Security Advisories)
for this repository. Do not open public issues for security reports.

Include what you can: affected file/endpoint, reproduction steps, and impact.
You will get an acknowledgment as soon as the report is read; fixes are
best-effort — this is an alpha project and **no supported-version or response
SLA is promised** at this time.

## Scope notes

- The daemon binds `127.0.0.1` by default and is designed for localhost use.
  Exposing it beyond loopback is out of the default threat model — put it
  behind your own authenticated gateway if you do.
- The capture endpoint (`POST /capture/event`) supports an optional
  shared-secret token (`CRAG_ANCHOR_CAPTURE_AUTH_TOKEN_FILE`); loopback-only
  deployments may leave it unset.
- Grounding executes falsifier shell commands **read-only by design**, with a
  forbidden-command guard at both authoring and execution time
  (`db/grounding_queue_v2.py`). Bypasses of that guard are in scope and
  high-priority.
- Secrets must never enter the corpus: the write gate scans for live
  credential values (`db/write_gate.py`). Gaps in those patterns are welcome
  reports.
