---
title: Site Branding
description: Configure the site name, header logo, favicon, and footer introduction
icon: i-carbon-paint-brush
---

# Site Branding

Open **Site Branding** in the admin portal to edit the site's display name and
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

GIF uploads retain their animation, transparency, and frame timing. Each image
has an independent **GIF playback** selector: **Loop forever** or **Play once**.
Choose the setting before uploading, or change it for an existing GIF and click
its **Save** button. Play once stops on the final frame; loading the
image again, such as after a page reload, starts a new playback. Existing GIFs
uploaded before animation support were stored as a single PNG frame and must
be uploaded again to restore animation.

GIF previews and header logos animate. Browser tab favicon animation depends
on browser support; some browsers show a static frame even for animated GIFs.

Other raster images are decoded and re-encoded as PNG, preserving transparency,
with decoded input limited to 16 million pixels. Header logos fit within
512 × 512 pixels and favicons within 256 × 256 pixels, retaining their aspect
ratio. For animated non-GIF raster images, the first frame is used. Each
normalized PNG or GIF must fit within 256 KiB.

Settings and images are stored in the application database, independently of
repository storage. Back up the database to preserve them. The normal migration
runner calls `init_db()` to create the `site_branding` table on an existing
installation. Deploy the updated backend and both frontends together.

Until an administrator saves a site name, the backend uses `app.site_name`
(`KOHAKU_HUB_SITE_NAME`). The default footer introduction is
“Self-hosted HuggingFace Hub alternative”. Restoring an image does not change
the other image or either text field.

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
`data:image/svg+xml;base64,...` string. GIF loop behavior is encoded in the GIF
contents and remains effective when loaded from the browser cache. Browser
favicons use the corresponding image MIME type. No admin credentials are part
of the public response or browser branding cache. `GET /api/site-config` also
returns the effective site name without including image contents.

All admin operations require `X-Admin-Token` and an enabled admin API:

| Method | Endpoint                                           | Behavior                         |
| ------ | -------------------------------------------------- | -------------------------------- |
| GET    | `/admin/api/site-branding`                         | Read saved branding and defaults |
| PUT    | `/admin/api/site-branding`                         | Update provided text fields      |
| POST   | `/admin/api/site-branding/assets/{kind}`           | Upload multipart field `file`    |
| PATCH  | `/admin/api/site-branding/assets/{kind}/animation` | Update GIF playback              |
| DELETE | `/admin/api/site-branding/assets/{kind}`           | Restore one default image        |

`kind` is `header_logo` or `favicon`. Each successful admin operation returns
the full branding object. The name must be nonblank and at most 100 characters;
the footer introduction may be empty and is limited to 2,000 characters.

Uploads optionally accept the multipart field `loop` (`true` by default for
infinite playback, `false` for one playback). Updating playback uses JSON
`{"loop": true}` or `{"loop": false}` and requires the selected asset to already
be a GIF. Playback changes preserve the other image and text settings.
