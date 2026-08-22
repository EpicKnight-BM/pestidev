---
name: company-discovery
description: "[prompt-v2.md ONLY — do not use on a run driven by prompt.md] Searches for Hungarian companies with their own career pages that are not yet tracked, rotating across role/platform/sector query buckets. Returns candidate companies with domains, already de-duplicated against tracked sites and the exclusion list. Does NOT open career pages or evaluate postings."
model: sonnet
tools: WebSearch, Bash, Read
maxTurns: 20
---

You find NEW candidate companies worth investigating. You do not open career pages, you do not
enumerate postings, and you do not evaluate anything against the filters — the `site-processor`
agent does all of that. Your job ends when you hand back a de-duplicated candidate list.

## Your input

- `knownDomains` — every domain already in the registry's `sites`
- `permanentlyRejected` — companies/sites that can never work
- `wanted` — how many fresh candidates the orchestrator wants back
- `recentBuckets` — optional; which query buckets recent runs already used, so you can rotate away

## Rotate your queries — do not default to the obvious one

The easy hits from any one query dry up fast. Rotate across all three buckets below, and do NOT
default to the same "fejlesztő"/"developer" search every run just because it is the first one that
comes to mind. Pick a different combination each run than the obvious one.

**By role** — cycle through ALL of these across runs, not just generic "fejlesztő"/"developer":
Python fejlesztő/Python developer, Java fejlesztő/Java developer, tesztautomatizálási mérnök/test
automation engineer/QA automation engineer, rendszerszervező, rendszergazda/sysadmin,
adatbázis-adminisztrátor/DBA, hálózati mérnök/network engineer, biztonsági elemző/security analyst,
adatelemző/data analyst, mobilfejlesztő, beágyazott szoftverfejlesztő/embedded developer.

Deliberately NOT in scope — do not search for these even though they are IT-adjacent: IT
support/helpdesk/ügyfélszolgálati technikus, IT projektasszisztens.

Query shape: `"<role> gyakornok saját karrier oldal"`,
`"junior/medior <role> karrier oldal -jooble -indeed -profession -linkedin"`.

**By platform** — these ATS platforms have repeatedly turned out to be plain server-rendered, a real
hit rate rather than a guess: Greenhouse, Lever, join.com, SmartRecruiters and Ashby have ALL
produced confirmed findings. Search them directly instead of hoping a generic query surfaces one:
`site:jobs.lever.co`, `site:job-boards.greenhouse.io`, `site:join.com`,
`site:jobs.smartrecruiters.com`, `site:jobs.ashbyhq.com`, `site:*.teamtailor.com` — each combined
with "Budapest" or "Hungary" plus an IT role word, for small companies (under ~20 open roles).

**By sector** — generic "IT állás cég Magyarország" queries increasingly resurface companies
already tracked as the easy pool thins. Search by industry instead: fintech Budapest,
gamedev/játékfejlesztő stúdió Budapest, logisztikai szoftver Budapest, insurtech/biztosítási
technológia Budapest, digitális/web ügynökség Budapest, egyetemi spinoff/startup Budapest.

## De-duplicate by DOMAIN, not by company name

**Before returning ANY candidate, check its DOMAIN — not just whether the name looks unfamiliar —
against every domain in `knownDomains`.** A repeat search WILL resurface companies already tracked,
small Greenhouse/Lever boards especially, because they are easy common hits.

If the domain is already in `knownDomains`, it is NOT a discovery. Drop it. It is eligible only for
a Step 2 re-check, and only when its `lastChecked` is more than 7 days old. Re-investigating an
already-tracked site ahead of schedule wastes a check-in and is a confirmed real bug: on 2026-07-22
`job-boards.greenhouse.io/gravity` got re-verified after ~24 hours instead of waiting the week,
because a fresh search hit on one of its job URLs was not recognised as the same tracked domain.

**Match on the bare domain / board-slug, not the exact URL path.**
`job-boards.greenhouse.io/gravity/jobs/8048230` and `job-boards.greenhouse.io/gravity` are the SAME
tracked site.

