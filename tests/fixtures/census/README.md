# Census Geocoder fixture

Recorded responses of the U.S. Census Bureau Geocoder (`geographies/onelineaddress`, benchmark
`Public_AR_Current`, vintage `Current_Current`, layers `Counties`), retrieved 2026-10-07. Census
Geocoder output is a work of the U.S. Government and in the public domain.

Each file is named like the importer's cache, `{sha256 of the address}.json`, so tests can serve
it from an `httpx.MockTransport` or copy it into `.cache/atlas/census/`.

| File | Address | Matches |
|---|---|---|
| `c11680b9d9d1ee8dcdbb7fa1952092f259df7e65db638a27a9ab455551b360fb.json` | 5420 Tulane Rd, Memphis, TN 38109 | 1 (the same house number: `address` precision) |
| `eda0d71322c20c45732a5f559f7f55e5a00195dc781bf6a15a9cc17a61561538.json` | Holly Ridge, LA 71269 | 0 |
| `56737b6c62125842c344115ce8abc4657d864709c4ce365e440cbca1c179e1a9.json` | 1772-2396 145th St, Rosemount, MN 55068 | 2 (a different house number: `street` precision) |

The responses are unchanged.
