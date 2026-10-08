# Census Geocoder fixture

Recorded responses of the U.S. Census Bureau Geocoder (`geographies/onelineaddress`, benchmark
`Public_AR_Current`, vintage `Current_Current`, layers `Counties`), retrieved 2026-10-07 and
2026-10-08 (UTC). Census Geocoder output is a work of the U.S. Government and in the public domain.

Each file is named like the importer's cache, `{sha256 of the address}.json`, so tests can serve
it from an `httpx.MockTransport` or copy it into `.cache/atlas/census/`.

| File | Address | Matches |
|---|---|---|
| `c11680b9d9d1ee8dcdbb7fa1952092f259df7e65db638a27a9ab455551b360fb.json` | 5420 Tulane Rd, Memphis, TN 38109 | 1 (the same house number: `address` precision) |
| `eda0d71322c20c45732a5f559f7f55e5a00195dc781bf6a15a9cc17a61561538.json` | Holly Ridge, LA 71269 | 0 |
| `56737b6c62125842c344115ce8abc4657d864709c4ce365e440cbca1c179e1a9.json` | 1772-2396 145th St, Rosemount, MN 55068 | 2: 145TH ST E and 145TH ST W, 4.6 km apart (ambiguous) |
| `a234c0b064939decbd3ae8b23481d0a4f77d10eaaeb06023d36913f59ecc5695.json` | Co Rd 42, Montgomery, AL 36105 | 1: 42 COUNTY CT (another street) |
| `e27464d9d02ea28b86269cbda1a639d09823084c025f5d6855bb3eaa141be9a0.json` | 2950 S. Litchfield Road | 1: 2950 LITCHFIELD RD BYP, GOODYEAR, AZ (another street) |
| `96e39e35439587451d8d6424649a699342e3f2cf4015d72a8189df143ebddb6e.json` | 55001 Larrison Blvd, New Carlisle, IN 46552 | 1: 55001 LARRISON DR (another street type) |
| `508ca07a30136477b70eb6431ae529072a657b21f8c2d758e4c30f0f517e0a5a.json` | 984 County Road 112, Afton, TX 79220 | 1: 984 CO RD 112 |
| `fae3c952d3574a65b8c316850bb4c651e5397a86c79345e2a0e6f1cb4c9da35d.json` | 500 8th St, Jeffersonville, IN 47130 | 1: 500 E 8TH ST (a directional the input leaves out) |
| `0b03e93d2415ef9bd0bb69f393fbb0e8c1e51f04002f781852dd2a174c60e3dd.json` | 1435 Hwy 54 W, Fayetteville, GA 30214 | 1: 1435 STATE RTE 54 |
| `f88cb6f6a9abd0a89d6c39ad56eb6d2d3ac797afe74e724ebc2072acb7e20ce2.json` | 1626 County Line Road Ridgeland, Mississippi | 1: 1626 E COUNTY LINE RD, RIDGELAND (the city is inside the street part) |

The responses are unchanged.
