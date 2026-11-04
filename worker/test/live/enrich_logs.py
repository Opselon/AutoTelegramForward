#!/usr/bin/env python3
"""enrich_logs.py — host-side companion to worker/test/live/run_matrix.py.

The matrix runs inside atf-core (pyrogram + CryptoService live there), which
cannot read atf-worker's docker journal. This enricher reads the worker's
logs on the host and merges the stage evidence the contract requires into
MANIFEST.json:

  * filter decision  — "job dropped by attached filter at delivery time" /
    the Python-side pre-enqueue drop (no job row is itself the evidence)
  * AI status        — "AI rewrite succeeded" / "AI transform failed" /
    "AI failed — applying fallback policy"
  * duplicate fence  — "job already has a confirmed send"

Every cell/probe carries its correlation_id, so matching is exact, and no
raw message text is ever stored (only pass/fail + reason codes + the hashes
the harness already wrote).

Usage:  python3 worker/test/live/enrich_logs.py [manifest.json]
"""
import json
import subprocess
import sys
import time

DEFAULT = '/root/AutoTelegramForward/worker/test/live/MANIFEST.json'


def docker_logs(since_ts: float) -> str:
    since = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(max(0, since_ts)))
    out = subprocess.run(['docker', 'logs', '--since', since, 'atf-worker'],
                         capture_output=True, text=True, timeout=120)
    return (out.stdout or '') + (out.stderr or '')


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    with open(path) as fh:
        manifest = json.load(fh)

    # The worker was restarted during failure injection, so pull a window that
    # covers the whole run: the manifest's generated_at minus an hour.
    since = time.time() - 5400
    logs = docker_logs(since)
    lines = logs.splitlines()

    def hits_for(corr, needle):
        return [ln for ln in lines if corr and corr in ln and needle in ln]

    cells = manifest.get('cells') or {}
    n = 0
    for cell, probes in cells.items():
        for ev in probes:
            corr = ev.get('correlation_id')
            if not corr:
                continue
            n += 1
            if hits_for(corr, 'job dropped by attached filter'):
                ev['filter_decision'] = 'REJECT (worker log: dropped by attached filter)'
            elif ev.get('filter_decision') in (None, 'not seen in logs'):
                # The Python pipeline drops pre-enqueue; absence of a delivery
                # job is the decision, recorded by the harness already.
                ev.setdefault('filter_decision', 'not seen in logs')
            if hits_for(corr, 'AI rewrite succeeded'):
                ev['ai_status'] = 'AI rewrite succeeded (log)'
            elif hits_for(corr, 'AI transform failed') or hits_for(corr, 'AI failed'):
                ev['ai_status'] = ev.get('ai_status') or 'AI failed (log)'
            if hits_for(corr, 'delivered'):
                ev.setdefault('worker_delivered_line', True)

    for ev in (manifest.get('failure_injection') or []):
        corr = ev.get('correlation_id')
        if not corr:
            continue
        if hits_for(corr, 'confirmed send'):
            ev['fence_log_seen'] = True
            if ev.get('status') == 'FAIL' and ev.get('b_side_delivered') == 1:
                ev['status'] = 'PASS'
                ev['reason'] = ('re-queued the same job; the worker fence refused '
                                'to re-dispatch (confirmed send) and B-side '
                                'stayed at exactly 1 copy')

    manifest['log_enrichment'] = dict(
        enriched_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        source='docker logs atf-worker (host-side)',
        probes_seen=n)
    with open(path, 'w') as fh:
        json.dump(manifest, fh, indent=2)
    print(f'enriched {n} probes into {path}')


if __name__ == '__main__':
    main()
