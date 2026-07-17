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
_FILES_PAGE_LENGTH = 200

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
    """
    rows = []
    for project_name in addon.get_enabled_projects():
        try:
            local_site = addon.get_active_site(project_name)
            remote_site = addon.get_remote_site(project_name)
            if not local_site or local_site == remote_site:
                continue

            merged = {}
            for side in ("local", "remote"):
                kwargs = {
                    "localSite": local_site,
                    "remoteSite": remote_site,
                    "pageLength": _QUEUE_PAGE_LENGTH,
                    "{}StatusFilter".format(side): list(_ACTIVE_STATUSES),
                }
                response = ayon_api.get(
                    "{}/{}/state".format(
                        addon.endpoint_prefix, project_name),
                    **kwargs
                )
                if response.status_code != 200:
                    continue
                for repre in response.data.get("representations") or []:
                    merged[repre["representationId"]] = repre

            for repre in merged.values():
                rows.append(
                    _make_row(project_name, local_site, remote_site, repre)
                )
        except Exception:
            addon.log.warning(
                "Sync control: couldn't fetch state of '{}'".format(
                    project_name),
                exc_info=True
            )
    return rows


def _collect_file_rows(addon, project_name, page, status, search_text):
    """One page of tracked representations of one project.

    Runs in a worker thread. Status/search need "either side" / "either
    name" semantics the endpoint cannot express in one call (filters are
    ANDed), so up to four calls are merged by representation id - except
    "Fully synced", which really is local AND remote OK in one call.
    Paging over merged calls is per-call, so a page can hold up to
    (calls x pageLength) rows; that is fine for a browsing UI.
    """
    local_site = addon.get_active_site(project_name)
    remote_site = addon.get_remote_site(project_name)
    if not local_site or local_site == remote_site:
        return []

    base_kwargs = {
        "localSite": local_site,
        "remoteSite": remote_site,
        "page": page,
        "pageLength": _FILES_PAGE_LENGTH,
    }

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
                continue
            for repre in response.data.get("representations") or []:
                merged[repre["representationId"]] = repre

    rows = [
        _make_row(project_name, local_site, remote_site, repre)
        for repre in merged.values()
    ]
    rows.sort(key=lambda row: (
        row["repre"].get("folder") or "",
        row["repre"].get("product") or "",
        row["repre"].get("version") or 0,
        row["repre"].get("representation") or "",
    ))
    return rows


class SyncControlWindow(QtWidgets.QWidget):
    """Tray-owned window bundling every artist-facing sync control."""

    _queue_done = QtCore.Signal(object)
    _files_done = QtCore.Signal(object)
    _action_done = QtCore.Signal(str)
    _projects_done = QtCore.Signal(object)

    def __init__(self, addon, parent=None):
        super(SyncControlWindow, self).__init__(parent)
        self._addon = addon
        self._queue_fetch_running = False
        self._files_fetch_running = False
        self._files_page = 1

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
        bar.addStretch(1)
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
                rows = _collect_queue_rows(self._addon)
            except Exception:
                self._addon.log.warning(
                    "Sync queue refresh failed", exc_info=True
                )
                rows = []
            self._queue_done.emit(rows)

        self._run_bg(_run)

    def _refresh_files(self):
        if self._files_fetch_running:
            return
        if self._tabs.currentIndex() != 1:
            return
        project_name = self._project_combo.currentText()
        if not project_name:
            return
        self._files_fetch_running = True

        page = self._files_page
        status = self._status_combo.currentData()
        search_text = self._search_input.text().strip()

        def _run():
            try:
                rows = _collect_file_rows(
                    self._addon, project_name, page, status, search_text
                )
            except Exception:
                self._addon.log.warning(
                    "Sync file list refresh failed", exc_info=True
                )
                rows = []
            self._files_done.emit(rows)

        self._run_bg(_run)

    def _apply_queue_rows(self, rows):
        self._queue_fetch_running = False
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
        self._queue_empty_label.setVisible(not has_rows)
        self._retry_btn.setEnabled(any_failed)
        self._mark_updated()

    def _apply_file_rows(self, rows):
        self._files_fetch_running = False
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
        self._files_empty_label.setVisible(not has_rows)
        self._page_label.setText("Page {}".format(self._files_page))
        self._prev_btn.setEnabled(self._files_page > 1)
        # merged calls can exceed one pageLength; a "full-ish" page means
        # there may be more
        self._next_btn.setEnabled(len(rows) >= _FILES_PAGE_LENGTH)
        self._mark_updated()

    def _mark_updated(self):
        self._status_label.setText(
            "Updated {}".format(datetime.now().strftime("%H:%M:%S"))
        )

    def _on_action_done(self, message):
        if message:
            self._status_label.setText(message)
        self._refresh_queue()
        self._refresh_files()

    # controls bar actions -----------------------------------------------
    def _on_sync_now(self):
        self._addon._on_tray_sync_now()
        self._status_label.setText("Sync pass requested")

    def _on_pause_clicked(self, checked=False):
        # the addon updates every UI (tray action + this window)
        self._addon._on_tray_pause_toggle(checked)

    def _on_auto_download_clicked(self, checked=False):
        self._addon._on_tray_auto_download_toggle(checked)

    def _on_adopt(self):
        self._addon._on_tray_validate()

    def _on_open_web(self):
        self._addon._on_tray_open_web()

    # files tab paging/filtering -----------------------------------------
    def _on_files_filter_changed(self, *_args):
        self._files_page = 1
        self._refresh_files()

    def _on_files_prev_page(self):
        if self._files_page > 1:
            self._files_page -= 1
            self._refresh_files()

    def _on_files_next_page(self):
        self._files_page += 1
        self._refresh_files()

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

        if SiteSyncStatus.FAILED in (local_status, remote_status):
            action = menu.addAction("Retry failed transfer")
            action.triggered.connect(
                lambda _=False, r=row: self._retry_row(r)
            )

        if local_status not in (
            SiteSyncStatus.OK,
            SiteSyncStatus.QUEUED,
            SiteSyncStatus.IN_PROGRESS,
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
                addon.add_site(
                    row["project"], row["repre_id"], site_name, force=True
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
