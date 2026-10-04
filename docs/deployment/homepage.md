# Homepage configuration

The homepage uses two views: visitors see an introductory card filling the first
viewport beneath the header, while signed-in users see a separate workspace.
The workspace has personal navigation and repositories on the left, repository
activity in the center, and trending repositories on the right.
It does not include the visitor discovery section or marketing footer.
Administrators can edit the visitor card at
**Admin → Site → Homepage**. The Site page groups Branding, Homepage, Footer and
Theme settings under one sidebar entry. Switching between its tabs preserves
unsaved drafts. See [Site appearance](./site-appearance.md) for footer and theme
configuration.
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

## Signed-in workspace

The activity feed combines stored repository creation times, main-branch
commits and active likes. It uses the time of each action rather than a
repository's latest modification time. **Personal** includes the account's
actions and repositories owned by the account or its member organizations.
**Following** includes followed people and organizations; **All** combines both.
Repository type filters and cursor pagination are applied on the server.

Local user and organization profiles provide Follow/Following controls and
paginated follower lists. Following expresses interest and does not grant access
to private repositories. The feed checks current repository access on every page.
Refreshing retains existing cards while loading; changing account or session
clears private content immediately. HTTP 401/403 also clears retained activity.

These events are derived from existing rows. Cancelling a like removes its entry;
liking again records a new time. Deleting repositories or recorded commits removes
their entries, and moving a repository changes its current namespace. Repository
creation has no recorded creator, so the feed identifies the namespace without
inventing an author. See [Following and activity](../features/following.md) for
the API contract and migration requirements.

## Frontend structure

The visitor's three preview columns share `RepositoryPreviewColumn` and the
catalog's `RepoDiscoveryCard`. Repository types use one shared configuration.
The workspace separates sidebar, activity and trending views from their data
composables. Discovery URL state and request lifecycle live in
`useRepositoryDiscovery`; catalog dialogs and `/new` share
`CreateRepositoryForm`. Site settings share their header, actions, request state
and base form styles while retaining feature-specific drafts and uploads.

Ordinary public-app cards and their loading placeholders use
`--site-card-radius`, `--site-card-shadow` and `--site-card-hover-shadow`.
The visitor hero, controls, avatars and code panels retain their own geometry.

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
The write and complete stored-configuration validation share a transaction;
if an existing invalid override prevents a successful response, the patch is
rolled back. A patch that repairs that override can still be saved.

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
