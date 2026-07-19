"""All-in-one Site Sync control panel, opened from the tray menu.

Grew out of the queue-only window (`tray_queue_window.py`). One window
gives artists everything except root configuration (which stays in Site
Settings):

- controls bar: sync now, pause syncing, auto-download new work,
  adopt existing local files, open the web status page
- "Queue" tab: queued / in-progress / failed / paused representations
  across every enabled project, with live progress and per-row actions
- "All files" tab: every representation the server tracks for the
  active/remote site pair of one project, filterable by status and
  folder/product name, paged, with the same per-row actions

Both tabs re-poll on their own while the window is visible (the
transfer loop writes real progress to the DB every ~5s) and stop when
hidden - there is no Refresh button to press. Fetches and actions run
in worker threads with results marshalled back through Qt signals;
keep it that way, REST on the UI thread freezes the tray.

Only imported from the tray process (Qt available).
"""
import threading
import time
from datetime import datetime

from qtpy import QtWidgets, QtCore, QtGui

import ayon_api

from ayon_core.lib import get_local_site_id

from .machine_role import get_machine_pref
from .utils import SiteSyncStatus

_QUEUE_REFRESH_MS = 4000
_FILES_REFRESH_MS = 8000
_SEARCH_DEBOUNCE_MS = 400
_QUEUE_PAGE_LENGTH = 100
# Upper bound of pages fetched per side per poll - only bounds a
# pathological backlog (500+ active rows per side).
_QUEUE_MAX_PAGES = 5
_FILES_PAGE_LENGTH = 200
# pages scanned per project by the superseded-versions cleanup
# (200 rows each - only bounds a truly enormous local footprint)
_CLEANUP_MAX_PAGES = 50

_ACTIVE_STATUSES = (
    SiteSyncStatus.IN_PROGRESS,
    SiteSyncStatus.QUEUED,
    SiteSyncStatus.FAILED,
    SiteSyncStatus.PAUSED,
)

_STATUS_LABELS = {
    SiteSyncStatus.NA: "N/A",
    SiteSyncStatus.IN_PROGRESS: "In progress",
    SiteSyncStatus.QUEUED: "Queued",
    SiteSyncStatus.FAILED: "Failed",
    SiteSyncStatus.PAUSED: "Paused",
    SiteSyncStatus.OK: "Synced",
}

_STATUS_COLORS = {
    SiteSyncStatus.IN_PROGRESS: "#539bf5",
    SiteSyncStatus.QUEUED: "#c69026",
    SiteSyncStatus.FAILED: "#e5534b",
    SiteSyncStatus.PAUSED: "#768390",
    SiteSyncStatus.OK: "#57ab5a",
}

# combo entries of the "All files" status filter; None == no filter.
# N/A is deliberately absent: an N/A side has no DB row at all, so the
# endpoint's 'status IN (...)' condition can never match it.
_FILTER_STATUSES = (
    ("All statuses", None),
    ("Failed", SiteSyncStatus.FAILED),
    ("Queued", SiteSyncStatus.QUEUED),
    ("In progress", SiteSyncStatus.IN_PROGRESS),
    ("Paused", SiteSyncStatus.PAUSED),
    ("Fully synced", SiteSyncStatus.OK),
)


def _format_size(size):
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return "{:.0f} {}".format(size, unit)
            return "{:.1f} {}".format(size, unit)
        size /= 1024.0


def _side_label(status_entity):
    """Status label of one side, with a live % while transferring."""
    status = status_entity["status"]
    label = _STATUS_LABELS.get(status, str(status))
    if (
        status == SiteSyncStatus.IN_PROGRESS
        and status_entity.get("totalSize")
    ):
        label = "{} {}%".format(
            label,
            int(
                100 * (status_entity.get("size") or 0)
                / status_entity["totalSize"]
            )
        )
    return label


def _describe(repre):
    """Direction, status label, progress % and message for a queue row."""
    local = repre["localStatus"]
    remote = repre["remoteStatus"]
    local_status = local["status"]
    remote_status = remote["status"]

    # the side that still has work is the one being written to
    if local_status in _ACTIVE_STATUSES:
        active, direction = local, "Download"
    elif remote_status in _ACTIVE_STATUSES:
        active, direction = remote, "Upload"
    else:
        active, direction = local, ""

    status = active["status"]
    label = _STATUS_LABELS.get(status, str(status))
    progress = None
    if status == SiteSyncStatus.IN_PROGRESS and active.get("totalSize"):
        progress = int(
            100 * (active.get("size") or 0) / active["totalSize"]
        )
    message = active.get("message") or ""
    return direction, label, progress, message, status


def _make_row(project_name, local_site, remote_site, repre):
    return {
        "project": project_name,
        "local_site": local_site,
        "remote_site": remote_site,
        "repre": repre,
        "repre_id": repre["representationId"],
        "local_status": repre["localStatus"]["status"],
        "remote_status": repre["remoteStatus"]["status"],
        "label": "{} / {} (v{:03d}) / {}".format(
            repre.get("folder") or "",
            repre.get("product") or "",
            repre.get("version") or 0,
            repre.get("representation") or "",
        ),
    }


def _collect_queue_rows(addon):
    """Gather active/failed representations across enabled projects.

    Runs in a worker thread. The endpoint ANDs local and remote status
    filters, so 'active on either side' needs one call per side, merged
    by representation id.

    Returns:
        tuple[list, bool]: (rows, had_error). A fetch failure MUST be
            distinguishable from a genuinely empty queue - an empty
            result caused by an unreachable server used to render as
            "everything is in sync".
    """
    rows = []
    had_error = False
    for project_name in addon.get_enabled_projects():
        try:
            local_site = addon.get_active_site(project_name)
            remote_site = addon.get_remote_site(project_name)
            if not local_site or local_site == remote_site:
                continue

            merged = {}
            for side in ("local", "remote"):
                # Page until a short page (via the shared pager) so a
                # busy queue isn't silently truncated; the page cap only
                # bounds a pathological backlog. Per-side containment:
                # one failing side still renders the other.
                kwargs = {
                    "localSite": local_site,
                    "remoteSite": remote_site,
                    "{}StatusFilter".format(side): list(_ACTIVE_STATUSES),
                }
                try:
                    side_rows = _fetch_state_pages(
                        addon, project_name,
                        _QUEUE_MAX_PAGES, _QUEUE_PAGE_LENGTH,
                        **kwargs
                    )
                except Exception:
                    had_error = True
                    addon.log.warning(
                        "Sync control: state fetch of '{}' failed".format(
                            project_name),
                        exc_info=True
                    )
                    continue
                for repre in side_rows:
                    merged[repre["representationId"]] = repre

            for repre in merged.values():
                rows.append(
                    _make_row(project_name, local_site, remote_site, repre)
                )
        except Exception:
            had_error = True
            addon.log.warning(
                "Sync control: couldn't fetch state of '{}'".format(
                    project_name),
                exc_info=True
            )
    return rows, had_error


