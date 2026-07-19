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

## Added on `luma` (unreleased, 2026-07-19): second-audit fix batch

One batch implementing every actionable finding of the 2026-07-19
five-agent audit (client resilience, server, UI, upstream, functional
gaps). No version bump (user bumps explicitly). Mixed rollout is
tolerated (a new client on an old server treats the missing
`requeueStale` endpoint as a warn-once), but server and client should
ship together for the fixes to be complete.

Server (`server/__init__.py`, `settings/settings.py`):

- **SQL injection closed on LIST params.** `SQLTool.array` (ayon-backend)
  wraps string elements in quotes WITHOUT escaping them — `repreNameFilter`
  and `siteNames` were live injection points even after the first audit's
  fixes. **Rule: never call `SQLTool.array` directly on request data —
  `sql_array` is the one sanctioned IN-list builder (it escapes
  internally); id lists additionally go through `validate_id_list`,
  site-name lists through `validate_site_name` per element.**
- **Folder-ACL on mutating endpoints** (`ensure_representation_access`):
  state POST/DELETE, `setPriority`, per-repre `resetFailed`; the batch
  `resetFailed` and `backfill` scope their set operations by
  `folder_access_list` for restricted users. Unrestricted users and
  managers are unaffected (no extra query).
- **`ORDER BY ..., r.id` tiebreaker** in `/state` — all sort keys are
  non-unique (most rows share priority 50); without a total order, paging
  skipped/duplicated rows, including in the sync loop's own fetch. Keep it.
- **`/params` `count` is the raw representation row count again** — the
  web DataTable uses it as `totalRecords`, and the audit-batch DISTINCT
  change capped the paginator at one page. `names` stays DISTINCT.
- **Backfill throttle lives in the DB** (`sitesync_meta` key/value table,
  created by `check_sync_status_table`): module-level state is per uvicorn
  worker and reset on deploy, so the anti-join scan ran N× per hour. The
  claim is one atomic `INSERT ... ON CONFLICT ... WHERE ... RETURNING`.
- **State POST**: insert branch has `ON CONFLICT DO UPDATE` (FOR UPDATE
  can't lock a nonexistent row — concurrent first-POSTs 500ed); an
  explicitly posted `priority` now updates ALL of the repre's rows
  (a per-side write reintroduced the divergence `setPriority` prevents).
- **Retry-all is malformed-row-proof**: `CASE WHEN jsonb_typeof(...)`
  guards `jsonb_each` (AND does not guarantee evaluation order), jsonb
  equality instead of `::int` casts, `isinstance(files, dict)` in Python.
- **NEW endpoint `POST /{project}/state/requeueStale?[siteName=]&olderThanSeconds=`**
  (registered before the generic route): flips IN_PROGRESS files whose
  timestamp stopped updating back to QUEUED. A tray killed mid-transfer
  left files IN_PROGRESS forever — the roll-up ranks IN_PROGRESS above
  everything, the loop fetches only OK/QUEUED pairs, and resetFailed only
  touches FAILED. Without `siteName` it sweeps EVERY site of the project
  — deliberately, because a wiped machine's own tray is exactly the one
  that will never ask for its site; syncing trays call it (no site) per
  project every 15 min with age 3600. **Clock-skew/mixed-rollout safety:
  the state POST stamps IN_PROGRESS file timestamps with the SERVER's
  clock, so staleness is a same-clock comparison regardless of artist
  machine clocks or older client builds re-posting copied timestamps.
  The staleness predicate lives in the SQL (CASE-guarded jsonb) so live
  rows are never fetched or FOR-UPDATE-locked. Companion rule:
  `update_db` still stamps a fresh int() timestamp on every post — the
  server model declares `timestamp: int` and float coercion is
  pydantic-version dependent.**
- `min_free_space_gb` gained `ge=0` (a negative typo silently disabled
  the free-space guard).

Client (providers, `sitesync.py`, `addon.py`, plugins):

- **The provider wedge is fixed.** local_drive/sftp ran the copy in a
  detached thread and size-polled forever; a failed/stalled copy (disk
  full, VPN drop, OneDrive handle) never surfaced, permanently ate one of
  the 3 executor slots, and three of them silently halted ALL transfers +
  hung tray exit. Now: copy threads are daemons with an exception holder;
  `_mark_progress` raises on thread death or a 300s no-new-bytes stall;
  sftp `_get_conn` raises instead of returning None. **Rule: no provider
  wait loop may lack a liveness/stall bound.**
- **Atomic transfer writes everywhere**: downloads (all providers), sftp
  uploads (remote tmp + rename) and the workarea mirror write to
  `*.ayon_tmp` and `os.replace` into place. A truncated file at a final
  path is trusted by every `os.path.exists` consumer (published tab,
  CollectAudio, adopt). **Rule: never write transfer bytes directly to
  the final path.** `sitesync.download` additionally verifies byte size
  against the representation after every provider download, and
  `validate_project` refuses to adopt a size-mismatched file.
- gdrive: HTTP 404/quota-403 now RAISE (returning `False` matched no
  `update_db` branch — the file retried every pass forever, never counted
  a retry, never went FAILED, never notified); pause raises
  `TransferPausedError` (`providers/exceptions.py`), which the loop turns
  into `update_db(requeue=True)` — a pause is not a failure and must not
  eat retries. dropbox: real 0-1 progress per chunk (was one `100` post
  after completion — wrong scale, no visibility). rclone:
  `_obscure_pass` gates `CREATE_NO_WINDOW` by platform (any web-config
  password made the provider unusable on macOS/Linux), `lsjson` existence
  checks get a 60s timeout.
- **`reset_timer` wakes are never lost**: a wake arriving while a pass is
  RUNNING used to cancel the previous already-finished timer (no-op) and
  the transfer waited out `loop_delay` anyway — the exact `+ls.0.0.4`
  symptom resurfacing under overlap. `_reset_requested` survives the pass
  and skips the next wait.
- The results loop posts per-file with containment (one failed status
  POST no longer drops the rest of the batch's results);
  `check_shutdown` contains task exceptions (it is also the only shutdown
  path); the REST wake POST is fire-and-forget in a daemon thread — NOT
  throttled (a dropped wake can cost a full `loop_delay` for a record
  written just after the previous pass fetched) and NOT blocking (a hung
  tray webserver used to add 2s per add_site to a publish).
- Launch hook: the blocking workfile wait has a 20-min wall-clock ceiling
  (`max_retries` only counts FAILED attempts — with no tray running the
  artist was stuck in the launch dialog forever).
- **Integrator**: a version-overwrite republish now force-requeues the
  OTHER sites' records too (`_reset_other_site_records`, mirroring the
  hero handling) — remote machines used to keep stale bytes reading
  "Synced" forever over regenerated file ids.
- `_remember_linked` marks ids only after the batched state fetch
  succeeds and hard-caps the cache; `task_tracking` prunes via
  read-merge-write and writes atomically.

Control panel (`tray_control_window.py`):

- **`_fetch_state_pages` exists now** — commit `325d832` shipped the call
  without the function, so "Clean up superseded versions" always
  NameErrored per project (swallowed) and reported "Nothing to clean up".
  The helper RAISES on non-200. **Rule: scan helpers must raise on fetch
  failure so it can never read as an empty (successful) result.**
- **Download guard requires `remote == OK`** — offering Download on a
  remote-NA row minted the QUEUED/NA invisible-forever pair AND
  permanently disqualified the repre from the zero-record backfill.
- Fetch failures render as "Couldn't reach the server", never as
  "everything is in sync"; action results/errors stay on the status label
  for 10s (the immediate refresh used to clobber them within a second).
- Files tab fetches carry a generation counter — project/filter/page
  changes invalidate in-flight fetches and refetch, so stale rows are
  never rendered under a new selection.
- "Download my renders" scans first (`collect_my_render_downloads`),
  shows a size-totaled confirm dialog (details behind "Show Details..."),
  queues only on Yes; it also honors the GLOBAL pause now.
- Cleanup re-validates every entry (both sides still OK) in one batched
  call per project right before deleting, reports skipped entries, and
  counts only actual removals ("freed up to"); no dialog pops after the
  window was closed.
- The "Paused" filter matches session-paused rows (client-side merge —
  the in-memory pause never writes the PAUSED DB status); menu tooltips
  are visible (`setToolTipsVisible`); the doctor's hidden-override bubble
  is aggregated into one; "Adopt existing local files" and the web-page
  button do their REST off the UI thread.

Frontend:

- `updateSite` builds a fresh params object and the fetch effect depends
  on the selected sites — switching a site dropdown actually refetches
  (mutating module-level `defaultParams` was a same-reference state
  update React bailed on).
- Every user-influenced query value is `encodeURIComponent`ed (site
  names with spaces, `&`/`#`/`+`/`%` in filters silently corrupted the
  query); `detail.jsx`'s no-data branch returns (it fell through and
  threw); load/retry failures surface an error message instead of
  looking identical to success.

