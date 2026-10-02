"""Shared helpers for the run-preparation and submission scripts in this directory.

Everything here is deterministic bookkeeping that used to be re-derived by the model on every run
(known-domains dedup, the aged-site work list, the submission payload) and kept going wrong in small
ways. Standard library only: the
cloud routine has a bare python3 and nothing pip-installed.

Not meant to be run directly; imported by the scripts next to it.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPTS_DIR)

RUN_DIR_DEFAULT = "/tmp/pestidev-run"
KNOWN_DOMAINS_DEFAULT = "/tmp/pestidev-known-domains.txt"

RECHECK_AFTER = timedelta(days=7)
BACKOFF = timedelta(days=30)

# Multi-tenant hosts: the tenant (subdomain label or first path segment), never the shared parent,
# is a company's identity there. Superset of the list prompt-v2.md / company-discovery.md name, plus
# the other tenant-per-subdomain platforms that show up in the registry.
SHARED_ATS_SUFFIXES = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com", "recruitee.com",
    "personio.com", "personio.de", "workable.com", "breezy.hr", "join.com", "karrierportal.hu",
    "hrfelho.hu", "myworkdayjobs.com", "teamtailor.com", "bamboohr.com", "careers-page.com",
    "traffit.com", "zohorecruit.com", "homerun.co",
)

# Shared hosts where the tenant is the FIRST PATH SEGMENT rather than a subdomain
# (`job-boards.greenhouse.io/gravity`). join.com is handled separately (`/companies/<tenant>`).
PATH_TENANT_HOSTS = (
    "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io", "boards.greenhouse.io",
    "jobs.lever.co", "jobs.eu.lever.co", "jobs.ashbyhq.com", "jobs.smartrecruiters.com",
    "careers.smartrecruiters.com", "apply.workable.com", "careers-page.com",
)

# Parent domains whose subdomains belong to unrelated owners. A subdomain here never also gets its
# bare parent written as an identity — `foo.netlify.app` says nothing about `bar.netlify.app`.
GENERIC_PARENTS = (
    "netlify.app", "vercel.app", "github.io", "gitlab.io", "pages.dev", "web.app", "firebaseapp.com",
    "herokuapp.com", "azurewebsites.net", "cloudfront.net", "wixsite.com", "wordpress.com",
    "blogspot.com", "webnode.hu", "webnode.page", "mozello.hu", "unas.hu", "shoprenter.hu",
    "linkedin.com", "facebook.com", "google.com",
)

# Second-level labels that are part of the public suffix, so the registrable domain has 3 labels.
MULTI_LABEL_TLDS = ("co.uk", "org.uk", "com.au", "co.hu", "org.hu", "com.hu", "info.hu", "gov.hu",
                    "com.tr", "co.at", "or.at", "com.pl", "co.jp")

# The eight hosts the board's own hourly ats-crawl worker harvests (prompt-v2.md Step 2).
ATS_CRAWL_SUFFIXES = (
    "jobs.ashbyhq.com", "greenhouse.io", "lever.co", "smartrecruiters.com", "recruitee.com",
    "jobs.personio.com", "jobs.personio.de", "bamboohr.com", "teamtailor.com",
)

# Site statuses that mean "we could not read the listing this time".
FAIL_STATUSES = {
    "unreachable_timeout", "fetch_error", "fetch_error_404", "unreachable_botcheck",
    "unreachable_ssl_error", "unreachable_network_error", "content_unreachable",
    "no_reachable_listing",
}
# Site statuses that mean "this listing structurally can't be read by curl right now" — back off
# immediately instead of waiting for a second failure.
LONG_BACKOFF_STATUSES = {"js_rendered", "bot_blocked"}


# --- time -------------------------------------------------------------------------------------

def now_utc(override=None):
    if override:
        return parse_ts(override)
    return datetime.now(timezone.utc)


def parse_ts(value):
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def fmt_ts(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# --- registry ---------------------------------------------------------------------------------

def load_registry(path):
    """Load a saved get_registry result. Accepts the bare JSON object, or an MCP content wrapper
    ([{"type":"text","text":"..."}] / {"content":[...]}) in case the harness saved it that way."""
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    data = json.loads(raw)
    if isinstance(data, dict) and "content" in data and "sites" not in data:
        data = data["content"]
    if isinstance(data, list):
        texts = [c.get("text", "") for c in data if isinstance(c, dict)]
        data = json.loads("".join(texts))
    if not isinstance(data, dict) or "sites" not in data:
        raise ValueError("%s does not look like a get_registry result (no 'sites' key)" % path)
    data.setdefault("permanentlyRejected", [])
    data.setdefault("knownUrls", [])
    data.setdefault("activeTitlesByCompany", {})
    return data


def rejected_slugs(registry):
    return {r.get("slug") for r in registry.get("permanentlyRejected", [])
            if isinstance(r, dict) and r.get("slug")}


# --- hosts and identities ---------------------------------------------------------------------

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def _split(value):
    """Return (host, path) for a URL or a bare domain[/path], lowercased host, www. stripped."""
    value = (value or "").strip()
    if not value:
        return "", ""
    if "://" not in value:
        value = "https://" + value
    p = urlparse(value)
    host = (p.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host, p.path or ""


def looks_like_host(value):
    host, _ = _split(value)
    return bool(host) and bool(_HOST_RE.match(host)) and " " not in (value or "").strip()


def _ends_with(host, suffix):
    return host == suffix or host.endswith("." + suffix)


def shared_suffix(host):
    for s in SHARED_ATS_SUFFIXES:
        if _ends_with(host, s):
            return s
    return None


def is_ats_crawl_host(url):
    host, _ = _split(url)
    return any(_ends_with(host, s) for s in ATS_CRAWL_SUFFIXES)


def registrable(host):
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_LABEL_TLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def identities(value):
    """Every identity string a URL/domain should match on in the known-domains file.

    - Shared multi-tenant host, tenant in the subdomain (`ltg.breezy.hr`): the full host.
    - Shared multi-tenant host, tenant in the path (`join.com/companies/kfs1`,
      `job-boards.greenhouse.io/gravity`): host + tenant path. A bare board host with no tenant
      yields nothing — it identifies no company.
    - Any other site: the host without `www.`, PLUS its registrable parent when the host is a
      subdomain (`karrier.nisz.hu` -> also `nisz.hu`), unless the parent is a generic hosting
      domain whose subdomains belong to unrelated owners.
    """
    host, path = _split(value)
    if not host or not _HOST_RE.match(host):
        return []
    suffix = shared_suffix(host)
    if suffix:
        segs = [s for s in path.split("/") if s]
        if host == "join.com":
            if len(segs) >= 2 and segs[0] == "companies":
                return ["join.com/companies/%s" % segs[1].lower()]
            return []
        if host in PATH_TENANT_HOSTS:
            return ["%s/%s" % (host, segs[0].lower())] if segs else []
        if host == suffix:
            return []          # the bare platform host identifies no company
        return [host]          # tenant in the subdomain: ltg.breezy.hr, bkk.karrierportal.hu, ...
    out = [host]
    parent = registrable(host)
    if parent != host and not any(_ends_with(parent, g) for g in GENERIC_PARENTS):
        out.append(parent)
    return out


def record_domain(*urls):
    """The `domain` to store on a `permanentlyRejected` record: the company's identity, not just
    its host. On a shared ATS host that means the tenant path (`join.com/companies/kfs1`) — a bare
    `join.com` identifies nobody, so once the server retires the matching `sites` entry the company
    could be "discovered" again. Tries each URL in order; falls back to the plain host."""
    for u in urls:
        ids = identities(u or "")
        if ids:
            return ids[0]
    for u in urls:
        host, _ = _split(u or "")
        if host:
            return host
    return None


def hosts_in_text(text):
    """Pull hostname-looking tokens out of free text (for permanentlyRejected records whose
    `domain` field holds a sentence instead of a domain)."""
    found = []
    for m in re.finditer(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:hu|com|eu|io|net|org|co|hr|app|"
                         r"dev|de|at|uk|tech|ai|cloud|digital|engineering))\b",
                         (text or "").lower()):
        found.append(m.group(1))
    return found


def build_known_domains(registry):
    """Return (sorted identity lines, stats)."""
    lines = set()
    stats = {"sites": 0, "rejected": 0, "rejectedFromText": 0, "rejectedSlugOnly": 0,
             "unparseable": 0}
    for slug, site in registry.get("sites", {}).items():
        url = (site or {}).get("url") or ""
        ids = identities(url)
        if ids:
            lines.update(ids)
            stats["sites"] += 1
        else:
            stats["unparseable"] += 1
    for rec in registry.get("permanentlyRejected", []):
        if not isinstance(rec, dict):
            continue
        domain = rec.get("domain") or ""
        ids = identities(domain) if looks_like_host(domain) else []
        if not ids:
            for h in hosts_in_text("%s %s" % (domain, rec.get("company") or "")):
                ids.extend(identities(h))
            if ids:
                stats["rejectedFromText"] += 1
        if ids:
            lines.update(ids)
            stats["rejected"] += 1
        elif rec.get("slug"):
            # No usable domain anywhere: keep the slug, per company-discovery.md's input contract.
            lines.add(rec["slug"].lower())
            stats["rejectedSlugOnly"] += 1
    return sorted(lines), stats


def check_known(candidate, known_lines):
    """Return the first identity of `candidate` found in `known_lines` (a set), or None."""
    for ident in identities(candidate):
        if ident in known_lines:
            return ident
    raw = (candidate or "").strip().lower()
    if raw in known_lines:
        return raw
    return None


# --- name folding / url tails (delegate to the existing shell scripts so there is one copy) -----

def _sh(script, *args):
    out = subprocess.run(["sh", os.path.join(SCRIPTS_DIR, script)] + list(args),
                         capture_output=True, text=True, timeout=10, encoding="utf-8")
    if out.returncode != 0:
        raise RuntimeError("%s failed: %s" % (script, out.stderr.strip()))
    return out.stdout.strip()


_ACCENTS = str.maketrans("áéíóöőúüűÁÉÍÓÖŐÚÜŰ", "aeiooouuuAEIOOOUUU")
_LEGAL = set("zrt nyrt kft bt kkt kht nonprofit ev zartkoruen mukodo reszvenytarsasag gmbh ag ltd "
             "limited llc inc plc sa srl bv nv oy ab as spa co".split())


def fold_name(mode, text):
    """Python port of fold-name.sh — must stay identical to it. Used for bulk lookups
    (one shell spawn per site was slow); the shell script stays the reference the agents call."""
    text = text or ""
    if mode == "title":
        text = re.sub(r"\([^)]*\)", "", text)
    text = text.translate(_ACCENTS)
    text = re.sub(r"[A-Z]+", lambda m: m.group(0).lower(), text)   # ASCII-only, like tr in C locale
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    if mode == "company":
        text = " ".join(w for w in text.split() if w not in _LEGAL)
    return text


def fold_company(name):
    return fold_name("company", name)


def fold_company_sh(name):
    return _sh("fold-name.sh", "company", name or "")


def strip_url_tail(url):
    return _sh("strip-url-tail.sh", url)


# --- tech labels --------------------------------------------------------------------------------

def load_tech_labels():
    """The canonical label list, parsed out of prompt-v2.md so it only lives in one place."""
    path = os.path.join(REPO_ROOT, "prompt-v2.md")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("JavaScript, TypeScript, Python, Java,"):
                    return {t.strip().rstrip(".") for t in s.split(", ") if t.strip()}
    except OSError:
        pass
    return None


# --- payload checks and backoff -------------------------------------------------------------------

_URL_RE = re.compile(r"^https?://[^\s\"'<>]+$")


def is_url(value):
    return isinstance(value, str) and bool(_URL_RE.match(value))


def apply_backoff(payload, registry, now):
    """Annotate each sitesChecked entry with failStreak/nextCheckAt (stored verbatim by the API, and
    honoured by prep-run.py). Returns a list of human-readable notes."""
    notes = []
    sites = registry.get("sites", {})
    for slug, entry in (payload.get("sitesChecked") or {}).items():
        if not isinstance(entry, dict):
            continue
        status = entry.get("status") or ""
        prev = sites.get(slug) or {}
        prev_streak = prev.get("failStreak")
        if not isinstance(prev_streak, int):
            prev_streak = 1 if (prev.get("status") in FAIL_STATUSES | LONG_BACKOFF_STATUSES) else 0
        if status in LONG_BACKOFF_STATUSES:
            entry["failStreak"] = prev_streak + 1
            entry["nextCheckAt"] = fmt_ts(now + BACKOFF)
            notes.append("%s: %s -> next check %s" % (slug, status, entry["nextCheckAt"][:10]))
        elif status in FAIL_STATUSES:
            streak = prev_streak + 1
            entry["failStreak"] = streak
            if streak >= 2:
                entry["nextCheckAt"] = fmt_ts(now + BACKOFF)
                notes.append("%s: %s, %d failures in a row -> next check %s"
                             % (slug, status, streak, entry["nextCheckAt"][:10]))
            else:
                entry["nextCheckAt"] = None
        else:
            entry["failStreak"] = 0
            entry["nextCheckAt"] = None
    return notes


def validate_payload(payload, registry, plan=None, results_slugs=None):
    """Return (errors, warnings). Errors mean: do not submit this payload."""
    errors, warnings = [], []
    if not isinstance(payload, dict):
        return ["payload is not a JSON object"], []
    extra = set(payload) - {"findings", "sitesChecked", "rejected"}
    if extra:
        errors.append("unknown top-level keys: %s" % ", ".join(sorted(extra)))

    sites = registry.get("sites", {})
    rej = rejected_slugs(registry)
    known_urls = set(registry.get("knownUrls") or [])
    sc = payload.get("sitesChecked") or {}
    rj = payload.get("rejected") or []
    fd = payload.get("findings") or []

    if not isinstance(sc, dict):
        errors.append("sitesChecked must be an object keyed by slug")
        sc = {}
    for slug, e in sc.items():
        where = "sitesChecked[%s]" % slug
        if not isinstance(e, dict):
            errors.append("%s is not an object" % where)
            continue
        if slug in rej:
            errors.append("%s: slug is in permanentlyRejected — never refresh it" % where)
        if not isinstance(e.get("status"), str) or not e.get("status"):
            errors.append("%s: missing status" % where)
        url = e.get("url")
        if url not in ("", None) and not is_url(url):
            errors.append("%s: url is not a URL: %r" % (where, str(url)[:80]))
        lu = e.get("listingUrls")
        if not isinstance(lu, list):
            errors.append("%s: listingUrls must be an array" % where)
            continue
        bad = [u for u in lu if not is_url(u)]
        if bad:
            errors.append("%s: %d listingUrls entries are not URLs, e.g. %r"
                          % (where, len(bad), str(bad[0])[:80]))
        if len(set(lu)) != len(lu):
            warnings.append("%s: listingUrls has duplicates" % where)
        stored = (sites.get(slug) or {}).get("listingUrls") or []
        if stored and not lu:
            if e.get("status") in FAIL_STATUSES | LONG_BACKOFF_STATUSES:
                errors.append("%s: failed fetch (%s) with empty listingUrls would wipe %d stored "
                              "URLs — carry the stored set forward" % (where, e["status"], len(stored)))
            else:
                warnings.append("%s: listingUrls empty, %d were stored — fine only if the listing "
                                "really emptied" % (where, len(stored)))

    if not isinstance(rj, list):
        errors.append("rejected must be an array")
        rj = []
    for i, r in enumerate(rj):
        where = "rejected[%d]" % i
        if not isinstance(r, dict):
            errors.append("%s is not an object (the API silently drops strings)" % where)
            continue
        if not r.get("slug"):
            errors.append("%s: missing slug" % where)
        elif r["slug"] in rej:
            errors.append("%s: %s is already permanentlyRejected — do not re-send" % (where, r["slug"]))
        elif r["slug"] in sc:
            errors.append("%s: %s is also in sitesChecked — pick one" % (where, r["slug"]))
        if not r.get("reason"):
            errors.append("%s: missing reason" % where)

    if not isinstance(fd, list):
        errors.append("findings must be an array")
        fd = []
    labels = load_tech_labels()
    if labels is None:
        warnings.append("could not load the tech label list from prompt-v2.md — labels not checked")
    seen = set()
    for i, f in enumerate(fd):
        where = "findings[%d]" % i
        if not isinstance(f, dict):
            errors.append("%s is not an object" % where)
            continue
        for k in ("slug", "title", "url", "company"):
            if not f.get(k):
                errors.append("%s: missing %s" % (where, k))
        if f.get("url") and not is_url(f["url"]):
            errors.append("%s: url is not a URL: %r" % (where, str(f["url"])[:80]))
        if f.get("url") in known_urls:
            errors.append("%s: %s is already in knownUrls" % (where, f["url"]))
        if f.get("url") in seen:
            errors.append("%s: duplicate url in this batch" % where)
        seen.add(f.get("url"))
        if "titleApiRisk" in f or "levelJudgment" in f or "techMentions" in f:
            errors.append("%s: agent-only fields (titleApiRisk/levelJudgment/techMentions) must "
                          "not be submitted" % where)
        if labels is not None and f.get("technologies"):
            unknown = [t.strip() for t in str(f["technologies"]).split(",")
                       if t.strip() and t.strip() not in labels]
            if unknown:
                errors.append("%s: technologies not on the canonical list: %s"
                              % (where, ", ".join(unknown)))
        if f.get("slug") and f["slug"] not in sc:
            warnings.append("%s: slug %s has no sitesChecked entry" % (where, f["slug"]))

    if plan:
        budget = (plan.get("uploadBudget") or {}).get("remaining")
        if isinstance(budget, int) and len(fd) > budget:
            errors.append("%d findings but uploadBudget.remaining is %d" % (len(fd), budget))
        touched = set(sc) | {r.get("slug") for r in rj if isinstance(r, dict)}
        for slug in (results_slugs or []):
            if slug not in touched:
                errors.append("a result file exists for %s but it is in neither sitesChecked nor "
                              "rejected" % slug)
        due = [d["slug"] for d in plan.get("due", [])] + [d["slug"] for d in plan.get("firstCheck", [])]
        missing = [s for s in due if s not in touched]
        if missing:
            warnings.append("%d due sites not recorded this run (fine only if the 40-minute clock "
                            "stopped dispatching): %s" % (len(missing), ", ".join(missing[:15])))
    return errors, warnings


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


def die(msg, code=2):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(code)