def _collect_file_rows(addon, project_name, page, status, search_text):
    """One page of tracked representations of one project.

    Runs in a worker thread. Status/search need "either side" / "either
    name" semantics the endpoint cannot express in one call (filters are
    ANDed), so up to four calls are merged by representation id - except
    "Fully synced", which really is local AND remote OK in one call.
    Paging over merged calls is per-call, so a page can hold up to
    (calls x pageLength) rows; that is fine for a browsing UI.

    Returns:
        tuple[list, bool]: (rows, had_error) - see '_collect_queue_rows'.
    """
    local_site = addon.get_active_site(project_name)
    remote_site = addon.get_remote_site(project_name)
    if not local_site or local_site == remote_site:
        return [], False

    base_kwargs = {
        "localSite": local_site,
        "remoteSite": remote_site,
        "page": page,
        "pageLength": _FILES_PAGE_LENGTH,
    }

    if status == SiteSyncStatus.PAUSED:
        return _collect_paused_rows(
            addon, project_name, local_site, remote_site,
            base_kwargs, search_text
        )

    if status is None:
        status_variants = [{}]
    elif status == SiteSyncStatus.OK:
        status_variants = [{
            "localStatusFilter": [SiteSyncStatus.OK],
            "remoteStatusFilter": [SiteSyncStatus.OK],
        }]
    else:
        status_variants = [
            {"localStatusFilter": [status]},
            {"remoteStatusFilter": [status]},
        ]

    if search_text:
        search_variants = [
            {"folderFilter": search_text},
            {"productFilter": search_text},
        ]
    else:
        search_variants = [{}]

    merged = {}
    had_error = False
    for status_kwargs in status_variants:
        for search_kwargs in search_variants:
            kwargs = dict(base_kwargs)
            kwargs.update(status_kwargs)
            kwargs.update(search_kwargs)
            response = ayon_api.get(
                "{}/{}/state".format(addon.endpoint_prefix, project_name),
                **kwargs
            )
            if response.status_code != 200:
                addon.log.warning(
                    "Sync control: file list fetch of '{}' returned"
                    " {}".format(project_name, response.status_code)
                )
                had_error = True
                continue
            for repre in response.data.get("representations") or []:
                merged[repre["representationId"]] = repre

    rows = [
        _make_row(project_name, local_site, remote_site, repre)
        for repre in merged.values()
    ]
    rows.sort(key=_row_sort_key)
    return rows, had_error


def _row_sort_key(row):
    return (
        row["repre"].get("folder") or "",
        row["repre"].get("product") or "",
        row["repre"].get("version") or 0,
        row["repre"].get("representation") or "",
    )


def _collect_paused_rows(
    addon, project_name, local_site, remote_site, base_kwargs, search_text
):
    """Rows for the 'Paused' filter.

    The panel's own pause is session-only (in-memory - it never writes
    the PAUSED DB status), so a server-side status filter can never find
    it, and an unfiltered page would miss paused rows sorting past the
    page boundary. The session-paused ids are fetched EXACTLY (chunked
    and paged by '_get_repres_state'), then genuinely DB-paused rows of
    either side are merged in. The result set is small by nature and is
    not paged.
    """
    merged = {}
    had_error = False
    paused_ids = list(addon.get_paused_representations())
    if paused_ids:
        try:
            for state in addon._get_repres_state(
                project_name, paused_ids, local_site, remote_site
            ):
                merged[state["representationId"]] = state
        except Exception:
            had_error = True
            addon.log.warning(
                "Sync control: couldn't fetch session-paused rows",
                exc_info=True
            )

    for side_key in ("localStatusFilter", "remoteStatusFilter"):
        kwargs = dict(base_kwargs)
        kwargs["page"] = 1
        kwargs[side_key] = [SiteSyncStatus.PAUSED]
        response = ayon_api.get(
            "{}/{}/state".format(addon.endpoint_prefix, project_name),
            **kwargs
        )
        if response.status_code != 200:
            had_error = True
            continue
        for repre in response.data.get("representations") or []:
            merged[repre["representationId"]] = repre

    rows = [
        _make_row(project_name, local_site, remote_site, repre)
        for repre in merged.values()
    ]
    if search_text:
        needle = search_text.lower()
        rows = [
            row for row in rows
            if needle in (row["repre"].get("folder") or "").lower()
            or needle in (row["repre"].get("product") or "").lower()
        ]
    rows.sort(key=_row_sort_key)
    return rows, had_error


def _fetch_state_pages(addon, project_name, max_pages, page_length, **kwargs):
    """Every row of a /state query, paged until a short page.

    Runs in worker threads. Returns the RAW representation dicts from
    the endpoint (not UI rows). Raises on a non-200 so callers report a
    failed scan instead of a confidently empty one; hitting 'max_pages'
    is logged so a truncated scan never silently reads as complete.
    """
    rows = []
    for page in range(1, max_pages + 1):
        query = dict(kwargs)
        query["page"] = page
        query["pageLength"] = page_length
        response = ayon_api.get(
            "{}/{}/state".format(addon.endpoint_prefix, project_name),
            **query
        )
        if response.status_code != 200:
            raise RuntimeError(
                "State query of '{}' failed with {}".format(
                    project_name, response.status_code)
            )
        page_rows = response.data.get("representations") or []
        rows.extend(page_rows)
        if len(page_rows) < page_length:
            break
    else:
        addon.log.warning(
            "State scan of '{}' hit the {}-page cap - result is"
            " truncated".format(project_name, max_pages)
        )
    return rows