### Review round (2026-07-19, applied on top of the batch)

An 8-angle adversarial review of the batch's own diff surfaced bugs in
the new code plus cleanups; all were applied:

- **`providers/transfer_utils.py` (NEW FILE) + `providers/exceptions.py`
  (NEW FILE) — both must be `git add`ed with the batch**: an
  unconditional module-level import chain reaches them from
  `sitesync.py` and `gdrive.py`, so committing without them ships a
  client that ImportErrors at tray init (the documented silent
  total-failure mode). `transfer_utils` holds the shared transfer
  machinery: `make_tmp_path` (UNIQUE per-attempt temp names — a stalled
  attempt's abandoned writer may still hold its file, and a retry
  reusing a fixed `.ayon_tmp` name would interleave two writers into
  one torn file), `cleanup_tmp`, `STALL_TIMEOUT`, and
  `wait_for_transfer` (the one poll loop for local_drive AND sftp).
- **`wait_for_transfer` RETURNS on clean worker exit** instead of
  raising "died before completing" — the sftp worker renames the temp
  file away the instant `put()` returns, so a fast upload could
  complete without the poll ever observing convergence and was being
  marked FAILED despite succeeding. **Rule: the poll never judges the
  outcome; the CALLER verifies the final path** (sftp upload stats
  `target_path`; downloads compare sizes before `os.replace`).
  local_drive joins the copy thread 60s and refuses to `os.replace`
  while it is still alive (OneDrive/AV filter drivers hold the handle
  past the last byte; replacing then deleted a fully-copied temp file).
- **gdrive quota-403 raises `TransientTransferError`** (new class):
  requeued with NO retry increment, like a pause — the first version
  raised plainly, so a day of quota exhaustion burned files into
  permanent FAILED, and its retained `sleep(60)` pinned an executor
  slot. **Rule: transient conditions (pause, quota, rate limit) must
  never consume `retry_cnt`.**
- **The wake is an `asyncio.Event`** (created in `run()` on the
  thread's loop, set via `call_soon_threadsafe`, awaited with
  `wait_for(timeout=loop_delay)`) — the boolean-flag version still had
  a lost-wake window between the flag check and the wait start.
  `run_timer`/`self.timer` are gone; do not reintroduce a cancel-a-task
  wake.
- **`add_site` refuses to mint the QUEUED/NA pair at the API level**:
  when `follow_links is None` (the discriminator for EXTERNAL callers —
  core's Loader/Scene Inventory cannot pass it; every internal caller
  passes it explicitly per the existing invariant) and the request is
  QUEUED, a target-NA-with-source-not-OK add raises ValueError. The
  control-panel menu guard remains as UX; the publish integrator
  (explicit `status`) is exempt by construction. The launch hook wraps
  its linked-repre adds per-repre so a refused dependency cannot break
  a launch.
- **`queue_render_downloads` re-applies every gate at commitment time**
  (global+project pause, `enable_auto_download`, free space, and
  re-validation through `_queueable_states` — now the SINGLE
  implementation of the remote-OK/local-NA queueability rule, also used
  by the periodic path and the scan). The confirm dialog can sit open
  indefinitely; scan-time gates alone were bypassable.
- Server: `/params` runs ONE aggregate query (`COUNT(*)` +
  `array_agg(DISTINCT ...)`); the backfill claim binds bigints (asyncpg
  REJECTS Python floats for `::numeric` — the endpoint 500ed on every
  call); the in-process backfill throttle dict is gone (the
  `sitesync_meta` claim is the single source of truth); backfill's
  hierarchy join is built only when a folder ACL applies; the state
  POST's all-rows priority write is `IS DISTINCT FROM`-guarded; the ACL
  join text lives once in `acl_repre_subquery`.
- `update_db`: `if priority:` → `is not None` (an explicit priority 0 —
  documented as valid — was silently dropped).
- Integrator `_reset_other_site_records`: per-record containment (one
  failing site no longer abandons the remaining requeues).
- Control panel: the "Paused" filter fetches the session-paused ids
  EXACTLY via `addon.get_paused_representations()` +
  `_get_repres_state` (an unfiltered page missed paused rows sorting
  past the page boundary) and merges genuinely DB-paused rows;
  `_collect_queue_rows` pages through `_fetch_state_pages`; "in flight"
  for file fetches is derived (`seq != applied_seq`, no separate
  boolean); `_set_status(message, sticky=True)` is the one way to write
  the status label (terminal action messages are sticky, transient
  notes are not); the renders confirm flow checks `isVisible()` like
  the cleanup flow.

**NOT implemented (need design, listed in the audit as follow-ups):**
event-driven server service (representation-created handler instead of
backfill polling), orphaned-local-bytes tracking (deleted/archived
entities leave local files invisible to every tool), provider
progress/pause/resume parity, hash-based (vs size) verification,
bandwidth limiting / transfer-hours window, site-id collision detection,
bulk per-project opt-in tooling, cross-project admin dashboard, and
narrowing `upload()`'s global lock (kept: gdrive folder creation mutates
a shared tree and unserialized creation is upstream's duplicate-folder
bug #69).

## Added on `luma` (unreleased, 2026-07-18): deferred-audit items A/C/D/E/G

