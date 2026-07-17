"""Auto-download of assigned-task dependencies for remote artists.

Runs inside the tray's sync loop. For every project where this machine is
the artist's local site, it periodically finds the last published
workfile of each task assigned to the logged-in user - plus the
representations those workfiles reference - and queues them for download.
The artist opens their DCC and the work is already there.

Design constraints:
- Idempotent and respectful of artist removals: every representation id
  that was ever auto-queued is remembered in a local ledger
  (never re-queued), so removing something from the local site via the
  Loader doesn't turn into a tug-of-war with this service.
- Only representations already available on the remote site are queued -
  a queued-but-absent pair is invisible to the sync loop forever.
- Disk-aware: skips (with a warning) when free space on the local roots
  drops under the configured minimum.
- Never raises into the sync loop.

Must stay Python 3.7 compatible - imported via the addon by DCCs.
"""
from __future__ import annotations

import json
import os
import shutil
import time

import ayon_api
from ayon_core.lib import Logger, get_local_site_id

from .machine_role import get_machine_pref
from .task_tracking import get_tracked_tasks
from .utils import (
    SiteAlreadyPresentError,
    SiteSyncStatus,
    get_last_published_workfile_representation,
    get_linked_representation_id,
)

log = Logger.get_logger("SiteSync")

_LEDGER_FILE_NAME = "sitesync_autodownload.json"

# Upper bound of newly queued representations per project per pass -
# keeps one giant backlog from starving actual transfers.
MAX_QUEUED_PER_PASS = 50


