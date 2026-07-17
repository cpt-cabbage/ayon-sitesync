# Site Sync for Admins

Since `1.3.1+ls.0.2.0` artists need no per-user configuration. The studio
setup is three steps.

## Setup

1. **Enable the addon** per project (or studio-wide):
   *Studio/Project Settings → Site Sync → Enabled*.
2. **Check the project Anatomy roots** (*Project Settings → Anatomy →
   Roots*): these are the studio-side paths that remote machines probe
   for reachability and sync against. No Site Sync "sites" need to be
   defined for the studio-share workflow — `studio` and each artist's
   machine (`local`) always exist.
3. Optionally tune *Site Sync → Config*:
    - `Auto-download assigned work` (default on), its interval, the
      minimum-free-space guard, and `Keep opened tasks synced for
      (days)` — how long a task an artist opened (assigned or not)
      keeps auto-downloading new published work (default 14; 0 turns
      opened-task tracking off).
    - `Loop Delay` / `Retry Count` for the transfer loop.
    - `User Default Active/Remote Site` — leave both at `studio`; the
      zero-touch logic handles remote machines. Set them explicitly only
      to force a behaviour for everyone.

Artists then answer the one-time "studio or remote?" tray prompt on each
machine. Explicit per-user site settings (active/remote site, local
roots) still work and always win over the automatic defaults.

## How zero-touch resolution works

A machine's role is resolved in this order:

1. The artist's own site settings (never overridden).
2. A non-default project `config` active/remote pair (admin force).
3. The remembered tray-prompt answer
   (`<launcher local dir>/sitesync_machine_role.json`).
4. A reachability probe of the project's studio roots — unreachable ⇒
   remote.

Remote machines resolve `active site = local`, `remote site = studio`,
and local roots default to `~/AYON_local/<root name>`.

## Known trap: the invisible per-site `enabled: false` override

Saving site settings through the AYON web UI can silently store an
`enabled: false` override for a user's machine that no UI shows, which
disables sync for that machine only. The tray now detects this at start
("Site Sync doctor") and notifies the artist.

To remove it (deleting the override is better than setting it true):

```bash
curl -X POST -H "x-api-key: $AYON_API_KEY" -H "x-as-user: <user>" \
  -H "Content-Type: application/json" \
  -d '{"action":"delete","path":["enabled"]}' \
  "$AYON_SERVER_URL/api/addons/sitesync/<version>/overrides/<project>?variant=production&site_id=<site>"
```

Find suspect machines by listing `GET /api/system/sites` and checking
`rawOverrides` per user/site for an `"enabled"` key.

## Operational notes

- One misconfigured site or project no longer stops the sync loop for
  other projects (since `1.3.1+ls.0.1.0`); look for `Site '...' is not
  configured` / `not working properly` warnings in the tray log.
- Failed transfers can be requeued from the web page (*Retry all
  failed*) or via `POST
  /api/addons/sitesync/<version>/<project>/state/resetFailed?siteName=`.
- Headless machines (render nodes, a studio box serving an
  always-accessible site) run
  `ayon addon sitesync syncservice --active_site <name>`.
