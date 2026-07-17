"""Live "what's syncing" window, opened from the tray menu.

Shows queued / in-progress / failed representations for every enabled
project this machine syncs, with live progress. Polls the addon's own
`/state` endpoint every few seconds while visible (the transfer loop
writes real progress to the DB every ~5s), stops polling when hidden.

Only imported from the tray process (Qt available); REST fetching runs
in a worker thread so the UI never blocks on the server.
"""
from qtpy import QtWidgets, QtCore

import ayon_api

from .utils import SiteSyncStatus

_REFRESH_MS = 4000
_PAGE_LENGTH = 100

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


def _collect_rows(addon):
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
                    "pageLength": _PAGE_LENGTH,
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
                rows.append((project_name, repre))
        except Exception:
            addon.log.warning(
                "Sync queue: couldn't fetch state of '{}'".format(
                    project_name),
                exc_info=True
            )
    return rows


def _describe(repre):
    """Direction, status label, progress % and message for a row."""
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
    return direction, label, progress, message


class SyncQueueWindow(QtWidgets.QWidget):
    """Tray-owned window listing the current sync queue."""

    _refresh_done = QtCore.Signal(object)

    def __init__(self, addon, parent=None):
        super(SyncQueueWindow, self).__init__(parent)
        self._addon = addon
        self._fetch_running = False

        self.setWindowTitle("AYON Site Sync - queue")
        self.resize(860, 420)

        view = QtWidgets.QTreeWidget(self)
        view.setColumnCount(6)
        view.setHeaderLabels([
            "Project", "Folder", "Product", "Representation",
            "Direction", "Status",
        ])
        view.setRootIsDecorated(False)
        view.setAlternatingRowColors(True)
        view.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        header = view.header()
        header.setStretchLastSection(True)
        for idx in range(5):
            header.setSectionResizeMode(
                idx, QtWidgets.QHeaderView.ResizeToContents
            )
        self._view = view

        empty_label = QtWidgets.QLabel(
            "Nothing is queued - everything on this machine is in"
            " sync.", self
        )
        empty_label.setAlignment(QtCore.Qt.AlignCenter)
        self._empty_label = empty_label

        retry_btn = QtWidgets.QPushButton("Retry all failed", self)
        retry_btn.clicked.connect(self._on_retry_failed)
        self._retry_btn = retry_btn

        refresh_btn = QtWidgets.QPushButton("Refresh", self)
        refresh_btn.clicked.connect(self._trigger_refresh)

        btn_layout = QtWidgets.QHBoxLayout()
        btn_layout.addWidget(retry_btn)
        btn_layout.addStretch(1)
        btn_layout.addWidget(refresh_btn)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(view, 1)
        layout.addWidget(empty_label)
        layout.addLayout(btn_layout)

        timer = QtCore.QTimer(self)
        timer.setInterval(_REFRESH_MS)
        timer.timeout.connect(self._trigger_refresh)
        self._timer = timer

        self._refresh_done.connect(self._apply_rows)

    # Qt lifecycle -------------------------------------------------------
    def showEvent(self, event):
        super(SyncQueueWindow, self).showEvent(event)
        self._timer.start()
        self._trigger_refresh()

    def hideEvent(self, event):
        super(SyncQueueWindow, self).hideEvent(event)
        self._timer.stop()

    # refresh ------------------------------------------------------------
    def _trigger_refresh(self):
        if self._fetch_running:
            return
        self._fetch_running = True

        import threading

        def _run():
            try:
                rows = _collect_rows(self._addon)
            except Exception:
                self._addon.log.warning(
                    "Sync queue refresh failed", exc_info=True
                )
                rows = []
            self._refresh_done.emit(rows)

        threading.Thread(target=_run, daemon=True).start()

    def _apply_rows(self, rows):
        self._fetch_running = False
        view = self._view
        view.clear()

        any_failed = False
        for project_name, repre in rows:
            direction, label, progress, message = _describe(repre)
            if label == "Failed":
                any_failed = True
            status_text = label
            if progress is not None:
                status_text = "{} {}%".format(label, progress)
            if message:
                status_text = "{} - {}".format(status_text, message)

            item = QtWidgets.QTreeWidgetItem([
                project_name,
                repre.get("folder") or "",
                "{} (v{:03d})".format(
                    repre.get("product") or "",
                    repre.get("version") or 0
                ),
                repre.get("representation") or "",
                direction,
                status_text,
            ])
            if message:
                item.setToolTip(5, message)
            view.addTopLevelItem(item)

        has_rows = bool(rows)
        view.setVisible(has_rows)
        self._empty_label.setVisible(not has_rows)
        self._retry_btn.setEnabled(any_failed)

    # actions ------------------------------------------------------------
    def _on_retry_failed(self):
        """Requeue failed files of every shown project on both sites."""
        addon = self._addon
        self._retry_btn.setEnabled(False)

        import threading

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
            except Exception:
                addon.log.warning(
                    "Retry all failed from sync queue window failed",
                    exc_info=True
                )
            self._refresh_done.emit(_collect_rows(addon))

        threading.Thread(target=_run, daemon=True).start()
