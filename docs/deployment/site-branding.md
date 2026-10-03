---
title: Site Branding
description: Configure the site name, header logo, favicon, and footer introduction
icon: i-carbon-paint-brush
---

# Site Branding

Open **Site → Branding** in the admin portal to edit the site's display name and
footer introduction. Upload the header logo and favicon separately; each image
has its own control to restore the bundled default. Saving text and uploading or
restoring an image are separate operations.

The site name appears in the public header, footer heading, browser title, and
administration header/title. The footer introduction is plain text, including
line breaks. Existing KohakuHub descriptions in the home page, About page, and
documentation, as well as footer links and project/license credits, stay intact.

## Images and persistence

Uploads accept SVG, PNG, JPEG, WebP, GIF, and ICO images up to 2 MiB each. SVG
images remain vectors: the backend validates and normalizes their XML instead
of rasterizing them. They must be static and self-contained. Scripts, event
handlers, embedded HTML, animation, external references, and external fonts
are rejected; local fragment references such as gradients and reusable paths
are supported. The normalized SVG must fit within 256 KiB.

Header-logo GIF uploads retain their animation, transparency, and frame timing.
The header logo has a **GIF playback** selector: **Loop forever** or **Play once**.
Choose the setting before uploading, or change it for an existing GIF and click
its **Save** button. Play once stops on the final frame; loading the
image again, such as after a page reload, starts a new playback. Existing header GIFs
uploaded before animation support were stored as a single PNG frame and must
be uploaded again to restore animation.

Header-logo GIF previews and header logos animate. Favicons are always static:
uploading a GIF uses its first frame and stores it as PNG, preserving transparency.
The favicon card has no GIF playback controls. Older saved GIF favicons are also
returned as first-frame PNGs without rewriting their database records; no data
migration or re-upload is required.

Other raster images are decoded and re-encoded as PNG, preserving transparency,
with decoded input limited to 16 million pixels. Header logos fit within
512 × 512 pixels and favicons within 256 × 256 pixels, retaining their aspect
ratio. For animated non-GIF raster images, the first frame is used. Each
normalized PNG or GIF must fit within 256 KiB.

Settings and images are stored in the application database, independently of
repository storage. Back up the database to preserve them. Migration
`025_site_branding` creates the table on an existing installation; `init_db()`
also includes it in a fresh database. Deploy the updated backend and both
frontends together. Follow the upgrade procedure below before starting API or
worker processes.

Until an administrator saves a site name, the backend uses `app.site_name`
(`KOHAKU_HUB_SITE_NAME`). The default footer introduction is
“Self-hosted HuggingFace Hub alternative”. Restoring an image does not change
the other image or either text field.

## Database upgrade and rollback

### Schema and compatibility

This release only adds the `site_branding` table. It does not alter existing
tables, foreign keys, indexes, users, repositories, or stored files. There is no
data backfill and no required change to PostgreSQL, SQLite, MinIO, or LakeFS.

| Column               | Type         | Meaning                                            |
| -------------------- | ------------ | -------------------------------------------------- |
| `id`                 | INTEGER PK   | The application reads and writes row `1`           |
| `site_name`          | VARCHAR(100) | NULL uses the configured site name                 |
| `footer_description` | TEXT         | NULL uses the default; an empty string stays empty |
| `header_logo`        | TEXT         | NULL uses the bundled logo; otherwise a data URL   |
| `favicon`            | TEXT         | NULL uses the bundled icon; otherwise a data URL   |

All four override columns are nullable. The migration leaves the table empty;
the first admin save or upload inserts row `1`. The primary key prevents
duplicate row `1`; the application enforces the single-row convention. Images
are inline Base64 data URLs, so a full database backup also includes SVGs, GIF
frames, and header-logo GIF playback settings. With two 256 KiB normalized images, their
combined Base64 content is approximately 683 KiB, plus data URL prefixes and
text; upload limits are enforced by the API rather than SQL constraints.

