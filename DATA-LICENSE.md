# Data license

The Gigawatt Atlas database is published under the
[Open Database License (ODbL) 1.0](LICENSE-ODbL-1.0.txt)
(<https://opendatacommons.org/licenses/odbl/1-0/>). This covers the records in `data/` and every
published release: the GeoParquet, GeoJSON, CSV, PMTiles and JSON files under
`tiles.moonspells.dev/v/`, and the per-record copies under `tiles.moonspells.dev/rec/`. The full
license text is in `LICENSE-ODbL-1.0.txt`; this page is a plain-words summary and does not replace
it.

## Why ODbL

Records derived from OpenStreetMap share one "data center" layer with records from other sources,
which makes the whole layer a derivative of OpenStreetMap's database. OpenStreetMap and the PNNL
IM3 Data Center Atlas are both ODbL, so the Atlas is too.

## What you may do

Use, copy, analyze, and build on the data, commercially or not, as long as you:

1. **Credit the Atlas and its sources.** Use the attribution line that ships with each release
   (for example "Gigawatt Atlas, moonspells.dev/atlas, release 20261020-0613, ODbL 1.0
   (https://opendatacommons.org/licenses/odbl/1-0/); contains information from OpenStreetMap
   contributors (ODbL), PNNL IM3 (ODbL), and data adapted from Epoch AI and AI GridWatch (CC BY
   4.0, https://creativecommons.org/licenses/by/4.0/)"). [ATTRIBUTION.md](ATTRIBUTION.md) lists
   every source, its license and what was changed.
2. **Share alike.** If you publicly use or distribute a database derived from the Atlas, offer
   that derivative database under the ODbL as well.
3. **Keep it open.** Do not add technical restrictions (such as DRM) to copies you distribute
   without also offering an unrestricted copy.

## Produced Works

The rendered map, map images, charts and screenshots made from the data are Produced Works. The
`facilities.pmtiles` download is not: it holds the records' values, so it is part of the database,
like the other release files.

A Produced Work made from the Atlas as published needs attribution only, for example
"© OpenStreetMap contributors · Protomaps · Gigawatt Atlas (ODbL) · PNNL IM3 · Epoch AI and AI
GridWatch (CC BY 4.0)" with a link to [ATTRIBUTION.md](ATTRIBUTION.md). If you make it from a
changed version of the database (records added, removed or edited), ODbL 1.0 section 4.6 asks you
to offer that changed database, or a file of the changes, too.

## Inputs under other licenses

Epoch AI and AI GridWatch data are CC BY 4.0 (<https://creativecommons.org/licenses/by/4.0/>). The
Atlas adapts them (it maps, filters, geocodes and shortens them, as ATTRIBUTION.md says) and credits
them in [ATTRIBUTION.md](ATTRIBUTION.md) and in each record's `sources[]` (publisher, link and
license). Keep those credits when you share the data or a Produced Work. News and company sources
are cited with a link and a quote of at most 300 characters; their text is not redistributed.

## Code

The code in this repository is under the [MIT License](LICENSE).

## Corrections and takedowns

- **A wrong value:** open an issue in
  [moonspells/gigawatt-atlas](https://github.com/moonspells/gigawatt-atlas/issues) with the record
  id, the field, the value you propose and a link to your evidence. Public issues are for values
  only: do not put personal data in them.
- **A takedown (personal data, legal notices):** never in a public issue. Email
  **hello@moonspells.dev** with "Atlas takedown" in the subject and the record id or URL; say what
  should come down and why, without repeating more personal data than needed to find it. The
  `atlas@moonspells.dev` address (once the owner confirms it) and the private
  moonspells.dev/contact form (once it ships) take over this route.

Corrections are triaged within 7 days and takedowns within 72 hours. Each accepted correction or
takedown lands as a reviewed pull request; a takedown then gets a hotfix release, and the record's
copies in older releases are deleted (docs/publishing.md).
