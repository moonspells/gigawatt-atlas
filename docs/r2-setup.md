# R2 and `tiles.moonspells.dev`: owner settings (setup step 15)

Every step here is an **owner** step in the Cloudflare dashboard, with `wrangler` on the owner's
own machine, or in the GitHub repo settings. Run the `wrangler` commands from the root of the
site clone (`~/moonspells.dev`, after `pnpm exec wrangler login`): `pnpm exec` uses the wrangler
version the site's lockfile pins, where `npx` would download the newest release with install
scripts on. Replace every `<placeholder>` before running a line: the shell reads a bare `<` as
"read from this file" and the command never starts. No Cloudflare API token goes
into GitHub or into a Claude session; the pipeline only ever holds bucket-scoped R2 tokens.
Source of truth: the site plan, 07 §11.2, 10 §3.6 and §11.8, 13 §2.4 step 15 and §5.3. Release
layout and workflows: [publishing.md](publishing.md).

Needs: the `moonspells.dev` zone on Cloudflare (step 3) and the public repo
`moonspells/gigawatt-atlas` with the Environment `production` limited to `main` (step 14).
Labels marked *(check)* were not visible to Claude; use the closest one in the dashboard and
note the real label here.

## 1. Buckets

```sh
pnpm exec wrangler r2 bucket create atlas-tiles   # public through the custom domain only
pnpm exec wrangler r2 bucket create atlas-raw     # private: raw upstream copies (M2)
pnpm exec wrangler r2 bucket list                 # check: both listed
```

## 2. Custom domain for `atlas-tiles`

