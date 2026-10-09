# OpenStreetMap test fixtures

`overpass-sample.json`, `overpass-cases.json`, `overpass-seed-check.json` and
`overpass-fix4.json` are small extracts of OpenStreetMap data for the tests of
`atlas/sources/osm.py` and `atlas/dissolve.py`.

**© OpenStreetMap contributors. Available under the Open Database License 1.0
(<https://www.openstreetmap.org/copyright>).** The elements and their tags are verbatim; only the
selection is ours.

## How `overpass-sample.json` was made

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

## What `overpass-sample.json` covers

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

## `overpass-cases.json`

28 elements copied verbatim, with the `version`, `generator` and `osm3s` header, from the full US
response of the importer's own query (`OVERPASS_QUERY`, `out tags bb`) that
`https://maps.mail.ru/osm/tools/overpass/api/interpreter` returned on 2026-10-07
(`osm3s.timestamp_osm_base` 2026-10-07T22:39:49Z, 1,886 elements). Elements are sorted by type and
id. Each group is a case the dissolve and the record mapping got wrong on that snapshot:

| Case | Elements |
|---|---|
| Unnamed buildings without an operator that form one site (rule 3) | ways 1422191468–1422191477, Dickey County, ND |
| Same-operator halls that touch, centers 320 m apart (rule 2 between boxes) | Google ways 844352473 and 844352474, Douglas County, GA |
| A campus whose operator is "Amazon Web Services us-east-2 datacenter" (the operator guard) | way 460067225 and AWS buildings 673532692, 874506250, 975484796, 975484797, 975484798, Hilliard, OH |
| Same address, same name, no operator, 154 m apart (rule 3 by address) | Blockfusion ways 832656125 and 1229749890, Niagara Falls, NY |
| Unnamed buildings 104 m and 22 m apart | ways 1530966380, 1530966381, 1530966382, Taylor County, TX |
| A named building with an operator and an unnamed way on the same footprint | Apple Data Center way 300974499 and way 567575425, Mesa, AZ |
| A site that PNNL lists in two counties | Digital Realty Atlanta ATL11, way 975064000 |
| Telecom sites outside the scope of 07 §2.2 | Cable & Wireless Cable Landing Station way 459188725 (Shirley, NY); PTC way 1365213539 (a telephone cooperative, Linn County, OR) |

## `overpass-seed-check.json`

47 elements, each a case the seed check of 2026-10-08 found (`tests/sources/test_osm_seed_check.py`
and `tests/sources/test_osm.py`; the finding numbers are in the tests). Elements are sorted by type
and id; the `version`, `generator` and `osm3s` header is the seed response's.

- 46 are copied verbatim from the seed import's Overpass response, which
  `https://maps.mail.ru/osm/tools/overpass/api/interpreter` returned on 2026-10-08 for the
  importer's query of that day (the five data center clauses, `out tags bb`;
  `osm3s.timestamp_osm_base` 2026-10-08T20:47:34Z, 1,888 elements, SHA-256
  `77651f2a1a481f43c697707f4e6d2f224603d7f1cd6fa7bd554247742831ae43`).
- 1, the site polygon way 1377227157 ("Microsoft Bison Business Park Data Center",
  `industrial=data_centre`), is copied verbatim, with its `bounds` and `geometry`, from the
  response of the same endpoint on 2026-10-09 to the site statement of the current query run on
  its own (`way` and `relation` with `industrial=data_centre` or `data_center` in the US, `out
  tags geom`; `osm3s.timestamp_osm_base` 2026-10-09T02:53:01Z, 172 elements).

| Case | Elements |
|---|---|
| A site polygon and the four unnamed buildings inside it | way 1377227157; ways 1377227154, 1485867694, 1485867695, 1485867696 (Laramie County, WY) |
| A construction site (`landuse=construction`, `construction=data_center`) and the halls on it | way 1319699416 "AWS IAD-500 and IAD-501"; Amazon IAD-500 way 1426663253; ways 1521913706, 1521913707 (Herndon, VA) |
| Same-operator buildings on two sides of a state line | xAI ways 1386926534 (Shelby County, TN) and 1077131079 (DeSoto County, MS) |
| A node without an operator and two polygons of the same name | QTS Data Center - Hillsboro 3, node 11721960464, ways 1465196735 and 1465196736 |
| An unnamed way at an operator's address | node 7985753364 and way 156468497 (8100 Boone Boulevard); ways 358455179 and 1075445245 (2220 De La Cruz Boulevard) |
| A node inside another operator's building | CoreSite - LA2 node 13042311881 in the USPO Terminal Annex, way 30666790 |
| Buildings inside another operator's polygon that has none of its own | Microsoft way 897226569; Amazon ways 897226574, 897226575, 1301654223, 1301654224 (Quail Ridge Lane) |
| Objects the scope screen holds | Greenidge way 253670255; Nautilus Cryptomine ways 1334234454, 1334234455; SDSU Computer Room node 2607827436; Vital Records way 758580536; K-Motion Interactive node 10938672218; The Putney School shed way 1552427895; CenturyLink way 1013396352; City of Searcy way 1020721870 |
| Objects it keeps | Date Center West - Eugene way 967131562 (under 200 m², an operator that says data center); TierPoint Milwaukee node 9721703993 and TierPoint way 465042594 |
| A proposed lot without a name or an operator | way 1549253250 (Manchester Township, NJ) |
| An opening_date that has passed | way 1374729776 (Tuscaloosa, AL) |
| A position OSM calls approximate | PowerHouse Pacific Building 3, way 1544360250 |
| A campus whose representative has no address | QTS Manassas node 14209729093, ways 1090837713, 1134911642, 1287260558, 1464257784 |
| A node that moved after PNNL took its position | Fiberhub LAS1, node 13311012216 |
| A postal city that is not the place of the point | Flexential Atlanta - Norcross, way 392324240 |

