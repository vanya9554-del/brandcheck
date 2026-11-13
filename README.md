# BrandCheck

**Brand Name Checker: domains, social handles, app stores and trademark pointers — one CLI, no API keys.**

`brandcheck.py` checks a product/brand name against public sources and produces a human-friendly Markdown report plus machine-readable JSON. It is designed for founders, indie hackers, naming agencies and agents that need a fast "is this name taken?" answer before buying a domain or filing a trademark.

- Stdlib-only Python 3.10+ (no pip install, no API keys)
- Read-only public requests; every check is isolated, so a blocked/rate-limited source never crashes the run — it becomes `unknown`
- Two output formats: Markdown (for humans) and JSON (for pipelines)
- Optional GitHub token only raises the anonymous GitHub API rate limit
- Never solves or bypasses captchas: if a source asks for one, that check is reported as `unknown`

**Demo page:** https://vanya9554-del.github.io/brandcheck/ · **Sample report:** [sample-report.md](sample-report.md) · [sample-report.json](sample-report.json)

## Quick start

```bash
python brandcheck.py novacat bitforge
# reports/novacat-bitforge.md + .json, plus a console summary
```

Check just domains and GitHub, and write into a custom folder:

```bash
python brandcheck.py mybrand --tlds com,io,ai,co --platforms github --out reports
```

Batch from a file (one name per line, `#` comments allowed):

```bash
python brandcheck.py --names-file names.txt --out reports
```

Use a GitHub token (optional, avoids the 60 req/h anonymous limit):

```bash
export GITHUB_TOKEN=ghp_...            # or: --github-token ghp_...
python brandcheck.py novacat
```

## What is checked