Use Wrangler: the dashboard's **Connect Domain** flow has no minimum-TLS field, and
`domain add` without `--min-tls` leaves the host at TLS 1.0 (Wrangler's default). 10 §1 and §3.2
require TLS 1.2 or later.

```sh
pnpm exec wrangler r2 bucket domain add atlas-tiles --domain tiles.moonspells.dev \
  --zone-id <moonspells.dev zone id> --min-tls 1.2
pnpm exec wrangler r2 bucket domain list atlas-tiles   # check: tiles.moonspells.dev, active, minimum TLS 1.2
```

If the domain was already connected (in the dashboard, or without `--min-tls`), raise it:

```sh
pnpm exec wrangler r2 bucket domain update atlas-tiles --domain tiles.moonspells.dev --min-tls 1.2
```

`atlas-raw` gets no domain.

## 3. `r2.dev` off on both buckets

```sh
pnpm exec wrangler r2 bucket dev-url disable atlas-tiles
pnpm exec wrangler r2 bucket dev-url disable atlas-raw
```

Check: **R2 → each bucket → Settings → Public Development URL** reads disabled. `r2.dev` URLs
are rate-limited and uncached, and `atlas-raw` must have no public URL at all.

## 4. Cache Rule

**Rules → Cache Rules → Create rule** on the `moonspells.dev` zone:

| Field | Value |
|---|---|
| Rule name | `tiles-cache` |
| When incoming requests match | custom filter expression: `(http.host eq "tiles.moonspells.dev")` |
| Cache eligibility | **Eligible for cache** |
| Edge TTL | **Use cache-control header if present, use default Cloudflare caching behavior if not** (API mode `respect_origin`) |
| Origin Range Requests | **On** (API `origin_range_requests: {"mode": "on"}`): Cloudflare fetches from R2 in cache-aligned ranges instead of whole files |
| Everything else | default |

Cloudflare does not cache `.pmtiles`, `.json` or `.parquet` without this rule. Objects carry
their own Cache-Control (immutable for `v/`, `rec/`, `basemap/`; 60 s for `atlas/latest.json`),
set at upload by `atlas publish`.

## 5. Smart Tiered Cache

**Caching → Tiered Cache → Smart Tiered Caching Topology**: on (free).

## 6. CORS on `atlas-tiles`

The file is [`r2/cors.json`](../r2/cors.json) in this repo (Wrangler format): origins
`https://moonspells.dev` and `http://localhost:4321`, methods `GET` and `HEAD`, request headers
`range` and `if-match`, exposed `etag`, `content-length` and `content-range`, max age 3000 s.
The command runs from the site clone like the others, so `--file` names the file in the
`gigawatt-atlas` clone.

```sh
pnpm exec wrangler r2 bucket cors set atlas-tiles --file <gigawatt-atlas clone>/r2/cors.json
pnpm exec wrangler r2 bucket cors list atlas-tiles      # check: one rule as above
```

Then purge, because cached objects keep their old CORS headers: **Caching → Configuration →
Purge Cache → Custom Purge → Hostname → `tiles.moonspells.dev` → Purge** (available on Free).
Purge the same way after every later CORS change.

## 7. Lifecycle: delete old releases

```sh
pnpm exec wrangler r2 bucket lifecycle add atlas-tiles delete-old-releases v/ --expire-days 120
pnpm exec wrangler r2 bucket lifecycle list atlas-tiles   # check: delete-old-releases, prefix v/, 120 days
```

Or **R2 → atlas-tiles → Settings → Object lifecycle rules → Add rule**: prefix `v/`, delete
objects 120 days after upload. `basemap/`, `rec/` and `archive/` are not affected (`archive/`
gets a bucket lock when the monthly archives start). A takedown deletes from `v/` and `rec/` at
once, without waiting for this rule (publishing.md §7), with the `atlas-tiles` token from step 9
*(check: Cloudflare's token page says **Object Read & Write** reads, writes and lists objects but
does not name delete; if the first delete of a takedown answers AccessDenied, the job stops
before deleting anything)*.

Age counts from each object's last upload, so the fixture release `v/20000101-0000/` also
expires 120 days after it was last uploaded. While site CI pins it, renew it before then (a
calendar reminder every 100 days): dispatch `publish.yml` from `main` with `fixture` ticked. Its
upload runs `atlas publish upload --renew`, which puts every object already stored with the same
SHA-256 again, byte for byte, so its age starts over; the last line of the **Upload to R2** step
counts them as `renewed`. A plain upload skips those objects and leaves their age as it was, and a changed
fixture still fails (publishing.md §6). The release the site pins must likewise stay younger
than 120 days, which every merged site data PR renews.

## 8. Response headers: Transform Rule `tiles-hardening`

**Rules → Transform Rules → Modify Response Header → Create rule** (10 §3.6):

| Field | Value |
|---|---|
| Rule name | `tiles-hardening` |
| When incoming requests match | `(http.host eq "tiles.moonspells.dev")` |
| Set static | `Content-Security-Policy` = `default-src 'none'; sandbox` |
| Set static | `X-Content-Type-Options` = `nosniff` |

Any HTML or SVG that ever lands in the bucket is inert when opened directly. MapLibre, PMTiles
and DuckDB fetches ignore a response CSP, so the map is unaffected. The uploader's allow-list
refuses `.html`, `.svg` and `.js` in the first place.

## 9. Bucket-scoped tokens into the `production` Environment

One token per bucket, each **Object Read & Write** on that bucket only, with a 90-day TTL:

1. **R2 object storage → Account details → API Tokens → Manage → Create Account API token**
   *(check: a User API token works the same; an Account token survives a change of user)*.
2. Name `gigawatt-atlas-tiles`; permissions **Object Read & Write**; **Apply to specific buckets
   only** → `atlas-tiles`; TTL 90 days *(check the TTL field name)*. Create.
3. Cloudflare then shows three values: **Token value**, **Access Key ID** and **Secret Access
   Key**. Only the Access Key ID and the Secret Access Key go into the `production` Environment
   (table below): copy them straight into GitHub, since Cloudflare shows the secret once, and do
   not store them anywhere else. The **Token value** is not stored anywhere, in GitHub or
   elsewhere: it is the same credential in Cloudflare API form (the Secret Access Key is its
   SHA-256), and nothing here uses it, because `atlas publish` talks to R2 over the S3 API with
   the key pair alone. Close the page once the two secrets are saved (if you already saved the
   Token value somewhere, delete that copy); if one is lost, create a new token as in the
   rotation below.
4. Repeat with name `gigawatt-atlas-raw` for `atlas-raw`.

GitHub: **moonspells/gigawatt-atlas → Settings → Environments → production**:

| Kind | Name | Value | Used by |
|---|---|---|---|
| Environment secret | `R2_TILES_ACCESS_KEY_ID` | Access Key ID of the `atlas-tiles` token | the `publish.yml` upload and takedown jobs and the `basemap.yml` upload job |
| Environment secret | `R2_TILES_SECRET_ACCESS_KEY` | its Secret Access Key | same |
| Environment secret | `R2_RAW_ACCESS_KEY_ID` | Access Key ID of the `atlas-raw` token | M2 fetch jobs |
| Environment secret | `R2_RAW_SECRET_ACCESS_KEY` | its Secret Access Key | M2 fetch jobs |
| Environment variable | `CF_ACCOUNT_ID` | the 32-character account id (R2 overview, **Account ID**) | the R2 endpoint `https://{CF_ACCOUNT_ID}.r2.cloudflarestorage.com` |

`R2_TILES_BUCKET` is optional and defaults to `atlas-tiles`. The secrets reach only the upload
steps of `publish.yml` and `basemap.yml` and the takedown step of `publish.yml`, through `env:`.

Checks:

```sh
gh api repos/moonspells/gigawatt-atlas/environments/production/secrets --jq '.secrets[].name'
#   R2_RAW_ACCESS_KEY_ID R2_RAW_SECRET_ACCESS_KEY R2_TILES_ACCESS_KEY_ID R2_TILES_SECRET_ACCESS_KEY
gh variable list --env production -R moonspells/gigawatt-atlas    # CF_ACCOUNT_ID
gh secret list -R moonspells/gigawatt-atlas                       # no repo-level R2 or Cloudflare secret
```

Rotate both tokens before day 90 (a calendar reminder at day 80): create the new token, replace
the two secrets, run one upload, then delete the old token. A token that expires makes the next
upload fail with an access error and changes nothing in the bucket.

## 10. First uploads and verification

1. Basemap: if `basemap.yml` already ran (its extract keeps the file 30 days), open that run and
   **Re-run failed jobs**; otherwise dispatch it with build `20261006` (publishing.md §5).
2. Fixture release: dispatch `publish.yml` with `fixture` ticked. Then set a calendar reminder every
   100 days, for as long as site CI pins `20000101-0000`, to dispatch it again (§7). After the first
   renewal, check once in **R2 → atlas-tiles → Objects** that `v/20000101-0000/manifest.json` shows
   the renewal's upload date.
3. Check the headers:

```sh
B=https://tiles.moonspells.dev/basemap/conus-z10-20261006.pmtiles
curl -sI -H "Range: bytes=0-16383" "$B"
#   HTTP/2 206, content-range: bytes 0-16383/…, content-type: application/octet-stream,
#   cache-control: public, max-age=31536000, immutable, no content-encoding,
#   content-security-policy: default-src 'none'; sandbox, x-content-type-options: nosniff
curl -sI -H "Range: bytes=0-16383" "$B" | grep -i cf-cache-status      # HIT on the second request

curl -s -o /dev/null -D - -H 'Accept-Encoding: br' https://tiles.moonspells.dev/v/20000101-0000/manifest.json \
  | grep -iE '^(content-type|content-encoding|cache-control)'
#   content-type: application/json, content-encoding: br (Cloudflare compresses JSON at the edge)

curl -s -o /dev/null -D - -H 'Origin: https://moonspells.dev' -H 'Range: bytes=0-99' "$B" \
  | grep -i '^access-control'
#   access-control-allow-origin: https://moonspells.dev, access-control-expose-headers: etag, content-length, content-range

curl -sI https://tiles.moonspells.dev/atlas/latest.json
#   404 until the first real release; then 200, content-type: application/json,
#   cache-control: public, max-age=60

curl -sI https://tiles.moonspells.dev/v/20000101-0000/facilities.parquet | grep -i '^content-type'
#   content-type: application/vnd.apache.parquet (GeoParquet 1.1), no content-encoding
```

4. TLS: the tiles host must refuse TLS 1.1 and accept TLS 1.2. `openssl` needs `@SECLEVEL=0` to
   offer TLS 1.1 at all (OpenSSL 3 disables it by default, so a plain `curl --tls-max 1.1` fails
   whatever the server allows and proves nothing):

```sh
openssl s_client -connect tiles.moonspells.dev:443 -servername tiles.moonspells.dev \
  -tls1_1 -cipher 'DEFAULT:@SECLEVEL=0' < /dev/null
#   must fail: a handshake failure or "unsupported protocol" alert, no certificate chain
openssl s_client -connect tiles.moonspells.dev:443 -servername tiles.moonspells.dev \
  -tls1_2 < /dev/null 2>/dev/null | grep -E '^ *(Protocol|Cipher) *:'
#   must succeed: Protocol : TLSv1.2
```

5. Confirm in the dashboard that `atlas-raw` has no custom domain and no `r2.dev` URL, and that
   no Cloudflare API token exists in any GitHub repo or Environment (13 §5.5).

Record the date and results in the site repo's `docs/ops/phase-1-status.md` (13 §5.5 tiles
acceptance check).