Implements five of the seven items deferred from the 2026-07-17 full
audit **without touching ayon-core** — only B (live-% poll timer in
core's Manager/Loader) and F (published-tab UX) remain deferred, both
being core-fork UI edits. No version bump (user bumps explicitly).
Mixed-rollout tolerant: the loop's `sortBy=priority` falls back to an
unsorted fetch on a 422 from a pre-priority server, and a missing
backfill endpoint only logs a contained warning. All new artist-facing
controls live in the **sync control panel**, never the tray menu
(user decision 2026-07-18).

- **(A) Manual transfers pull dependencies.** The audit filed this as a
  core change, but core's Loader/Scene Inventory call the addon's
  `add_site` UNCONDITIONALLY *before* core's workfile-only link loop
  (upstream's own "TODO this should happen in site sync addon"), so the
  addon can follow links itself. `add_site` grew `follow_links`
  (default `None` = follow for QUEUED requests, which is what external
  callers send): `_add_linked_site_records` follows `reference` +
  `generative` version links to `auto_download_link_depth`,
  best-effort (never fails the primary transfer), no recursion (linked
  adds go through `_add_site_record`, the record-writing core of
  `add_site` with no follow/timer-reset of its own).
  **Safety rules — do not weaken them:**
  - a linked repre is queued ONLY when the target site has NO record
    AND the opposite side of the pair is fully OK. Anything else would
    mint a QUEUED/NA pair the sync loop can never match (the
    documented invisible-forever trap) or force-reset a record already
    queued/transferring/deliberately-FAILED;
  - pair state of all candidates comes from ONE batched
    `_get_repres_state` call (no per-link REST), additions use
    `force=False` + swallowed `SiteAlreadyPresentError`;
  - a 60s in-memory cache (`_recent_link_follows`) stops re-traversal
    when core's own workfile link loop re-adds each dependency;
  - alternate sites outside the active/remote pair are never followed.
  **Invariant: every sitesync-internal `add_site` caller passes
  `follow_links` EXPLICITLY** — `False` everywhere (the publish
  integrator must not amplify a publish; launch hook / auto-download
  follow links themselves) except the control panel's per-row manual
  download/upload, which passes `True` (same semantics as a Loader
  transfer). Kill switch: `config.manual_transfer_dependencies`
  (default on). NOTE: only pulls anything if the publish plugins
  actually wrote version links — check a production USD publish
  (`GET /versions/{id}/links`) before debugging "it pulled nothing".
- **(D) Priority is wired end-to-end.** `SortByEnum.priority` + a
  `GREATEST(COALESCE(local.priority,50), COALESCE(remote.priority,50))
  AS priority` select alias; `/state` rows now return `priority`; the
  sync loop fetches `sortBy=priority&sortDesc=true`, so higher priority
  genuinely transfers first (on a pre-priority server the 422 is
  remembered — `_priority_sort_supported` — and the loop fetches
  unsorted with ONE warning instead of a doomed request per pass).
  `add_site` accepts `priority` and the launch hook's blocking workfile
  download now posts `priority=99` (upstream's own `# priority=99 TODO
  add when implemented`). Writing goes through a dedicated endpoint
  `POST /{project}/state/setPriority/{repre}?priority=N` (registered
  BEFORE the generic `state/{repre}/{site}` route so the literal
  segment wins): one UPDATE across ALL of the repre's rows, so the two
  sides can never diverge and no spurious NA row can ever be created
  (which a per-site state POST on an absent record would do). Control
  panel: per-row "Set transfer priority..." (0–100, pre-filled from
  the row's roll-up; priority 0 is valid — no `or 50` falsy-zero, and
  no value==current shortcut, since re-entering the shown roll-up is
  how diverged sides get equalized).
- **(G) Zero-record backfill.** New `POST /{project}/state/backfill`
  (CurrentUser; `siteName` query param): stamps a fully-synced record
  onto active representations with NO `sitesync_files_status` rows at
  all (Push-to-project, editorial ingest, publish with sitesync
  disabled/crashed — the NA/NA-invisible-forever class). The client
  passes the project's resolved REMOTE site — stamping a site outside
  the active/remote pair would not make the repre syncable and would
  permanently consume its zero-record state ('studio' is only the
  server-side fallback). One set-based `INSERT ... SELECT` with the
  files JSON built in SQL (no per-row round-trips, no long-held
  transaction), `NOT EXISTS` + `ON CONFLICT DO NOTHING`, and a 1h
  per-(project, site) server-side throttle because every participating
  tray triggers it. The sync thread calls it once per project per tray
  session (`_backfilled_projects`), only when the machine's pair is
  non-degenerate, via `run_in_executor` so a big first run can't block
  the event loop. KNOWN LIMIT (accepted interim per the audit): it
  stamps availability on faith — a publish whose bytes exist only on
  the crashed machine's disk will produce failing downloads until the
  files reach the remote site.
- **(E) "Download my renders"** (control panel button).
  `AutoDownloader.download_renders()` follows `generative` links in the
  **output** direction (`get_linked_representation_id` grew
  `link_direction`; also accepts a list of link types now) from the
  last published workfile version of assigned + tracked tasks, then
  queues repres that are remote-OK/local-NA. Ignores the auto-download
  ledger and the ARTIST-LOCAL switch (an explicit request beats a
  prior removal) but **respects the studio-wide `enable_auto_download`
  kill switch** — renders are the heaviest data and an admin
  protecting the VPN must not be bypassable from a button — plus pause
  and `min_free_space_gb`.
- **(C) "Clean up superseded versions..."** (control panel button).
  List-first, per the audit's "no automatic policy until this earns
  trust": scans rows that are **both** local-OK and remote-OK (deleting
  loses nothing), groups by (PRODUCT ID, representation name) — product
  id fetched from version entities because folder/product names repeat
  across hierarchies, and per-representation-name so a newer version
  downloaded only as `mov` cannot doom the older version's `exr` twin —
  offers only versions with a NEWER same-name representation fully
  downloaded on this machine, skips hero versions (negative numbers);
  confirm dialog shows count + size with the full list behind Qt's
  "Show Details..." (inlining rows outgrows the screen), then
  `remove_site(remove_local_files=True)` per row (sibling-file guard
  applies). The one-shot buttons (renders/cleanup) re-enable ONLY via
  their own completion signals — a shared `_action_done` must not
  re-enable a button whose worker still runs.

## Added on `luma` (unreleased, after opt-in): full-audit fix batch

One batch implementing every actionable finding of the 2026-07-17 full
audit. No version bump (user bumps explicitly). **Server and client must
ship together** — the progress persistence and the client's explicit
paging both assume the matching counterpart.

Server (`server/__init__.py`, `settings/models.py`, `settings/settings.py`):

- **SQL injection + crash-on-apostrophe fixed**: `localSite`/`remoteSite`
  are regex-validated (`validate_site_name`, 400 on anything outside
  `[A-Za-z0-9 _.-]`), `folderFilter`/`productFilter` go through
  `escape_ilike` (quotes doubled, `\`/`%`/`_` escaped). The tray's
  "All files" search feeds artist text straight into these — do not
  reintroduce raw f-string interpolation for any new filter.
- **Auth**: `CurrentUser` added to POST `state/{repre}/{site}` and GET
  `state/representations` (the only two handlers without it);
  `state/representations` now 400s without `representationIds`.
- **Archived entities excluded**: the state and params queries filter
  `active IS TRUE` on folder/product/version/representation — a
  soft-deleted version stops syncing instead of transferring until its
  hard delete cascades.
- **Per-file `progress` persists** (see the corrected live-% section
  below): `SyncStatusModel.progress`, stored only while IN_PROGRESS,
  returned by `/state`.
- **"Retry all failed" now matches rows with any FAILED file**, not only
  rows whose roll-up is FAILED — the roll-up ranks IN_PROGRESS above
  FAILED, so mixed rows used to be skipped.
- Hardening: `size`/`timestamp` `or 0` guards (a repre file without size
  500ed the whole state page), `data.files or {}`, params `count` now
  counts DISTINCT names, `versionFilter is not None` (version 0 was
  unfilterable), DDL in `check_sync_status_table` runs once per project
  per server process (was 3 statements on every 4s tray poll).
- **`get_user_sites` mirrors `_get_zero_touch_role` exactly**: synthesis
  requires project `enabled`, BOTH `local_setting` sides unset
  (all-or-nothing; a half-explicit pair resolves its unset side through
  the project config, like the client), a degenerate project config pair,
  site membership and `sync_enabled`. Keep the two implementations in
  lock-step.
- New setting `config.auto_download_link_depth` (default 2, 1–5).

Client (`addon.py`, `auto_download.py`, `tray_control_window.py`,
`plugins/publish/integrate_site_sync.py`):

- **The 50-row default page is dead as a silent truncator**:
  `_get_repres_state` chunks ids (100) and pages explicitly (fixes Loader
  availability, adopt, auto-download state checks in one place);
  `get_version_availability` and the repaired `get_repre_info_for_versions`
  (its URL was a nonexistent route and its param misspelled — every call
  404ed) chunk + page too; `get_sync_representations` passes
  `pageLength=limit` so provider batch limits above 50 are real; the
  queue tab pages up to `_QUEUE_MAX_PAGES`. **Any new `/state` caller
  must pass `pageLength` and page explicitly.**
- **`validate_project` ("Adopt existing local files") rewritten to be
  per-file honest**: files on disk → OK, missing files keep their status
  or become QUEUED (so the loop can complete a partial adopt), one
  `force=True` POST per changed repre (also heals stale file-id rows),
  one `reset_timer()` for the batch. The old version marked ALL files OK
  when one existed and force-reset anything past the 50-row page —
  actively corrupting state on real projects.
- **`update_db` matches the transferred file by `id`, not `fileHash`** —
  two identical files at different paths share a hash; the twin was
  marked OK without ever being copied.
- **`get_representations_sync_state` counts per-file OK + live fractions**
  — an IN_PROGRESS repre at 25/26 files used to read 0.
- **`handle_alternate_site` uses project-RESOLVED settings** (was the
  unresolved studio fetch — same class of bug as the `enabled` veto fixed
  in `+ls.0.0.2`; project-scoped `alternative_sites` were ignored on the
  post-transfer propagation).
- **`_remove_local_file` keeps files a sibling repre still holds**:
  the integrator attaches version-level resources (textures) to EVERY
  repre, so removing one repre used to delete files another repre of the
  same version still had marked OK locally (`_get_paths_held_by_siblings`,
  best-effort).
- **`IntegrateSiteSync` survives version-overwrite republish**: catches
  `SiteAlreadyPresentError` and retries `force=True` (repre ids survive
  an overwrite but file ids regenerate — without the reset the publish
  failed AND the stale record wedged the repre forever, since the server
  skips unknown file ids on update).
- **Auto-download**: follows `reference` links to
  `auto_download_link_depth` (default 2 — a loaded asset's own linked
  dependencies now come along); ledger pruned of deleted representations
  once per project per tray session (`_prune_project_ledger`).

NOT implemented at the time: the deferred list. Since 2026-07-18 most
of it HAS shipped addon-side — see *"deferred-audit items A/C/D/E/G"*
above; only the core-fork UI items (live-% poll timer, published-tab
UX) remain in *"Deferred from the 2026-07-17 full audit"* under
**Possible Future Work**.

## Added on `luma` (unreleased, after `0.9.0`): all-in-one sync control panel

Versioning note: `package.py` deliberately NOT bumped — the user bumps
versions explicitly, never per change.

`tray_queue_window.py` was renamed/expanded into **`tray_control_window.py`**
(`SyncQueueWindow` → `SyncControlWindow`, addon singleton `_queue_window` →
`_control_window`). One window now holds every artist-facing control except
roots (which stay in Site Settings):

- **Tray menu slimmed** to Sync now / Pause syncing / "Sync control panel..."
  / web page. The auto-download toggle and "Adopt existing local files"
  moved INTO the window's controls bar (`_on_tray_auto_download_toggle` and
  `_on_tray_validate` are now called from the window). Pause state is kept
  in step between the tray action and the window checkbox via
  `_sync_pause_ui` — safe because `setChecked` does not re-emit the
  user-interaction signals (`triggered`/`clicked`), so no feedback loop.
- **Two self-polling tabs, no Refresh buttons**: "Queue" (4s, cross-project
  active/failed rows as before) and "All files" (8s; one project at a time,
  status filter, debounced folder/product search, paged 200/page). Only the
  visible tab polls; timers stop on hide. Fetches/actions run in worker
  threads marshalled back via Qt signals — keep it that way.
- **"All files" filter semantics**: the `/state` endpoint ANDs all filters,
  so "either side has status X" / "folder OR product matches" is built from
  up to 4 calls merged by representation id. "Fully synced" is the one
  genuinely ANDed case (local OK + remote OK, single call). "N/A" is not
  offered as a filter — an N/A side has no `sitesync_files_status` row, so
  `status IN (...)` can never match it server-side.
- **Per-row context menu** (both tabs): retry failed
  (`resetFailed?siteName=&representationId=`, query params in the URL —
  `ayon_api.post` sends kwargs as JSON body), download/upload
  (`add_site(force=True)` — wakes the loop itself), pause/resume this file
  (session-only, see below), and "Remove download from this machine"
  (confirm dialog, `remove_site(remove_local_files=True)`, offered ONLY
  when `local_site == get_local_site_id()` so one artist's tray can never
  unsync the studio site).
- **`pause_representation`/`unpause_representation` fixed + wired**: the
  upstream bodies passed a repre entity to `update_db` without the
  mandatory `side`/`file` args — guaranteed crash (`KeyError: 'NoneStatus'`
  / `TypeError` on `file["fileHash"]`). Now in-memory only
  (`_paused_representations`, which the sync loop already checks per repre
  in `_sync_project`); a pause lasts until tray restart and the UI labels
  it "paused this session". Do NOT reintroduce the `update_db` call on an
  upstream sync.

## Added on `luma` (unreleased, after `0.9.0`): site sync is opt-in per site

Decision (2026-07-17): the studio/remote tray popup was judged noise, and
site sync should never activate on a machine nobody opted in. A new
site-scoped **`local_setting.sync_enabled`** toggle (default **off**,
rendered on the Site Settings page above My Active Site) is now the ONLY
zero-touch trigger — opting a site in *means* "this machine works
remotely", so no role question is ever asked:

- `_get_zero_touch_role` returns `remote` iff the resolved active/remote
  pair is degenerate AND `sync_enabled` is true; otherwise `None` (machine
  behaves as a plain studio workstation — no synthesis, no local roots, no
  auto-download, no mirror). Explicit `local_setting` active/remote and a
  non-degenerate project `config` pair still take precedence, unchanged.
- **Deleted:** `tray_prompt.py` and its `tray_start` scheduling;
  `get_saved_machine_role`/`save_machine_role` in `machine_role.py`. The
  prefs file `sitesync_machine_role.json` **remains** (machine-local prefs,
  e.g. the auto-download switch via `get_machine_pref`/`set_machine_pref`);
  a stale `"role"` key from older builds is simply ignored.
- `probe_machine_role` is kept ONLY as `workarea_mirror.py`'s
  share-reachability check — it is no longer a role source anywhere.
- Server `get_user_sites` mirrors the gate: the zero-touch local/studio
  pair is synthesized only for the user's sites with `sync_enabled` true.
- Rollout shape: project `enabled` stays true studio-wide; an admin (via
  API/web) or the artist (Site Settings page) flips `sync_enabled` per
  site. Machines never opted in stay silent — no popups, no probes.
- **Doctor exception to the no-popups rule** (added 2026-07-18 after a
  machine that used to sync went silent post-upgrade with zero
  explanation): `_run_doctor_checks` detects enabled projects where
  active==remote (the not-opted-in idle state) and always LOGS it; a
  one-time tray bubble is shown ONLY when the machine has evidence of
  prior sync use (stale `"role": "remote"` machine pref from pre-opt-in
  builds, or a non-empty auto-download ledger). Plain studio
  workstations stay popup-free. Keep the bubble behind that
  prior-sync-evidence gate.
- The toggle is stored per PROJECT (project-site settings) — opting a
  machine in for one project does not opt it in for others.

## Added on `luma` (`1.3.1+ls.0.9.0`): work-area workfile mirror + log fixes

**Why a mirror and not the sitesync DB** (asked and answered - keep this
rationale): `sitesync_files_status` is representation-keyed at every layer
(schema, endpoints, loop status-pair matching, retry/progress, UI) and its
state machine assumes files are immutable once OK - work-area files mutate
on every save. Expanding it means a parallel schema + endpoints + loop + UI
(the old §2 "option 3", a permanent upstream divergence). Instead
`workarea_mirror.py` uses AYON's **workfile entities** (already the
server-side index of work-area files, rootless-path-keyed) as the database
and the filesystem as the state: missing locally ⇒ direct copy over the
reachable share; existing local files are NEVER overwritten (local edits
win; publishing remains the transfer medium back). Idempotent, no
bookkeeping; limitation: needs the share reachable (probed with timeout),
download-only, no progress rows in the queue window.

- `AutoDownloader`: `_get_relevant_tasks` extracted; `_mirror_workarea`
  runs `mirror_workarea_files` in its own thread (copies can be huge; one
  at a time, busy = skip pass). Gated by new `mirror_workarea_workfiles`
  setting (default on).
- Launch hook: before the published-workfile flow, mirror the task's
  work-area workfiles (host extensions only) and open the newest -
  published seeding is now the fallback for share-unreachable/no-workfile
  cases. This also fixes "opening a task triggered nothing" when the task
  had no *published* workfile.
- `_warn_once` fix: never pass `exc_info=False` into logging - the literal
  False lands on `record.exc_info` and ayon-core's formatter subscripts it
  (`TypeError: 'bool' object is not subscriptable`). Branch on it instead.
- `_working_sites` now checks each site separately and names the failing
  site in its warning (was one merged "some of the sites" message).

## Added on `luma` (`1.3.1+ls.0.8.0`): live sync-queue visibility

- **Tray "Show sync queue…" window** (`tray_queue_window.py`,
  `SyncQueueWindow` — since superseded by `tray_control_window.py`'s
  `SyncControlWindow`, see the control-panel section above): queued /
  in-progress (with %)
  / failed / paused representations across enabled projects, direction
  inferred from which side still has work, "Retry all failed" via the
  `resetFailed` endpoint. Polls the addon's `/state` endpoint every 4s
  **only while visible** (QTimer in show/hideEvent); fetches run in worker
  threads with results marshalled back through a Qt signal - keep it that
  way, REST on the UI thread freezes the tray. NOTE the `/state` endpoint
  **ANDs** `localStatusFilter` and `remoteStatusFilter`, so "active on
  either side" requires one call per side merged by representation id.
  `ayon_api.post` sends kwargs as JSON body - `resetFailed`'s query params
  must be embedded in the URL.
- **Web page live progress**: `summary.jsx` silently re-polls every 5s
  while any visible row is IN_PROGRESS, so the existing progress bars
  animate. Poll stops automatically when nothing is transferring.

## Added on `luma` (`1.3.1+ls.0.7.0`): opened-task tracking + artist off-switch

- **Opening a task tracks it for auto-download**, assigned or not: the
  launch hook calls `task_tracking.record_opened_task` (before ANY of its
  early returns - tracking must happen even when the work area already has a
  workfile or seeding is disabled). `AutoDownloader._collect_candidates`
  unions tracked tasks with assigned ones. Entries expire
  `opened_task_retention_days` (new `config` setting, default 14, 0 = off)
  after the last open; re-opening refreshes the timestamp. Store:
  `<launcher_local_dir>/sitesync_tracked_tasks.json`, written from
  launcher/DCC processes, read by the tray - keep it lock-free
  last-writer-wins, and keep it py3.7-safe (DCC pythons import it).
- **Artist-local auto-download switch**: tray "Auto-download new work"
  (checkable) persists `auto_download` in the machine prefs file (the role
  file, `sitesync_machine_role.json`, generalized to
  `get_machine_pref`/`set_machine_pref`). It only stops background
  *downloads*; "Pause syncing" stops everything including publish uploads.
  The server-side `enable_auto_download` remains the studio-wide kill
  switch; the two AND together.

## Added on `luma` (`1.3.1+ls.0.5.0`): web-page error visibility + retry

The web frontend was 100% read-only; failures showed a truncated message with
no way to read it or act on it.

- New server endpoint `POST /{project}/state/resetFailed?siteName=` (+optional
  `representationId`): transactionally flips FAILED files back to QUEUED and
  clears `retries`/`message`; recomputes the roll-up status. Serves both the
  per-representation Retry (detail dialog footer) and the toolbar "Retry all
  failed" (summary), each POSTing once per selected site. This is the ONLY
  mutating call the frontend makes - keep new frontend actions going through
  dedicated server endpoints rather than reconstructing file payloads in JS.
- `formatStatus` shows the stored failure message wrapped + in a tooltip.

## Added on `luma` (`1.3.1+ls.0.4.0`): tray menu, notifications, doctor

The tray finally has a face (`tray_menu` was `pass`):

- **Site Sync submenu**: *Sync now* (`reset_timer`), *Pause syncing*
  (checkable; wires the dormant `pause_server`/`unpause_server` — enabled by a
  `sync_loop` fix: the pause check moved INSIDE the loop; previously
  `while ... and not is_paused()` meant pausing ended the coroutine
  permanently and resume never worked. Do not move it back), *Adopt existing
  local files* (revived `validate_project`, scheduled through
  `long_running_tasks` via `_safe_validate_project` — scheduled funcs must
  never raise or they kill `check_shutdown`), *Open sync status page*.
- **Failure notifications**: `update_db` → `_notify_failed_transfer` shows a
  tray bubble when a transfer flips to FAILED, throttled to one per project
  per 5 min, marshalled via `execute_in_main_thread`, no-op outside the tray.
- **Doctor** (`_run_doctor_checks`, worker thread at `tray_start`): detects
  the invisible per-site `enabled:false` override (see Deployment Trap below)
  by comparing `get_addon_project_settings(use_site=False)` vs `use_site=True`
  per project, and names the fix in a tray bubble + warning log.

## Added on `luma` (`1.3.1+ls.0.3.0`): auto-download of assigned work

`auto_download.py` (`AutoDownloader`, owned by `SiteSyncThread`, called at the
top of each per-project loop pass) queues the last published workfile of every
task assigned to the logged-in user — plus its `reference`-linked
representations — for download to the local site. Gates: project enabled + not
paused, `enable_auto_download` (new `config` setting, default on), machine IS
the artist's local site (`get_active_site == get_local_site_id()`), throttled
by `auto_download_interval` (default 300s), skipped with a warning below
`min_free_space_gb` free disk.

**Rules / design notes:**
- **Ledger** (`<launcher_local_dir>/sitesync_autodownload.json`): every id
  ever auto-queued is remembered and never re-queued — an artist removing a
  repre from local must not fight the service. Do not "optimize" the ledger
  away in favor of pure state checks.
- Only repres with `remoteStatus == OK` are queued: a queued/NA pair is
  invisible to the sync loop forever (see the `+ls.0.0.2` section). Not-yet-
  uploaded work is retried next interval, NOT ledgered.
- `get_last_published_workfile_representation` moved to `utils.py` and is
  shared with the launch hook - keep them shared.
- The launch hook now honors the profile toggle
  `core → tools → Workfiles → last_workfile_on_startup →
  use_last_published_workfile` itself (`_use_last_published_workfile_enabled`)
  - core never read it (see "dead setting" note below); the hook-side check
  makes the toggle real without a core change. `app_groups` extended with
  resolve, unreal, substancepainter/designer, motionbuilder, gaffer, openrv,
  premiere.

## Added on `luma` (`1.3.1+ls.0.2.0`): zero-touch site configuration

Artists no longer hand-edit site settings. Design (all client-side synthesis —
server-side *defaults* were deliberately NOT pointed at `local`, because
site-scoped defaults cannot distinguish studio workstations from remote
machines):

- **Machine role** (`machine_role.py`): `studio` or `remote`. Precedence:
  artist's explicit `local_setting` (always wins, never synthesized over) →
  non-degenerate project `config` pair (admin force) → **[superseded]** role
  file written by a one-time tray prompt → reachability probe. The prompt +
  role-file + probe steps were replaced by the per-site `sync_enabled`
  opt-in (see the unreleased opt-in section above).
- **Synthesis fires ONLY when the resolved active/remote pair is degenerate**
  (equal — today's guaranteed silent no-op, studio→studio default), so it can
  never regress a working configuration. Implemented in
  `get_active_site_type` + `get_remote_site` via `_get_zero_touch_role`
  (never raises — publish path).
- **Local roots**: `get_local_roots_with_defaults` is the single source for
  every consumer (Anatomy via `get_site_root_overrides`, dirmap, sync loop via
  `_get_default_site_configs` — do NOT read `local_setting["local_roots"]`
  directly anywhere else). Empty roots + remote role ⇒ synthesized
  `~/AYON_local/<root_name>` (`get_default_local_root_base`, cloud-synced-path
  vetoed via `utils.is_cloud_synced_path`; artist-configured cloud roots get a
  deduplicated warning).
- **Server**: `local_roots` server default changed from the
  `C:/projects_local` placeholder to **empty** (explicit artist values are
  overrides and unaffected — but a merge restoring `default_roots` would
  disable synthesis for everyone, since roots would never be "unset").
  `get_user_sites` mirrors the zero-touch defaults for the user's own
  machines (filtered by `sites.data["users"]`) so the web page renders for
  unconfigured artists instead of returning empty lists.

**Rules:**
- Never write `local_setting` (or anything) to per-site server overrides with
  a whole-object PUT — that is how the invisible `enabled:false` trap is
  minted. The client deliberately writes NOTHING to the server; artists flip
  `sync_enabled` themselves on the Site Settings page (or an admin does via
  the API with `x-as-user`).
- Synthesis must stay behind the `enabled` checks and behind the
  degenerate-pair check.
- `get_active_site_type` and `get_remote_site` must stay consistent - if one
  synthesizes, both must.

## Fixed on `luma` (`1.3.1+ls.0.1.0`): failure isolation & silent-trap fixes

All still present upstream — a merge can reintroduce any of them.

1. **One bad project/site killed sync for everyone.** `sync_loop`'s catch-all
   called `self.stop()`; `_working_sites` indexed `sync_config.get("sites")[site]`
   directly (KeyError for a site missing from `sites` settings); rclone's
   handler raises from `__init__` on bad config. Any of these stopped the whole
   thread, silently, for all projects. Now: per-project try/except in
   `sync_loop` (continue to next project), `.get()` + warn-once for missing
   site configs, provider construction guarded (degrades to "site not
   working"), and the catch-all logs + sleeps 30s + retries instead of
   stopping. **Rule: the sync thread must never stop itself** — a dead-silent
   stopped thread is the worst outcome; keep new failure paths inside the
   per-project containment.
2. **`local_drive.is_active()` was hardcoded `True`.** A machine with the
   studio share unmounted (VPN down) counted as working and every transfer
   failed file-by-file. Now: studio site requires roots to **exist** (mount
   points are never created), non-studio sites require roots to be
   **creatable** (`makedirs exist_ok`). Results cached 30s per
   (project, site) — handler construction happens on hot paths.
3. **Unpause was broken**: `status_entity.remove("pause")` — dicts have no
   `.remove` — now `.pop("pause", None)` (`update_db`).
4. **`validate_project` used `repre_file["_id"]`** (KeyError; AYON uses
   `"id"`). Method is dormant but is the "adopt files already on disk"
   feature; fixed ahead of wiring it to the tray menu.
5. **"Remove from local" rmdir traceback** (§1b below): `_remove_local_file`
   now warns and continues when the empty-folder `rmdir` fails; `os.remove`
   failures still raise.
6. **Silent 403 project drops** (settings unreadable → project skipped at
   debug level) are now deduplicated **warnings**
   (`_warn_missing_permission`).
7. **Server roll-up masked live transfers**: `get_overal_status` now ranks
   IN_PROGRESS above FAILED — a representation reads FAILED only once nothing
   is still moving.

## Fixed on `luma`: manual transfers waited out `loop_delay`

**Fixed in `1.3.1+ls.0.0.4`. Still present upstream — a merge can reintroduce it.**

Clicking **Download**/**Upload** in the Loader or Scene Inventory created the site
record but did not wake the sync loop, so nothing moved for up to `loop_delay`
(60s default). Same for the upload after a publish. `reset_timer()` existed but its
**only** caller was the launch hook (`sitesync.py:309`) — `add_site` never called
it.

**Correction (`1.3.1+ls.0.1.0`):** the claim that the REST wake "already worked
cross-process" was **wrong**. `_reset_timer_with_rest_api` POSTs
`{AYON_WEBSERVER_URL}/sitesync/reset_timer`, but the addon never implemented
`webserver_initialization(server_manager)` — the only hook core's tray webserver
uses to let addons register routes (`tools/tray/webserver/server.py::
connect_with_addons`). The POST was a silent **404** (`requests.post` doesn't
raise on it), so from a DCC publish or the launch hook the wake did nothing and
transfers still waited out `loop_delay`. Only the in-tray direct call worked.
Fixed in `+ls.0.1.0`: `SiteSyncAddon.webserver_initialization` registers
`POST /sitesync/reset_timer` (calling `sitesync_thread.reset_timer()` directly —
NOT `self.reset_timer()`, which would recurse into the POST when the thread is
absent), `_reset_timer_with_rest_api` now logs non-2xx responses, and
`SiteSyncThread.reset_timer` cancels the timer via `loop.call_soon_threadsafe`
because callers live in foreign threads (tray UI, webserver route).

**Fix:** `add_site` now calls `self.reset_timer()` after writing the state.

```python
if self.sitesync_thread is None:
    self._reset_timer_with_rest_api()   # in a DCC -> POST to the tray's webserver
else:
    self.sitesync_thread.reset_timer()  # in the tray -> cancel the wait directly
```

**Required companion fix — do not drop it.** `_reset_timer_with_rest_api` had an
unguarded, timeout-less `requests.post(rest_api_url)`. Since `add_site` runs during
publish (`integrate_site_sync` calls it per representation), an unreachable tray
webserver would have raised `ConnectionError` straight through `add_site` and
**failed the publish**; a hung tray would have blocked it forever. The POST is now
`timeout=2` inside `try/except` — resetting a timer is best-effort and must never
break its caller. Verified by stubbing `requests.post` to raise: the call survives
and logs a warning.

**Rule:** anything called from `add_site` must be non-fatal. It sits on the publish
path.

No-code alternative to this fix, if it is ever reverted: lower
`sitesync → config → Loop Delay`, at the cost of constant polling from every tray.

### Related: live progress % — CORRECTED (was wrong until the audit-fix batch)

An earlier version of this section claimed `local_drive._mark_progress()`
"writes a real 0–1 fraction to the DB". **That was false**: the client
posted a `progress` key, but the server's `SyncStatusPostModel` had no such
field and the POST handler copied only timestamp/status/size/message/retries
— the fraction was silently dropped, upstream and here. Every progress UI
was file-count granularity at best; a single-file repre read 0% until done.

Fixed in the audit-fix batch (unreleased): `SyncStatusModel.progress`
(`server/settings/models.py`) now persists the fraction (stored only while
IN_PROGRESS, dropped on any other status) and the `/state` endpoint returns
it per file, so `_mark_progress`'s 5s posts are finally real end-to-end.
**Requires server AND client from the same build** — an old server drops
the field again (harmlessly). For both directions:

```python
side = "local"
if direction == "Upload":
    side = "remote"
```

So **Download animates the *Active site* column, Upload the *Remote site*
column** — one refresh updates both; no separate window is needed.

What still doesn't display it: the **core UI never re-asks**. Scene Inventory
refreshes once via `view.data_changed` (fired right after the click, *before*
any bytes move) or the manual Refresh button. There is no poll timer.

**The Manager and Loader are `ayon-core` tools**, not sitesync — sitesync only
supplies the data. So a poll timer (~5s while anything is in flight, stopped when
settled) is a **core** change and would mean forking core's UI. Deferred: the
practical value is mostly on Download (the user is watching); Upload happens during
publish with the window usually closed.

Note `1.3.1+ls.0.0.3`'s accumulator fix is what makes a climbing % meaningful — before
it, a part-synced representation reported 0%.

## Fixed on `luma`: Scene Inventory showed 0% and "Download" silently no-opped

**Fixed in `1.3.1+ls.0.0.3`. Still present upstream — a merge can reintroduce it.**

Symptom: in Nuke's Scene Inventory (Manager), representations that *are* on
studio showed **Active 0% / Remote 0%**, and right-click → **Download** did
nothing, with no error. The Loader (standalone browser) worked fine.

`get_repre_sync_state` refuses to return state when the **local** site has no
record:

```python
if repre_state["localStatus"]["status"] != -1:   # -1 == NA (no local record)
    return repre_state
# else -> None
```

`_get_progress_for_repre_new` then hit `if not sync_status: return {local: -1,
remote: -1}` and the UI rendered **0%/0%** — discarding the *remote* progress
purely because the *local* side was empty.

That created a **catch-22** with the Manager's guard
(`ayon-core/tools/sceneinventory/models/sitesync.py`):

```python
check_progress = repre_progress["remote_site"]   # the OPPOSITE site
if check_progress == 1:                          # must be exactly 100%
    sitesync_addon.add_site(project_name, repre_id, site, force=True)
```

- Download needs remote == 100%
- remote only reads 100% once the **local** record exists
- the local record only exists **after** a download

⇒ Manager Download could never work for anything not already local, and failed
silently. The Loader was unaffected because it uses
`get_representations_sync_state` / `get_version_availability`
(the `/state/representations` endpoint, no `-1` filter) and calls
`add_site(force=True)` unguarded.

**Fix:** `_get_progress_for_repre_new` now calls `_get_repres_state` directly
instead of `get_repre_sync_state`.

**Rule:** do NOT "simplify" that back to `get_repre_sync_state`, and do NOT
remove the `!= -1` filter globally. Four other callers (`add_site`,
`remove_site`, `is_representation_on_site`, the alternate-site update) pass a
single site and **rely** on None-when-absent as their "does this site have a
record?" test. Only the progress path needs the exemption.

Also fixed alongside: the accumulator branch did `progress[site_name] = 0`,
which **reset** rather than preserved, so one unsynced file zeroed a whole
representation's percentage. It now keeps `norm_progress` — a 13-of-26 sync
reads 50%, not 0%.

Do **not** patch core's Manager guard: with progress correct, it passes on its
own.

## Fixed on `luma`: studio-level `enabled` vetoed project overrides

**Fixed in `1.3.1+ls.0.0.2`. Still present upstream — a merge can reintroduce it.**

`compute_resource_sync_sites` used to gate on the **raw studio-level** setting as
well as the project one:

```python
if (not self.sync_studio_settings["enabled"]                     # unresolved studio fetch
    or not self.sync_project_settings[project_name]["enabled"]): # already-resolved
    return [create_metadata(self.DEFAULT_SITE)]                  # marks ONLY studio=OK
```

`sync_project_settings` comes from `get_addon_project_settings(...)`, whose
`use_site=True` default means it is **already resolved through studio → project →
site**. `sync_studio_settings` is a *separate, unresolved* fetch
(`get_studio_settings()`), so AND-ing it let a studio `false` veto a project
`true`:

| studio | project (resolved) | correct | old behaviour |
|---|---|---|---|
| True | True | on | on |
| True | False | off | off |
| False | False | off | off |
| **False** | **True** | **on** | **off — the bug** |

The check only ever changed the one row where an override is doing its job. With
studio `false` + project `true`, every publish marked **only `studio=OK`** and no
local record, so nothing synced in either direction — silently. It was also
inconsistent with `get_active_site_type`, which checks only the resolved value.

**Rule:** never re-check `sync_studio_settings["enabled"]`. The resolved project
value is the whole hierarchy. Re-adding it breaks *"sitesync off studio-wide, on
for one pilot project"*, which is the normal rollout shape.

### Symptom to recognise

A publish produces a single site entry (`studio=OK`) and no local record. The
loop is then blind to it, because it only ever matches two exact pairs:

```python
upload:   local=OK      remote=QUEUED
download: local=QUEUED  remote=OK
```

`NA (-1)` is neither, so the representation is invisible forever — waiting does
not help. Status codes: `-1 NA · 0 IN_PROGRESS · 1 QUEUED · 2 FAILED · 3 PAUSED ·
4 OK`.

### Not fixed by this (by design)

Farm renders publish with the farm's own `active=studio`, so they mark
`studio=OK` only and never auto-download to an artist. Pull via the Manager/Nuke
loader (which creates the local record), or configure `always_accessible_on`.

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

## How local sites resolve paths (read this before changing site config)

**Residency is per-ROOT, not per-asset.** `local_setting.local_roots` is a list of
*root name → local path*. Only the roots listed there are remapped:

- `get_site_root_overrides` returns overrides **only** for `site_name == "local"`,
  built from `local_roots`. `studio` returns `{}` — studio roots come from the
  project Anatomy (Roots tab).
- `ayon-core` `host/dirmap.py::_get_local_sync_dirmap` builds the workfile path
  remap from exactly those entries (`W:/…` → `C:\WORK_LOCAL\…`).

**LumaRND has a single `work` root, and BOTH templates hang off it:**

```
roots:   work -> W:
work    : {root[work]}/{project}/{hierarchy}/{folder}/work/{task}
publish : {root[work]}/{project}/{hierarchy}/{folder}/publish/{product[type]}/...
```

So overriding `work` moves the **work area** as well as publishes. With
`active_site = local` everything under it resolves to `WORK_LOCAL`, and anything
not downloaded is simply absent.

**sitesync only ever syncs published representations.** Work-area files are not
representations, so studio WIP scenes can never be fetched. This is by design,
not a missing feature.

**Consequences with a single root + `active_site = local`:**

- create → save → publish → auto-upload: works.
- studio work area (WIP scenes): unreachable, permanently.
- any dependency not downloaded: missing (audio, plates, caches). See the
  CollectAudio note in `ayon-core/CLAUDE.md` — *anything resolved through anatomy
  on a local site can be absent*. Audio is just the first case people hit.

Mixed residency ("scene over VPN, heavy sim local") is only expressible with
**multiple roots** — e.g. `work` stays on `W:`, a `cache` root is routed via
publish templates and listed in `local_roots`. **Untested**: the sync loop's
behaviour for representations under a non-overridden root has not been verified.

### The workfile bridge: `CopyLastPublishedWorkfile`

`launch_hooks/pre_copy_last_published_workfile.py` (`order = -1`) seeds a task
from the last **published** workfile. It verifies the repre is on the remote
site, adds it *plus its `reference`-linked representations* to the local site,
calls **`reset_timer()`** (so this path does not wait out `loop_delay`), blocks
until it lands, `shutil.copy`s it into the work area at **`version + 1`**, and
sets `data["last_workfile_path"]`.

Selection matches the task, not just the folder:

```python
get_products(project_name, folder_ids={folder_id}, product_base_types={"workfile"})
versions = get_last_versions(project_name, product_ids)
version_ids = {v["id"] for v in versions.values() if v["taskId"] == task_id}
# then first representation whose ext is in host_addon.get_workfile_extensions()
```

**Conditions — all must hold:**

- **Local work area empty for that task** (`if os.path.exists(last_workfile):
  return`). It is a *first-workfile seeder*, not a sync. Once a local workfile
  exists it never fires again — holding a local v001 while a colleague publishes
  v005 means launch silently opens **your v001**.
- The published workfile must already be **on studio**.
- **Launch blocks** while downloading.
- **Host must be in `app_groups`:** blender, photoshop, tvpaint, aftereffects,
  nuke, nukeassist, nukex, hiero, nukestudio, maya, harmony, celaction, flame,
  fusion, houdini. **Not** resolve, unreal, substancepainter, substancedesigner,
  motionbuilder, gaffer, openrv, premiere — on a local site those have **no
  bridge at all**.

### Bug: `use_last_published_workfile` is a dead setting

`core → tools → Workfiles → last_workfile_on_startup` exposes
`use_last_published_workfile` (core `server/settings/tools.py`), but it is read
**nowhere** in core's client. The hook gates on the wrong function:

```python
from ayon_core.pipeline.workfile import should_use_last_workfile_on_launch
use_last_published_workfile = should_use_last_workfile_on_launch(...)
if use_last_published_workfile is False:
    return
```

…and that returns `matching_item.get("enabled")`. No
`should_use_last_published_workfile_on_launch` exists. **So the toggle does
nothing** and the hook runs whenever *"open last workfile on startup"* is true.
An empty scene therefore means the task had no published workfile matching its
`taskId` + extension — not that the feature is off. Candidate upstream PR.

---

## Possible Future Work

### Deferred from the 2026-07-17 full audit (read before extending sitesync)

The full audit (see the "full-audit fix batch" section above for what WAS
fixed) deliberately left seven items unimplemented. **2026-07-18 update:
A, C, D, E and G have since been implemented entirely addon-side** — see
*"deferred-audit items A/C/D/E/G"* near the top of this file for what
shipped and the rules that came with it. Only B and F below remain
deferred; both are core-fork UI edits.

**~~A. Loader dependency-pull for non-workfile products~~ — IMPLEMENTED
2026-07-18** inside the addon's `add_site` (core calls it before its
workfile-only guard, so no core change was needed after all; follows
`reference` + `generative` inputs to `auto_download_link_depth`). Still
open from the original notes: *link existence* — links only exist if the
DCC addon populated `loadedVersions`/`inputVersions` at publish, so
**check one real production USD publish for links**
(`GET /versions/{id}/links`); if absent, it is a publish-side collection
problem in the USD plugins and no amount of link-following helps.

**B. Live-progress poll timer in core's Manager/Loader.** The audit batch
made the per-file 0-1 fraction real in the DB for the first time, but
core's Scene Inventory/Loader still fetch availability exactly once (on
click / manual Refresh). A ~5s QTimer while anything reads IN_PROGRESS,
stopped when settled, is a small self-contained change in the core fork;
sitesync needs nothing more. The tray control panel already polls, so
artists have one place to watch transfers meanwhile.

**~~C. Local retention / GC of downloaded data~~ — IMPLEMENTED
2026-07-18** as the conservative shape this note asked for: the control
panel's "Clean up superseded versions..." lists candidates with size
totals and deletes only on confirmation. An automatic policy remains
undesigned — do not add one until the manual action has earned trust.

**~~D. Priority is schema-deep but wired to nothing~~ — IMPLEMENTED
2026-07-18**: `SortByEnum.priority` + ORDER BY, loop fetch sorted
descending, launch-hook workfile downloads post 99, control-panel
per-row "Set transfer priority...".

**~~E. Farm renders never reach an artist's local site~~ — IMPLEMENTED
2026-07-18** as the preferred pull-on-demand variant: the control
panel's "Download my renders" follows `generative` output links from
the latest published workfile versions of the user's tasks. The
automatic default-off variant was deliberately NOT built (renders are
the heaviest data; auto-pulling could saturate VPN + disks).

**F. Published-tab discoverability** (see § 1c below): the colleague's
workfile is visible-but-inert (`Qt.NoItemFlags` in core's
`files_widget_published.py`), no download action, no hint the Loader can
fetch it. Minimum: tooltip. Proper: selectable-when-unavailable +
Download action (`add_site` + poll). Both are core-fork edits.

**~~G. Backfill for repres created outside the normal publish~~ —
IMPLEMENTED 2026-07-18** as the endpoint variant:
`POST /{project}/state/backfill` stamps `studio=OK` on zero-record
representations, triggered once per project per tray session by the
sync thread. The "clean fix" (a server-side event handler reacting to
representation creation) would need a sitesync addon *service* — still
possible without touching core, but new infrastructure; revisit only if
the once-per-session cadence proves too slow in practice.

**Deliberately untouched smalls:** `update_db`'s dead `pause`-key POST
(harmless, removing it grows the upstream diff); alternative-site
order-dependency when a site is alt-paired to BOTH active and remote
(a misconfiguration); the multi-root caching model (still untested,
still the only shape matching Luma's actual goal - an anatomy/pilot
decision, not addon code; see the Standing verdict below).

### 0. Read this first: local site targets a *remote* artist, not a *hot-desking* one

Investigated 2026-07-16 while piloting `active_site = local` over VPN. This
reframes everything below it.

**The intended flow is NOT "download a published workfile, then open it."** It is:
**launch the task**. `CopyLastPublishedWorkfile` seeds an *empty* work area from
the last published workfile automatically — download, copy to `version + 1`, open.
Opening an empty scene and loading via the DCC's Workfiles → Published tab is a
fallback that happens to work, not the design. Nobody should be told to do that.

**Two genuine gaps, both narrow:**

1. **The launcher's Workfiles page is a dead end on a local site.**
   `tools/launcher/models/workfiles.py::get_workfile_items`:

   ```python
   for workfile_entity in ayon_api.get_workfiles_info(project_name, task_ids={task_id}, ...):
       path = anatomy.fill_root(workfile_entity["path"])   # ACTIVE site's root
       exists = os.path.exists(path)                       # False -> greyed
       version = workfile_data.get("version")              # version IS in data
   ```

   It lists work-area entities, resolves them against the **local** root, finds
   nothing, and greys them — **with no action attached, no published tab, no
   download button**. It shows studio WIP files that can never be opened.

2. **The hook only seeds an empty work area.** Once any local workfile exists for
   the task it never fires again, so a colleague's newer published version cannot
   be picked up at launcher level — only via the in-DCC Published tab.

**The conclusion that matters.** sitesync's local site assumes the artist has **no
access to the studio share**: pull everything down, work locally, publish back.
Published workfiles are the transfer medium *between* sites — which is exactly why
they are the only syncable workfile form, and why work-area files are not synced.

Luma's actual goal is different: **VPN-connected with `W:` reachable, wanting to
cache only heavy data (e.g. sim) locally while scene + small assets stay on the
share.** That is a *caching* problem being solved with a *remote-artist* feature,
which is why every step fights back — greyed workfiles, missing audio, manual
downloads, the CollectAudio failure. None of those are misconfiguration; they are
all the same assumption showing through.

**The only shape in AYON that expresses the actual goal is multi-root:** `work`
stays on `W:` (launcher works normally, nothing greys, scene opens over VPN) and a
separate cache-style root — routed via publish templates — is listed in
`local_roots`. **Untested.** Verify the sync loop's behaviour for representations
under a non-overridden root before committing an anatomy change to it.

Also relevant to any "coordinate local workfile versions via the DB" idea:
`data.version` and `data.host_name` are **already** stored on workfile entities, and
paths are rootless — so that coordination is not missing. Only file transfer is.

**Next test (cheap, decides a lot):** on a task that *has* a published workfile,
with an *empty* local work area, just launch Maya/Nuke. If it opens seeded from the
published workfile, the feature works as designed and the remaining friction is
purely wrong-fit. If it does not, there is another bug to chase.

### Standing verdict (2026-07-17, after a full day of piloting)

**The fixes make sitesync work correctly. They do not make it the right tool for
this job.**

Five upstream bugs were found, fixed and shipped (`+ls.0.0.1` → `+ls.0.0.4`), and
sitesync now behaves as designed: downloads, uploads, publishes and sharing all
work. What remains is **fit**, not defects — and it is one mismatch surfacing
repeatedly, not a list of unrelated problems:

| symptom | same root cause |
|---|---|
| greyed-out workfiles in the launcher | work area moved to the local root; work-area files are never synced |
| review publish failed on missing audio | anything resolved through anatomy on a local site can be absent |
| downloads must be requested by hand | only published representations sync, and only on request |
| sharing needs 3 tools (§ 1c) | published representations are the *only* transfer medium between sites |

All of it follows from one assumption: **the local site targets a *remote* artist
with no access to the studio share** — pull everything down, work locally, publish
back. Luma's actual goal is a *caching* problem: VPN-connected, `W:` reachable,
wanting only heavy data (e.g. sim caches) local while the scene and small assets
stay on the share.

**Multi-root remains the only shape in AYON that expresses the actual goal**
(`work` stays on `W:`; a cache-style root routed via publish templates and listed
in `local_roots`) — and it is **untested**. Verify sync-loop behaviour for
representations under a non-overridden root before committing an anatomy change.

Revisit this section before further investment. The deferred items below are worth
doing *if* the model is kept; none of them changes the fit.

### 1. ~~`reset_timer()` is not wired to manual actions~~ — FIXED in `1.3.1+ls.0.0.4`

See *"Fixed on `luma`: manual transfers waited out `loop_delay`"* below.

### 1b. ~~"Remove from local" raises on a cosmetic `rmdir` failure~~ — FIXED in `1.3.1+ls.0.1.0`

Reported 2026-07-17 on `1.3.1+ls.0.0.4`, removing a representation from the local
site via the Loader:

```
File "ayon_sitesync\addon.py", line 2109, in _remove_local_file
    os.rmdir(folder)
PermissionError: [WinError 5] Access is denied:
    'C:\Users\<user>\OneDrive - Luma\Desktop\WORK_LOCAL/LumaRND/.../renderLookdevTest/v005'
...
ValueError: folder ... cannot be removed
```

Files were deleted; the empty folder remained; the artist got a traceback.

**Not data-corrupting.** `remove_site` deletes the server-side site record
**before** touching any file, so the DB is already correct when this blows up —
the representation simply reads as "not on local", which is true. Re-download
works.

**The design bug** — `_remove_local_file`:

```python
folder = os.path.dirname(local_file_path)
if os.listdir(folder):      # not empty -> skip
    continue
try:
    os.rmdir(folder)
except OSError:
    msg = "folder {} cannot be removed".format(folder)
    self.log.warning(msg)
    raise ValueError(msg)   # <-- aborts the whole removal
```

Deleting the now-empty directory is **incidental cleanup**, but a failure
`raise`s. Two consequences:

1. An alarming traceback for an operation that actually succeeded.
2. The `raise` aborts the loop — a representation whose files span **several**
   folders would be **partially deleted** if an early folder's `rmdir` fails.
   (Not the case above: `rmdir` only runs once a folder is empty, i.e. after its
   last file, so that single-folder repre was fully removed.)

**Deferred fix:** log the warning and continue; never raise. The directory is not
the point of the operation. (`os.remove` failing *should* still raise — that is
the actual work.)

**Root cause of the `rmdir` denial — the local root is inside OneDrive.**
`WinError 5` (access denied, *not* `145` "directory not empty") on an empty
folder means a handle is held or the ACL forbids it. The path is
`C:\Users\<user>\OneDrive - Luma\Desktop\WORK_LOCAL\…` — **OneDrive is syncing
the sitesync local root**.

**A OneDrive-backed folder is a poor choice for a sitesync local root**, quite
apart from this bug:

- OneDrive holds handles on folders it is syncing → exactly this `rmdir` denial,
  and it will be intermittent/timing-dependent.
- Everything sitesync pulls down gets **re-uploaded to the cloud** — renders and
  caches included. Double sync, double bandwidth, and potentially enormous
  OneDrive usage.
- Files On-Demand can leave placeholders (dehydrated files) where sitesync
  expects real bytes, so `os.path.exists` can be true while a read stalls or
  fails.

Recommend a plain local path outside any cloud-synced tree (e.g.
`C:\ayon_local\…`) before drawing conclusions from further local-site testing —
some flakiness may be OneDrive, not sitesync.

### 1c. Sharing work on a local site: possible, but heavily obfuscated

**Verified working 2026-07-17.** A local-site artist *can* pick up a colleague's
studio-published workfile — so this does not rule out the model. But it takes
**three tools**, none of which signposts the next, and no artist will find it
unaided.

**The path:**

1. **Loader** → the colleague's `workfile` product → **Download**
2. wait for sync (near-instant since the `reset_timer` fix in `+ls.0.0.4`)
3. **Workfiles → Published tab** → the item becomes selectable → **Copy & Open**

**Why step 1 is the magic one** — `ayon-core`
`tools/loader/models/sitesync.py::_add_site` special-cases workfile products,
pulling the scene **and its dependencies**:

```python
# TODO this should happen in site sync addon     <- upstream's own comment
if product_type != "workfile":
    return
links = self._get_linked_representation_id(project_name, repre_entity, "reference")
for link_repre_id in links:
    ...add_site(project_name, link_repre_id, site_name, force=True)
```

That code exists *only* to enable this workflow — from a tool most artists would
never associate with opening a workfile.

**Why it looks impossible** — `ayon-core`
`tools/workfiles/widgets/files_widget_published.py`:

```python
if file_item.available:        # available = os.path.exists(filepath)
    flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
else:
    flags = Qt.NoItemFlags     # visible, inert: no select, no click, no tooltip
```

The colleague's workfile is **visible and completely inert**. There is **no
download action anywhere in the Workfiles tool**, and nothing pointing at the
Loader. Add the launch hook refusing to fire once any local workfile exists
(§ *"How local sites resolve paths"*), and every obvious door is locked — the
unlocked one is in another tool.

**Deferred fix options** — both are `ayon-core` **UI** changes (forking core's UI,
same bucket as the live-% poll timer):

- Published tab: allow selecting an unavailable item and offer **Download**
  (`add_site` + poll until available), or
- minimum viable: a tooltip/status — *"not on your site — download it from the
  Loader"*.

**The real defect is discoverability, not capability.** Worth keeping straight:
that is a tooltip-sized problem, not a blocker.

### 2. Opening existing (unpublished) workfiles on a local site

Investigated 2026-07-16. **Most of the coordination already exists** — the gap is
only file transfer:

- **Workfile entities are already server-side, keyed by ROOTLESS path**
  (`save_workfile_info(..., rootless_path)`, `find_workfile_rootless_path` in
  `ayon_core/pipeline/workfile/utils.py`). A studio `W:/…/scene_v001.ma` and a
  local `C:\WORK_LOCAL\…\scene_v001.ma` are the **same record**, so cross-site
  version coordination is already done — saving writes the entity.
- **The Workfiles tool lists from the server, not disk**, then greys by local
  existence (`tools/workfiles/models/workfiles.py`):

```python
workfile_entities = list(ayon_api.get_workfiles_info(...))   # server
exists = os.path.exists(filepath)                            # local -> greyed
```

  So studio workfiles **are** listed for a local-site artist; they are greyed
  because the bytes are absent, not because they are unknown.
- **The published-workfiles tab already exists and is availability-aware.**
  `get_published_file_items()`; `PublishedWorkfileInfo` carries
  `representation_id` **and `available: bool`** ("True if workfile is available on
  the machine"); `copy_workfile_representation()` copies one into the work area as
  a new version. Published workfiles are the **only** syncable workfile form — so
  they are worth keeping.

**Hard constraint:** sitesync cannot sync work-area files. It is
representation-keyed throughout (`sitesync_files_status` = `representation_id` +
`file_id`); a work-area file has no representation to hang a site record on.

**Options, cheapest first:**

1. **Use what exists** — publish workfiles; pull the workfile representation via
   the Loader; Workfiles → Published tab → *Copy & Open*. Zero code. Test whether
   this is usable before building anything.
2. **New launch hook: copy latest work-area file studio → local.** ~150 lines,
   mirroring `CopyLastPublishedWorkfile`: `ayon_api.get_workfiles_info(task_id)` →
   pick latest by rootless path → resolve studio + local paths from anatomy → copy
   if the local is missing → set `last_workfile_path`. **No sitesync involvement**
   — a plain copy between two reachable roots. Only valid while the studio root is
   reachable (VPN); useless for a genuinely remote artist.
3. **Teach sitesync to sync work-area files.** Large: needs a parallel mechanism
   (server tables, endpoints, UI, loop logic) since nothing is representation-keyed.
   A new feature and a permanent upstream divergence. Not recommended.

### 3. Extend the launch hook's `app_groups`

Roughly half our DCCs (resolve, unreal, substancepainter, substancedesigner,
motionbuilder, gaffer, openrv, premiere) are absent from the hook's `app_groups`,
so on a local site they get an empty scene with no bridge. Adding them is a
one-line list change; whether the hook actually *works* for each host is unverified.

## Commit Conventions

```
<type>(<scope>): <summary>
```

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `build`, `ci`, `perf`.