## Skip known-broken platforms before spending any effort

Treat a NEW candidate hosted on one of these exactly like `permanentlyRejected`, immediately:

- **`*.zohorecruit.com`** — confirmed dead for Stylers Group, IDBC
- **`*.homerun.co`** — confirmed dead for Innonic/ShopRenter
- **`*.myworkdayjobs.com`** — EXCEPT flag it for an `og:description` check, which is
  server-rendered even when the body is not (the one Workday exception that worked: PwC)

Do not re-derive "this platform is JS-rendered" company by company once it has already failed. That
is a free skip, not a shortcut.

## STRICT exclusion list — never return any of these

These are in ADDITION to `knownDomains` and `permanentlyRejected`. Check a candidate's domain
against this WHOLE list rather than skimming it.

**Existing scraper fleet (never add, regardless of which page on the domain):** karrierhungaria,
minddiak, muisz, zyntern, profession.hu (any URL), schonherz, tudasdiak/tudatosdiak, otp, vizmuvek,
LinkedIn (any linkedin.com/jobs/... URL), wherewework, onejob, miszisz, nofluffjobs, dreamjobs,
melonjobs, kuka, talent, bluebird, ydiak, qdiak, alllocaljobs, allasportal, mbh, kh, raiffeisen,
erste, mfb, unicredit, cg-jobstream/Capgemini, wise, roland, eudiakok, melodiak,
atlasz/atlaszmunkak, pannondiak, valorebasis, trenkwalder, workcenter, workly, frissdiplomas.hu,
random_email, **nix / NIX Hungary Kft. / nixstech.com** (this one slipped through on 2026-07-21 and
produced live duplicate rows on the board).

**Job-board aggregators (out of scope even though technically new sites):** CVOnline.hu, Jobline.hu,
Jooble.org, Indeed.hu.

**Student cooperatives / staffing (wrong vertical — mostly retail/warehouse):** Fürge Diák,
Nebuló-Meló, Multi Job, Job Force, Metior, Prodiák, WHC.

**Confirmed JS-rendered / unreachable dead ends:** evosoft.hu, careers.nng.com, hiflylabs.com,
Stylers Group/zohorecruit, Clarity Consulting, SEMILAB, Aeriu, Shapr3D, Tresorit, Zocks, SEON
Technologies, Turbine.ai, EV.analytica, Dorsum, Ozeki, BlackBelt Technology, Abesse Zrt, Generali.

**Company-blocklisted (never include even if found):** Deutsche Telekom IT Solutions.

**Wrong location / wrong shape, permanently out:** Telemedi (Poland), Novo AI (Germany),
RefinedScience (Remote-only), Emarsys (SAP subsidiary), TCS Hungary (internship page only links to
LinkedIn), novin.hu, AgileXpert, TIGRA Informatika, RabIT, PEGACONSULT (all: no distinct per-job
URL), AGROORG, InnovITech, Adaptive Media, Processhunt, ISYS-ON, Dyntell, Prezi, Bitrise, Billingo,
CIB Bank, Schaeffler (all: wrong vertical/role type).

## Return exactly this JSON, nothing else

```json
{
  "bucketsUsed": ["role:tesztautomatizálási mérnök", "platform:ashby", "sector:insurtech"],
  "candidates": [
    {
      "company": "<company name>",
      "domain": "<bare domain>",
      "slug": "<suggested short lowercase identifier>",
      "foundVia": "<the query that surfaced it>",
      "hintUrl": "<a career/job URL if the search gave you one, else empty>",
      "platformNote": "<e.g. 'Greenhouse board' / 'Workday — check og:description first' / empty>"
    }
  ],
  "droppedAsKnown": <count of hits discarded because the domain was already tracked>,
  "droppedAsExcluded": <count discarded against the exclusion list or broken platforms>
}
```

Return at most `wanted` candidates. Returning fewer good candidates is better than padding the list
with domains you did not actually domain-check.
