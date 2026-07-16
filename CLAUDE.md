# CLAUDE.md

Guidance for working on the Luma Studios fork of `ayon-sitesync`.

## Project Overview

SiteSync keeps representation files in sync between sites (studio, local,
remote providers). It ships:

- `client/ayon_sitesync/` — tray addon + publish plugins + provider handlers
- `server/` — REST endpoints and settings (runs on the server's Python 3.9+)
- `frontend/` — React (Vite) UI served in the AYON web app

## Branch Convention

```
origin   -> github.com/{GITHUB_USERNAME}/ayon-sitesync   (fork)
upstream -> github.com/ynput/ayon-sitesync                (ynput)

develop  -- mirrors upstream. Never commit directly.
luma     -- Luma production branch. Based on upstream release tags.
```

`luma` was branched from tag **1.3.1**.

## Versioning (Luma Studios)

Format: `{upstream_version}+ls.{major}.{minor}.{patch}` — e.g. `1.3.1+ls.0.0.1`.

**`package.py` is the single source of truth — edit it and nothing else.**
`create_package.py` rewrites every other version location from it at build
time, so do NOT hand-maintain them:

| File | Rewritten by |
|---|---|
| `client/ayon_sitesync/version.py` | `update_client_version()` |
| `pyproject.toml` (root) | `update_general_version()` |
| `frontend/package.json`, `frontend/package-lock.json`, `frontend/src/main.jsx` | `update_frontend_version()` |

A build therefore produces version-file diffs in the working tree. That is
expected — commit them, or just rebuild.

## Building

**Requires `yarn`** — `create_package.py` calls `build_frontend()`, which raises
`RuntimeError("Yarn executable was not found.")` without it. `yarn` must be on
`PATH` (the lookup uses `which yarn`).

```bash
python3 create_package.py          # builds frontend/dist, then the addon zip
```

---

## Upstream Sync Risks

Re-check every item below after each `ayon-upstream-sync`.

### 1. Unguarded `import dropbox` takes the whole addon down

`providers/lib.py` imports `.dropbox` **unconditionally**, so anything that
fails inside the `dropbox` package kills sitesync at `tray_init`.

Observed on sitesync **1.3.1** with the AYON dependency package:

```
File "...\ayon_sitesync\providers\dropbox.py", line 3, in <module>
    import dropbox
File "...\dependency_packages\ayon_..._windows.zip\runtime\dropbox\session.py", line 1
    import pkg_resources
ModuleNotFoundError: No module named 'pkg_resources'
```

**Why this is worse than it looks.** `ayon-core`'s `TrayAddonsManager` sets
`addon.tray_initialized = True` *after* `tray_init()` returns, so a crash leaves
it `False`, and core then skips **both** `tray_menu()` and `tray_start()`:

```python
# ayon_core/tools/tray/ui/addons_manager.py
def start_addons(self):
    for addon in self.get_enabled_tray_addons():
        if not addon.tray_initialized:
            if isinstance(addon, ITrayService):   # sitesync is NOT one
                addon.set_service_failed_icon()
            continue                             # tray_start() never runs
```

`tray_start()` is what starts `SiteSyncThread` — so **nothing syncs, for any
provider, including local drive**. `SiteSyncAddon` is `ITrayAddon` (not
`ITrayService`), so there is **no failed-service icon**: it fails silently.
Tell-tale: `N/A` in the *Tray menu* / *Addons start* columns of the tray report.

**Why a dependency-package rebuild does NOT fix it.** dropbox 11.x's `setup.py`
declares only `requests`, `six`, `stone` — **setuptools is undeclared**, yet
`session.py` imports `pkg_resources`. `ayon-dependencies-tool` resolves with
`uv pip compile` and installs with `uv pip install --no-deps --prefix runtime/`,
so only the resolved closure lands. `pkg_resources` never enters it, and a
rebuild reproduces the failure exactly. It historically "worked" only because
setuptools used to be present in every Python environment.

**Fix (shipped in `1.3.1+ls.0.0.1`)** — two independent halves:

1. **Guard the import** in `providers/dropbox.py`, mirroring the existing
   pattern in `providers/sftp.py`, plus an early return in `__init__` when the
   module is missing. `is_active()` already returns False when `self.dbx` is
   None, so Dropbox degrades to "unavailable" instead of killing site sync.
   **Works with the current dependency package — no rebuild needed.**
2. **Cherry-picked upstream `4539f78`** — `dropbox = "^12.0.0"` in
   `client/pyproject.toml`. dropbox **12.0.0** dropped `pkg_resources` (it also
   stopped shipping a hardcoded CA bundle, which is what `pkg_resources` was
   locating). **Only takes effect after a dependency-package rebuild.**

**Rules**:
- Do NOT drop the guard on upstream sync. As of upstream `develop`, `dropbox.py`
  is **still unguarded**, so a merge can silently reintroduce the crash.
- An optional provider must never be able to kill the addon on import. If a new
  provider is added with a module-level third-party import, guard it the same
  way.
- The guard is upstreamable — worth a PR to ynput.

### 2. Python 3.7 (Nuke 13) compatibility in `client/`

Client code is imported in-process by DCCs, some still on Python 3.7. Server
code is exempt.

Fixed in `1.3.1+ls.0.0.1`: `plugins/publish/integrate_site_sync.py` annotated a
parameter `sites: list[dict]`, which raises
`TypeError: 'type' object is not subscriptable` on 3.7. Fixed with
`from __future__ import annotations`.

**What actually breaks** — only annotations Python *evaluates*:

| Position | Evaluated? | 3.7 safe? |
|---|---|---|
| Function signature (params, return) | yes, at `def` time | **breaks** |
| Module- / class-level `x: list[str]` | yes | **breaks** |
| Inside a function body (local variable) | **no** | safe |

So `list[dict]` in a method body is harmless; in a signature it is not. A
syntax gate (`ast.parse(..., feature_version=(3, 7))`) will **not** catch these
— `list[dict]` is valid 3.7 syntax and fails only at runtime. Scan for
runtime-evaluated annotations in files lacking `from __future__ import
annotations`, and separately grep for walrus / `match` / `str.removeprefix` /
runtime PEP 604. See `_ayon-manager/docs/dev_contribute.md`.

### 3. `frontend/src/main.jsx` hardcodes the addon version

```js
// const addonVersion = useContext(AddonContext).addonVersion
const addonVersion = '1.3.1+ls.0.0.1'
```

That value builds **every** REST URL the UI calls
(`/api/addons/${addonName}/${addonVersion}/${projectName}/state`, `/params`,
`/get_user_sites`). If it drifts from `package.py`, the frontend silently calls
a different addon version's endpoints.

`create_package.py:update_frontend_version()` rewrites this line from
`package.py` at build time, so normally it self-corrects — but the dynamic
`AddonContext` version is commented out upstream, so the hardcoded string is
load-bearing. Do not "tidy" it away without switching to the context value.

A `+` in the version is safe in the URL path — verified against the AYON server
(`/api/addons/core/1.9.8+ls.0.2.0/settings` → HTTP 200, literal and
percent-encoded; a bogus version → 404).

---

## Deployment Trap: the invisible `enabled: false` site override

**Symptom:** sitesync does nothing for one artist on one machine — no syncing,
for any provider — while Studio and Project settings both show `enabled: true`
and the artist's Active/Remote sites and local root all look correct. No error,
no failed-service icon (SiteSync is `ITrayAddon`, not `ITrayService`).

**Cause:** a project-site override storing `enabled: false`, which beats the
project's `true`. It is **not editable, or even visible, in the UI.**

Why it is invisible — `ayon-frontend` `ObjectFieldTemplate.tsx` hides any field
whose scope excludes the current level, and an **unscoped field defaults to
`['studio','project']`**:

```ts
const validScopes = [...(ppts?.scope || ['studio', 'project'])]
if (!validScopes.includes(props.formContext.level)) hiddenFields.push(propName)
```

`SiteSyncSettings.enabled` declares no `scope`, so the *Site settings* page
(`/manageProjects/siteSettings`) never renders it — while `AddonSettings.jsx`
still PUTs the whole form object, hidden fields included:

```js
const payloadData = { ...localData[key], __pinned_fields__: changedKeys[key], ... }
```

`ayon-backend` then stores it, because the write path is **not** scope-aware —
`extract_overrides(default, overriden, existing, explicit_pins, explicit_unpins)`
takes no `scope` argument, unlike its read-path sibling `list_overrides(...,
scope=None)`. `enabled` defaults to `False`, so the stray value disables the
addon.

**This is an upstream AYON bug — it cannot be fixed from this addon.** All three
routes are closed: declaring `scope` changes nothing (the field is already
hidden); `get_project_site_overrides` only calls `convert_settings_overrides`
during version migration, so an addon cannot filter it on read; and
`site_settings_model` is consumed only by the studio-level `/settings/site`
endpoint, not the project-site page.

**Why it fully disables sync** (`client/ayon_sitesync/addon.py`):

```python
if not sync_project_settings["enabled"]:
    return "studio"        # active forced to studio == remote -> nothing to sync

roots = {}
if not sitesync_settings["enabled"]:
    return roots           # the artist's local root override is ignored too
```

### Detect

The override is only visible with **both** `site_id` **and** the right user —
overrides live in `project_{project}.project_site_settings`, keyed by
`(addon_name, addon_version, user_name, site_id)`:

```bash
curl -s -H "x-api-key: $AYON_API_KEY" -H "x-as-user: <user>" \
  "$AYON_SERVER_URL/api/addons/sitesync/<version>/rawOverrides/<project>?variant=production&site_id=<site>"
# Bad if the response contains "enabled": false
```

Tray-side tell-tale: the addon report table shows `N/A` in the *Tray menu* and
*Addons start* columns for sitesync.

### Fix (removes only the stray key)

```bash
curl -X POST -H "x-api-key: $AYON_API_KEY" -H "x-as-user: <user>" \
  -H "Content-Type: application/json" -d '{"action":"delete","path":["enabled"]}' \
  "$AYON_SERVER_URL/api/addons/sitesync/<version>/overrides/<project>?variant=production&site_id=<site>"
```

Prefer deleting the override over setting it `true`: the site then inherits the
project value instead of carrying its own private copy. `local_setting`
(active/remote/roots) is untouched.

### Audit (run after onboarding artists onto sitesync)

Every artist who saves site settings can silently acquire this override, so
re-check per project as it rolls out:

```python
# for each site in GET /api/system/sites, for each of its users:
#   GET /api/addons/sitesync/<version>/rawOverrides/<project>?variant=production&site_id=<site>
#   with header x-as-user: <user>   -> flag any response containing "enabled"
```

## Commit Conventions

```
<type>(<scope>): <summary>
```

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `build`, `ci`, `perf`.
