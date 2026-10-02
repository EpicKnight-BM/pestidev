#!/usr/bin/env python3
"""Build and query the known-domains de-duplication file — one normalization for both sides.

Replaces the per-run, model-written script that kept collapsing join.com tenants to a single
`join.com/companies` line and missing `nisz.hu` vs `karrier.nisz.hu` (INCIDENTS.md § join.com
tenant collisions in the known-domains file).

Usage:
  timeout 30 python3 scripts/known-domains.py build <registry-file> [--out /tmp/pestidev-known-domains.txt]
  timeout 30 python3 scripts/known-domains.py check <url-or-domain> [<url-or-domain> ...] [--file /tmp/pestidev-known-domains.txt]
  timeout 30 python3 scripts/known-domains.py identity <url-or-domain> [...]

`check` prints one line per candidate, `KNOWN <candidate> (<matched line>)` or `NEW <candidate>`,
and exits 2 with `NO knownDomainsFile` if the file is missing or empty.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pestidev_lib as lib  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("registry")
    b.add_argument("--out", default=lib.KNOWN_DOMAINS_DEFAULT)
    c = sub.add_parser("check")
    c.add_argument("candidates", nargs="+")
    c.add_argument("--file", default=lib.KNOWN_DOMAINS_DEFAULT)
    i = sub.add_parser("identity")
    i.add_argument("values", nargs="+")
    a = ap.parse_args()

    if a.cmd == "build":
        lines, stats = lib.build_known_domains(lib.load_registry(a.registry))
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("wrote %d lines to %s (sites: %d, rejected: %d [%d recovered from free text, %d slug "
              "only], site urls with no usable host: %d)"
              % (len(lines), a.out, stats["sites"], stats["rejected"], stats["rejectedFromText"],
                 stats["rejectedSlugOnly"], stats["unparseable"]))
        return 0

    if a.cmd == "identity":
        for v in a.values:
            print("%s -> %s" % (v, ", ".join(lib.identities(v)) or "(none)"))
        return 0

    if not os.path.isfile(a.file) or os.path.getsize(a.file) == 0:
        print("NO knownDomainsFile at %s" % a.file)
        return 2
    with open(a.file, encoding="utf-8") as f:
        known = {ln.strip() for ln in f if ln.strip()}
    for cand in a.candidates:
        hit = lib.check_known(cand, known)
        print("KNOWN %s (%s)" % (cand, hit) if hit else "NEW %s" % cand)
    return 0


if __name__ == "__main__":
    sys.exit(main())