def _collect_superseded_rows(addon):
    """Locally downloaded representations superseded by a newer version.

    Runs in a worker thread. A representation qualifies only when BOTH
    sides are fully synced (the remote copy guarantees deleting the local
    bytes loses nothing) and a NEWER version of the same product AND the
    same representation name is also fully synced on this machine - a
    newer version downloaded only as 'mov' must not offer its older
    version's 'exr' twin for deletion. "Superseded" is judged against
    what the artist actually has, not against server versions never
    downloaded. Hero versions (negative numbers) are ignored entirely.
    Products are matched by product id fetched from the version entities,
    never by folder/product name, which can repeat across a hierarchy.
    """
    local_site_id = get_local_site_id()
    entries = []
    had_error = False
    for project_name in addon.get_enabled_projects():
        try:
            local_site = addon.get_active_site(project_name)
            remote_site = addon.get_remote_site(project_name)
            if local_site != local_site_id or local_site == remote_site:
                continue

            rows = _fetch_state_pages(
                addon, project_name,
                _CLEANUP_MAX_PAGES, _FILES_PAGE_LENGTH,
                localSite=local_site,
                remoteSite=remote_site,
                localStatusFilter=[SiteSyncStatus.OK],
                remoteStatusFilter=[SiteSyncStatus.OK],
            )
            if not rows:
                continue

            product_id_by_version_id = {}
            version_ids = list({row["versionId"] for row in rows})
            chunk_size = 500
            for chunk_start in range(0, len(version_ids), chunk_size):
                chunk = version_ids[chunk_start:chunk_start + chunk_size]
                for version in ayon_api.get_versions(
                    project_name,
                    version_ids=chunk,
                    fields={"id", "productId"}
                ):
                    product_id_by_version_id[version["id"]] = (
                        version["productId"]
                    )

            rows_by_key = {}
            for row in rows:
                version = row.get("version") or 0
                if version <= 0:
                    continue
                product_id = product_id_by_version_id.get(
                    row["versionId"])
                if not product_id:
                    continue
                key = (product_id, row.get("representation") or "")
                rows_by_key.setdefault(key, []).append((version, row))

            for grouped in rows_by_key.values():
                newest = max(version for version, _row in grouped)
                for version, row in grouped:
                    if version >= newest:
                        continue
                    entries.append({
                        "project": project_name,
                        "repre_id": row["representationId"],
                        "local_site": local_site,
                        "size": row.get("size") or 0,
                        "label": "{} / {} / {} (v{:03d}) / {}".format(
                            project_name,
                            row.get("folder") or "",
                            row.get("product") or "",
                            version,
                            row.get("representation") or "",
                        ),
                    })
        except Exception:
            had_error = True
            addon.log.warning(
                "Cleanup scan failed for project '{}'".format(
                    project_name),
                exc_info=True
            )
    if had_error and not entries:
        # every project failed - report a failed scan, never a
        # confidently empty "nothing to clean up"
        return None
    return entries


def _revalidate_cleanup_entries(addon, entries):
    """Keep only cleanup entries that are STILL safe to delete.

    Runs in a worker thread, right before deletion. The safety invariant
    of the cleanup is "both sides fully synced, so deleting the local
    bytes loses nothing" - re-checked here because the confirm dialog
    can sit open for any amount of time while transfers, retries or a
    republish change the picture. One batched state call per project.
    """
    by_project = {}
    for entry in entries:
        by_project.setdefault(entry["project"], []).append(entry)

    still_valid = []
    for project_name, project_entries in by_project.items():
        try:
            local_site = project_entries[0]["local_site"]
            remote_site = addon.get_remote_site(project_name)
            states = addon._get_repres_state(
                project_name,
                [entry["repre_id"] for entry in project_entries],
                local_site,
                remote_site,
            )
            ok_ids = {
                state["representationId"]
                for state in states
                if state["localStatus"]["status"] == SiteSyncStatus.OK
                and state["remoteStatus"]["status"] == SiteSyncStatus.OK
            }
            for entry in project_entries:
                if entry["repre_id"] in ok_ids:
                    still_valid.append(entry)
                else:
                    addon.log.info(
                        "Cleanup: skipping '{}' - its sync state changed"
                        " since the scan".format(entry["label"])
                    )
        except Exception:
            # can't verify -> don't delete anything of this project
            addon.log.warning(
                "Cleanup: couldn't re-validate entries of '{}' -"
                " skipping them".format(project_name),
                exc_info=True
            )
    return still_valid


