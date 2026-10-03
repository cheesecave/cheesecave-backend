# Homepage configuration

The homepage uses two views: visitors see an introductory card filling the first
viewport beneath the header, while signed-in users see a separate workspace.
The workspace has personal navigation and repositories on the left, recent
repository updates in the center, and trending repositories on the right.
It does not include the visitor discovery section or marketing footer.
Administrators can edit the visitor card at
**Admin → Site → Homepage**. The Site page groups Branding and Homepage settings
under one sidebar entry. Switching between its tabs preserves unsaved drafts.
Homepage controls appear first, settings next, and the live preview last.
Saved settings apply to all visitors and persist in the
database across restarts. Text is rendered as plain text.

The default card pairs the final `MouseCheese.vue` artwork with an
introduction and two actions. Its four-second rustle-and-peek animation plays
once, then rests on the completed illustration. Visitors with reduced motion
enabled see the completed illustration immediately. Its layout adapts to mobile screens. Use the
editor to change the heading, introduction, action labels and destinations,
hide the illustration, pause its animation, or hide the visitor repository lists.
A blank action
label or destination hides that action. Disabling the card leaves the other
homepage content available. These controls do not change the signed-in workspace.

The title supports Enter/newline characters (LF or CRLF), as well as literal
`\n` and `\r\n` typed in the editor. Both the preview and visitor card display
these as line breaks. The saved title remains plain text.

## Upgrade existing installations

Run the normal migration command before starting the upgraded application:

```sh
python scripts/run_migrations.py
```

After the existing `026_path_commits` migration, migration `027_site_homepage`
creates the `site_homepage` table. It preserves
existing site branding, accounts and repositories, and does not insert a default
row. Fresh databases create the table automatically. The singleton override
row uses ID `1`; omitted columns continue to use application defaults.

## API

`GET /api/site-homepage` is public and returns the complete configuration.
`GET /admin/api/site-homepage` and `PUT /admin/api/site-homepage` require the
configured `X-Admin-Token`. PUT updates only supplied fields, so concurrent edits
to different fields do not overwrite one another. Unknown fields, null values,
invalid types and unsafe links return HTTP 422 without saving changes.

| Field               | Default                                                                                                  | Constraint                                        |
| ------------------- | -------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| `enabled`           | `true`                                                                                                   | Boolean                                           |
| `eyebrow`           | `THE HOME FOR YOUR AI PROJECTS`                                                                          | String, at most 100 characters                    |
| `title`             | `Your next idea starts here.`                                                                            | Nonblank string, at most 200 characters           |
| `description`       | `Discover, share, and build with models, datasets, and spaces. A home for your work and your community.` | String, at most 2000 characters                   |
| `primary_label`     | `Get Started`                                                                                            | String, at most 80 characters; blank hides action |
| `primary_url`       | `/get-started`                                                                                           | String, at most 2048 characters                   |
| `secondary_label`   | `Host Your Own Hub`                                                                                      | String, at most 80 characters; blank hides action |
| `secondary_url`     | `/self-hosted`                                                                                           | String, at most 2048 characters                   |
| `illustration`      | `mouse-cheese`                                                                                           | `mouse-cheese` or `none`                          |
| `animation_enabled` | `true`                                                                                                   | Boolean                                           |
| `show_repositories` | `true`                                                                                                   | Boolean                                           |

Action destinations accept root-relative paths or absolute HTTP(S) URLs.
Protocol-relative URLs, executable schemes, whitespace, control characters and
backslashes and URLs containing credentials are rejected. An empty destination
is allowed for a hidden action.

Example partial update:

```json
{
  "title": "Build with our community",
  "description": "Share models and datasets with your team.",
  "primary_label": "Explore models",
  "primary_url": "/models",
  "animation_enabled": false
}
```

The public response sends `Cache-Control: no-store`. During a database outage or
when stored configuration is invalid, it returns safe bundled defaults and
`X-Site-Homepage-Fallback: true`. The admin endpoints surface failures rather
than presenting a fallback as saved configuration.
