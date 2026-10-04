# Footer and theme configuration

Administrators can manage footer link groups, build information visibility
and the site's default theme in
**Admin → Site → Footer / Theme**. Changes apply to visitors and signed-in users and persist
across restarts. The footer introduction continues to come from
`SiteBranding.footer_description`; appearance settings do not duplicate it.

Each tab places Reload, Restore and Save beside its title, followed by the
settings and then a live draft preview. Restore changes the draft; Save publishes
it. Footer settings reorganize the original four link columns into three groups:
Using this hub, Open source and Policies. Administrators can edit or remove these
groups and their links.

Project and upstream attribution, copyright and license remain fixed. These
credits are rendered by the site, cannot be edited in the appearance editor,
and are absent from its configurable API payloads and responses. Existing stored overrides for
`project_label`, `project_url`, `upstream_label`, `upstream_url`, `copyright_text`,
`license_label` and `license_url` are ignored; sending any of these fields in an
update returns HTTP 422. Saving footer settings removes these obsolete keys
from the saved footer JSON without changing theme settings. No additional
database migration is required.

Theme settings provide separate primary, page and card colors for light and dark
modes. Previews stay inside the editor; the admin interface retains its own theme.
The site default applies when visitors have not chosen a mode themselves. A
visitor's saved light/dark preference takes priority; system mode follows OS
changes. Saved settings load on the next main-site visit or reload. The main site
keeps a validated local cache for offline visits, and generates readable text and
link colors from the configured surfaces.

The bundled palette takes its colors from the Welcome card: amber accents and
cream surfaces in light mode, gold accents and dark green surfaces in dark mode.
These defaults apply across the main site. Saved custom colors keep taking
priority; changing the bundled palette does not rewrite existing theme settings.
Only unset color fields inherit the new defaults.

## Database upgrade

Run the standard migration command before starting an upgraded installation:

```sh
python scripts/run_migrations.py
```

Migration `028_site_appearance` creates a singleton `site_appearance` table with
nullable `footer` and `theme` JSON text columns. It preserves branding, homepage,
user and repository data. No default row is inserted. Fresh databases create
the table automatically. Overrides use row ID `1`; omitted fields retain the
bundled defaults.

## APIs and validation

`GET /api/site-appearance` is public. `GET /admin/api/site-appearance` and
`PUT /admin/api/site-appearance` require the configured `X-Admin-Token`.
The response is `{ "footer": { ... }, "theme": { ... } }`.
The `footer` object contains only `groups` and `show_build_info`.
Both public and admin responses send `Cache-Control: no-store`.

PUT accepts nested partial updates. Updating one footer field preserves the
other footer fields and the theme. Updating one theme field preserves the rest
of the theme and footer. The `groups` array is replaced as a whole. Concurrent
updates use a SQLite write transaction or PostgreSQL row lock, so first writes
and edits to different nested fields do not overwrite each other. An empty
patch does not create a row.

```json
{
  "footer": {
    "groups": [],
    "show_build_info": false
  },
  "theme": {
    "default_mode": "dark",
    "primary_dark": "#60a5fa"
  }
}
```

Invalid types, null values, unknown fields, excessive lengths, invalid colors
and unsafe URLs return HTTP 422 without changing saved settings.
Strings remain plain text. Footer settings support:

| Field | Constraint |
| --- | --- |
| `groups` | 0–3 groups; each group has a `title` and 0–8 `links` |
| Group `title` | Nonblank string, at most 100 characters; surrounding spaces trimmed |
| Link `label` | Nonblank string, at most 100 characters; surrounding spaces trimmed |
| Link `url` | Nonblank safe URL, at most 2048 characters |
| `show_build_info` | Boolean |

Safe URLs accept root-relative paths with a single leading slash or absolute
`http://` and `https://` URLs without credentials. Protocol-relative URLs,
executable schemes, whitespace, control characters and backslashes are rejected.

Theme `default_mode` accepts `system`, `light` or `dark`. Theme colors accept
exactly six hexadecimal digits prefixed with `#`; CSS expressions are rejected.

| Theme field | Default |
| --- | --- |
| `default_mode` | `system` |
| `primary_light` | `#94621f` |
| `primary_dark` | `#e6b85c` |
| `background_light` | `#f7f4eb` |
| `background_dark` | `#1c211d` |
| `card_light` | `#fffdf7` |
| `card_dark` | `#282e27` |

The default footer groups are **Using this hub** (Documentation, Get started,
About, Self-host), **Open source** (DeepGHS fork, Upstream project, Report an
issue, Discord), and **Policies** (Terms, Privacy). Fixed attribution identifies
DeepGHS and KohakuHub with their respective GitHub projects. The fixed copyright
is `© 2025 KohakuHub`; the license is `AGPL-3.0` with the fork's LICENSE link.
Build information is enabled by default and its visibility remains configurable.

If database reads fail or stored settings contain invalid data, the public API
returns safe bundled defaults with `X-Site-Appearance-Fallback: true`. The admin
API surfaces failures instead of presenting a fallback as saved settings. A
write against other invalid stored settings rolls back completely.
