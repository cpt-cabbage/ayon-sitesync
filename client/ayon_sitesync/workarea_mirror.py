"""Studio -> local mirroring of work-area workfiles.

Work-area scenes are NOT representations, so the sitesync database
cannot track them - its schema, endpoints, status state-machine and UI
are all keyed by representation id, and its model assumes files are
immutable once synced (published), while workfiles change on every save.

Instead of growing a parallel sync system, this module mirrors by
direct file copy over the (VPN-reachable) studio share, using AYON's
workfile entities - which are already the server-side index of
work-area files, keyed by rootless path - as the source of truth and
the filesystem as the state:

- a workfile missing locally gets copied from the studio share,
- an existing local file is NEVER overwritten (the artist's local edits
  win; publishes remain the transfer medium back to the studio),
- unreachable studio roots mean a clean skip (probed with a timeout so
  dead mounts can't hang callers).

Consequences: idempotent, conflict-free, nothing to retry or migrate -
but it only works while the share is reachable and is download-only.

Must stay Python 3.7 compatible - imported by launch hooks in DCC
pythons.
"""
from __future__ import annotations

import os
import shutil

import ayon_api
from ayon_core.lib import Logger
from ayon_core.pipeline import Anatomy

from .machine_role import ROLE_STUDIO, probe_machine_role

log = Logger.get_logger("SiteSync")


def mirror_workarea_files(addon, project_name, task_ids, extensions=None):
    """Copy missing studio work-area workfiles of tasks to the local site.

    Args:
        addon (SiteSyncAddon): Addon instance (for studio roots lookup).
        project_name (str): Project name.
        task_ids (Iterable[str]): Tasks whose workfiles should be local.
        extensions (Optional[Iterable[str]]): Limit to these extensions
            (with leading dot).

    Returns:
        list[str]: Local paths of the tasks' workfiles that exist locally
            after the pass (already-present and freshly copied), ordered
            oldest to newest by entity update time.
    """
    task_ids = set(task_ids or [])
    if not task_ids:
        return []

    try:
        studio_roots = addon._get_studio_roots(project_name)
    except Exception:
        log.debug(
            "Couldn't fetch studio roots of '{}'".format(project_name),
            exc_info=True
        )
        return []

    if probe_machine_role(studio_roots) != ROLE_STUDIO:
        log.debug(
            "Studio roots of '{}' are not reachable from this machine -"
            " work-area workfiles can't be fetched".format(project_name)
        )
        return []

    try:
        studio_anatomy = Anatomy(project_name, site_name="studio")
        local_anatomy = Anatomy(project_name, site_name="local")
    except Exception:
        log.warning(
            "Couldn't prepare anatomy for work-area mirror of"
            " '{}'".format(project_name),
            exc_info=True
        )
        return []

    present = []
    copied = 0
    for entity in ayon_api.get_workfiles_info(
        project_name, task_ids=task_ids
    ):
        rootless = entity.get("path")
        if not rootless:
            continue
        if extensions:
            ext = os.path.splitext(rootless)[1]
            if ext not in extensions:
                continue
        try:
            src = studio_anatomy.fill_root(rootless)
            dst = local_anatomy.fill_root(rootless)
        except Exception:
            log.debug(
                "Couldn't resolve roots for workfile '{}'".format(
                    rootless),
                exc_info=True
            )
            continue
        if not src or not dst:
            continue
        if os.path.normpath(src) == os.path.normpath(dst):
            # same location on both sites - nothing to mirror
            continue

        if not os.path.exists(dst):
            if not os.path.isfile(src):
                # entity exists but the studio bytes are gone/moved
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                # copy2 keeps mtime so 'newest workfile' logic in tools
                # keeps working on the copy
                shutil.copy2(src, dst)
                copied += 1
            except Exception:
                log.warning(
                    "Couldn't mirror work-area file {} -> {}".format(
                        src, dst),
                    exc_info=True
                )
                continue

        present.append((entity.get("updatedAt") or "", dst))

    if copied:
        log.info(
            "Mirrored {} work-area workfile(s) for '{}'".format(
                copied, project_name)
        )

    present.sort()
    return [path for _, path in present]
