# Attribution

The Gigawatt Atlas database is published under the Open Database License (ODbL) 1.0,
<https://opendatacommons.org/licenses/odbl/1-0/>; the full text is in
[LICENSE-ODbL-1.0.txt](LICENSE-ODbL-1.0.txt). It is built from the sources below. Each record also
lists its own sources in `sources[]`, with publisher, link and license. What the licenses mean in
plain words:
[DATA-LICENSE.md](https://github.com/moonspells/gigawatt-atlas/blob/main/DATA-LICENSE.md).

Every release ships a copy of this file next to `LICENSE-ODbL-1.0.txt`, so the links here go to
that file or to absolute URLs.

Copy-ready line for a release (the release id is in its `manifest.json`):

> Gigawatt Atlas, moonspells.dev/atlas, release {release}, ODbL 1.0
> (https://opendatacommons.org/licenses/odbl/1-0/); contains information from OpenStreetMap
> contributors (ODbL), PNNL IM3 (ODbL), and data adapted from Epoch AI and AI GridWatch
> (CC BY 4.0, https://creativecommons.org/licenses/by/4.0/)

## Datasets

### OpenStreetMap

© OpenStreetMap contributors. Available under the Open Database License 1.0.
<https://www.openstreetmap.org/copyright>

Used for: operating, under-construction and proposed campuses and buildings (`telecom`,
`building`, `construction`, `construction:telecom` and `proposed:telecom` = `data_center`), their
locations and bounding boxes, names, operators, start dates and power tags.

### PNNL IM3 Open Source Data Center Atlas

Mongird, K., Vernon, C., Burleyson, C., Akdemir, K. Z. and Rice, J. (2026). *IM3 Open Source Data
Center Atlas* (v2026.02.09) [Data set]. MSD-LIVE. <https://doi.org/10.57931/3017294>. Open
Database License 1.0, <https://opendatacommons.org/licenses/odbl/1-0/>. Creators as listed on the
MSD-LIVE record.

The pipeline reads the public web-map file `im3_datacenter_centroids.geojson` from
<https://immm-sfa.github.io/datacenter-atlas/>. The repository that serves it,
`immm-sfa/datacenter-atlas`, is "Copyright (c) 2025, Battelle Memorial Institute", open source
under the BSD 2-Clause license; MSD-LIVE gives the dataset as ODbL 1.0, so both notices are kept.

Used for: county mapping, floor area, and a cross-check of the OpenStreetMap seed.

### Epoch AI

Epoch AI, "AI data centers". Published online at epoch.ai. Retrieved from
<https://epoch.ai/data/ai-data-centers>. Creative Commons Attribution 4.0 International
(CC BY 4.0), <https://creativecommons.org/licenses/by/4.0/>.

Used for: frontier AI sites, dated construction timelines, IT and facility power, water use.

Changes made (the data is adapted, not copied): US sites only; mapped to the Atlas record schema
and status crosswalk; timeline rows kept only where the status changes, with rows dated after the
import kept as planned events; addresses geocoded with the Census Geocoder and Gazetteer, or placed
from a cited source where Epoch gives none; party names split and Epoch's confidence tags removed;
status notes shortened. Details:
<https://github.com/moonspells/gigawatt-atlas/blob/main/docs/sources/epoch-aigridwatch.md>.

### AI GridWatch

AI GridWatch (<https://aigridwatch.com>), AI GridWatch data center project tracker. Creative
Commons Attribution 4.0 International (CC BY 4.0), <https://creativecommons.org/licenses/by/4.0/>.

Used for: proposed and contested projects, hearing and decision dates, filing entities.

Changes made (the data is adapted, not copied): verified rows only, and rows that are Epoch AI
sites are left to the Epoch record; stages and milestone dates mapped to Atlas statuses and events;
rows without coordinates placed at their Census place or county; organizations only, with personal
names left out; capacity and acreage outside plausible ranges left out. Details:
<https://github.com/moonspells/gigawatt-atlas/blob/main/docs/sources/epoch-aigridwatch.md>.

### U.S. Census Bureau

U.S. Census Bureau, 2025 Cartographic Boundary Files (`cb_2025_us_county_500k` for county checks,
`cb_2025_us_county_5m` for map display), 2025 Gazetteer Files, and the Census Geocoder. Public
domain. Details and checksums:
<https://github.com/moonspells/gigawatt-atlas/blob/main/reference/README.md>.

Used for: county point-in-polygon checks, county and place centroids, address geocoding.

## Basemap (a Produced Work)

Protomaps basemap, build 20261006: © OpenStreetMap contributors, Protomaps. The basemap tiles are a
Produced Work of OpenStreetMap and carry "© OpenStreetMap contributors · Protomaps". A map that
shows Atlas data on them is a Produced Work of the Atlas too, and credits every source in its
corner: "© OpenStreetMap contributors · Protomaps · Gigawatt Atlas (ODbL) · PNNL IM3 · Epoch AI and
AI GridWatch (CC BY 4.0)", with a link to this file or to moonspells.dev/atlas/about#attribution.
`facilities.pmtiles` carries the same sources in its own attribution.

## Later sources

Further sources are added here when the pipeline first uses them: LBNL interconnection queue data,
WRI Aqueduct 4.0, the US Drought Monitor, EIA, ORNL/HIFLD and EPA.
