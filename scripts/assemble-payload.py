#!/usr/bin/env python3
"""Build the submit_findings payload from the run's result files instead of retyping it (v2).

Every site-change-check and every site-processor writes its
result to <run-dir>/results/<slug>.json. This script turns those into `sitesChecked`, `rejected`
and `findings`, applies the recheck backoff, validates the whole thing, and writes the payload
the orchestrator then passes to submit_findings unchanged. It exists because a hand-assembled
payload put garbage into a site's listingUrls on 2026-10-02.

Usage:
  timeout 120 python3 scripts/assemble-payload.py [--run-dir /tmp/pestidev-run]
          [--labels <run-dir>/tech-labels.json] [--drop <slug> ...]

  --labels  JSON object mapping each finding's url to its comma-joined canonical technology labels
            (the one judgment call Step 4 still owns), e.g. {"https://x.hu/job/1": "PHP, SQL, Hungarian"}.
  --drop    leave a result file out (e.g. a duplicate the orchestrator decided not to record).

Writes <run-dir>/payload.json only when validation passes (exit 0). On errors it writes
<run-dir>/payload.invalid.json, prints every error, and exits 1 — fix the result files or labels
and run it again; never submit the invalid file.
"""

import argparse
import glob
import json
import os
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pestidev_lib as lib  # noqa: E402

PROCESSOR_KEYS = ("findings", "itRelevant", "passedLevel", "rejectReason")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default=lib.RUN_DIR_DEFAULT)
    ap.add_argument("--labels")
    ap.add_argument("--drop", action="append", default=[])
    ap.add_argument("--now")
    a = ap.parse_args()

    plan_path = os.path.join(a.run_dir, "plan.json")
    if not os.path.isfile(plan_path):
        lib.die("no %s — run prep-run.py first" % plan_path)
    plan = lib.read_json(plan_path)
    reg = lib.load_registry(plan["registryFile"])
    rej = lib.rejected_slugs(reg)
    sites = reg.get("sites", {})
    now = lib.now_utc(a.now)

    labels = {}
    labels_path = a.labels or os.path.join(a.run_dir, "tech-labels.json")
    if os.path.isfile(labels_path):
        labels = lib.read_json(labels_path)
        if not isinstance(labels, dict):
            lib.die("%s must be a JSON object {url: \"Label, Label\"}" % labels_path)

    sc, rj, fd, warnings, risky = {}, [], [], [], set()
    result_slugs = []
    for path in sorted(glob.glob(os.path.join(a.run_dir, "results", "*.json"))):
        slug = os.path.splitext(os.path.basename(path))[0]
        if slug in a.drop:
            continue
        try:
            r = lib.read_json(path)
        except ValueError as exc:
            lib.die("%s is not valid JSON (%s) — have the agent rewrite it" % (path, exc))
        result_slugs.append(slug)
        if r.get("slug") and r["slug"] != slug:
            warnings.append("%s: slug field %r differs from file name — file name wins"
                            % (path, r["slug"]))
        prev = sites.get(slug) or {}
        stored = prev.get("listingUrls") or []
        status = r.get("status") or ""
        is_processor = r.get("source") == "site-processor" or any(k in r for k in PROCESSOR_KEYS)

        if is_processor and status == "reject_permanent":
            if slug in rej:
                continue
            listing = r.get("listingUrl") or prev.get("url") or ""
            rj.append({"slug": slug, "domain": (urlparse(listing).hostname or "").lower()
                       or None, "company": r.get("company") or prev.get("company") or slug,
                       "reason": r.get("rejectReason") or r.get("note") or "rejected permanently"})
            continue

        entry = {
            "url": (r.get("listingUrl") if is_processor else r.get("url")) or prev.get("url") or "",
            "company": r.get("company") or prev.get("company") or slug,
            "status": status,
            "listingUrls": list(r.get("listingUrls") or []),
        }
        if not is_processor:
            if status == "ok":
                prev_status = r.get("previousStatus") or prev.get("status") or ""
                entry["status"] = (prev_status if prev_status and prev_status not in
                                   lib.FAIL_STATUSES | lib.LONG_BACKOFF_STATUSES
                                   | {"needs_first_check"} else "checked")
            if r.get("changed") and not r.get("suspectExtraction"):
                warnings.append("%s: change-check reported %d new URL(s) but no site-processor "
                                "result replaced it — fine only for a bulk non-IT skip"
                                % (slug, len(r.get("newUrls") or [])))
        if status in lib.FAIL_STATUSES | lib.LONG_BACKOFF_STATUSES and not entry["listingUrls"]:
            entry["listingUrls"] = list(stored)
        if isinstance(r.get("postingsFound"), int):
            entry["postingsFound"] = r["postingsFound"]
        if r.get("note"):
            entry["note"] = str(r["note"])[:400]
        sc[slug] = entry

        for f in r.get("findings") or []:
            url = f.get("url")
            finding = {"slug": slug, "title": f.get("title"), "url": url,
                       "company": r.get("company") or prev.get("company") or slug}
            if f.get("location"):
                finding["location"] = f["location"]
            if f.get("experienceLiteral"):
                finding["experience"] = f["experienceLiteral"]
            tech = labels.get(url)
            if tech:
                finding["technologies"] = tech
            elif f.get("techMentions"):
                warnings.append("finding %s has techMentions but no entry in %s — submitted without "
                                "technologies" % (url, os.path.basename(labels_path)))
            if f.get("titleApiRisk"):
                risky.add(url)
            fd.append(finding)

    for rec in plan.get("atsCrawlToRetire", []):
        if rec["slug"] not in rej and rec["slug"] not in sc \
                and rec["slug"] not in {x["slug"] for x in rj}:
            rj.append(dict(rec))

    budget = (plan.get("uploadBudget") or {}).get("remaining")
    if isinstance(budget, int) and len(fd) > budget:
        fd.sort(key=lambda f: f["url"] in risky)
        dropped = fd[budget:]
        fd = fd[:budget]
        warnings.append("trimmed %d finding(s) over the upload budget (titleApiRisk first): %s"
                        % (len(dropped), ", ".join(f["url"] for f in dropped)))

    payload = {"findings": fd, "sitesChecked": sc}
    if rj:
        payload["rejected"] = rj
    backoff_notes = lib.apply_backoff(payload, reg, now)
    errors, more_warnings = lib.validate_payload(payload, reg, plan, result_slugs)
    warnings += more_warnings

    out = os.path.join(a.run_dir, "payload.json" if not errors else "payload.invalid.json")
    lib.write_json(out, payload)
    if not errors and os.path.exists(os.path.join(a.run_dir, "payload.invalid.json")):
        os.remove(os.path.join(a.run_dir, "payload.invalid.json"))

    print("findings: %d   sitesChecked: %d   rejected: %d   size: %d bytes"
          % (len(fd), len(sc), len(rj), len(json.dumps(payload, ensure_ascii=False))))
    for n in backoff_notes:
        print("backoff: " + n)
    for w in warnings:
        print("WARNING: " + w)
    for e in errors:
        print("ERROR: " + e)
    print(("INVALID — not written as payload.json: %s" if errors else "OK — submit exactly: %s") % out)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
