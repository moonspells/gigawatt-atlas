# Attribution

The Gigawatt Atlas database (ODbL 1.0, see [DATA-LICENSE.md](DATA-LICENSE.md)) is built from the
sources below. Each record also lists its own sources in `sources[]`, with publisher, link and
license.

Copy-ready line for a release (the release id is in its `manifest.json`):

> Gigawatt Atlas, moonspells.dev/atlas, release {release}, ODbL 1.0; contains information from
> OpenStreetMap contributors (ODbL), PNNL IM3 (ODbL), Epoch AI (CC BY 4.0), AI GridWatch (CC BY 4.0)

## Datasets

### OpenStreetMap

© OpenStreetMap contributors. Available under the Open Database License 1.0.
<https://www.openstreetmap.org/copyright>

Used for: operating campuses and buildings (`telecom`, `building`, `construction:telecom` and
`proposed:telecom` = `data_center`), footprints and operator names.

### PNNL IM3 Open Source Data Center Atlas

Mongird, K., Vernon, C., Burleyson, C., Akdemir, K. Z. and Rice, J. (2026). *IM3 Open Source Data
Center Atlas* (v2026.02.09) [Data set]. MSD-LIVE. <https://doi.org/10.57931/3017294>. Open
Database License 1.0. Creators as listed on the MSD-LIVE record.

Used for: county mapping, floor area, and a cross-check of the OpenStreetMap seed.

### Epoch AI

Epoch AI, "AI data centers". Published online at epoch.ai. Retrieved from
<https://epoch.ai/data/ai-data-centers>. Creative Commons Attribution 4.0 International
(CC BY 4.0).

Used for: frontier AI sites, dated construction timelines, IT and facility power, water use.

### AI GridWatch

AI GridWatch (<https://aigridwatch.com>), AI GridWatch data center project tracker. Creative
Commons Attribution 4.0 International (CC BY 4.0).

Used for: proposed and contested projects, hearing and decision dates, filing entities.

### U.S. Census Bureau

U.S. Census Bureau, 2025 Cartographic Boundary Files (`cb_2025_us_county_5m`), 2025 Gazetteer
Files, and the Census Geocoder. Public domain. Details and checksums in
[reference/README.md](reference/README.md).

Used for: county point-in-polygon checks, county and place centroids, address geocoding.

## Basemap (a Produced Work)

Protomaps basemap, build 20261006: © OpenStreetMap contributors, Protomaps. Map tiles are a
Produced Work and carry the attribution "© OpenStreetMap contributors · Protomaps · Gigawatt Atlas
(ODbL)".

## Later sources

Further sources are added here when the pipeline first uses them: LBNL interconnection queue data,
WRI Aqueduct 4.0, the US Drought Monitor, EIA, ORNL/HIFLD and EPA.