Migration `025_site_branding` uses the same DDL on SQLite and PostgreSQL. It can
be rerun without replacing saved settings, creates no default override row,
and reports failure for an incompatible pre-existing table instead of dropping
or rewriting it. Its completion check also requires the preceding repository
operation-lock schema, so a table created by an earlier development build does
not cause pending historical migrations to be skipped.

### Upgrade an existing installation

Use the installation's existing Compose file, environment, database URL, and
SQLite volume mounts. The Compose commands below use the production example's
service names (`hub-ui`, `hub-api`, `khub-worker`, and `postgres`); adapt the
names to the actual deployment. The development Compose file only starts
infrastructure, so run the host migration command for that environment.

1. Prepare the new backend image and both frontend builds in a separate release
   directory. Install the updated backend dependencies, including `tinycss2`.
   Keep the current frontend artifacts served until the release is switched.
2. Stop public traffic and all API and background worker processes. For the
   production Compose example:

   ```bash
   docker compose stop hub-ui hub-api khub-worker
   ```

3. Back up the application database and retain the previous release artifacts.
   PostgreSQL stays running while taking a consistent dump. For a bundled
   PostgreSQL service, write the archive inside the container before copying it
   to the host (this also avoids binary output redirection in Windows shells):

   ```bash
   docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/hub-before-site-branding.dump'
   docker compose cp postgres:/tmp/hub-before-site-branding.dump ./hub-before-site-branding.dump
   ```

   Use a unique archive filename for each upgrade. Confirm that `POSTGRES_DB`
   is the application database from `KOHAKU_HUB_DATABASE_URL`; for an external
   PostgreSQL service, use its normal backup tooling and credentials. For
   SQLite, use SQLite's `.backup` command against the configured database file
   after stopping all writers, rather than copying a live file in WAL mode:

   ```bash
   sqlite3 /path/to/hub.db ".backup '/path/to/hub-before-site-branding.db'"
   ```

   Confirm the backup can be read or restored in an isolated database before
   continuing.

4. Run the full migration runner once with the **new** backend code and the
   **existing** database configuration. For the prepared Compose image:

   ```bash
   docker compose run --rm --no-deps hub-api python scripts/run_migrations.py
   ```

   For a host installation, run from the new release's project directory in
   its Python environment with the existing `HUB_CONFIG` and/or
   `KOHAKU_HUB_DB_BACKEND` / `KOHAKU_HUB_DATABASE_URL` settings:

   ```bash
   python scripts/run_migrations.py
   ```

   Require exit status `0`. This command runs pending older migrations before
   `025_site_branding`; their changes are outside this feature's additive
   schema change. Use the full runner when upgrading from an older release.
   Only when migrations through `024` are already applied, a targeted check
   can use `python scripts/db_migrations/025_site_branding.py`.

5. Verify the new table exists and read the saved values without returning
   whole image contents:

   ```sql
   SELECT id, site_name, footer_description,
          length(header_logo) AS header_logo_chars,
          length(favicon) AS favicon_chars
   FROM site_branding;
   ```

   An empty result is correct before the first save. Existing development
   settings should be unchanged after migration. Check the migration logs;
   the public branding endpoint's default fallback alone cannot establish
   database health.

6. Switch both frontend builds to the new release, start the new API, then
   the workers and frontend service. The standard Docker startup repeats the
   migration check safely before starting Uvicorn. Avoid running multiple
   initial migration jobs concurrently against the same database.

   ```bash
   docker compose up -d hub-api
   docker compose up -d khub-worker hub-ui
   ```

   Verify the authenticated admin branding page loads, save a test change,
   upload an SVG/GIF, and confirm the public header, static favicon, footer, and
   header-logo GIF playback after reloading. Restore any temporary validation settings.

Do not rely on starting several raw Uvicorn workers to perform the upgrade:
the application's existing import-time `init_db()` can create missing tables,
but it does not replace historical migrations, and simultaneous initial DDL
can race on PostgreSQL. Run one migration job before starting those workers.