## `overpass-fix4.json`

176 elements copied verbatim, one per line under the `version`, `generator` and `osm3s` header,
from the second seed import's Overpass response, which
`https://maps.mail.ru/osm/tools/overpass/api/interpreter` returned on 2026-10-09 for the query of
that day (`osm3s.timestamp_osm_base` 2026-10-09T09:47:46Z, 2,062 elements, SHA-256
`b0fc73d78f3494d8fd1cf56b0ef0b8911578e7e13ccc98f3b5895aac1f40c234`). Elements are sorted by type
and id; way 521300038 comes twice, as in the response (from the first statement with `out tags
bb`, and from the second with its geometry). Each group is a case the second seed check found
(`tests/sources/test_osm_fix4.py`; the finding numbers are in the tests), and every element that
`config/overrides/osm.json` names is here, so the tests show each entry's effect:

| Case | Elements |
|---|---|
| A post office's operator on the building that holds CoreSite LA2 | way 30666790, node 13042311881 |
| One building's owner on a six-building campus | Equinix DC10, DC12, DC15, DC16, DC17 ways and the DC18 node |
| Sibling names | KOMO Plaza East and West (ways 417682780, 417682781); PowerHouse Pacific Buildings 1-3 (ways 1534356804, 1534356805, 1544360250); EAT12-EAT14 (ways 1173822721-1173822723) |
| A polygon whose buildings contradict its operator, and a site OSM names two ways | Microsoft way 897226569 and AWS ways 897226574, 897226575, 1301654223, 1301654224; STACK way 1344901566, node 14189562575, AWS-tagged ways 1344901567, 1344901568 |
| Lists of house numbers and of streets | Digital Realty PDX10 way 328702750; way 1556736805 (FM56;County Road 3610A) |
| Unnamed large halls, and a development mapped hall by hall | Stream San Antonio III ways 1432637060-1432637062; Prince William ways 1509985717, 1561790221; QTS Atlanta ways 1566676867, 1566676868; the 40 Abilene halls, ways 1472056705-1472056714 and 1530966366-1530966395 |
| Names that imply an operator | the eleven Compass Data Center ways at Red Oak (850965775, 1426525212-1426525221) and Compass IAD IE (way 1387391201, operator Compass Datacenters, which makes "compass" an operator's word); Google Leesburg Building 3 way 1460776174 with Google ways 706726088, 710070925, 844374689; CyrusOne San Antonio II-IV ways 509611438, 509611439, 1121592786 |
| One name and one operator within 500 m | QTS ways 1132632705, 1315636714 (Phoenix) and 1510517635, 1510517636 (Fayette County); the 18 Google Council Bluffs ways 1073720208-1073720221, 1287260560, 1287260561, 1565614265, 1565614266 |
| A contractor's operator tag, a one-letter typo, a county line | HITT way 1194169753, Digital Realty IAD51 way 1561771300 and IAD53 way 1269259655, AWS ways 996899486, 996899487, 1227612684; CyrysOne way 1556736879 and CyrusOne ways 1556736877, 1556736878, 1556736880 |
| Containment in an outline across a county line | Microsoft Texas Research Park ways 521300038, 639176155, 639176157, 1010260675-1010260677, 1533350872; Google New Albany site way 844391357 with ways 844391358, 1222007663, 1252196811, 1252196812, 1375443893, 1386016632, 1386016633, 1427613543 |
| No acreage from an approximate or another boundary | Meta Cheyenne site way 1455907959 and ways 1485867768, 1485867769; Amazon New Albany site way 1281982572 and ways 1386016627, 1386016628 |
| An area without an operator over two operators | Digital Loudoun Plaza ways 1020953454, 1020953461 with Digital Realty ways 300969614, 300969616, 300969617, 300969619, 597970806, 597970809, 597970811, 597970813 and AWS ways 300969615, 460175672 |
| The scope screen | Compute North way 1092757323 (`resource=cryptocurrency`); Duke's lab node 10567817971; Myndshift Technologies way 836947535 (`healthcare:speciality`); Old Main way 42000820 (`amenity=university`) |
| The committed overrides | node 10780794741 (630 3rd Street); way 293211687 with Sabey's Intergate ways 205724881, 205724885, 293211683, 293211686, 337126739, 337126741, 337126742; ways 439365340, 460212563, 635022480 |

## `overrides-empty.json`

An empty overrides file (`{}`): the tests that run the importer on the other fixtures pass it with
`--overrides`, because the committed `config/overrides/osm.json` names elements those fixtures do
not have, and an unknown ref stops the import.
