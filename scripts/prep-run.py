#!/usr/bin/env python3
"""Turn a saved get_registry result into this run's work list — the Step 2 bookkeeping, done once,
the same way every run.

Usage:
  timeout 120 python3 scripts/prep-run.py <registry-file> [--run-dir /tmp/pestidev-run]
                                          [--known-domains /tmp/pestidev-known-domains.txt]

Writes:
  <run-dir>/plan.json            budget, counts, and the lists below
  <run-dir>/dispatch/<slug>.json one per due / first-check site: url, slug, company, status,
                                 storedListingUrls (copied verbatim from the registry),
                                 knownActiveTitles (fold-name.sh's fold, via its Python port), resultFile
  <run-dir>/results/             emptied; agents write their results here
  the known-domains file         see known-domains.py

plan.json lists:
  due                     re-check now (lastChecked > 7 days, nextCheckAt passed, not excluded)
  firstCheck              status needs_first_check / no lastChecked — straight to site-processor
  deferred                backed off by an earlier run's nextCheckAt (js_rendered, bot_blocked,
                          repeated fetch failures) — do nothing
  atsCrawlToRetire        tracked sites on the eight ats-crawl hosts, not yet retired — ready-made
                          `rejected` records, send each once
  skippedPermanentlyRejected  `sites` entries that permanentlyRejected outranks — never touch
"""

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pestidev_lib as lib  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("registry")
    ap.add_argument("--run-dir", default=lib.RUN_DIR_DEFAULT)
    ap.add_argument("--known-domains", default=lib.KNOWN_DOMAINS_DEFAULT)
    ap.add_argument("--now", help="ISO timestamp to use instead of the clock (tests)")
    ap.add_argument("--force", action="store_true", help="wipe existing result files")
    a = ap.parse_args()

    reg = lib.load_registry(a.registry)
    now = lib.now_utc(a.now)
    rej = lib.rejected_slugs(reg)
    titles = reg.get("activeTitlesByCompany") or {}

    dispatch_dir = os.path.join(a.run_dir, "dispatch")
    results_dir = os.path.join(a.run_dir, "results")
    if os.path.isdir(results_dir) and os.listdir(results_dir) and not a.force:
        lib.die("%s already holds result files from this run — re-running prep would delete them. "
                "Run prep-run.py once per run; pass --force only to deliberately start over."
                % results_dir)
    for d in (dispatch_dir, results_dir):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)

    plan = {
        "generatedAt": lib.fmt_ts(now),
        "registryFile": os.path.abspath(a.registry),
        "uploadBudget": reg.get("uploadBudget"),
        "counts": reg.get("counts"),
        "due": [], "firstCheck": [], "deferred": [], "atsCrawlToRetire": [],
        "skippedPermanentlyRejected": [], "notDue": 0,
    }

    fold_cache = {}

    def known_titles(company):
        if company not in fold_cache:
            try:
                fold_cache[company] = lib.fold_company(company)
            except Exception as exc:  # never let one odd name stop the plan
                print("warning: fold-name.sh failed for %r: %s" % (company, exc), file=sys.stderr)
                fold_cache[company] = None
        key = fold_cache[company]
        return list(titles.get(key) or []) if key else []

    def write_dispatch(slug, site, kind):
        path = os.path.join(dispatch_dir, slug + ".json")
        company = site.get("company") or slug
        lib.write_json(path, {
            "kind": kind,
            "slug": slug,
            "url": site.get("url") or "",
            "company": company,
            "previousStatus": site.get("status") or "",
            "platformNote": site.get("platformNote") or site.get("note") or "",
            "storedListingUrls": list(site.get("listingUrls") or []),
            "knownActiveTitles": known_titles(company),
            "resultFile": os.path.join(results_dir, slug + ".json"),
        })
        return path

    rows = []
    for slug, site in sorted(reg.get("sites", {}).items()):
        site = site or {}
        if slug in rej:
            plan["skippedPermanentlyRejected"].append(slug)
            continue
        url = site.get("url") or ""
        if url and lib.is_ats_crawl_host(url):
            host = url.split("//", 1)[-1].split("/", 1)[0]
            plan["atsCrawlToRetire"].append({
                "slug": slug, "domain": host, "company": site.get("company") or slug,
                "reason": "covered by the board's own ats-crawl source"})
            continue
        last = lib.parse_ts(site.get("lastChecked"))
        if site.get("status") == "needs_first_check" or last is None:
            plan["firstCheck"].append({"slug": slug, "url": url, "company": site.get("company"),
                                       "dispatchFile": write_dispatch(slug, site, "first_check")})
            continue
        nxt = lib.parse_ts(site.get("nextCheckAt"))
        if nxt and nxt > now:
            plan["deferred"].append({"slug": slug, "status": site.get("status"),
                                     "nextCheckAt": site.get("nextCheckAt")})
            continue
        if now - last > lib.RECHECK_AFTER or (nxt and nxt <= now):
            rows.append((last, slug, site))
        else:
            plan["notDue"] += 1

    for last, slug, site in sorted(rows, key=lambda r: r[0]):
        plan["due"].append({
            "slug": slug, "url": site.get("url"), "company": site.get("company"),
            "lastChecked": site.get("lastChecked"), "previousStatus": site.get("status"),
            "storedCount": len(site.get("listingUrls") or []),
            "dispatchFile": write_dispatch(slug, site, "recheck"),
        })

    lines, stats = lib.build_known_domains(reg)
    os.makedirs(os.path.dirname(os.path.abspath(a.known_domains)), exist_ok=True)
    with open(a.known_domains, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    plan["knownDomainsFile"] = os.path.abspath(a.known_domains)
    plan["knownDomainsLines"] = len(lines)

    plan_path = os.path.join(a.run_dir, "plan.json")
    lib.write_json(plan_path, plan)

    budget = plan["uploadBudget"] or {}
    print("plan: %s" % plan_path)
    print("uploadBudget: %s/%s remaining" % (budget.get("remaining"), budget.get("limit")))
    print("due for re-check: %d   first check: %d   deferred (backoff): %d   not due: %d"
          % (len(plan["due"]), len(plan["firstCheck"]), len(plan["deferred"]), plan["notDue"]))
    print("skipped (permanently rejected, still in sites): %d" % len(plan["skippedPermanentlyRejected"]))
    print("ats-crawl sites to retire once under `rejected`: %d" % len(plan["atsCrawlToRetire"]))
    print("known-domains: %d lines -> %s" % (len(lines), plan["knownDomainsFile"]))
    for d in plan["due"]:
        print("  due  %-28s stored=%-3d last=%s" % (d["slug"], d["storedCount"],
                                                   (d["lastChecked"] or "")[:10]))
    for d in plan["firstCheck"]:
        print("  1st  %-28s %s" % (d["slug"], d["url"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
