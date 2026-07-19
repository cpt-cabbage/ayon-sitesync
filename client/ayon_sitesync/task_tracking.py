"""Locally tracked tasks for auto-download.

Every task an artist opens in a DCC (assigned to them or not) is recorded
here by the launch hook; the auto-download service unions these with the
artist's assigned tasks, so an opened shot keeps its dependencies synced.

Entries expire a configurable number of days after the task was last
opened - otherwise every task ever touched would sync forever. Opening a
task again refreshes its timestamp.

Written from launcher/DCC processes, read from the tray - plain file in
the launcher local dir, no locking (last-writer-wins is fine for this).

Must stay Python 3.7 compatible - imported by launch hooks in DCC pythons.
"""
from __future__ import annotations

import json
import os
import time

from ayon_core.lib import Logger

log = Logger.get_logger("SiteSync")

_TRACKED_FILE_NAME = "sitesync_tracked_tasks.json"


def _get_tracked_file_path():
    from ayon_core.lib import get_launcher_local_dir

    return get_launcher_local_dir(_TRACKED_FILE_NAME)


def _load():
    try:
        path = _get_tracked_file_path()
        if os.path.exists(path):
            with open(path, "r") as stream:
                return json.load(stream)
    except Exception:
        log.warning("Couldn't read tracked tasks file", exc_info=True)
    return {}


def _save(content):
    try:
        path = _get_tracked_file_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # atomic write: concurrent writers (tray prune vs DCC record)
        # may lose an update to each other, but never tear the file
        tmp_path = "{}.tmp.{}".format(path, os.getpid())
        with open(tmp_path, "w") as stream:
            json.dump(content, stream)
        os.replace(tmp_path, path)
    except Exception:
        log.warning("Couldn't save tracked tasks file", exc_info=True)


def record_opened_task(project_name, task_id, folder_id):
    """Remember that the artist opened this task on this machine.

    Called from the launch hook - must never raise (launch path).
    """
    try:
        content = _load()
        project_tasks = content.setdefault(project_name, {})
        project_tasks[task_id] = {
            "folderId": folder_id,
            "opened": time.time(),
        }
        _save(content)
        log.debug(
            "Tracked opened task '{}' of '{}' for auto-download".format(
                task_id, project_name)
        )
    except Exception:
        log.warning("Couldn't record opened task", exc_info=True)


def get_tracked_tasks(project_name, retention_days):
    """Tracked task_id -> folder_id for a project, pruning expired ones.

    Args:
        project_name (str): Project name.
        retention_days (float): Days since last open after which a task
            stops syncing. Zero or negative disables tracking entirely.

    Returns:
        dict[str, str]: task_id -> folder_id.
    """
    if retention_days <= 0:
        return {}
    content = _load()
    project_tasks = content.get(project_name) or {}
    if not project_tasks:
        return {}

    cutoff = time.time() - retention_days * 24 * 3600
    valid = {}
    expired = []
    for task_id, info in project_tasks.items():
        if (info.get("opened") or 0) >= cutoff:
            valid[task_id] = info.get("folderId")
        else:
            expired.append(task_id)

    if expired:
        _prune_expired(project_name, expired, cutoff)

    return {
        task_id: folder_id
        for task_id, folder_id in valid.items()
        if folder_id
    }


def _prune_expired(project_name, task_ids, cutoff):
    """Drop expired entries with a fresh read-merge-write.

    This prune runs on the READER side (the tray) while DCC processes
    keep writing new entries - saving the copy loaded at the top of
    'get_tracked_tasks' could drop a task recorded in between.
    Re-reading right before the write shrinks that window to near zero,
    and only entries still expired in the fresh copy are removed (a task
    re-opened meanwhile survives).
    """
    try:
        content = _load()
        project_tasks = content.get(project_name) or {}
        changed = False
        for task_id in task_ids:
            info = project_tasks.get(task_id)
            if info and (info.get("opened") or 0) < cutoff:
                project_tasks.pop(task_id, None)
                changed = True
        if changed:
            _save(content)
    except Exception:
        log.warning("Couldn't prune tracked tasks", exc_info=True)
