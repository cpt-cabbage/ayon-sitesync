"""Machine-local sitesync preferences and site-role helpers.

A machine is either working "in the studio" (the share is its storage,
nothing needs syncing for it) or "remote" (the artist works in a local
folder and published files are synced in the background). Which one it
is comes from the server-side per-site opt-in
('local_setting.sync_enabled', see 'addon._get_zero_touch_role') - this
module only keeps the machine-local preference file (e.g. the tray's
auto-download switch), the synthesized-local-root base and the
studio-root reachability probe used by the work-area mirror.

Must stay Python 3.7 compatible - imported in-process by DCCs.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import platform

from ayon_core.lib import Logger

ROLE_STUDIO = "studio"
ROLE_REMOTE = "remote"

_ROLE_FILE_NAME = "sitesync_machine_role.json"

log = Logger.get_logger("SiteSync")


def _get_role_file_path():
    from ayon_core.lib import get_launcher_local_dir

    return get_launcher_local_dir(_ROLE_FILE_NAME)


def _read_prefs():
    try:
        path = _get_role_file_path()
        if os.path.exists(path):
            with open(path, "r") as stream:
                return json.load(stream)
    except Exception:
        log.warning("Couldn't read machine prefs file", exc_info=True)
    return {}


def _write_prefs(content):
    path = _get_role_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as stream:
        json.dump(content, stream)


def get_machine_pref(key, default=None):
    """Per-machine sitesync preference (artist-local, not server)."""
    return _read_prefs().get(key, default)


def set_machine_pref(key, value):
    """Persist a per-machine sitesync preference."""
    content = _read_prefs()
    content[key] = value
    _write_prefs(content)


def get_default_local_root_base():
    """Base folder for synthesized local roots.

    '~/AYON_local' - the home folder itself is not OneDrive-redirected on
    Windows (only Desktop/Documents are), but if a cloud tool does cover
    it, fall back to the system drive: a cloud-synced sitesync root causes
    intermittent failures and re-uploads every synced file to the cloud.

    Returns:
        str: Absolute base path (root names are appended per project).
    """
    from .utils import is_cloud_synced_path

    base = os.path.join(os.path.expanduser("~"), "AYON_local")
    if is_cloud_synced_path(base):
        if platform.system().lower() == "windows":
            drive = os.environ.get("SystemDrive", "C:")
            base = os.path.join(drive + os.sep, "AYON_local")
        log.warning(
            "Home folder appears cloud-synced, using '{}' for local"
            " sitesync roots instead".format(base)
        )
    return base


def probe_machine_role(studio_roots, timeout=4.0):
    """Guess the machine role from studio root reachability.

    Args:
        studio_roots (dict[str, str]): Studio root name -> path for the
            current platform.
        timeout (float): Per-probe ceiling. Dead network mounts can block
            ``os.path.isdir`` for a long time (especially UNC paths on
            Windows), so the checks run in worker threads and anything
            that doesn't answer in time counts as unreachable.

    Returns:
        Union[str, None]: 'studio' when every root is reachable, 'remote'
            when any is not, None when there is nothing to probe.
    """
    root_paths = [path for path in (studio_roots or {}).values() if path]
    if not root_paths:
        return None

    def _isdir(path):
        try:
            return os.path.isdir(path)
        except OSError:
            return False

    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=len(root_paths)
    )
    try:
        futures = [executor.submit(_isdir, path) for path in root_paths]
        for future in futures:
            try:
                if not future.result(timeout=timeout):
                    return ROLE_REMOTE
            except concurrent.futures.TimeoutError:
                return ROLE_REMOTE
        return ROLE_STUDIO
    finally:
        # Don't wait for probes stuck on a dead mount.
        executor.shutdown(wait=False)
