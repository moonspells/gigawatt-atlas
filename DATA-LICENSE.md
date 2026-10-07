# Data license

The Gigawatt Atlas database is published under the
[Open Database License (ODbL) 1.0](LICENSE-ODbL-1.0.txt). This covers the records in `data/`
and every published release (the GeoParquet, GeoJSON, CSV, PMTiles and JSON files under
`tiles.moonspells.dev/v/`). The full license text is in `LICENSE-ODbL-1.0.txt`; this page is a
plain-words summary and does not replace it.

## Why ODbL

Records derived from OpenStreetMap share one "data center" layer with records from other sources,
which makes the whole layer a derivative of OpenStreetMap's database. OpenStreetMap and the PNNL
IM3 Data Center Atlas are both ODbL, so the Atlas is too.

## What you may do

Use, copy, analyze, and build on the data, commercially or not, as long as you:

1. **Credit the Atlas and its sources.** Use the attribution line that ships with each release
   (for example "Gigawatt Atlas, moonspells.dev/atlas, release 20261020-0613, ODbL 1.0; contains
   information from OpenStreetMap contributors (ODbL), PNNL IM3 (ODbL), Epoch AI (CC BY 4.0),
   AI GridWatch (CC BY 4.0)"). [ATTRIBUTION.md](ATTRIBUTION.md) lists every source.
2. **Share alike.** If you publicly use or distribute a database derived from the Atlas, offer
   that derivative database under the ODbL as well.
3. **Keep it open.** Do not add technical restrictions (such as DRM) to copies you distribute
   without also offering an unrestricted copy.

## Produced Works

Map tiles, the rendered map, charts and screenshots made from the data are Produced Works. They
need attribution only (for example "© OpenStreetMap contributors · Protomaps · Gigawatt Atlas
(ODbL)"); the share-alike rule does not apply to them.

## Inputs under other licenses

Epoch AI and AI GridWatch data are CC BY 4.0. They are credited in [ATTRIBUTION.md](ATTRIBUTION.md)
and in each record's `sources[]` (publisher, link and license). News and company sources are cited
with a link and a quote of at most 300 characters; their text is not redistributed.

## Code

The code in this repository is under the [MIT License](LICENSE).

## Corrections and takedowns

To report a wrong value or ask for a takedown (personal data, legal notices), open an issue in
[moonspells/gigawatt-atlas](https://github.com/moonspells/gigawatt-atlas/issues). A private route
through the [moonspells.dev/contact](https://moonspells.dev/contact) form follows when it ships.
Corrections are triaged within 7 days and takedowns within 72 hours, and each accepted correction
lands as a reviewed pull request.
