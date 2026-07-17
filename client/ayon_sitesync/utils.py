import os
import time

from ayon_core.lib import Logger, is_func_signature_supported
from ayon_api import (
    get_representations,
    get_versions_links,
    get_products,
    get_last_versions,
)


log = Logger.get_logger("SiteSync")

# Path segments that identify folders mirrored by cloud-sync tools. A
# sitesync local root inside one of these is trouble: the tool holds
# handles on folders (intermittent rmdir/copy failures), re-uploads every
# synced render to the cloud, and Files On-Demand placeholders can read as
# existing files with no bytes behind them.
_CLOUD_SYNCED_SEGMENTS = (
    "onedrive",
    "dropbox",
    "google drive",
    "googledrive",
    "cloudstorage",
)


def is_cloud_synced_path(path):
    """Heuristic check that 'path' lives inside a cloud-synced folder.

    Used to veto synthesized default local roots and to warn about
    artist-configured ones. False negatives are acceptable - this guards
    defaults and produces warnings, it never blocks explicit choices.

    Args:
        path (str): Path to check.

    Returns:
        bool: Path looks like it is inside OneDrive/Dropbox/GDrive/etc.
    """
    if not path:
        return False

    normalized = path.replace("\\", "/").lower()
    for segment in normalized.split("/"):
        for marker in _CLOUD_SYNCED_SEGMENTS:
            if marker in segment:
                return True

    for env_key in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        cloud_dir = os.environ.get(env_key)
        if not cloud_dir:
            continue
        try:
            common = os.path.commonpath(
                [os.path.abspath(path), os.path.abspath(cloud_dir)]
            )
            if common == os.path.abspath(cloud_dir):
                return True
        except ValueError:
            # different drives on windows
            continue
    return False


class ResumableError(Exception):
    """Error which could be temporary, skip current loop, try next time"""
    pass


class SiteAlreadyPresentError(Exception):
    """Representation has already site skeleton present."""
    pass


class SyncStatus:
    DO_NOTHING = 0
    DO_UPLOAD = 1
    DO_DOWNLOAD = 2


class SiteSyncStatus:
    NA = -1
    IN_PROGRESS = 0
    QUEUED = 1
    FAILED = 2
    PAUSED = 3
    OK = 4


def time_function(method):
    """ Decorator to print how much time function took.
        For debugging.
        Depends on presence of 'log' object
    """

    def timed(*args, **kw):
        ts = time.time()
        result = method(*args, **kw)
        te = time.time()
        if "log_time" in kw:
            name = kw.get("log_name", method.__name__.upper())
            kw["log_time"][name] = int((te - ts) * 1000)
        else:
            log.debug("%r  %2.2f ms" % (method.__name__, (te - ts) * 1000))
        return result

    return timed


class EditableScopes:
    SYSTEM = 0
    PROJECT = 1
    LOCAL = 2


def get_last_published_workfile_representation(
    project_name, folder_id, task_id, workfile_extensions=None
):
    """Last published workfile representation for a task.

    Shared by the pre-launch hook (which passes the host's workfile
    extensions) and the auto-download service (which accepts any).

    Args:
        project_name (str): Project name.
        folder_id (str): Folder id.
        task_id (str): Task id - only versions of this task match.
        workfile_extensions (Optional[Iterable[str]]): Extensions with
            leading dot to accept. All accepted when None.

    Returns:
        Union[dict, None]: Representation entity or None.
    """
    kwargs = dict(
        folder_ids={folder_id},
        product_base_types={"workfile"},
    )
    # TODO add requirement for AYON launcher 1.4.3 when removed
    if not is_func_signature_supported(
        get_products, project_name, **kwargs
    ):
        kwargs["product_types"] = kwargs.pop("product_base_types")

    product_entities = get_products(project_name, **kwargs)
    product_ids = {
        product_entity["id"]
        for product_entity in product_entities
    }
    if not product_ids:
        return None

    versions_by_product_id = get_last_versions(
        project_name,
        product_ids
    )
    version_ids = {
        version_entity["id"]
        for version_entity in versions_by_product_id.values()
        if version_entity["taskId"] == task_id
    }
    if not version_ids:
        return None

    for representation_entity in get_representations(
        project_name,
        version_ids=version_ids,
    ):
        if workfile_extensions is None:
            return representation_entity
        ext = representation_entity["context"].get("ext")
        if not ext:
            continue
        if ".{}".format(ext) in workfile_extensions:
            return representation_entity
    return None


def get_linked_representation_id(
    project_name,
    repre_entity,
    link_type,
    max_depth=None
):
    """Returns list of linked ids of particular type (if provided).

    One of representation document or representation id must be passed.
    Note:
        Representation links now works only from representation through
            version back to representations.

    Todos:
        Missing depth query. Not sure how it did find more representations
            in depth, probably links to version?
        This function should probably live in sitesync addon?

    Args:
        project_name (str): Name of project where look for links.
        repre_entity (dict[str, Any]): Representation entity.
        link_type (str): Type of link (e.g. 'reference', ...).
        max_depth (int): Limit recursion level. Default: 0

    Returns:
        List[ObjectId] Linked representation ids.
    """

    if not repre_entity:
        return []

    version_id = repre_entity["versionId"]
    if max_depth is None or max_depth == 0:
        max_depth = 1

    link_types = None
    if link_type:
        link_types = [link_type]

    # Store already found version ids to avoid recursion, and also to store
    #   output -> Don't forget to remove 'version_id' at the end!!!
    linked_version_ids = {version_id}
    # Each loop of depth will reset this variable
    versions_to_check = {version_id}
    for _ in range(max_depth):
        if not versions_to_check:
            break

        versions_links = get_versions_links(
            project_name,
            versions_to_check,
            link_types=link_types,
            link_direction="in")  # looking for 'in'puts for version

        versions_to_check = set()
        for links in versions_links.values():
            for link in links:
                # Care only about version links
                if link["entityType"] != "version":
                    continue
                entity_id = link["entityId"]
                linked_version_ids.add(entity_id)
                versions_to_check.add(entity_id)

    linked_version_ids.remove(version_id)
    if not linked_version_ids:
        return []
    representations = get_representations(
        project_name,
        version_ids=linked_version_ids,
        fields=["id"])
    return [
        repre["id"]
        for repre in representations
    ]
