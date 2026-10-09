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

Used for: county mapping, floor area, and a cross-check of the OpenStreetMap seed.

The seed reads the dataset's public web-map file,
<https://immm-sfa.github.io/datacenter-atlas/im3_datacenter_centroids.geojson> (the v2026.02.09
export, SHA-256 2e7bd7e650fe86fe0d156b4e483ebd331cfa98a0468ce33b932f8c1b6c3245df; records cite any
other version of the file as the file itself). The repository that serves it,
immm-sfa/datacenter-atlas, carries this license:

> IM3 Open Source Data Center Atlas
>
> Copyright (c) 2025, Battelle Memorial Institute
>
> Open source under license BSD 2-Clause
>
> 1. Battelle Memorial Institute (hereinafter Battelle) hereby grants permission to any person or
> entity lawfully obtaining a copy of this software and associated documentation files
> (hereinafter “the Software”) to redistribute and use the Software in source and binary forms,
> with or without modification. Such person or entity may use, copy, modify, merge, publish,
> distribute, sublicense, and/or sell copies of the Software, and may permit others to do so,
> subject to the following conditions:
>    - Redistributions of source code must retain the above copyright notice, this list of
>      conditions and the following disclaimers.
>    - Redistributions in binary form must reproduce the above copyright notice, this list of
>      conditions and the following disclaimer in the documentation and/or other materials
>      provided with the distribution.
>    - Other than as used herein, neither the name Battelle Memorial Institute or Battelle may be
>      used in any form whatsoever without the express written consent of Battelle.
>
> 2. THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR
> IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND
> FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL BATTELLE OR CONTRIBUTORS BE
> LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
> (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
> OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
> CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF
> THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

MSD-LIVE gives the dataset as ODbL 1.0; both notices are kept until PNNL confirms which covers the
web-map file.

### Epoch AI

Epoch AI, "AI data centers". Published online at epoch.ai. Retrieved from
<https://epoch.ai/data/ai-data-centers>. Creative Commons Attribution 4.0 International
(CC BY 4.0), <https://creativecommons.org/licenses/by/4.0/>.

Used for: frontier AI sites, dated construction timelines, IT and facility power, water use.

Changes made (the data is adapted, not copied): US sites only; mapped to the Atlas record schema
and status crosswalk; timeline rows kept only where the status changes, with rows dated after the
import kept as planned events; a timeline that covers only buildings added to an older facility
kept as a phase of it, and sites at one street address combined into one campus; addresses
geocoded with the Census Geocoder and Gazetteer, or placed from a cited source where Epoch gives
none or only a postal city; party names split, Epoch's confidence tags removed and the owner of the
AI hardware (Epoch's `Owner` column, "not necessarily the owner or operator of the facility")
listed as a tenant, never as the facility's owner or operator; status notes shortened. Details:
<https://github.com/moonspells/gigawatt-atlas/blob/main/docs/sources/epoch-aigridwatch.md>.

### AI GridWatch

AI GridWatch (<https://aigridwatch.com>), AI GridWatch data center project tracker. Creative
Commons Attribution 4.0 International (CC BY 4.0), <https://creativecommons.org/licenses/by/4.0/>.

Used for: proposed and contested projects, hearing and decision dates, filing entities.

Changes made (the data is adapted, not copied): verified rows only, and rows that are Epoch AI sites
are left to the Epoch record; stages and milestone dates mapped to Atlas statuses and events (a
decision counts as an approval only when the row names a land-use approval or a permit, and a date
on January 1 is read as its year), a stage the milestones do not reach recorded as seen on the row's
as_of date or the file's date, and a first report taken from the row's event log only where it can
be dated; rows without coordinates placed at their Census place, town or county, and a city or
municipality named only where the row's point lies in it; the "Operator/developer" field listed as
the developer, never as the operator, and an electric utility named there left out; organizations
only, with personal names left out; capacity and acreage outside plausible ranges left out. Details:
<https://github.com/moonspells/gigawatt-atlas/blob/main/docs/sources/epoch-aigridwatch.md>.

### U.S. Census Bureau

U.S. Census Bureau, 2025 Cartographic Boundary Files (`cb_2025_us_county_500k` for county checks,
`cb_2025_us_county_5m` for map display, `cb_2025_us_place_500k` for the place a point lies in),
2025 Gazetteer Files (places, counties and county subdivisions), and the Census Geocoder. Public
domain. The place polygons and the county subdivisions are downloaded from census.gov on first use
rather than committed. Details and checksums:
<https://github.com/moonspells/gigawatt-atlas/blob/main/reference/README.md>.

Used for: county point-in-polygon checks, the city a record names, county, place and town
centroids, address geocoding.

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