class AutoDownloader:
    """Stateful helper owned by the sync thread."""

    def __init__(self, addon):
        self.addon = addon
        self._last_run_by_project = {}
        self._ledger = None
        self._username = None
        self._disk_warned_projects = set()

    def process_project(self, project_name):
        """Queue missing assigned-task dependencies for download.

        Throttled internally per project; cheap no-op between intervals.
        """
        try:
            self._process_project(project_name)
        except Exception:
            log.warning(
                "Auto-download failed for project '{}'".format(
                    project_name),
                exc_info=True
            )

    def _process_project(self, project_name):
        addon = self.addon
        settings = addon.sync_project_settings.get(project_name)
        if not settings or not settings["enabled"]:
            return
        config = settings["config"]
        if not config.get("enable_auto_download", True):
            return
        # artist-local off switch (tray: 'Auto-download new work')
        if not get_machine_pref("auto_download", True):
            return
        if addon.is_project_paused(project_name):
            return

        interval = int(config.get("auto_download_interval") or 300)
        now = time.time()
        if now - self._last_run_by_project.get(project_name, 0) < interval:
            return

        local_site = addon.get_active_site(project_name)
        remote_site = addon.get_remote_site(project_name)
        if local_site == remote_site:
            return
        if local_site != get_local_site_id():
            # This machine is not the artist's local site (studio
            # machine, headless site service) - nothing to pre-fetch.
            return

        # Stamp before the work so failures don't retry in a hot loop.
        self._last_run_by_project[project_name] = now

        candidate_ids = self._collect_candidates(project_name, config)
        ledger = self._get_project_ledger(project_name)
        fresh_ids = [
            repre_id for repre_id in candidate_ids
            if repre_id not in ledger
        ]
        if not fresh_ids:
            return

        if not self._enough_free_space(project_name, config):
            return

        to_queue = self._filter_by_state(
            project_name, fresh_ids, local_site, remote_site, ledger
        )
        queued = 0
        for repre_id in to_queue:
            if queued >= MAX_QUEUED_PER_PASS:
                log.info(
                    "Auto-download cap of {} reached for '{}',"
                    " remaining items queue next pass".format(
                        MAX_QUEUED_PER_PASS, project_name)
                )
                break
            try:
                addon.add_site(
                    project_name, repre_id, local_site, force=False
                )
                queued += 1
            except SiteAlreadyPresentError:
                pass
            except Exception:
                log.warning(
                    "Couldn't auto-queue representation '{}'".format(
                        repre_id),
                    exc_info=True
                )
                continue
            ledger.add(repre_id)
        if queued:
            log.info(
                "Auto-download queued {} representation(s) for"
                " '{}'".format(queued, project_name)
            )
        self._save_ledger()

    def _collect_candidates(self, project_name, config):
        """Last published workfile + referenced repres per relevant task.

        Relevant = tasks assigned to the logged-in user, unioned with
        tasks the artist opened on this machine (tracked by the launch
        hook, expiring per 'opened_task_retention_days').
        """
        folder_id_by_task_id = {}

        username = self._get_username()
        if username:
            for task in ayon_api.get_tasks(
                project_name,
                assignees=[username],
                fields={"id", "folderId"}
            ):
                folder_id_by_task_id[task["id"]] = task["folderId"]

        retention_days = float(
            config.get("opened_task_retention_days", 14) or 0
        )
        folder_id_by_task_id.update(
            get_tracked_tasks(project_name, retention_days)
        )

        candidate_ids = set()
        for task_id, folder_id in folder_id_by_task_id.items():
            repre = get_last_published_workfile_representation(
                project_name, folder_id, task_id
            )
            if not repre:
                continue
            candidate_ids.add(repre["id"])
            candidate_ids.update(get_linked_representation_id(
                project_name, repre, "reference"
            ))
        return candidate_ids

    def _filter_by_state(
        self, project_name, repre_ids, local_site, remote_site, ledger
    ):
        """Keep ids that are on remote but have no local record yet.

        Ids already present locally (queued, syncing, done, failed) go
        straight to the ledger - they are handled. Ids not yet uploaded
        to the remote site are left out of BOTH (retried next pass):
        queueing them would create a queued/NA pair the sync loop never
        matches.
        """
        repre_states = self.addon._get_repres_state(
            project_name, set(repre_ids), local_site, remote_site
        )
        to_queue = []
        for state in repre_states:
            repre_id = state["representationId"]
            local_status = state["localStatus"]["status"]
            remote_status = state["remoteStatus"]["status"]
            if local_status != SiteSyncStatus.NA:
                ledger.add(repre_id)
                continue
            if remote_status == SiteSyncStatus.OK:
                to_queue.append(repre_id)
        return to_queue

    def _enough_free_space(self, project_name, config):
        min_free_gb = float(config.get("min_free_space_gb") or 0)
        if min_free_gb <= 0:
            return True
        roots = self.addon.get_local_roots_with_defaults(project_name)
        for root_path in roots.values():
            path = root_path
            # walk up to the closest existing folder for disk_usage
            while path and not os.path.exists(path):
                parent = os.path.dirname(path)
                if parent == path:
                    break
                path = parent
            try:
                free_gb = shutil.disk_usage(path).free / (1024 ** 3)
            except OSError:
                continue
            if free_gb < min_free_gb:
                if project_name not in self._disk_warned_projects:
                    self._disk_warned_projects.add(project_name)
                    log.warning(
                        "Auto-download paused for '{}': only {:.1f} GB"
                        " free under '{}' (minimum {} GB). Free up space"
                        " to resume.".format(
                            project_name, free_gb, root_path, min_free_gb)
                    )
                return False
        self._disk_warned_projects.discard(project_name)
        return True

    def _get_username(self):
        if self._username is None:
            try:
                self._username = ayon_api.get_user()["name"]
            except Exception:
                log.warning("Couldn't resolve user name", exc_info=True)
                return None
        return self._username

    # ledger -------------------------------------------------------------
    def _get_ledger_path(self):
        from ayon_core.lib import get_launcher_local_dir

        return get_launcher_local_dir(_LEDGER_FILE_NAME)

    def _get_project_ledger(self, project_name):
        if self._ledger is None:
            self._ledger = self._load_ledger()
        return self._ledger.setdefault(project_name, set())

    def _load_ledger(self):
        try:
            path = self._get_ledger_path()
            if os.path.exists(path):
                with open(path, "r") as stream:
                    content = json.load(stream)
                return {
                    project_name: set(repre_ids)
                    for project_name, repre_ids in content.items()
                }
        except Exception:
            log.warning("Couldn't read auto-download ledger", exc_info=True)
        return {}

    def _save_ledger(self):
        if self._ledger is None:
            return
        try:
            path = self._get_ledger_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            content = {
                project_name: sorted(repre_ids)
                for project_name, repre_ids in self._ledger.items()
            }
            with open(path, "w") as stream:
                json.dump(content, stream)
        except Exception:
            log.warning("Couldn't save auto-download ledger", exc_info=True)