class SyncControlWindow(QtWidgets.QWidget):
    """Tray-owned window bundling every artist-facing sync control."""

    _queue_done = QtCore.Signal(object)
    _files_done = QtCore.Signal(object)
    _action_done = QtCore.Signal(str)
    _projects_done = QtCore.Signal(object)
    # the one-shot buttons get dedicated completion signals so ONLY their
    # own worker re-enables them - a shared completion (e.g. a quick row
    # retry finishing) must not re-enable a button whose worker still runs
    _renders_done = QtCore.Signal(str)
    _renders_scan_done = QtCore.Signal(object)
    _cleanup_scan_done = QtCore.Signal(object)
    _cleanup_remove_done = QtCore.Signal(str)

    def __init__(self, addon, parent=None):
        super(SyncControlWindow, self).__init__(parent)
        self._addon = addon
        self._queue_fetch_running = False
        self._files_page = 1
        # Generation counters for file fetches: 'seq' advances when a
        # fetch starts, 'applied_seq' when its result lands. "A fetch is
        # in flight" is exactly seq != applied_seq - no separate boolean
        # to drift out of step. A project/filter/page change bumps seq
        # (invalidating the in-flight fetch, whose stale rows are then
        # discarded) and re-fetches immediately.
        self._files_fetch_seq = 0
        self._files_applied_seq = 0
        # action results/errors stay visible for a bit - the refresh
        # triggered right after an action used to overwrite them with
        # "Updated HH:MM:SS" within a second
        self._status_sticky_until = 0.0

        self.setWindowTitle("AYON Site Sync")
        self.resize(1000, 560)

        controls = self._build_controls_bar()

        tabs = QtWidgets.QTabWidget(self)
        tabs.addTab(self._build_queue_tab(), "Queue")
        tabs.addTab(self._build_files_tab(), "All files")
        tabs.currentChanged.connect(self._on_tab_changed)
        self._tabs = tabs

        status_label = QtWidgets.QLabel("", self)
        status_label.setAlignment(QtCore.Qt.AlignRight)
        self._status_label = status_label

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(tabs, 1)
        layout.addWidget(status_label)

        queue_timer = QtCore.QTimer(self)
        queue_timer.setInterval(_QUEUE_REFRESH_MS)
        queue_timer.timeout.connect(self._refresh_queue)
        self._queue_timer = queue_timer

        files_timer = QtCore.QTimer(self)
        files_timer.setInterval(_FILES_REFRESH_MS)
        files_timer.timeout.connect(self._refresh_files)
        self._files_timer = files_timer

        self._queue_done.connect(self._apply_queue_rows)
        self._files_done.connect(self._apply_file_rows)
        self._action_done.connect(self._on_action_done)
        self._projects_done.connect(self._apply_projects)
        self._renders_done.connect(self._on_renders_done)
        self._renders_scan_done.connect(self._on_renders_scan_done)
        self._cleanup_scan_done.connect(self._on_cleanup_scan_done)
        self._cleanup_remove_done.connect(self._on_cleanup_remove_done)

    # widget building ----------------------------------------------------
    def _build_controls_bar(self):
        sync_now_btn = QtWidgets.QPushButton("Sync now", self)
        sync_now_btn.setToolTip("Wake the sync loop immediately.")
        sync_now_btn.clicked.connect(self._on_sync_now)

        pause_chk = QtWidgets.QCheckBox("Pause syncing", self)
        pause_chk.setChecked(self._addon.is_paused())
        pause_chk.setToolTip(
            "Stop all transfers on this machine, including uploads of"
            " your own publishes, until unchecked."
        )
        pause_chk.clicked.connect(self._on_pause_clicked)
        self._pause_chk = pause_chk

        auto_chk = QtWidgets.QCheckBox("Auto-download new work", self)
        auto_chk.setChecked(bool(get_machine_pref("auto_download", True)))
        auto_chk.setToolTip(
            "Automatically download published work for your assigned and"
            " recently opened tasks. Uncheck to stop background"
            " downloads; your own publishes still upload."
        )
        auto_chk.clicked.connect(self._on_auto_download_clicked)
        self._auto_chk = auto_chk

        renders_btn = QtWidgets.QPushButton("Download my renders", self)
        renders_btn.setToolTip(
            "Queue the latest published renders of your assigned and"
            " recently opened tasks (outputs generated from their newest"
            " published workfiles) for download to this machine."
        )
        renders_btn.clicked.connect(self._on_download_renders)
        self._renders_btn = renders_btn

        cleanup_btn = QtWidgets.QPushButton(
            "Clean up superseded versions...", self
        )
        cleanup_btn.setToolTip(
            "List downloaded versions that already have a newer version"
            " on this machine and optionally delete their local files."
            " Only versions fully synced to the remote site are offered."
        )
        cleanup_btn.clicked.connect(self._on_cleanup)
        self._cleanup_btn = cleanup_btn

        adopt_btn = QtWidgets.QPushButton(
            "Adopt existing local files", self
        )
        adopt_btn.setToolTip(
            "Scan the local folder and mark files that are already"
            " present as synced, so they are not downloaded again."
        )
        adopt_btn.clicked.connect(self._on_adopt)

        web_btn = QtWidgets.QPushButton("Open web status page...", self)
        web_btn.clicked.connect(self._on_open_web)

        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(sync_now_btn)
        bar.addWidget(pause_chk)
        bar.addWidget(auto_chk)
        bar.addWidget(renders_btn)
        bar.addStretch(1)
        bar.addWidget(cleanup_btn)
        bar.addWidget(adopt_btn)
        bar.addWidget(web_btn)
        return bar

    def _build_queue_tab(self):
        tab = QtWidgets.QWidget(self)

        view = QtWidgets.QTreeWidget(tab)
        view.setColumnCount(6)
        view.setHeaderLabels([
            "Project", "Folder", "Product", "Representation",
            "Direction", "Status",
        ])
        self._setup_view(view)
        self._queue_view = view

        empty_label = QtWidgets.QLabel(
            "Nothing is queued - everything on this machine is in"
            " sync.", tab
        )
        empty_label.setAlignment(QtCore.Qt.AlignCenter)
        self._queue_empty_label = empty_label

        retry_btn = QtWidgets.QPushButton("Retry all failed", tab)
        retry_btn.clicked.connect(self._on_retry_all_failed)
        retry_btn.setEnabled(False)
        self._retry_btn = retry_btn

        btn_layout = QtWidgets.QHBoxLayout()
        btn_layout.addWidget(retry_btn)
        btn_layout.addStretch(1)

        layout = QtWidgets.QVBoxLayout(tab)
        layout.addWidget(view, 1)
        layout.addWidget(empty_label)
        layout.addLayout(btn_layout)
        return tab

    def _build_files_tab(self):
        tab = QtWidgets.QWidget(self)

        project_combo = QtWidgets.QComboBox(tab)
        project_combo.setMinimumWidth(160)
        project_combo.currentIndexChanged.connect(
            self._on_files_filter_changed
        )
        self._project_combo = project_combo

        status_combo = QtWidgets.QComboBox(tab)
        for label, value in _FILTER_STATUSES:
            status_combo.addItem(label, value)
        status_combo.currentIndexChanged.connect(
            self._on_files_filter_changed
        )
        self._status_combo = status_combo

        search_input = QtWidgets.QLineEdit(tab)
        search_input.setPlaceholderText("Filter by folder or product...")
        search_input.setClearButtonEnabled(True)
        self._search_input = search_input

        search_timer = QtCore.QTimer(self)
        search_timer.setSingleShot(True)
        search_timer.setInterval(_SEARCH_DEBOUNCE_MS)
        search_timer.timeout.connect(self._on_files_filter_changed)
        search_input.textChanged.connect(
            lambda _text: search_timer.start()
        )

        prev_btn = QtWidgets.QPushButton("<", tab)
        prev_btn.setFixedWidth(28)
        prev_btn.clicked.connect(self._on_files_prev_page)
        self._prev_btn = prev_btn

        next_btn = QtWidgets.QPushButton(">", tab)
        next_btn.setFixedWidth(28)
        next_btn.clicked.connect(self._on_files_next_page)
        self._next_btn = next_btn

        page_label = QtWidgets.QLabel("Page 1", tab)
        self._page_label = page_label

        filter_layout = QtWidgets.QHBoxLayout()
        filter_layout.addWidget(QtWidgets.QLabel("Project:", tab))
        filter_layout.addWidget(project_combo)
        filter_layout.addWidget(status_combo)
        filter_layout.addWidget(search_input, 1)
        filter_layout.addWidget(prev_btn)
        filter_layout.addWidget(page_label)
        filter_layout.addWidget(next_btn)

        view = QtWidgets.QTreeWidget(tab)
        view.setColumnCount(7)
        view.setHeaderLabels([
            "Folder", "Product", "Representation", "Files", "Size",
            "This machine", "Remote site",
        ])
        self._setup_view(view)
        self._files_view = view

        empty_label = QtWidgets.QLabel(
            "No tracked files match the current filter.", tab
        )
        empty_label.setAlignment(QtCore.Qt.AlignCenter)
        empty_label.setVisible(False)
        self._files_empty_label = empty_label

        layout = QtWidgets.QVBoxLayout(tab)
        layout.addLayout(filter_layout)
        layout.addWidget(view, 1)
        layout.addWidget(empty_label)
        return tab

    def _setup_view(self, view):
        view.setRootIsDecorated(False)
        view.setAlternatingRowColors(True)
        view.setSelectionMode(
            QtWidgets.QAbstractItemView.SingleSelection
        )
        view.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        view.customContextMenuRequested.connect(
            lambda pos, v=view: self._open_row_menu(v, pos)
        )
        header = view.header()
        header.setStretchLastSection(True)
        for idx in range(view.columnCount() - 1):
            header.setSectionResizeMode(
                idx, QtWidgets.QHeaderView.ResizeToContents
            )

    # state pushed from the addon (tray menu toggles) --------------------
    def set_pause_checked(self, checked):
        self._pause_chk.setChecked(checked)

    def set_auto_download_checked(self, checked):
        self._auto_chk.setChecked(checked)

    # Qt lifecycle -------------------------------------------------------
    def showEvent(self, event):
        super(SyncControlWindow, self).showEvent(event)
        self._pause_chk.setChecked(self._addon.is_paused())
        self._auto_chk.setChecked(
            bool(get_machine_pref("auto_download", True))
        )
        self._queue_timer.start()
        self._files_timer.start()
        self._reload_projects()
        self._refresh_queue()
        self._refresh_files()

    def hideEvent(self, event):
        super(SyncControlWindow, self).hideEvent(event)
        self._queue_timer.stop()
        self._files_timer.stop()

    def _on_tab_changed(self, _index):
        # don't wait out the poll interval after switching tabs
        if self.isVisible():
            self._refresh_queue()
            self._refresh_files()

    # refresh ------------------------------------------------------------
    def _run_bg(self, func):
        threading.Thread(target=func, daemon=True).start()

    def _reload_projects(self):
        """Fill the project combo off-thread, keeping the selection."""
        def _run():
            try:
                projects = list(self._addon.get_enabled_projects())
            except Exception:
                self._addon.log.warning(
                    "Sync control: couldn't list enabled projects",
                    exc_info=True
                )
                return
            self._projects_done.emit(projects)

        self._run_bg(_run)

    def _apply_projects(self, projects):
        combo = self._project_combo
        current = combo.currentText()
        existing = [combo.itemText(idx) for idx in range(combo.count())]
        if existing == list(projects):
            return
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(projects)
        if current in projects:
            combo.setCurrentText(current)
        combo.blockSignals(False)
        if current not in projects:
            self._on_files_filter_changed()

    def _refresh_queue(self):
        if self._queue_fetch_running:
            return
        if self._tabs.currentIndex() != 0:
            return
        self._queue_fetch_running = True

        def _run():
            try:
                rows, had_error = _collect_queue_rows(self._addon)
            except Exception:
                self._addon.log.warning(
                    "Sync queue refresh failed", exc_info=True
                )
                rows, had_error = [], True
            self._queue_done.emit({"rows": rows, "error": had_error})

        self._run_bg(_run)

    def _refresh_files(self, force=False):
        """Fetch the files tab.

        'force' bypasses the in-flight guard - used by project/filter/
        page changes, which also bump the generation counter so the
        superseded fetch's result is discarded instead of rendering old
        rows under the new selection.
        """
        in_flight = self._files_fetch_seq != self._files_applied_seq
        if in_flight and not force:
            return
        if self._tabs.currentIndex() != 1:
            return
        project_name = self._project_combo.currentText()
        if not project_name:
            return
        self._files_fetch_seq += 1
        seq = self._files_fetch_seq

        page = self._files_page
        status = self._status_combo.currentData()
        search_text = self._search_input.text().strip()

        def _run():
            try:
                rows, had_error = _collect_file_rows(
                    self._addon, project_name, page, status, search_text
                )
            except Exception:
                self._addon.log.warning(
                    "Sync file list refresh failed", exc_info=True
                )
                rows, had_error = [], True
            self._files_done.emit({
                "seq": seq,
                "page": page,
                "rows": rows,
                "error": had_error,
            })

        self._run_bg(_run)

    def _apply_queue_rows(self, payload):
        self._queue_fetch_running = False
        rows = payload["rows"]
        had_error = payload["error"]
        view = self._queue_view
        scroll_pos = view.verticalScrollBar().value()
        view.clear()

        any_failed = False
        for row in rows:
            repre = row["repre"]
            direction, label, progress, message, status = _describe(repre)
            if status == SiteSyncStatus.FAILED:
                any_failed = True
            status_text = label
            if progress is not None:
                status_text = "{} {}%".format(label, progress)
            if self._addon.is_representation_paused(row["repre_id"]):
                status_text = "{} (paused this session)".format(status_text)
            if message:
                status_text = "{} - {}".format(status_text, message)

            item = QtWidgets.QTreeWidgetItem([
                row["project"],
                repre.get("folder") or "",
                "{} (v{:03d})".format(
                    repre.get("product") or "",
                    repre.get("version") or 0
                ),
                repre.get("representation") or "",
                direction,
                status_text,
            ])
            item.setData(0, QtCore.Qt.UserRole, row)
            color = _STATUS_COLORS.get(status)
            if color:
                item.setForeground(5, QtGui.QBrush(QtGui.QColor(color)))
            if message:
                item.setToolTip(5, message)
            view.addTopLevelItem(item)

        has_rows = bool(rows)
        view.setVisible(has_rows)
        view.verticalScrollBar().setValue(scroll_pos)
        # A fetch failure must never render as "everything is in sync" -
        # a VPN-less artist with 40 failing transfers used to get a
        # green-looking panel with a fresh timestamp.
        if had_error and not has_rows:
            self._queue_empty_label.setText(
                "Couldn't reach the server - the sync state is unknown."
                " See the log."
            )
        else:
            self._queue_empty_label.setText(
                "Nothing is queued - everything on this machine is in"
                " sync."
            )
        self._queue_empty_label.setVisible(not has_rows)
        self._retry_btn.setEnabled(any_failed)
        self._mark_updated(had_error)

    def _apply_file_rows(self, payload):
        if payload["seq"] != self._files_fetch_seq:
            # a newer fetch (changed project/filter/page) superseded
            # this one - never render stale rows under a new selection
            return
        self._files_applied_seq = payload["seq"]
        rows = payload["rows"]
        had_error = payload["error"]
        view = self._files_view
        scroll_pos = view.verticalScrollBar().value()
        view.clear()

        for row in rows:
            repre = row["repre"]
            local = repre["localStatus"]
            remote = repre["remoteStatus"]
            local_text = _side_label(local)
            remote_text = _side_label(remote)
            if self._addon.is_representation_paused(row["repre_id"]):
                local_text = "{} (paused this session)".format(local_text)

            item = QtWidgets.QTreeWidgetItem([
                repre.get("folder") or "",
                "{} (v{:03d})".format(
                    repre.get("product") or "",
                    repre.get("version") or 0
                ),
                repre.get("representation") or "",
                str(repre.get("fileCount") or ""),
                _format_size(repre.get("size")),
                local_text,
                remote_text,
            ])
            item.setData(0, QtCore.Qt.UserRole, row)
            for column, status_entity in ((5, local), (6, remote)):
                color = _STATUS_COLORS.get(status_entity["status"])
                if color:
                    item.setForeground(
                        column, QtGui.QBrush(QtGui.QColor(color))
                    )
                message = status_entity.get("message")
                if message:
                    item.setToolTip(column, message)
            view.addTopLevelItem(item)

        has_rows = bool(rows)
        view.setVisible(has_rows)
        view.verticalScrollBar().setValue(scroll_pos)
        if had_error and not has_rows:
            self._files_empty_label.setText(
                "Couldn't reach the server - the file list is unknown."
                " See the log."
            )
        else:
            self._files_empty_label.setText(
                "No tracked files match the current filter."
            )
        self._files_empty_label.setVisible(not has_rows)
        self._page_label.setText("Page {}".format(payload["page"]))
        self._prev_btn.setEnabled(payload["page"] > 1)
        # merged calls can exceed one pageLength; a "full-ish" page means
        # there may be more
        self._next_btn.setEnabled(len(rows) >= _FILES_PAGE_LENGTH)
        self._mark_updated(had_error)

    def _set_status(self, message, sticky=False):
        """Set the status label; 'sticky' holds it for 10s.

        Action results and errors must survive the refresh that follows
        them - '_mark_updated' used to clobber the message with
        "Updated HH:MM:SS" within a second. Every terminal action
        message (success, failure, cancellation) should be sticky;
        transient progress notes need not be.
        """
        self._status_label.setText(message)
        if sticky:
            self._status_sticky_until = time.time() + 10

    def _mark_updated(self, had_error=False):
        # don't clobber a recent action result/error with the timestamp
        if time.time() < self._status_sticky_until:
            return
        text = "Updated {}".format(datetime.now().strftime("%H:%M:%S"))
        if had_error:
            text = "{} (some fetches failed - see the log)".format(text)
        self._status_label.setText(text)

    def _on_action_done(self, message):
        if message:
            self._set_status(message, sticky=True)
        self._refresh_queue()
        self._refresh_files(force=True)

    def _on_renders_done(self, message):
        self._renders_btn.setEnabled(True)
        self._on_action_done(message)

    def _on_cleanup_remove_done(self, message):
        self._cleanup_btn.setEnabled(True)
        self._on_action_done(message)

    # controls bar actions -----------------------------------------------
    def _on_sync_now(self):
        self._addon._on_tray_sync_now()
        self._set_status("Sync pass requested", sticky=True)

    def _on_pause_clicked(self, checked=False):
        # the addon updates every UI (tray action + this window)
        self._addon._on_tray_pause_toggle(checked)

    def _on_auto_download_clicked(self, checked=False):
        self._addon._on_tray_auto_download_toggle(checked)

    def _on_adopt(self):
        self._addon._on_tray_validate()

    def _on_download_renders(self):
        # scan first, confirm with the total size, queue only on Yes -
        # renders are the heaviest data the addon can pull and used to
        # queue without any "this will fetch N GB" moment
        self._renders_btn.setEnabled(False)
        self._set_status("Looking for renders to download...")
        addon = self._addon

        def _run():
            try:
                entries = addon.collect_my_render_downloads()
            except Exception:
                addon.log.warning(
                    "Render scan failed", exc_info=True
                )
                entries = None
            self._renders_scan_done.emit(entries)

        self._run_bg(_run)

    def _on_renders_scan_done(self, entries):
        self._renders_btn.setEnabled(True)
        if not self.isVisible():
            return
        if entries is None:
            self._set_status(
                "Render scan failed - see the log", sticky=True)
            return
        if not entries:
            self._set_status(
                "No new renders to download for your tasks", sticky=True)
            return

        total_size = sum(entry["size"] for entry in entries)
        lines = [
            "{} - {}".format(entry["label"], _format_size(entry["size"]))
            for entry in entries
        ]
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Download my renders")
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setText(
            "Queue {} render representation(s) (about {}) for download"
            " to this machine?\n\nClick 'Show Details...' for the full"
            " list.".format(len(entries), _format_size(total_size))
        )
        box.setDetailedText("\n".join(lines))
        box.setStandardButtons(
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        box.setDefaultButton(QtWidgets.QMessageBox.No)
        if box.exec_() != QtWidgets.QMessageBox.Yes:
            self._set_status("Render download cancelled", sticky=True)
            return

        self._renders_btn.setEnabled(False)
        self._set_status("Queueing render downloads...")
        addon = self._addon

        def _run():
            try:
                count = addon.queue_my_render_downloads(entries)
                if count:
                    message = (
                        "Queued {} render representation(s) for"
                        " download".format(count)
                    )
                else:
                    message = (
                        "No new renders to download for your tasks"
                    )
            except Exception:
                addon.log.warning(
                    "Render download failed", exc_info=True
                )
                message = "Render download failed - see the log"
            self._renders_done.emit(message)

        self._run_bg(_run)

    def _on_cleanup(self):
        self._cleanup_btn.setEnabled(False)
        self._set_status("Scanning for superseded versions...")
        addon = self._addon

        def _run():
            try:
                entries = _collect_superseded_rows(addon)
            except Exception:
                addon.log.warning(
                    "Cleanup scan failed", exc_info=True
                )
                entries = None
            self._cleanup_scan_done.emit(entries)

        self._run_bg(_run)

    def _on_cleanup_scan_done(self, entries):
        self._cleanup_btn.setEnabled(True)
        if not self.isVisible():
            # the artist closed the window while the scan ran - don't
            # pop a confirm dialog "out of nowhere"
            return
        if entries is None:
            self._set_status(
                "Cleanup scan failed - see the log", sticky=True)
            return
        if not entries:
            self._set_status(
                "Nothing to clean up - no superseded versions are"
                " downloaded on this machine", sticky=True)
            return

        total_size = sum(entry["size"] for entry in entries)
        lines = [
            "{} - {}".format(entry["label"], _format_size(entry["size"]))
            for entry in entries
        ]

        # summary in the message, full list only behind Qt's own
        # "Show Details..." - inlining rows makes the (non-scrolling)
        # message box outgrow the screen for exactly the big cleanups
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Clean up superseded versions")
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setText(
            "Delete the local files of {} representation(s) that already"
            " have a newer version of the same representation downloaded"
            " on this machine, freeing about {}?\n\nEverything listed is"
            " fully synced on the remote site and can be downloaded again"
            " at any time. Click 'Show Details...' for the full"
            " list.".format(len(entries), _format_size(total_size))
        )
        box.setDetailedText("\n".join(lines))
        box.setStandardButtons(
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        box.setDefaultButton(QtWidgets.QMessageBox.No)
        if box.exec_() != QtWidgets.QMessageBox.Yes:
            self._set_status("Cleanup cancelled", sticky=True)
            return

        self._cleanup_btn.setEnabled(False)
        self._set_status("Removing superseded versions...")
        addon = self._addon

        def _run():
            # Re-validate right before deleting: the confirm dialog can
            # sit open indefinitely, and meanwhile a remote record may
            # have been requeued (deleting would drop the only verified
            # copy's local twin) or the repre removed. Skipped entries
            # are reported, and 'freed' counts only what was actually
            # removed - remove_site returns silently when the record is
            # already gone, which used to be counted as freed space.
            still_valid = _revalidate_cleanup_entries(addon, entries)
            removed = 0
            freed = 0
            skipped = len(entries) - len(still_valid)
            for entry in still_valid:
                try:
                    addon.remove_site(
                        entry["project"],
                        entry["repre_id"],
                        entry["local_site"],
                        remove_local_files=True
                    )
                    removed += 1
                    freed += entry["size"]
                except Exception:
                    addon.log.warning(
                        "Couldn't remove '{}'".format(entry["label"]),
                        exc_info=True
                    )
            message = (
                "Removed {} of {} representation(s), freed up to"
                " {}".format(removed, len(entries), _format_size(freed))
            )
            if skipped:
                message = (
                    "{} ({} skipped - their sync state changed since the"
                    " scan)".format(message, skipped)
                )
            if removed + skipped < len(entries):
                message = "{} - see the log for failures".format(message)
            self._cleanup_remove_done.emit(message)

        self._run_bg(_run)

    def _on_open_web(self):
        self._addon._on_tray_open_web()

    # files tab paging/filtering -----------------------------------------
    def _on_files_filter_changed(self, *_args):
        self._files_page = 1
        self._refresh_files(force=True)

    def _on_files_prev_page(self):
        if self._files_page > 1:
            self._files_page -= 1
            self._refresh_files(force=True)

    def _on_files_next_page(self):
        self._files_page += 1
        self._refresh_files(force=True)

    # row actions --------------------------------------------------------
    def _open_row_menu(self, view, pos):
        item = view.itemAt(pos)
        if item is None:
            return
        row = item.data(0, QtCore.Qt.UserRole)
        if not row:
            return

        addon = self._addon
        local_status = row["local_status"]
        remote_status = row["remote_status"]
        repre_id = row["repre_id"]

        menu = QtWidgets.QMenu(self)
        # QMenu does not display action tooltips unless asked - without
        # this every setToolTip below is dead text
        menu.setToolTipsVisible(True)

        if SiteSyncStatus.FAILED in (local_status, remote_status):
            action = menu.addAction("Retry failed transfer")
            action.triggered.connect(
                lambda _=False, r=row: self._retry_row(r)
            )

        # Download requires the REMOTE side to be fully synced - queueing
        # a download whose source is NA would mint the QUEUED/NA pair the
        # sync loop can never match (and, because the repre then HAS a
        # row, permanently disqualify it from the zero-record backfill).
        # Mirrors the Upload guard below and the linked-record rules.
        if (
            remote_status == SiteSyncStatus.OK
            and local_status not in (
                SiteSyncStatus.OK,
                SiteSyncStatus.QUEUED,
                SiteSyncStatus.IN_PROGRESS,
            )
        ):
            action = menu.addAction("Download to this machine")
            action.triggered.connect(
                lambda _=False, r=row: self._queue_transfer_row(
                    r, r["local_site"], "Download queued")
            )

        if (
            local_status == SiteSyncStatus.OK
            and remote_status not in (
                SiteSyncStatus.OK,
                SiteSyncStatus.QUEUED,
                SiteSyncStatus.IN_PROGRESS,
            )
        ):
            action = menu.addAction("Upload to remote site")
            action.triggered.connect(
                lambda _=False, r=row: self._queue_transfer_row(
                    r, r["remote_site"], "Upload queued")
            )

        # priority lives on the site records - at least one must exist
        if (
            local_status != SiteSyncStatus.NA
            or remote_status != SiteSyncStatus.NA
        ):
            action = menu.addAction("Set transfer priority...")
            action.setToolTip(
                "Higher priority transfers first (default 50)."
            )
            action.triggered.connect(
                lambda _=False, r=row: self._set_row_priority(r)
            )

        if addon.is_representation_paused(repre_id):
            action = menu.addAction("Resume syncing this file")
            action.triggered.connect(
                lambda _=False, r=row: self._set_row_paused(r, False)
            )
        else:
            action = menu.addAction("Pause syncing this file")
            action.setToolTip("Lasts until the tray is restarted.")
            action.triggered.connect(
                lambda _=False, r=row: self._set_row_paused(r, True)
            )

        # only offer deleting bytes on the machine's own local site -
        # never let one artist's tray unsync the studio site
        if (
            local_status == SiteSyncStatus.OK
            and row["local_site"] == get_local_site_id()
        ):
            menu.addSeparator()
            action = menu.addAction("Remove download from this machine...")
            action.triggered.connect(
                lambda _=False, r=row: self._remove_row_local(r)
            )

        if menu.isEmpty():
            return
        menu.exec_(view.viewport().mapToGlobal(pos))

    def _retry_row(self, row):
        addon = self._addon

        def _run():
            try:
                for site in {row["local_site"], row["remote_site"]}:
                    # query params must live in the URL: ayon_api.post
                    # sends kwargs as the JSON body
                    ayon_api.post(
                        "{}/{}/state/resetFailed?siteName={}"
                        "&representationId={}".format(
                            addon.endpoint_prefix, row["project"],
                            site, row["repre_id"])
                    )
                addon.reset_timer()
                self._action_done.emit(
                    "Retry queued for {}".format(row["label"]))
            except Exception:
                addon.log.warning(
                    "Couldn't retry representation", exc_info=True
                )
                self._action_done.emit("Retry failed - see the log")

        self._run_bg(_run)

    def _queue_transfer_row(self, row, site_name, done_message):
        addon = self._addon

        def _run():
            try:
                # follow_links=True explicitly: a manual per-row
                # download/upload has the same semantics as a Loader
                # transfer, dependencies included
                addon.add_site(
                    row["project"], row["repre_id"], site_name,
                    force=True, follow_links=True
                )
                self._action_done.emit(
                    "{}: {}".format(done_message, row["label"]))
            except Exception:
                addon.log.warning(
                    "Couldn't queue transfer to '{}'".format(site_name),
                    exc_info=True
                )
                self._action_done.emit(
                    "Couldn't queue transfer - see the log")

        self._run_bg(_run)

    def _set_row_priority(self, row):
        # no 'or 50': priority 0 is a valid stored value and must
        # pre-fill as 0, not as the default
        current = row["repre"].get("priority")
        if current is None:
            current = 50
        value, accepted = QtWidgets.QInputDialog.getInt(
            self,
            "Transfer priority",
            "Priority for\n{}\n\n0-100, higher transfers first"
            " (default 50):".format(row["label"]),
            int(current), 0, 100
        )
        if not accepted:
            return

        # No 'value == current' shortcut: 'current' is the server's
        # highest-of-both-sides roll-up, so re-entering the shown value
        # is exactly how diverged sides get equalized.
        addon = self._addon

        def _run():
            try:
                addon.set_representation_priority(
                    row["project"], row["repre_id"], value
                )
                self._action_done.emit(
                    "Priority {} set for {}".format(value, row["label"]))
            except Exception:
                addon.log.warning(
                    "Couldn't set transfer priority", exc_info=True
                )
                self._action_done.emit(
                    "Couldn't set priority - see the log")

        self._run_bg(_run)

    def _set_row_paused(self, row, paused):
        addon = self._addon
        if paused:
            addon.pause_representation(
                row["project"], row["repre_id"], row["local_site"]
            )
            self._action_done.emit(
                "Paused for this session: {}".format(row["label"]))
        else:
            addon.unpause_representation(
                row["project"], row["repre_id"], row["local_site"]
            )
            addon.reset_timer()
            self._action_done.emit(
                "Resumed: {}".format(row["label"]))

    def _remove_row_local(self, row):
        addon = self._addon
        answer = QtWidgets.QMessageBox.question(
            self,
            "Remove download",
            "Delete the local copy of\n\n{}\n\nfrom this machine? The"
            " files stay on the remote site and can be downloaded"
            " again.".format(row["label"]),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return

        def _run():
            try:
                addon.remove_site(
                    row["project"], row["repre_id"], row["local_site"],
                    remove_local_files=True
                )
                self._action_done.emit(
                    "Removed from this machine: {}".format(row["label"]))
            except Exception:
                addon.log.warning(
                    "Couldn't remove local copy", exc_info=True
                )
                self._action_done.emit(
                    "Couldn't remove local copy - see the log")

        self._run_bg(_run)

    def _on_retry_all_failed(self):
        """Requeue failed files of every shown project on both sites."""
        addon = self._addon
        self._retry_btn.setEnabled(False)

        def _run():
            try:
                for project_name in addon.get_enabled_projects():
                    local_site = addon.get_active_site(project_name)
                    remote_site = addon.get_remote_site(project_name)
                    for site in {local_site, remote_site}:
                        if not site:
                            continue
                        # query params must live in the URL: ayon_api.post
                        # sends kwargs as the JSON body
                        ayon_api.post(
                            "{}/{}/state/resetFailed?siteName={}".format(
                                addon.endpoint_prefix, project_name, site)
                        )
                addon.reset_timer()
                self._action_done.emit("All failed transfers requeued")
            except Exception:
                addon.log.warning(
                    "Retry all failed from sync control window failed",
                    exc_info=True
                )
                self._action_done.emit("Retry failed - see the log")

        self._run_bg(_run)