| Group | Source | Status meaning |
|---|---|---|
| Domains | [RDAP](https://rdap.org/) (`rdap.org/domain/<name>.<tld>`) with DNS fallback | `taken` = registration found, `free` = 404 / NXDOMAIN |
| GitHub | `api.github.com/users/<handle>` | `taken` = user exists, `free` = 404 |
| X (Twitter) | `x.com/<handle>` | page/body heuristics; often `unknown` behind anti-bot |
| Telegram | `t.me/<handle>` | public preview page (`tgme_page_title`) |
| Reddit | `reddit.com/user/<handle>/about.json` | `taken` / `free` / `unknown` when anonymous API is blocked |
| Instagram | `instagram.com/<handle>/` | page heuristics; often `unknown` behind login wall |
| Apple App Store | [iTunes Search API](https://itunes.apple.com/search) | exact-name app ⇒ `taken` |
| Google Play | public search page | app ids found ⇒ `taken` (manual exact-name check) |
| Web presence | DuckDuckGo Lite + Bing failover | informational: does the name appear online? |
| Trademarks | USPTO / EUIPO / WIPO links + best-effort public search | always `manual` — a human must verify |

Every check result carries `status` (`free` / `taken` / `unknown` / `manual`), `confidence` (`high` / `medium` / `low`), `source` (the exact URL queried), `checked_at` (UTC) and optional `details`/`note` fields.

## CLI options

| Flag | Default | Description |
|---|---|---|
| `names...` | — | brand names to check (max 25 per run) |
| `--names-file FILE` | — | read names from a file (one per line) |
| `--tlds LIST` | `com,io,ai,co` | comma-separated TLDs to check via RDAP |
| `--platforms LIST` | `github,x,telegram,reddit,instagram` | social platforms to check |
| `--out DIR` | `reports` | output directory |
| `--json-name`, `--md-name` | `<slug>.json/.md` | custom output filenames |
| `--no-web` | web on | skip the web-presence search |
| `--no-apps` | apps on | skip app-store checks |
| `--no-trademarks` | trademarks on | skip trademark pointers/search |
| `--indirect` | off | when a platform blocks direct checks, try an indirect web-search signal (more requests) |
| `--timeout SEC` | `10` | per-request timeout |
| `--retries N` | `1` | retries for failed requests (use `2` on flaky networks) |
| `--delay SEC` | `0.25` | global delay between requests |
| `--concurrency N` | `6` | parallel checks per name |
| `--github-token TOKEN` | env `GITHUB_TOKEN` | raises api.github.com rate limit |
| `--insecure` | off | disable TLS verification (debug only) |

## Sample output

Console:

```text
brandcheck 1.0.0 — 3 name(s)
  novacat              domains free 3/4 | domains taken 1 | handles free 0/5 | handles taken: github | verdict: likely taken
  bitforge             domains free 1/4 | domains taken 3 | handles free 0/5 | handles taken: github | verdict: likely taken
  lumenpaw             domains free 3/4 | domains taken 1 | handles free 1/5 | handles taken: none | verdict: likely taken
```

Markdown (excerpt from [sample-report.md](sample-report.md)):

| Domain | Status | Confidence | Details |
|---|---|---|---|
| `novacat.com` | taken | high | registrar: Key-Systems GmbH; created: 2010-08-14 |
| `novacat.io` | available | high | RDAP: no registration (404) |

## JSON schema

```jsonc
{
  "schema_version": "1.0",
  "tool": {"name": "brandcheck", "version": "1.0.0", "repo": "..."},
  "generated_at": "2026-11-13T23:36:00Z",
  "elapsed_seconds": 34.8,
  "params": {"tlds": ["com", "io", "ai", "co"], "platforms": ["github", "..."], "apps": true, "web": true, "trademarks": true, "indirect": false},
  "results": [
    {
      "name": "novacat",
      "handle": "novacat",
      "checked_at": "2026-11-13T23:36:00Z",
      "checks": {
        "domain:com": {"status": "taken", "confidence": "high", "source": "https://rdap.org/domain/novacat.com", "checked_at": "...", "details": {"registrar": "...", "events": {"registration": "..."}}, "note": "RDAP found a registration"},
        "github": {"status": "taken", "confidence": "high", "source": "https://api.github.com/users/novacat", "details": {"login": "Novacat", "public_repos": 5}},
        "trademarks": {"status": "manual", "confidence": "low", "details": {"official_links": {"...": "..."}}}
      },
      "summary": {"domains": {"free": ["io", "ai", "co"], "free_count": 3, "taken_count": 1}, "handles": {"taken": ["github"], "taken_count": 1, "unknown_count": 4}, "verdict": "likely taken"}
    }
  ]
}
```

## Demo page

`docs/index.html` is a static, self-contained page rendered from `sample-report.json`:

```bash
python tools/make_demo.py            # regenerates docs/index.html from sample-report.json
```

GitHub Pages is served from the `docs/` folder on `main`: https://vanya9554-del.github.io/brandcheck/

## Limitations (read this before relying on results)

- **Best effort, not legal advice.** TMs must be verified in USPTO TM Search / EUIPO eSearch / WIPO Global Brand DB — the tool only surfaces links and best-effort public hits.
- Some sources are JS apps or anti-bot protected (X, Instagram, Google Play, Reddit's anonymous API, search engines). They can return `unknown`; that is not "free". Re-run later, use the official UI, or check manually.
- Search providers (DuckDuckGo Lite, Bing) may rate-limit or ask for a captcha; the tool then disables that provider and reports `unknown`. Captchas are **never** solved or bypassed.
- DNS fallback (when RDAP is unreachable) is a weaker signal: a resolving host can be a parked/forwarded domain. Such results are reported with lower confidence and an explicit note — verify with RDAP/registrar.
- Public sources change constantly; `checked_at` is included with every row so reports can be aged.
- Rate limits: anonymous GitHub API is 60 req/h per IP; reduce `--concurrency`/increase `--delay` for heavy batch runs, and prefer `--no-web` for large lists.

## Legal / ethical use

The tool performs only read-only public GET requests (no logins, no scraping behind auth, no captcha bypass, no mass sends). Please respect each source's terms and rate limits. Use it for your own naming research — not to mass-probe or spam.

## Development

```bash
python -m py_compile brandcheck.py          # syntax check
python brandcheck.py novacat --no-web       # fast smoke test
```

`sample-report.md` / `sample-report.json` in this repo were generated with:

```bash
python brandcheck.py novacat bitforge lumenpaw --out . --md-name sample-report.md --json-name sample-report.json --timeout 10 --retries 2
```

## License

MIT — see [LICENSE](LICENSE).
