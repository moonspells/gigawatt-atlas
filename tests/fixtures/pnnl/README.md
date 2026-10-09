# PNNL IM3 test fixtures

Small inputs for the tests of `atlas/sources/pnnl.py` (the PNNL cross-check of the OSM seed).

**Source: Pacific Northwest National Laboratory (IM3), *IM3 Open Source Data Center Atlas*,
v2026.02.09, MSD-LIVE, <https://doi.org/10.57931/3017294>. Available under the Open Database
License 1.0.** The atlas is derived from OpenStreetMap: © OpenStreetMap contributors, ODbL 1.0.
Web map: <https://im3.pnnl.gov/datacenter-atlas> (mirror:
<https://immm-sfa.github.io/datacenter-atlas/>).

The GeoJSON features below come from the web map's file `im3_datacenter_centroids.geojson`, which
the GitHub repository `immm-sfa/datacenter-atlas` serves. Its version: the file with SHA-256
`2e7bd7e650fe86fe0d156b4e483ebd331cfa98a0468ce33b932f8c1b6c3245df` was committed on 2026-02-12
(commit 74ab37d, "Updated existing dc db, citation, doi link and last update date"), the commit
that set the map's own citation to v2026.02.09 (DOI 10.57931/3017294) and its legend to "Last
Updated Feb 09, 2026". The repository README still links the v1 record (MSD-LIVE 65g71-a4731) for
the existing data centers; that link predates the update. The file's Last-Modified of 2026-03-31
is a later site deploy that changed only the projected layers. `atlas/sources/pnnl.py` records
this file in `WEBMAP_VERSIONS`.

That repository carries this license, reproduced as it requires:

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

MSD-LIVE gives the dataset as ODbL 1.0; whether the BSD license also covers the data file the
repository serves is an open owner question, so both notices are kept.

## `centroids-sample.geojson`

17 of the 1,382 features of the public web-map file
<https://immm-sfa.github.io/datacenter-atlas/im3_datacenter_centroids.geojson> (retrieved
2026-10-07; 373,790 bytes; Last-Modified 2026-03-31; SHA-256
`2e7bd7e650fe86fe0d156b4e483ebd331cfa98a0468ce33b932f8c1b6c3245df`). Features and properties are
verbatim, under the file's own `type`, `name` and `crs` header. They are the original file's
features 318, 322, 323, 330, 331, 332, 459, 489, 497, 498, 573, 592, 593, 731, 1042, 1043 and 258
(0-based), in that order.

- The first 16 lie in the bounding box of `tests/fixtures/osm/overpass-sample.json` and each
  matches one OSM building there (the unnamed feature 1043 is Cologix ASH1's footprint).
- The last one, "Equinix Ashburn DC2" (OSM way 234722029), lies just outside that box, so it
  matches nothing in the OSM sample and becomes an `unmatched` review item. The sample's match rate
  is 16/17 = 94.1%, below the 95% threshold, which exercises the warning.

## `centroids-cases.geojson`

13 features of the same web-map file (same retrieval, same SHA-256), verbatim under its header:
the original file's features 344, 458, 577, 640, 806, 855, 856, 858, 859, 860, 1206, 1337 and 1363
(0-based). They are every feature that the PNNL join matches to `tests/fixtures/osm/overpass-cases.json`:

- 344 and 577: two building rows on the Apple Data Center (way 300974499) in Mesa, AZ, one named
  and one unnamed (two footprints of one building; 1,338,261 and 1,263,277 sq ft);
- 855 and 856: Digital Realty Atlanta ATL11 twice, once in Douglas County and once in Cobb County;
- 1337 and 1363: `campus` rows whose campus polygons OpenStreetMap no longer has, so their points
  land on buildings ("Google Datacenter - Douglas County" and "Applied Digital / Ellendale Data
  Center");
- 640, 806, 858, 859 and 860: the AWS buildings in Hilliard, OH;
- 458 and 1206: the cable landing station and the telephone cooperative.

## `msdlive-sample.csv`

**Constructed for tests; it is not a PNNL file.** The MSD-LIVE GPKG and CSV files need an MSD-LIVE
sign-in, so the importer never downloads them. This CSV only reproduces their column layout (`id,
state, state_abb, state_id, county, county_id, ref, operator, name, sqft, lat, lon, type`) so that
the id join can be tested:

- one row per feature of `centroids-sample.geojson`, in the same order, with its `state_abb`,
  `county`, `operator`, `name`, `sqft` and `type`, and `lat`/`lon` rounded to 7 decimals;
- `id` is the OSM way id that the spatial join assigns to the feature (way 234722029 for Equinix
  Ashburn DC2, from the 2026-10-07 Overpass snapshot);
- `state` "Virginia", `state_id` "51" and `county_id` "51107" (Loudoun County) for every row;
- `ref` is the OSM `ref` tag of that way, where it has one.

## `centroids-seed-check.geojson`

6 features of the same web-map file (same SHA-256 as above, as the seed import of 2026-10-08 read
it), verbatim under its header: the original file's features 91, 191, 971, 1000, 1153 and 1326
(0-based). They go with `tests/fixtures/osm/overpass-seed-check.json`:

- 971, 1000, 1153 and 1326: the four QTS Manassas building rows, among them "QTS Manassas DC1"
  (128,120 sq ft), whose point lies on the polygon OSM now calls DC2;
- 91: Fiberhub LAS1, a point row at the old position of OSM node 13311012216;
- 191: "Verizon Wireline Network Building", a building row that no data center element of the
  fixture takes (an `unmatched` item).