### Rollback

For rollback to the immediately preceding release, stop the new API and
workers and restore the old backend and both frontend artifacts. **Leave
`site_branding` in the database.** The old code does not use this additional
table, so users and repositories do not need a database downgrade, and the
branding settings remain available for a later re-upgrade. Old frontends show
their original branding; the browser cache is not a database backup.

Do not drop the new table or restore the whole pre-upgrade backup for an
application-only rollback: that would discard saved branding or subsequent
application writes. If this upgrade also ran historical migrations, evaluate
those migrations' rollback requirements separately. A full database restore
is a recovery operation and must happen with all writers stopped, with the
loss of writes after the backup explicitly accounted for.

## Backend availability

Both frontends read a validated browser cache immediately, then refresh the
public branding configuration in the background with a three-second timeout.
Branding refresh does not delay application mounting. The cache includes the
normalized image contents, so a cached custom logo and favicon do not need an
additional request to the backend.

- A returning browser keeps its last successfully loaded branding when the
  backend is unreachable, times out, or returns invalid configuration.
- A browser without valid cached branding displays the bundled KohakuHub
  defaults during an outage. It cannot know custom settings it has never loaded.
- If the API is running but its database is unavailable, the public endpoint
  returns defaults with `X-Site-Branding-Fallback: true`. The frontends keep their
  cached branding instead of replacing it with those temporary defaults.
- If browser storage is unavailable or full, successful settings still apply to
  the current page. Persistence across reloads is then unavailable.

Changes saved in the admin portal update other open tabs on the same origin via
browser storage events. Other browsers receive the latest configuration on
their next page load. Separate frontend origins, including the two standalone
Vite development ports, have separate caches.

These fallbacks keep the frontend shell and brand assets usable during an API
outage. API-backed features, including repository lists and admin saves, still
require a working backend. The frontend static files must also remain served.
Admin branding requests time out after 30 seconds; failed saves retain the
draft and previously displayed branding.

## API

`GET /api/site-branding` is public. Its response has the following shape:

```json
{
  "site_name": "My Hub",
  "footer_description": "A home for our models and datasets.",
  "header_logo": null,
  "favicon": null
}
```

Image values are either `null` (use the bundled default) or an inline
`data:image/png;base64,...`, `data:image/gif;base64,...`, or
`data:image/svg+xml;base64,...` string. Favicon responses use PNG or SVG.
Header-logo GIF loop behavior is encoded in the GIF
contents and remains effective when loaded from the browser cache. Browser
favicons use the corresponding image MIME type. No admin credentials are part
of the public response or browser branding cache. `GET /api/site-config` also
returns the effective site name without including image contents.

All admin operations require `X-Admin-Token` and an enabled admin API:

| Method | Endpoint                                                | Behavior                         |
| ------ | ------------------------------------------------------- | -------------------------------- |
| GET    | `/admin/api/site-branding`                              | Read saved branding and defaults |
| PUT    | `/admin/api/site-branding`                              | Update provided text fields      |
| POST   | `/admin/api/site-branding/assets/{kind}`                | Upload multipart field `file`    |
| PATCH  | `/admin/api/site-branding/assets/header_logo/animation` | Update header GIF playback       |
| DELETE | `/admin/api/site-branding/assets/{kind}`                | Restore one default image        |

`kind` is `header_logo` or `favicon`. Each successful admin operation returns
the full branding object. The name must be nonblank and at most 100 characters;
the footer introduction may be empty and is limited to 2,000 characters.

Header-logo uploads optionally accept the multipart field `loop` (`true` by default for
infinite playback, `false` for one playback). Favicon uploads always use the first
frame; a legacy `loop` value has no effect. Updating header-logo playback uses JSON
`{"loop": true}` or `{"loop": false}` and requires the selected asset to already
be a GIF. Playback changes preserve the favicon and text settings. Favicon
animation updates return HTTP 400 because favicon images have no playback settings.
