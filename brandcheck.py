#!/usr/bin/env python3
"""
brandcheck.py — Brand Name Checker: domains, handles, apps and trademark pointers.

Checks a brand name against public, no-key sources:
  * domain availability via RDAP (rdap.org) with DNS fallback
  * GitHub / X / Telegram / Reddit / Instagram handle status
  * App Store (iTunes Search API) and Google Play (public search page)
  * trademark pointers (USPTO, EUIPO, WIPO) + a best-effort public search
  * optional web-presence check via DuckDuckGo Lite

Everything is best-effort and read-only: every check is wrapped so a network
error, timeout or rate limit never crashes the run — it becomes "unknown".
No captchas, no logins, no API keys required (an optional GitHub token only
raises the low anonymous API rate limit).

Usage:
    python brandcheck.py novacat bitforge --tlds com,io,ai,co
    python brandcheck.py --names-file names.txt --out reports
    python brandcheck.py novacat --no-web --timeout 8

Output: a Markdown report and a JSON report (default: reports/<slug>.md/.json),
plus a short summary printed to stdout.

License: MIT. Not legal advice — trademarks must be verified by a human.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0.0"
UA = "brandcheck/1.0 (+https://github.com/vanya9554-del/brandcheck)"
API_ACCEPT = "application/vnd.github+json"
MAX_BODY = 2_000_000

DEFAULT_TLDS = ["com", "io", "ai", "co"]

PLATFORMS = ["github", "x", "telegram", "reddit", "instagram"]

TRADEMARK_LINKS = {
    "USPTO (US)": "https://tmsearch.uspto.gov/search/search-information",
    "USPTO TSDR (status)": "https://tsdr.uspto.gov/",
    "EUIPO eSearch (EU)": "https://euipo.europa.eu/eSearch/",
    "WIPO Global Brand DB": "https://branddb.wipo.int/",
}

TRADEMARK_DOMAINS = (
    "uspto.gov",
    "tsdr.uspto.gov",
    "tmsearch.uspto.gov",
    "euipo.europa.eu",
    "branddb.wipo.int",
    "tmdn.org",
    "wipo.int",
    "trademarkia.com",
    "justia.com",
    "uspto.report",
    "trademarkelite.com",
    "tmsearch.uspto.gov",
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "name"


def norm_handle(name: str) -> str:
    """Handles on the checked platforms are case-insensitive, [a-z0-9_.] mostly."""
    return re.sub(r"[^a-z0-9_.]", "", name.lower())


def norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def result(status: str, confidence: str, source: str, details=None, note: str = "") -> dict:
    """Build one check result with uniform fields."""
    assert status in ("free", "taken", "unknown", "manual"), status
    assert confidence in ("high", "medium", "low"), confidence
    data = {
        "status": status,
        "confidence": confidence,
        "source": source,
        "checked_at": utc_now(),
    }
    if details:
        data["details"] = details
    if note:
        data["note"] = note
    return data


@dataclass
class Response:
    status: int | None
    url: str
    body: bytes = b""
    error: str | None = None
    elapsed: float = 0.0

    @property
    def text(self) -> str:
        try:
            return self.body.decode("utf-8", "replace")
        except Exception:
            return ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


class Http:
    """Tiny urllib client with throttling, timeouts, retries and no crashes."""

    def __init__(self, timeout: float = 10.0, retries: int = 1, delay: float = 0.25, insecure: bool = False):
        self.timeout = timeout
        self.retries = max(0, retries)
        self.delay = max(0.0, delay)
        self._lock = threading.Lock()
        self._last = 0.0
        self._ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()

    def _throttle(self) -> None:
        if self.delay <= 0:
            return
        with self._lock:
            wait = self.delay - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()

    def get(self, url: str, headers: dict | None = None, method: str = "GET", max_bytes: int = MAX_BODY) -> Response:
        req_headers = {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if headers:
            req_headers.update(headers)
        last_error = "unknown error"
        started = time.time()
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                req = urllib.request.Request(url, headers=req_headers, method=method)
                with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                    body = resp.read(max_bytes)
                    return Response(status=resp.status, url=resp.geturl(), body=body,
                                    elapsed=round(time.time() - started, 2))
            except urllib.error.HTTPError as exc:
                try:
                    body = exc.read(4096)
                except Exception:
                    body = b""
                return Response(status=exc.code, url=url, body=body,
                                error=f"HTTP {exc.code}", elapsed=round(time.time() - started, 2))
            except Exception as exc:  # noqa: BLE001 - never let a network error escape
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    time.sleep(0.4 * (attempt + 1))
        return Response(status=None, url=url, error=last_error, elapsed=round(time.time() - started, 2))


def _json(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def _title_tag(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


# --------------------------------------------------------------------------- #
# Domain checks (RDAP + DNS fallback)
# --------------------------------------------------------------------------- #
def check_domain(http: Http, name: str, tld: str) -> dict:
    domain = f"{norm_name(name)}.{tld}"
    rdap_url = f"https://rdap.org/domain/{domain}"
    resp = http.get(rdap_url, headers={"Accept": "application/rdap+json"}, max_bytes=400_000)
    if resp.status == 200:
        details = {"domain": domain}
        data = _json(resp.text)
        if isinstance(data, dict):
            details["ldh_name"] = data.get("ldhName")
            details["statuses"] = data.get("status") or []
            registrar = ""
            for entity in data.get("entities") or []:
                if "registrar" in (entity.get("roles") or []):
                    vcard = entity.get("vcardArray") or []
                    if isinstance(vcard, list) and len(vcard) > 1:
                        for item in vcard[1]:
                            if isinstance(item, list) and len(item) > 3 and item[0] == "fn":
                                registrar = str(item[3])
                    details["registrar"] = registrar
                    break
            events = {}
            for ev in data.get("events") or []:
                action = ev.get("eventAction")
                if action:
                    events[action] = ev.get("eventDate")
            if events:
                details["events"] = events
        return result("taken", "high", rdap_url, details, "RDAP found a registration")
    if resp.status == 404:
        return result("free", "high", rdap_url, {"domain": domain}, "RDAP: no registration (404)")
    # RDAP unavailable -> DNS fallback
    dns = check_domain_dns(name, tld)
    dns["details"] = {**dns.get("details", {}), "rdap_status": resp.status, "rdap_error": resp.error}
    dns["source"] = rdap_url
    return dns


def check_domain_dns(name: str, tld: str) -> dict:
    domain = f"{norm_name(name)}.{tld}"
    try:
        info = socket.getaddrinfo(domain, 443)
        ips = sorted({item[4][0] for item in info})
        return result("taken", "medium", f"dns:{domain}", {"domain": domain, "ip": ips[:3]},
                      "DNS resolves (RDAP unavailable)")
    except socket.gaierror as exc:
        message = str(exc).lower()
        if "name or service not known" in message or "nodename nor servname" in message \
                or "11001" in message or "name does not resolve" in message or "getaddrinfo failed" in message:
            return result("free", "medium", f"dns:{domain}", {"domain": domain},
                          "DNS: NXDOMAIN (RDAP unavailable)")
        return result("unknown", "low", f"dns:{domain}", {"domain": domain, "error": str(exc)[:120]},
                      "DNS lookup failed")
    except Exception as exc:  # noqa: BLE001
        return result("unknown", "low", f"dns:{domain}", {"domain": domain, "error": str(exc)[:120]},
                      "DNS lookup failed")


# --------------------------------------------------------------------------- #
# Handle checks
# --------------------------------------------------------------------------- #
def check_github(http: Http, handle: str, token: str | None = None) -> dict:
    url = f"https://api.github.com/users/{handle}"
    headers = {"Accept": API_ACCEPT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = http.get(url, headers=headers, max_bytes=200_000)
    if resp.status == 200:
        data = _json(resp.text) or {}
        details = {"login": data.get("login"), "name": data.get("name"),
                   "public_repos": data.get("public_repos"), "created_at": data.get("created_at")}
        return result("taken", "high", url, details, "GitHub user exists")
    if resp.status == 404:
        return result("free", "high", url, {"handle": handle}, "GitHub: 404 (username is available)")
    if resp.status == 403:
        return result("unknown", "low", url, {"error": "rate limited / forbidden"},
                      "GitHub API rate limit reached (set GITHUB_TOKEN to raise it)")
    return result("unknown", "low", url, {"error": resp.error or f"HTTP {resp.status}"}, "GitHub check failed")


def check_x(http: Http, handle: str, opts: "Options") -> dict:
    url = f"https://x.com/{handle}"
    resp = http.get(url, max_bytes=1_500_000)
    if resp.ok:
        html = resp.text
        if re.search(r"this account (doesn['’]t|does not) exist", html, re.I):
            return result("free", "medium", url, {"handle": handle}, "X page says account doesn't exist")
        if "<title>" in html.lower() or "og:title" in html:
            title = _title_tag(html)
            if handle.lower() in title.lower():
                return result("taken", "low", url, {"handle": handle, "title": title[:120]},
                              "X profile page looks populated (verify manually)")
        return result("unknown", "low", url, {"handle": handle},
                      "X returned 200 but content is inconclusive (JS app)")
    if resp.status == 404:
        return result("free", "low", url, {"handle": handle}, "X: 404")
    indirect = _indirect_handle_signal(http, "x.com", handle, opts) if opts.indirect else None
    if indirect:
        return result("taken", "low", url, {"handle": handle, "indirect_search": indirect},
                      "direct X check blocked; search results suggest the handle exists (verify manually)")
    return result("unknown", "low", url, {"error": resp.error or f"HTTP {resp.status}"},
                  "X blocked the request / timeout")


def _indirect_handle_signal(http: Http, site: str, handle: str, opts: "Options") -> dict | None:
    """Fallback signal when a platform blocks direct HTTP checks: search for site:<site> <handle>."""
    hits, meta = opts.search.search(f"site:{site} {handle}")
    if meta.get("blocked") or not hits:
        return None
    target = f"{site}/{handle}".lower().replace("www.", "")
    for hit in hits:
        blob = f"{hit.get('url', '')} {hit.get('title', '')}".lower().replace("www.", "")
        if target in blob:
            return {"provider": meta.get("provider"), "matches": len(hits), "top": hits[:3]}
    return None


def check_telegram(http: Http, handle: str, opts: "Options") -> dict:
    url = f"https://t.me/{handle}"
    resp = http.get(url, max_bytes=500_000)
    if resp.ok:
        html = resp.text
        if "tgme_page_title" in html:
            title = ""
            m = re.search(r'<div class="tgme_page_title"[^>]*>\s*(?:<span[^>]*>)?(.*?)<', html, re.S)
            if m:
                title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            return result("taken", "high", url, {"handle": handle, "title": title[:120]},
                          "Telegram public page exists")
        if "tgme_page" in html:
            return result("free", "medium", url, {"handle": handle}, "Telegram page is a placeholder")
        return result("unknown", "low", url, {"handle": handle}, "Telegram page inconclusive")
    if resp.status == 404:
        return result("free", "medium", url, {"handle": handle}, "Telegram: 404")
    indirect = _indirect_handle_signal(http, "t.me", handle, opts) if opts.indirect else None
    if indirect:
        return result("taken", "low", url, {"handle": handle, "indirect_search": indirect},
                      "direct Telegram check blocked; search results suggest the channel exists (verify manually)")
    return result("unknown", "low", url, {"error": resp.error or f"HTTP {resp.status}"},
                  "Telegram blocked the request / timeout")


def check_reddit(http: Http, handle: str, opts: "Options") -> dict:
    url = f"https://www.reddit.com/user/{handle}/about.json"
    resp = http.get(url, headers={"Accept": "application/json"}, max_bytes=200_000)
    if resp.status == 200:
        data = _json(resp.text) or {}
        payload = data.get("data") or {}
        return result("taken", "high", url, {"handle": payload.get("name") or handle,
                                             "karma": payload.get("total_karma")},
                      "Reddit user exists")
    if resp.status == 404:
        return result("free", "high", url, {"handle": handle}, "Reddit: 404 (user does not exist)")
    if resp.status in (403, 429):
        indirect = _indirect_handle_signal(http, "reddit.com", handle, opts) if opts.indirect else None
        if indirect:
            return result("taken", "low", url, {"handle": handle, "indirect_search": indirect},
                          "Reddit blocked the direct API; search results suggest the user exists (verify manually)")
        return result("unknown", "low", url, {"error": f"HTTP {resp.status}"},
                      "Reddit blocked anonymous access (429/403); verify manually")
    return result("unknown", "low", url, {"error": resp.error or f"HTTP {resp.status}"}, "Reddit check failed")


def check_instagram(http: Http, handle: str, opts: "Options") -> dict:
    url = f"https://www.instagram.com/{handle}/"
    resp = http.get(url, max_bytes=1_500_000)
    if resp.ok:
        html = resp.text
        if "Sorry, this page isn't available" in html or "page isn\\'t available" in html:
            return result("free", "medium", url, {"handle": handle},
                          "Instagram: page not available")
        if "og:title" in html and handle.lower() in html.lower()[:200_000]:
            return result("taken", "low", url, {"handle": handle},
                          "Instagram page looks populated (verify manually)")
        return result("unknown", "low", url, {"handle": handle}, "Instagram page inconclusive")
    if resp.status == 404:
        return result("free", "medium", url, {"handle": handle}, "Instagram: 404")
    if resp.status in (301, 302):
        return result("unknown", "low", url, {"handle": handle}, "Instagram redirected (login wall)")
    indirect = _indirect_handle_signal(http, "instagram.com", handle, opts) if opts.indirect else None
    if indirect:
        return result("taken", "low", url, {"handle": handle, "indirect_search": indirect},
                      "direct Instagram check blocked; search results suggest the profile exists (verify manually)")
    return result("unknown", "low", url, {"error": resp.error or f"HTTP {resp.status}"},
                  "Instagram blocked the request / timeout")


# --------------------------------------------------------------------------- #
# App stores
# --------------------------------------------------------------------------- #
def check_itunes(http: Http, name: str) -> dict:
    query = urllib.parse.quote(name)
    url = f"https://itunes.apple.com/search?term={query}&entity=software&country=us&limit=10"
    resp = http.get(url, headers={"Accept": "application/json"}, max_bytes=500_000)
    if not resp.ok:
        return result("unknown", "low", url, {"error": resp.error or f"HTTP {resp.status}"},
                      "iTunes Search API failed")
    data = _json(resp.text) or {}
    apps = data.get("results") or []
    exact = [a for a in apps if norm_name(str(a.get("trackName", ""))) == norm_name(name)]
    details = {
        "result_count": data.get("resultCount", len(apps)),
        "exact_match": bool(exact),
        "top_results": [{"name": a.get("trackName"), "seller": a.get("sellerName"),
                         "bundle_id": a.get("bundleId")} for a in apps[:5]],
    }
    if exact:
        return result("taken", "high", url, details, "App Store has an app with this exact name")
    if apps:
        return result("free", "medium", url, details, "No exact-name app, but similar apps exist")
    return result("free", "high", url, details, "No apps found in the US App Store")


def check_google_play(http: Http, name: str, opts: "Options") -> dict:
    query = urllib.parse.quote(name)
    url = f"https://play.google.com/store/search?q={query}&c=apps&hl=en&gl=US"
    resp = http.get(url, max_bytes=1_500_000)
    if resp.ok:
        html = resp.text
        ids = []
        for match in re.findall(r"/store/apps/details\?id=([A-Za-z0-9_.]+)", html):
            if match not in ids:
                ids.append(match)
        details = {"app_ids": ids[:8], "direct": True}
        if not ids:
            if re.search(r"no results", html, re.I):
                return result("free", "medium", url, details, "Google Play: no results")
            return result("unknown", "low", url, details, "Google Play returned no app ids (page may be JS-only)")
        return result("taken", "low", url, details,
                      f"Search returns {len(ids)} app(s); check exact-name match manually")
    # direct page blocked -> best-effort indirect signal via web search
    hits, meta = opts.search.search(f"site:play.google.com {name}")
    links = [hit for hit in hits if "play.google.com" in f"{hit.get('url', '')} {hit.get('domain', '')}"]
    details = {"direct": False, "search_provider": meta.get("provider"), "search_hits": links[:5]}
    if links:
        return result("taken", "low", url, details,
                      "Google Play page blocked; search results point to Play listings (verify manually)")
    return result("unknown", "low", url, {**details, "error": resp.error or f"HTTP {resp.status}"},
                  "Google Play page unavailable (blocked / timeout)")


# --------------------------------------------------------------------------- #
# Web search (best effort, provider failover, captchas are never bypassed)
# --------------------------------------------------------------------------- #
def parse_ddg_lite(html: str) -> list[dict]:
    hits: list[dict] = []
    for m in re.finditer(r"<a[^>]+class='result-link'[^>]*>(.*?)</a>", html, re.S | re.I):
        title = re.sub(r"<[^>]+>", " ", m.group(1)).strip()
        href_match = re.search(r'href="([^"]+)"', m.group(0))
        href = href_match.group(1) if href_match else ""
        if "uddg=" in href:
            try:
                href = urllib.parse.unquote(re.search(r"uddg=([^&]+)", href).group(1))
            except Exception:
                pass
        if title or href:
            hits.append({"title": re.sub(r"\s+", " ", title)[:160], "url": href[:400]})
    domains = re.findall(r"<span class='link-text'>(.*?)</span>", html, re.S | re.I)
    for i, dom in enumerate(domains):
        if i < len(hits):
            hits[i].setdefault("domain", re.sub(r"<[^>]+>", " ", dom).strip())
    return hits


def decode_bing_url(href: str) -> str:
    """Bing wraps result links in bing.com/ck/a?...&u=a1<base64url>; unwrap when possible."""
    if "bing.com/ck/a" not in href:
        return href
    match = re.search(r"u=a1([A-Za-z0-9_\-]+)", href)
    if not match:
        return href
    token = match.group(1)
    padding = "=" * (-len(token) % 4)
    try:
        decoded = base64.urlsafe_b64decode(token + padding).decode("utf-8", "replace")
        if decoded.startswith("http"):
            return decoded
    except (binascii.Error, ValueError):
        pass
    return href


def parse_bing(html: str) -> list[dict]:
    hits: list[dict] = []
    for m in re.finditer(r'<li class="b_algo".*?<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        href = decode_bing_url(m.group(1))
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2))).strip()
        try:
            domain = urllib.parse.urlparse(href).netloc
        except Exception:
            domain = ""
        hits.append({"title": title[:160], "url": href[:400], "domain": domain})
    return hits


class WebSearch:
    """Best-effort web search with provider failover.

    DuckDuckGo and Bing are tried in order. If a provider asks for a captcha or
    starts rate-limiting, it is disabled for the rest of the run and the next
    provider is used — captchas are never bypassed or solved.
    """

    PROVIDERS = ("duckduckgo", "bing")

    def __init__(self, http: Http):
        self.http = http
        self._lock = threading.Lock()
        self.disabled: dict[str, str] = {}

    def _provider_query(self, provider: str, query: str):
        if provider == "duckduckgo":
            url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(query)
            resp = self.http.get(url, max_bytes=800_000)
            if not resp.ok:
                return None, "error", resp.error or f"HTTP {resp.status}"
            html = resp.text
            low = html.lower()
            if ("complete the following challenge" in low or "bots use duckduckgo" in low
                    or "select all squares" in low or "anomaly" in low):
                return None, "captcha", "DuckDuckGo asked for a captcha"
            return parse_ddg_lite(html), "ok", ""
        if provider == "bing":
            url = "https://www.bing.com/search?" + urllib.parse.urlencode(
                {"q": query, "count": "10", "setlang": "en", "cc": "us"})
            resp = self.http.get(url, max_bytes=1_500_000)
            if not resp.ok:
                return None, "error", resp.error or f"HTTP {resp.status}"
            html = resp.text
            low = html.lower()
            if "unusual traffic" in low or "b_captcha" in low:
                return None, "captcha", "Bing asked for a captcha"
            hits = parse_bing(html)
            if not hits and ("verify you are human" in low or "captcha" in low):
                return None, "captcha", "Bing asked for a captcha"
            return hits, "ok", ""
        return None, "error", f"unknown provider {provider}"

    def search(self, query: str, limit: int = 10) -> tuple[list[dict], dict]:
        with self._lock:
            tried = []
            for provider in self.PROVIDERS:
                if provider in self.disabled:
                    continue
                tried.append(provider)
                try:
                    hits, status, detail = self._provider_query(provider, query)
                except Exception as exc:  # noqa: BLE001
                    hits, status, detail = None, "error", f"{type(exc).__name__}: {exc}"
                if status == "ok":
                    return hits[:limit], {"provider": provider, "blocked": False, "tried": tried}
                if status == "captcha":
                    self.disabled[provider] = detail or "captcha"
        return [], {"provider": None, "blocked": True, "tried": tried, "disabled": dict(self.disabled)}


def check_web(http: Http, name: str, opts: "Options") -> dict:
    hits, meta = opts.search.search(f'"{name}"')
    details = {"provider": meta.get("provider"), "count": len(hits), "top_results": hits[:5]}
    url = "https://duckduckgo.com/?q=" + urllib.parse.quote(f'"{name}"')
    if meta.get("blocked"):
        details.update({"blocked_providers": list((meta.get("disabled") or {}).keys())})
        return result("unknown", "low", url, details,
                      "Search providers asked for a captcha / are rate-limited — not bypassed")
    if hits:
        return result("taken", "low", url, details, "Name appears in search results (collision risk)")
    return result("free", "medium", url, details, f"No web results for this exact name ({meta.get('provider')})")


def check_trademarks(http: Http, name: str, opts: "Options") -> dict:
    hits, meta = opts.search.search(f'"{name}" trademark')
    potential = []
    if not meta.get("blocked"):
        for hit in hits:
            blob = f"{hit.get('domain', '')} {hit.get('url', '')}".lower()
            if any(tm in blob for tm in TRADEMARK_DOMAINS):
                potential.append(hit)
    search_links = {
        "web search": f'https://duckduckgo.com/?q="{urllib.parse.quote(name)}"+trademark',
        "USPTO search": TRADEMARK_LINKS["USPTO (US)"],
        "EUIPO search": TRADEMARK_LINKS["EUIPO eSearch (EU)"],
        "WIPO search": TRADEMARK_LINKS["WIPO Global Brand DB"],
    }
    details = {
        "manual": True,
        "official_links": TRADEMARK_LINKS,
        "search_links": search_links,
        "public_hits": potential[:5],
        "search_provider": meta.get("provider"),
        "public_search_blocked": bool(meta.get("blocked")),
        "checked_public_search": not meta.get("blocked"),
    }
    if potential:
        return result("unknown", "medium", search_links["web search"], details,
                      "Public search surfaced trademark-related results — verify manually")
    if meta.get("blocked"):
        return result("manual", "low", search_links["web search"], details,
                      "Public trademark search unavailable (captcha/rate limit) — check USPTO/EUIPO/WIPO manually")
    return result("manual", "low", search_links["web search"], details,
                  "Trademarks are not auto-verifiable — check USPTO/EUIPO/WIPO manually")


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
CHECKS = {
    "github": lambda http, name, handle, opts: check_github(http, handle, opts.github_token),
    "x": lambda http, name, handle, opts: check_x(http, handle, opts),
    "telegram": lambda http, name, handle, opts: check_telegram(http, handle, opts),
    "reddit": lambda http, name, handle, opts: check_reddit(http, handle, opts),
    "instagram": lambda http, name, handle, opts: check_instagram(http, handle, opts),
    "itunes": lambda http, name, handle, opts: check_itunes(http, name),
    "google_play": lambda http, name, handle, opts: check_google_play(http, name, opts),
    "web": lambda http, name, handle, opts: check_web(http, name, opts),
    "trademarks": lambda http, name, handle, opts: check_trademarks(http, name, opts),
}


@dataclass
class Options:
    tlds: list[str] = field(default_factory=lambda: list(DEFAULT_TLDS))
    platforms: list[str] = field(default_factory=lambda: list(PLATFORMS))
    github_token: str | None = None
    web: bool = True
    trademarks: bool = True
    apps: bool = True
    timeout: float = 10.0
    retries: int = 1
    delay: float = 0.25
    concurrency: int = 6
    insecure: bool = False
    indirect: bool = False
    search: "WebSearch | None" = None


def check_name(name: str, opts: Options) -> dict:
    http = Http(timeout=opts.timeout, retries=opts.retries, delay=opts.delay, insecure=opts.insecure)
    handle = norm_handle(name)
    checks: dict[str, dict] = {}
    jobs: list[tuple[str, object]] = []
    for tld in opts.tlds:
        jobs.append((f"domain:{tld}", lambda http_=http, n=name, t=tld: check_domain(http_, n, t)))
    for platform in opts.platforms:
        fn = CHECKS.get(platform)
        if fn:
            jobs.append((platform, lambda http_=http, n=name, h=handle, f=fn: f(http_, n, h, opts)))
    if opts.apps:
        jobs.append(("itunes", lambda http_=http, n=name: check_itunes(http_, n)))
        jobs.append(("google_play", lambda http_=http, n=name: check_google_play(http_, n, opts)))
    if opts.web:
        jobs.append(("web", lambda http_=http, n=name: check_web(http_, n, opts)))
    if opts.trademarks:
        jobs.append(("trademarks", lambda http_=http, n=name: check_trademarks(http_, n, opts)))

    with ThreadPoolExecutor(max_workers=max(2, min(opts.concurrency, len(jobs)))) as pool:
        futures = {pool.submit(fn): key for key, fn in jobs}
        for future in as_completed(futures):
            key = futures[future]
            try:
                checks[key] = future.result()
            except Exception as exc:  # noqa: BLE001
                checks[key] = result("unknown", "low", "internal", {"error": str(exc)[:200]},
                                     "internal error while running this check")

    order = [f"domain:{tld}" for tld in opts.tlds] + [p for p in opts.platforms if p in CHECKS]
    if opts.apps:
        order.extend(["itunes", "google_play"])
    if opts.web:
        order.append("web")
    if opts.trademarks:
        order.append("trademarks")
    checks = {key: checks[key] for key in order if key in checks}

    summary = summarize(checks)
    return {
        "name": name,
        "handle": handle,
        "checked_at": utc_now(),
        "checks": checks,
        "summary": summary,
    }


def summarize(checks: dict) -> dict:
    domains = {k: v for k, v in checks.items() if k.startswith("domain:")}
    handles = {k: v for k, v in checks.items() if k in PLATFORMS}

    def count(group, status):
        return sum(1 for value in group.values() if value["status"] == status)

    free_domains = [k.split(":", 1)[1] for k, v in domains.items() if v["status"] == "free"]
    taken_domains = [k.split(":", 1)[1] for k, v in domains.items() if v["status"] == "taken"]
    unknown_domains = [k.split(":", 1)[1] for k, v in domains.items() if v["status"] == "unknown"]
    taken_handles = [k for k, v in handles.items() if v["status"] == "taken"]
    free_handles = [k for k, v in handles.items() if v["status"] == "free"]
    unknown_handles = [k for k, v in handles.items() if v["status"] == "unknown"]

    if taken_domains or taken_handles:
        verdict = "likely taken"
    elif free_domains and not taken_handles:
        verdict = "likely available"
    else:
        verdict = "unclear — manual review"

    return {
        "domains": {"free": free_domains, "taken": taken_domains, "unknown": unknown_domains,
                    "free_count": count(domains, "free"), "taken_count": count(domains, "taken"),
                    "unknown_count": count(domains, "unknown")},
        "handles": {"free": free_handles, "taken": taken_handles, "unknown": unknown_handles,
                    "free_count": count(handles, "free"), "taken_count": count(handles, "taken"),
                    "unknown_count": count(handles, "unknown")},
        "verdict": verdict,
    }


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #
STATUS_ICON = {"free": "available", "taken": "taken", "unknown": "unknown", "manual": "manual"}


def render_markdown(names: list[dict], opts: Options, names_arg: str) -> str:
    lines: list[str] = []
    lines.append("# Brand Name Check — report")
    lines.append("")
    lines.append(f"Generated: `{utc_now()}`  ·  tool: `brandcheck {VERSION}`  ·  input: `{names_arg}`")
    lines.append("")
    lines.append(f"TLDs: `{', '.join(opts.tlds)}` · platforms: `{', '.join(opts.platforms)}` "
                 f"· app stores: `{'on' if opts.apps else 'off'}` · web presence: `{'on' if opts.web else 'off'}` "
                 f"· trademark pointers: `{'on' if opts.trademarks else 'off'}`")
    lines.append("")
    lines.append("**Status legend:** `available` = no public registration/handle found (good), "
                 "`taken` = already in use, `unknown` = source blocked or rate-limited (verify manually), "
                 "`manual` = requires a human check (trademarks).")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Name | Domains available | Domains taken | Handles available | Handles taken | Verdict |")
    lines.append("|---|---|---|---|---|---|")
    for item in names:
        summary = item["summary"]
        lines.append(
            f"| **{item['name']}** | {summary['domains']['free_count']}/{len(opts.tlds)}"
            f" ({', '.join('.' + t for t in summary['domains']['free']) or '—'})"
            f" | {summary['domains']['taken_count']}"
            f" | {summary['handles']['free_count']}/{len(opts.platforms)}"
            f" | {summary['handles']['taken_count']}"
            f" ({', '.join(summary['handles']['taken']) or '—'})"
            f" | {summary['verdict']} |"
        )
    lines.append("")
    for item in names:
        lines.extend(render_name_markdown(item, opts))
    lines.append("---")
    lines.append("")
    lines.append("Checks are read-only public requests; network failures are reported as `unknown` "
                 "and never abort the run. Trademarks are **not** auto-verified — always confirm with "
                 "USPTO/EUIPO/WIPO (or a lawyer) before filing or launching.")
    lines.append("")
    return "\n".join(lines)


def render_name_markdown(item: dict, opts: Options) -> list[str]:
    lines: list[str] = []
    lines.append(f"## {item['name']}")
    lines.append("")
    lines.append(f"Handle used for social checks: `{item['handle']}`  ·  checked: `{item['checked_at']}`")
    lines.append("")
    lines.append(f"**Verdict: {item['summary']['verdict']}**")
    lines.append("")
    lines.append("### Domains (RDAP, DNS fallback)")
    lines.append("")
    lines.append("| Domain | Status | Confidence | Details | Source |")
    lines.append("|---|---|---|---|---|")
    for key, check in item["checks"].items():
        if not key.startswith("domain:"):
            continue
        tld = key.split(":", 1)[1]
        details = check.get("details") or {}
        detail_bits = []
        if details.get("registrar"):
            detail_bits.append(f"registrar: {details['registrar']}")
        events = details.get("events") or {}
        if events.get("registration"):
            detail_bits.append(f"created: {events['registration'][:10]}")
        if details.get("ip"):
            detail_bits.append(f"dns: {', '.join(details['ip'])}")
        if check.get("note"):
            detail_bits.append(check["note"])
        lines.append(f"| `{item['handle']}.{tld}` | {STATUS_ICON[check['status']]} | {check['confidence']}"
                     f" | {'; '.join(detail_bits) or '—'} | {check.get('source', '')} |")
    lines.append("")
    lines.append("### Social handles")
    lines.append("")
    lines.append("| Platform | Status | Confidence | Details | Source |")
    lines.append("|---|---|---|---|---|")
    for platform in opts.platforms:
        check = item["checks"].get(platform)
        if not check:
            continue
        details = check.get("details") or {}
        detail = check.get("note") or ""
        for key in ("login", "title", "karma", "public_repos"):
            if details.get(key):
                detail = f"{detail} ({key}: {details[key]})" if detail else f"{key}: {details[key]}"
        lines.append(f"| {platform} | {STATUS_ICON[check['status']]} | {check['confidence']}"
                     f" | {detail or '—'} | {check.get('source', '')} |")
    lines.append("")
    lines.append("### App stores")
    lines.append("")
    lines.append("| Store | Status | Confidence | Details | Source |")
    lines.append("|---|---|---|---|---|")
    for store, label in (("itunes", "Apple App Store"), ("google_play", "Google Play")):
        check = item["checks"].get(store)
        if not check:
            continue
        details = check.get("details") or {}
        bits = []
        if details.get("result_count") is not None:
            bits.append(f"results: {details['result_count']}")
        if details.get("exact_match"):
            bits.append("exact-name app found")
        if details.get("app_ids"):
            bits.append(f"ids: {', '.join(details['app_ids'][:3])}")
        if check.get("note"):
            bits.append(check["note"])
        lines.append(f"| {label} | {STATUS_ICON[check['status']]} | {check['confidence']}"
                     f" | {'; '.join(bits) or '—'} | {check.get('source', '')} |")
    lines.append("")
    if "web" in item["checks"]:
        check = item["checks"]["web"]
        details = check.get("details") or {}
        lines.append("### Web presence (web search, best effort)")
        lines.append("")
        lines.append(f"Status: **{check['status']}** ({check['confidence']} confidence) — {check.get('note', '')}")
        lines.append("")
        for hit in (details.get("top_results") or [])[:5]:
            lines.append(f"- [{hit.get('title', 'result')}]({hit.get('url', '')})")
        lines.append("")
    if "trademarks" in item["checks"]:
        check = item["checks"]["trademarks"]
        details = check.get("details") or {}
        lines.append("### Trademarks (manual verification required)")
        lines.append("")
        lines.append(f"Status: **{check['status']}** — {check.get('note', '')}")
        lines.append("")
        lines.append("| Office | Link |")
        lines.append("|---|---|")
        for label, link in (details.get("official_links") or {}).items():
            query = urllib.parse.quote(item["name"])
            href = f"{link}" if "?" not in link else link
            lines.append(f"| {label} | {href} (search for: `{item['name']}`) |")
        if details.get("public_hits"):
            lines.append("")
            lines.append("Public search hits that look trademark-related:")
            for hit in details["public_hits"]:
                lines.append(f"- [{hit.get('title', '')}]({hit.get('url', '')})")
        lines.append("")
    return lines


def print_summary(names: list[dict], opts: Options) -> None:
    print(f"brandcheck {VERSION} — {len(names)} name(s)")
    for item in names:
        summary = item["summary"]
        handles_taken = ", ".join(summary["handles"]["taken"]) or "none"
        print(f"  {item['name']:<20} domains free {summary['domains']['free_count']}/{len(opts.tlds)}"
              f" | domains taken {summary['domains']['taken_count']}"
              f" | handles free {summary['handles']['free_count']}/{len(opts.platforms)}"
              f" | handles taken: {handles_taken}"
              f" | verdict: {summary['verdict']}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="brandcheck",
        description="Check a brand name across domains, social handles, app stores and trademark pointers.",
    )
    parser.add_argument("names", nargs="*", help="brand names to check (e.g. novacat bitforge)")
    parser.add_argument("--names-file", help="text file with one name per line (blank lines and # comments ignored)")
    parser.add_argument("--tlds", default=",".join(DEFAULT_TLDS),
                        help=f"comma-separated TLD list (default: {','.join(DEFAULT_TLDS)})")
    parser.add_argument("--platforms", default=",".join(PLATFORMS),
                        help=f"comma-separated platforms (default: {','.join(PLATFORMS)})")
    parser.add_argument("--out", default="reports", help="output directory for reports (default: reports)")
    parser.add_argument("--json-name", default=None, help="JSON report filename (default: <slug>.json)")
    parser.add_argument("--md-name", default=None, help="Markdown report filename (default: <slug>.md)")
    parser.add_argument("--no-web", action="store_true", help="skip the DuckDuckGo web-presence check")
    parser.add_argument("--no-apps", action="store_true", help="skip the app-store checks (iTunes, Google Play)")
    parser.add_argument("--no-trademarks", action="store_true", help="skip trademark pointers/search")
    parser.add_argument("--indirect", action="store_true", help="when a platform blocks direct checks, also try an indirect web-search signal (more requests)")
    parser.add_argument("--github-token", default=None, help="optional GitHub token for api.github.com")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-request timeout in seconds (default 10)")
    parser.add_argument("--retries", type=int, default=1, help="retries for failed requests (default 1)")
    parser.add_argument("--delay", type=float, default=0.25, help="global delay between requests in seconds (default 0.25)")
    parser.add_argument("--concurrency", type=int, default=6, help="parallel checks per name (default 6)")
    parser.add_argument("--insecure", action="store_true", help="disable TLS certificate verification (debug only)")
    parser.add_argument("--version", action="version", version=f"brandcheck {VERSION}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    names: list[str] = list(args.names)
    if args.names_file:
        try:
            for line in Path(args.names_file).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    names.append(line)
        except Exception as exc:  # noqa: BLE001
            print(f"error: cannot read --names-file: {exc}", file=sys.stderr)
            return 2
    names = [name.strip() for name in names if name.strip()]
    if not names:
        print("error: no names given. Try: python brandcheck.py novacat bitforge", file=sys.stderr)
        return 2
    if len(names) > 25:
        print("error: at most 25 names per run (be polite to public APIs)", file=sys.stderr)
        return 2

    tlds = [t.strip().lstrip(".") for t in args.tlds.split(",") if t.strip()]
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip() in PLATFORMS]
    if not tlds:
        print("error: empty TLD list", file=sys.stderr)
        return 2

    token = args.github_token or os.environ.get("GITHUB_TOKEN")
    opts = Options(
        tlds=tlds,
        platforms=platforms,
        github_token=token,
        web=not args.no_web,
        trademarks=not args.no_trademarks,
        apps=not args.no_apps,
        indirect=bool(args.indirect),
        timeout=max(1.0, args.timeout),
        retries=max(0, args.retries),
        delay=max(0.0, args.delay),
        concurrency=max(1, args.concurrency),
        insecure=args.insecure,
    )

    started = time.time()
    opts.search = WebSearch(Http(timeout=opts.timeout, retries=opts.retries, delay=opts.delay,
                                 insecure=opts.insecure))
    items: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(3, len(names))) as pool:
        futures = {pool.submit(check_name, name, opts): name for name in names}
        for future in as_completed(futures):
            try:
                items.append(future.result())
            except Exception as exc:  # noqa: BLE001
                items.append({"name": futures[future], "handle": norm_handle(futures[future]),
                              "checked_at": utc_now(), "checks": {}, "error": str(exc)[:300],
                              "summary": {"domains": {"free": [], "taken": [], "unknown": [],
                                                      "free_count": 0, "taken_count": 0, "unknown_count": 0},
                                          "handles": {"free": [], "taken": [], "unknown": [],
                                                      "free_count": 0, "taken_count": 0, "unknown_count": 0},
                                          "verdict": "error"}})
    order = {name: index for index, name in enumerate(names)}
    items.sort(key=lambda item: order.get(item["name"], 999))

    slug = slugify("_".join(names))[:80] if len(names) <= 3 else f"brandcheck-{len(names)}-names"
    out_dir = Path(args.out)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        print(f"error: cannot create output directory: {exc}", file=sys.stderr)
        return 2
    json_path = out_dir / (args.json_name or f"{slug}.json")
    md_path = out_dir / (args.md_name or f"{slug}.md")

    payload = {
        "schema_version": "1.0",
        "tool": {"name": "brandcheck", "version": VERSION, "repo": "https://github.com/vanya9554-del/brandcheck"},
        "generated_at": utc_now(),
        "elapsed_seconds": round(time.time() - started, 2),
        "params": {"tlds": tlds, "platforms": platforms, "apps": opts.apps,
                   "web": opts.web, "trademarks": opts.trademarks, "indirect": opts.indirect},
        "results": items,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = render_markdown(items, opts, " ".join(names))
    md_path.write_text(markdown, encoding="utf-8")

    print_summary(items, opts)
    print()
    print(f"Markdown: {md_path}")
    print(f"JSON:     {json_path}")
    print(f"Done in {payload['elapsed_seconds']}s")
    unknown = sum(1 for item in items for check in item["checks"].values() if check["status"] == "unknown")
    if unknown:
        print(f"Note: {unknown} check(s) came back unknown (blocked/rate-limited) — see report notes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
