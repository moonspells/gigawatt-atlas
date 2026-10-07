# OpenStreetMap test fixture

`overpass-sample.json` is a small extract of OpenStreetMap data for the tests of
`atlas/sources/osm.py` and `atlas/dissolve.py`.

**© OpenStreetMap contributors. Available under the Open Database License 1.0
(<https://www.openstreetmap.org/copyright>).** The elements and their tags are verbatim; only the
selection is ours.

## How it was made

Retrieved on 2026-10-07 from the Overpass API instance at
`https://maps.mail.ru/osm/tools/overpass/api/interpreter` (the instance that answered from the
build sandbox), with the same query shape as the importer (`OVERPASS_QUERY`: the five data center
tag clauses and `out tags bb`), restricted to a small bounding box in Ashburn, Virginia:

```overpassql
[out:json][timeout:180];
(
  nwr["telecom"="data_center"](39.018,-77.462,39.030,-77.448);
  nwr["building"="data_center"](39.018,-77.462,39.030,-77.448);
  nwr["construction:telecom"="data_center"](39.018,-77.462,39.030,-77.448);
  nwr["proposed:telecom"="data_center"](39.018,-77.462,39.030,-77.448);
  nwr["construction"="data_center"](39.018,-77.462,39.030,-77.448);
);
out tags bb;
```

That returned 27 elements (`osm3s.timestamp_osm_base` 2026-10-07T22:19:21Z). Three nodes from
elsewhere were appended from a second query by id (`node(10014176940); node(11721960464);
node(13154826379); out tags bb;`, osm_base 2026-10-07T22:20:24Z). The `version`, `generator` and
`osm3s` header is the one from the first query. Elements are sorted by type and id.

## What it covers

| Case | Elements |
|---|---|
| Campus polygons (`telecom=data_center`, no `building` tag) | way 460053028 "Amazon Web Services Datacenter Complex", way 460053030 "Amazon Web Services" |
| Buildings inside a campus bounding box | Amazon IAD-78, IAD-79, IAD-80 (in 460053028); IAD-50, IAD-60, IAD-71 (in 460053030) |
| Other operators' objects inside a campus bounding box | Equinix DC17 (way 1543931283) and DC18 (node 14156477686) in 460053028's box |
| Same-operator buildings within 300 m | Equinix DC10, DC12, DC15, DC16, DC17; Centersquare IAD4-A and IAD4-B; Digital Realty ACC2–ACC5 |
| A node | Equinix Ashburn DC18 (node 14156477686) |
| `telecom=data_center` + `building=construction` + `construction=data_center` | Cologix ASH2 (way 1560827027), next to the operating Cologix ASH1 |
| `it_power` and `input:electricity` | the Amazon and Digital Realty buildings, NTT VA1 and VA2 |
| `start_date` | Amazon IAD-50, IAD-60, IAD-71, IAD-78, IAD-79, IAD-80; Equinix DC12 |
| Outside the 50 states and DC | node 10014176940, Microsoft, Guaynabo, Puerto Rico |
| `proposed:telecom=data_center` | node 11721960464, QTS Data Center - Hillsboro 3, Oregon |
| `construction:telecom=data_center` with `it_power` | node 13154826379, NTT VA11 (96 MW), Gainesville, Virginia |
