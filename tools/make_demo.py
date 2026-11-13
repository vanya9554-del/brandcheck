#!/usr/bin/env python3
"""Generate the static demo page (docs/index.html) from a brandcheck JSON report.

Usage:
    python tools/make_demo.py [sample-report.json] [-o docs/index.html]

Stdlib only. The page is fully self-contained (no external CSS/JS).
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

STATUS_LABEL = {"free": "available", "taken": "taken", "unknown": "unknown", "manual": "manual"}
STATUS_CLASS = {"free": "ok", "taken": "bad", "unknown": "warn", "manual": "manual"}


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def status_badge(status: str) -> str:
    return f'<span class="badge {STATUS_CLASS.get(status, "warn")}">{esc(STATUS_LABEL.get(status, status))}</span>'


def render_checks_table(checks: dict, keys: list[str], label_map: dict, source_prefix: str = "") -> str:
    rows = []
    for key in keys:
        check = checks.get(key)
        if not check:
            continue
        details = check.get("details") or {}
        bits = []
        if details.get("registrar"):
            bits.append(f"registrar: {esc(details['registrar'])}")
        events = details.get("events") or {}
        if events.get("registration"):
            bits.append(f"created: {esc(str(events['registration'])[:10])}")
        if details.get("ip"):
            bits.append("dns: " + esc(", ".join(str(ip) for ip in details["ip"][:3])))
        if details.get("login"):
            bits.append(f"login: {esc(details['login'])}")
        if details.get("public_repos") is not None:
            bits.append(f"repos: {esc(details['public_repos'])}")
        if details.get("result_count") is not None:
            bits.append(f"results: {esc(details['result_count'])}")
        if details.get("exact_match"):
            bits.append("exact-name app")
        if details.get("app_ids"):
            bits.append("ids: " + esc(", ".join(details["app_ids"][:3])))
        note = check.get("note") or ""
        if note:
            bits.append(esc(note))
        source = check.get("source") or ""
        source_html = f'<a href="{esc(source)}" title="{esc(source)}">source</a>' if source else ""
        rows.append(
            f"<tr><td><code>{esc(label_map.get(key, key))}</code></td>"
            f"<td>{status_badge(check.get('status', 'unknown'))}</td>"
            f"<td>{esc(check.get('confidence', ''))}</td>"
            f"<td>{' · '.join(bits)}</td>"
            f"<td>{source_html}</td></tr>"
        )
    if not rows:
        return '<p class="muted">no data</p>'
    return (
        "<table><thead><tr><th>Check</th><th>Status</th><th>Confidence</th><th>Details</th><th>Source</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render_web(checks: dict) -> str:
    check = checks.get("web")
    if not check:
        return ""
    details = check.get("details") or {}
    hits = details.get("top_results") or []
    provider = details.get("provider") or "search"
    items = "".join(
        f'<li><a href="{esc(hit.get("url", ""))}" rel="nofollow noopener">{esc(hit.get("title") or hit.get("url"))}</a></li>'
        for hit in hits[:5]
    )
    return (
        f'<h4>Web presence</h4><p>{status_badge(check.get("status", "unknown"))} '
        f'<span class="muted">via {esc(provider)} — {esc(check.get("note") or "")}</span></p>'
        f'<ul class="links">{items}</ul>'
    )


def render_trademarks(checks: dict, name: str) -> str:
    check = checks.get("trademarks")
    if not check:
        return ""
    details = check.get("details") or {}
    official = details.get("official_links") or {}
    links = "".join(
        f'<li><a href="{esc(url)}" rel="nofollow noopener">{esc(label)}</a> <span class="muted">(search: “{esc(name)}”)</span></li>'
        for label, url in official.items()
    )
    hits = details.get("public_hits") or []
    hits_html = ""
    if hits:
        hits_html = '<h4>Public trademark hits</h4><ul class="links">' + "".join(
            f'<li><a href="{esc(hit.get("url", ""))}" rel="nofollow noopener">{esc(hit.get("title") or hit.get("url"))}</a></li>'
            for hit in hits[:5]
        ) + "</ul>"
    return (
        f'<h4>Trademarks <span class="muted">(manual verification required)</span></h4>'
        f'<p>{status_badge(check.get("status", "manual"))} <span class="muted">{esc(check.get("note") or "")}</span></p>'
        f'<ul class="links">{links}</ul>{hits_html}'
    )


def render_name(item: dict) -> str:
    checks = item.get("checks") or {}
    summary = item.get("summary") or {}
    domain_keys = [key for key in checks if key.startswith("domain:")]
    platform_keys = [key for key in ("github", "x", "telegram", "reddit", "instagram") if key in checks]
    app_keys = [key for key in ("itunes", "google_play") if key in checks]
    verdict = summary.get("verdict", "unknown")
    verdict_class = "bad" if "taken" in verdict else "ok" if "available" in verdict else "warn"
    domains = summary.get("domains") or {}
    handles = summary.get("handles") or {}
    domain_labels = {key: key.split(":", 1)[1] for key in domain_keys}
    return f"""
    <section class="card">
      <div class="card-head">
        <h2>{esc(item.get('name'))}</h2>
        <span class="badge {verdict_class}">{esc(verdict)}</span>
      </div>
      <p class="muted">handle: <code>{esc(item.get('handle'))}</code> · checked: {esc(item.get('checked_at'))}</p>
      <div class="stats">
        <div class="stat"><span class="stat-num">{esc(domains.get('free_count', 0))}/{esc(len(domain_keys))}</span><span>domains available</span></div>
        <div class="stat"><span class="stat-num">{esc(domains.get('taken_count', 0))}</span><span>domains taken</span></div>
        <div class="stat"><span class="stat-num">{esc(handles.get('free_count', 0))}/{esc(len(platform_keys))}</span><span>handles available</span></div>
        <div class="stat"><span class="stat-num">{esc(handles.get('taken_count', 0))}</span><span>handles taken</span></div>
      </div>
      <h4>Domains (RDAP)</h4>
      {render_checks_table(checks, domain_keys, domain_labels)}
      <h4>Social handles</h4>
      {render_checks_table(checks, platform_keys, {key: key for key in platform_keys})}
      <h4>App stores</h4>
      {render_checks_table(checks, app_keys, {'itunes': 'Apple App Store', 'google_play': 'Google Play'})}
      {render_web(checks)}
      {render_trademarks(checks, item.get('name', ''))}
    </section>
    """


def build_page(report: dict) -> str:
    results = report.get("results") or []
    tool = report.get("tool") or {}
    cards = "".join(render_name(item) for item in results)
    params = report.get("params") or {}
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BrandCheck — demo report</title>
<style>
  :root {{
    --bg: #0b1020; --panel: #131a2e; --panel2: #0f1527; --text: #e8ecf8; --muted: #93a0bd;
    --line: #24304d; --ok: #2ecc71; --bad: #ff6b6b; --warn: #f5b041; --manual: #7d8cf8; --accent: #5b8cff;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--text); font: 15px/1.55 -apple-system, "Segoe UI", Roboto, Inter, sans-serif; }}
  .wrap {{ max-width: 1080px; margin: 0 auto; padding: 40px 20px 80px; }}
  header.hero {{ padding: 8px 0 28px; border-bottom: 1px solid var(--line); margin-bottom: 28px; }}
  h1 {{ font-size: 34px; margin: 0 0 8px; letter-spacing: -0.5px; }}
  h1 span {{ color: var(--accent); }}
  .sub {{ color: var(--muted); max-width: 780px; }}
  .sub a {{ color: var(--accent); }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 18px; color: var(--muted); font-size: 13px; margin-top: 18px; }}
  .meta code {{ color: var(--text); }}
  .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 22px 22px 18px; margin: 0 0 22px; }}
  .card-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
  .card-head h2 {{ margin: 0; font-size: 24px; }}
  h4 {{ margin: 22px 0 8px; font-size: 13px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13.5px; }}
  th, td {{ text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }}
  th {{ color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  code {{ background: var(--panel2); border: 1px solid var(--line); border-radius: 5px; padding: 1px 5px; font-size: 12.5px; }}
  .badge {{ display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; text-transform: lowercase; }}
  .badge.ok {{ background: rgba(46,204,113,.14); color: var(--ok); border: 1px solid rgba(46,204,113,.4); }}
  .badge.bad {{ background: rgba(255,107,107,.14); color: var(--bad); border: 1px solid rgba(255,107,107,.4); }}
  .badge.warn {{ background: rgba(245,176,65,.14); color: var(--warn); border: 1px solid rgba(245,176,65,.4); }}
  .badge.manual {{ background: rgba(125,140,248,.14); color: var(--manual); border: 1px solid rgba(125,140,248,.4); }}
  .stats {{ display: flex; gap: 14px; flex-wrap: wrap; margin: 14px 0 4px; }}
  .stat {{ background: var(--panel2); border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px; min-width: 130px; }}
  .stat-num {{ display: block; font-size: 20px; font-weight: 700; }}
  .stat span:last-child {{ color: var(--muted); font-size: 12px; }}
  .muted {{ color: var(--muted); }}
  ul.links {{ margin: 6px 0 0; padding-left: 20px; }}
  ul.links li {{ margin: 3px 0; }}
  footer {{ margin-top: 36px; padding-top: 18px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12.5px; }}
  footer strong {{ color: var(--text); }}
  @media (max-width: 640px) {{ th:nth-child(5), td:nth-child(5) {{ display: none; }} }}
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <h1>Brand<span>Check</span> — demo report</h1>
    <p class="sub">
      Public-source brand check: <strong>RDAP domain availability</strong>, <strong>social handles</strong>
      (GitHub · X · Telegram · Reddit · Instagram), <strong>app stores</strong> (Apple · Google Play),
      <strong>web presence</strong> and <strong>trademark pointers</strong>. Generated by
      <a href="https://github.com/vanya9554-del/brandcheck" rel="noopener">brandcheck.py</a> v{esc(tool.get('version', '1.0.0'))} —
      stdlib only, no API keys, captchas are never bypassed.
    </p>
    <div class="meta">
      <span>generated: <code>{esc(report.get('generated_at'))}</code></span>
      <span>elapsed: <code>{esc(report.get('elapsed_seconds'))}s</code></span>
      <span>tlds: <code>{esc(", ".join(params.get('tlds') or []))}</code></span>
      <span>platforms: <code>{esc(", ".join(params.get('platforms') or []))}</code></span>
      <span><a href="https://github.com/vanya9554-del/brandcheck/blob/main/sample-report.md" rel="noopener">Markdown report</a></span>
      <span><a href="https://github.com/vanya9554-del/brandcheck" rel="noopener">Source code</a></span>
    </div>
  </header>
  {cards}
  <footer>
    <p><strong>How to read this:</strong> <span class="badge ok">available</span> = no public registration/handle found (good),
    <span class="badge bad">taken</span> = already in use, <span class="badge warn">unknown</span> = source blocked or rate-limited
    (verify manually), <span class="badge manual">manual</span> = trademarks always need a human check.</p>
    <p><strong>Limitations:</strong> read-only public requests, best effort. Some sources (X, Instagram, Google Play, Reddit API)
    are anti-bot protected and may return <code>unknown</code> — that is not "free". Trademarks are not legal advice; verify in
    USPTO / EUIPO / WIPO. Rate limits apply.</p>
    <p>MIT licensed · sample generated with
    <code>python brandcheck.py novacat bitforge lumenpaw --out . --md-name sample-report.md --json-name sample-report.json --timeout 10 --retries 2</code></p>
  </footer>
</div>
</body>
</html>
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Render docs/index.html from a brandcheck JSON report")
    parser.add_argument("input", nargs="?", default="sample-report.json", help="JSON report (default: sample-report.json)")
    parser.add_argument("-o", "--output", default="docs/index.html", help="output HTML path (default: docs/index.html)")
    args = parser.parse_args(argv)
    try:
        report = json.loads(Path(args.input).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"error: cannot read {args.input}: {exc}", file=sys.stderr)
        return 2
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_page(report), encoding="utf-8")
    print(f"wrote {output} ({output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
