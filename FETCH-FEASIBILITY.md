# Tender collection feasibility — 12 October 2026

Live direct HTTP tests succeeded for all four sources, with HSL using its NIC address. These are bounded feasibility tests, not proof of exhaustive coverage or long-term reliability.

| Source | Tested route | Observed result |
|---|---|---|
| GeM | GET https://bidplus.gem.gov.in/all-bids, then the page's own POST https://bidplus.gem.gov.in/all-bids-data using its anonymous session cookie and dynamically extracted CSRF token | Two pages returned code 200, 10 records each. The response reported 45,522 matching ongoing records; that total was not fully downloaded. |
| CPPP eProcurement | https://eprocure.gov.in/eprocure/app | 10 latest tenders and 10 latest corrigenda extracted. One tender and one corrigendum detail successfully fetched. |
| CPPP ePublishing | https://eprocure.gov.in/epublish/app | Additional 10 latest tenders and 10 latest corrigenda extracted. One tender and one corrigendum detail successfully fetched. |
| Defproc | https://defproc.gov.in/nicgep/app | 10 latest tenders and 10 latest corrigenda extracted. One tender and one corrigendum detail successfully fetched. |
| HSL | https://eprocurehsl.nic.in/nicgep/app | 8 latest tenders and 10 latest corrigenda extracted. Detail pages explicitly identified Hindustan Shipyard Limited. One tender and one corrigendum detail successfully fetched. |

## Evidence

The saved JSON and HTML responses are in `probe-results/`. The reproducible bounded collector is `probe_sources.py` (Python standard library only). Run `python probe_sources.py` with network access. It overwrites its own probe-result files and makes a small number of public read requests; it does not schedule recurring collection.

Sample verified IDs:

- CPPP: 2026_DCSEM_929734_1 — electrical installation upgrades in Annapurna and Dhawalgiri buildings.
- Defproc: 2026_MES_793441_1 — vehicle shed and air-conditioning repairs under GE Rangia.
- HSL: 2026_HSL_100315_1 — furniture repair rate contract.
- GeM: GEM/2026/R/750011 — ongoing reverse auction; GEM/2026/R/748877 appeared on page two.
- CPPP ePublishing: 2026_BHEL_853889_1 — clamps for ammonia leakage work at Sagardighi.

## Recommended implementation

Use a direct HTTP connector for each source, sharing a GePNIC parser between CPPP, Defproc and HSL. GeM's anonymous listing endpoint returns structured JSON, so extract its current CSRF token from the public page and retain cookies within the request session. This endpoint is used by the website but is not a verified supported integration API; monitor it for changes.

Store stable tender IDs, references, titles, dates, source URLs, first/last seen times and revision hashes. Render escaped text in the application rather than raw source HTML. GeM includes internal fields that the app should omit from its public data model. Preserve raw dates until timezone interpretation is verified against the visible UI.

Use the recent homepage notices for an initial feed, and fetch linked details and corrigenda. Keep ePublishing and eProcurement as separate source namespaces. Deduplicate only with sufficient evidence.

## Remaining gaps

- Homepage feeds are bounded recent windows: polling them alone can miss records and cannot backfill all open tenders.
- Earlier active-tender search tests showed CAPTCHAs. No CAPTCHA bypass was used in these direct tests.
- Full listing traversal/backfill, announcement pages, awards, cancellations and all document downloads remain unverified.
- GePNIC links include session-related parameters. Detail links worked in the collector session; long-term and clean-browser link durability remain unverified.
- GeM pagination worked for two pages; full traversal, status filters and corrigendum fetching are not yet tested.
- The old HSL .gov.in address failed previously. The .nic.in portal is verified working, but an official migration date was not established.
- Success from this network does not guarantee access from a future hosting provider. Test again from the deployment environment.

The earlier Firecrawl results were tool-specific failures, not evidence that these public records cannot be collected.
