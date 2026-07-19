"""Python 3 only implementation."""
import os
import asyncio
import threading
import concurrent.futures
import time

from typing import Union

from ayon_core.lib import get_local_site_id
from ayon_core.addon import AddonsManager
from ayon_core.lib import Logger
from ayon_core.pipeline import Anatomy
from ayon_core.pipeline.load import get_representation_path_with_anatomy

from .providers import lib
from .providers.exceptions import (
    TransferPausedError,
    TransientTransferError,
)
from .utils import SyncStatus, ResumableError, get_linked_representation_id
from .auto_download import AutoDownloader

# Wall-clock ceiling for the blocking workfile download during launch.
# The retry-based bail-out below only fires when transfer ATTEMPTS fail -
# with no tray/sync service running the record just sits QUEUED, retries
# stays 0 and the artist used to be stuck in the launch dialog forever.
_WORKFILE_WAIT_CEILING = 1200  # seconds

# How often (per project) the loop asks the server to requeue IN_PROGRESS
# files that stopped receiving updates (tray killed mid-transfer). The
# server sweeps EVERY site of the project (a wiped machine's own tray is
# exactly the one that will never ask for its site) and judges staleness
# by its OWN clock stamps, so the age just needs to exceed any realistic
# progress-post gap.
_STALE_REQUEUE_INTERVAL = 900  # seconds
_STALE_REQUEUE_AGE = 3600  # seconds without an update = dead transfer


async def upload(
    addon,
    project_name,
    file,
    representation,
    provider_name,
    remote_site_name,
    tree=None,
    preset=None
):
    """Upload representation file.

    Upload single 'file' of a 'representation' to 'provider'.
    Source url is taken from 'file' portion, where {root} placeholder
    is replaced by 'representation.Context.root'
    Provider could be one of implemented in provider.py.

    Updates database, fills in id of file from provider (ie. file_id
        from GDrive), 'created_dt' - time of upload

    Value of 'provider_name' doesn't have to match to 'site_name', single
    provider (GDrive) might have multiple sites ('projectA', 'projectB')

    Args:
        addon (SiteSyncAddon): object to run SiteSyncAddon API
        project_name (str): Project name.
        file (dict[str, Any]): of file from representation in Mongo
        representation (dictionary): of representation
        provider_name (str): gdrive, gdc etc.
        remote_site_name (string): Site on provider, single provider(gdrive)
            could have multiple sites (different accounts, credentials)
        tree (Optional[dict]): Injected memory structure for performance.
        preset (Optional[dict]): site config ('credentials_url', 'root'...)

    """
    # create ids sequentially, upload file in parallel later
    with addon.lock:
        # this part modifies structure on 'remote_site', only single
        # thread can do that at a time, upload/download to prepared
        # structure should be run in parallel
        remote_handler = lib.factory.get_provider(
            provider_name,
            project_name,
            remote_site_name,
            tree=tree,
            presets=preset
        )

        file_path = file.get("path", "")

        local_file_path, remote_file_path = resolve_paths(
            addon, file_path, project_name,
            remote_site_name, remote_handler
        )

        target_folder = os.path.dirname(remote_file_path)
        folder_id = remote_handler.create_folder(target_folder)

        if not folder_id:
            err = "Folder {} wasn't created. Check permissions.". \
                format(target_folder)
            raise NotADirectoryError(err)

    loop = asyncio.get_running_loop()
    file_id = await loop.run_in_executor(
        None,
        remote_handler.upload_file,
        local_file_path,
        remote_file_path,
        addon,
        project_name,
        file,
        representation,
        remote_site_name,
        True
    )

    return file_id


async def download(
    addon,
    project_name,
    file,
    representation,
    provider_name,
    remote_site_name,
    tree=None,
    preset=None
):
    """Downloads file to local folder denoted in representation.Context.

    Args:
        addon (SiteSyncAddon): SiteSyncAddon object.
        project_name (str): Project name.
        file (dict) : Info about processed file.
        representation (dict):  repr that 'file' belongs to
        provider_name (str):  'gdrive' etc
        remote_site_name (str): site on provider, single provider(gdrive)
            could have multiple sites (different accounts, credentials)
        tree (Optional[dict]): Injected memory structure for performance.
        preset (Optional[dict]): Site config ('credentials_url', 'root'...).

    Returns:
        str: Name of local file

    """
    with addon.lock:
        remote_handler = lib.factory.get_provider(
            provider_name,
            project_name,
            remote_site_name,
            tree=tree,
            presets=preset
        )

        file_path = file.get("path", "")
        local_file_path, remote_file_path = resolve_paths(
            addon, file_path, project_name, remote_site_name, remote_handler
        )

        local_folder = os.path.dirname(local_file_path)
        os.makedirs(local_folder, exist_ok=True)

    local_site = addon.get_active_site(project_name)

    loop = asyncio.get_running_loop()
    file_id = await loop.run_in_executor(
        None,
        remote_handler.download_file,
        remote_file_path,
        local_file_path,
        addon,
        project_name,
        file,
        representation,
        local_site,
        True
    )

    # Provider-agnostic integrity check: the DB knows the expected size,
    # so never mark a short/corrupt download OK. (local_drive/sftp verify
    # sizes themselves; cloud providers used to trust their API blindly.)
    expected_size = file.get("size")
    if expected_size:
        try:
            actual_size = os.path.getsize(local_file_path)
        except OSError:
            actual_size = None
        if actual_size is not None and actual_size != expected_size:
            try:
                os.remove(local_file_path)
            except OSError:
                pass
            raise OSError(
                "Downloaded file '{}' has size {} but the representation"
                " expects {} - removed, will retry".format(
                    local_file_path, actual_size, expected_size)
            )

    return file_id


def resolve_paths(
    addon, file_path, project_name, remote_site_name=None, remote_handler=None
):
    """Resolve local and remote full path.

    Returns tuple of local and remote file paths with {root}
    placeholders replaced with proper values from Settings or Anatomy

    Ejected here because of Python 2 hosts (GDriveHandler is an issue)

    Args:
        addon (SiteSyncAddon): object to run SiteSyncAddon API
        file_path (str): File path with {root}.
        project_name (str): Project name.
        remote_site_name (Optional[str]): Remote site name.
        remote_handler (Optional[AbstractProvider]): implementation

    Returns:
        tuple[str, str]: Proper absolute paths, remote path is optional.

    """
    remote_file_path = ""
    if remote_handler:
        remote_file_path = remote_handler.resolve_path(file_path)

    local_handler = lib.factory.get_provider(
        "local_drive", project_name, addon.get_active_site(project_name)
    )
    local_file_path = local_handler.resolve_path(file_path)

    return local_file_path, remote_file_path


def _site_is_working(addon, project_name, site_name, site_config):
    """Confirm that 'site_name' is configured correctly for 'project_name'.

    Must be here as lib.factory access doesn't work in Python 2 hosts.

    Args:
        addon (SiteSyncAddon): SiteSyncAddon object.
        project_name (str): Project name.
        site_name (str): Site name.
        site_config (dict): Configuration for site from Settings.

    Returns
        bool: Site is working.

    """
    provider = addon.get_provider_for_site(site=site_name)
    handler = lib.factory.get_provider(
        provider,
        project_name,
        site_name,
        presets=site_config
    )

    return handler.is_active()


def download_last_published_workfile(
    host_name: str,
    project_name: str,
    task_name: str,
    workfile_representation: dict,
    max_retries: int,
    anatomy: Anatomy = None,
    sitesync_addon=None,
) -> Union[str, None]:
    """Download the last published workfile

    Args:
        host_name (str): Host name.
        project_name (str): Project name.
        task_name (str): Task name.
        workfile_representation (dict): Workfile representation.
        max_retries (int): complete file failure only after so many attempts
        anatomy (Optional[Anatomy]): Project anatomy, used for optimization.
            Defaults to None.
        sitesync_addon (Optional[SiteSyncAddon]): Addons manager,
            used for optimization.

    Returns:
        Union[str, None]: last published workfile path localized

    """
    if not workfile_representation:
        print(
            "Not published workfile for task '{}' and host '{}'.".format(
                task_name, host_name
            )
        )
        return None

    if sitesync_addon is None:
        addons_manager = AddonsManager()
        sitesync_addon = addons_manager.addons_by_name.get("sitesync")

    # Get sync server addon
    if not sitesync_addon or not sitesync_addon.enabled:
        print("Site sync addon is disabled or unavailable.")
        return None

    if not anatomy:
        anatomy = Anatomy(project_name)

    last_published_workfile_path = get_representation_path_with_anatomy(
        workfile_representation, anatomy
    )
    if not last_published_workfile_path:
        return None

    # If representation isn't available on remote site, then return.
    remote_site = sitesync_addon.get_remote_site(project_name)
    if not sitesync_addon.is_representation_on_site(
        project_name,
        workfile_representation["id"],
        remote_site,
    ):
        print(
            "Representation not available for task '{}', site '{}'".format(
                task_name, remote_site
            )
        )
        return None

    # Get local site
    local_site_id = get_local_site_id()

    # Add workfile representation to local site
    representation_ids = {workfile_representation["id"]}
    representation_ids.update(
        get_linked_representation_id(
            project_name, workfile_representation, "reference"
        )
    )
    for repre_id in representation_ids:
        if not sitesync_addon.is_representation_on_site(
            project_name, repre_id, local_site_id
        ):
            try:
                sitesync_addon.add_site(
                    project_name,
                    repre_id,
                    local_site_id,
                    force=True,
                    # the artist is blocked in a launch dialog waiting
                    # for this download - jump the queue
                    priority=99,
                    # reference links were already followed above
                    follow_links=False,
                )
            except ValueError as exc:
                # add_site refuses to queue a download whose remote side
                # is not fully synced (it would mint the QUEUED/NA pair
                # the loop can never match). A linked dependency that is
                # not on the remote site yet must not break the launch -
                # the workfile itself was verified above.
                print(
                    "Skipping linked representation {}: {}".format(
                        repre_id, exc)
                )
    sitesync_addon.reset_timer()
    print("Starting to download:{}".format(last_published_workfile_path))
    # While representation unavailable locally, wait - but never forever:
    # 'max_retries' only counts FAILED transfer attempts, so with no
    # tray/sync service running the record sits QUEUED with retries at 0
    # and this used to block the launch indefinitely.
    wait_started = time.time()
    while not sitesync_addon.is_representation_on_site(
        project_name,
        workfile_representation["id"],
        local_site_id,
        max_retries=max_retries
    ):
        if time.time() - wait_started > _WORKFILE_WAIT_CEILING:
            print(
                "Timed out after {}s waiting for the workfile download -"
                " is the tray (or a sync service) running? Launching"
                " without the published workfile.".format(
                    _WORKFILE_WAIT_CEILING)
            )
            return None
        time.sleep(5)

    return last_published_workfile_path


class SiteSyncThread(threading.Thread):
    """
        Separate thread running synchronization server with asyncio loop.
        Stopped when tray is closed.
    """
    def __init__(self, addon):
        self.log = Logger.get_logger(self.__class__.__name__)
        super(SiteSyncThread, self).__init__()
        self.addon = addon
        self.loop = None
        self.is_running = False
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        self._warned_keys = set()
        self.auto_downloader = AutoDownloader(addon)
        # projects whose zero-record representations were already
        # backfilled this tray session
        self._backfilled_projects = set()
        # Wake handling: '_wake_event' (created on the thread's own
        # event loop in run()) is awaited instead of a plain sleep, so a
        # wake arriving at ANY moment - mid-pass, or in the gap between
        # finishing a pass and starting the wait - is never lost. The
        # old cancel-the-timer-task approach lost wakes that landed
        # while a pass was running (cancelling an already-finished task
        # is a no-op) and the triggering transfer waited out the full
        # loop_delay. '_reset_requested' only backstops calls arriving
        # before the loop has started.
        self._wake_event = None
        self._reset_requested = False
        # last per-project ask to requeue stale IN_PROGRESS files
        self._stale_requeue_last = {}

    def run(self):
        self.is_running = True

        try:
            self.log.info("Starting SiteSync")
            self.loop = asyncio.new_event_loop()  # create new loop for thread
            asyncio.set_event_loop(self.loop)
            self.loop.set_default_executor(self.executor)
            # created here, after set_event_loop, so the primitive binds
            # to THIS thread's loop on every supported Python version
            self._wake_event = asyncio.Event()

            asyncio.ensure_future(self.check_shutdown(), loop=self.loop)
            asyncio.ensure_future(self.sync_loop(), loop=self.loop)
            self.log.info("SiteSync Started")
            self.loop.run_forever()
        except Exception:
            self.log.warning(
                "SiteSync service has failed", exc_info=True
            )
        finally:
            self.loop.close()  # optional

    async def sync_loop(self):
        """
            Runs permanently, each time:
                - gets list of collections in DB
                - gets list of active remote providers (has configuration,
                    credentials)
                - for each project_name it looks for representations that
                  should be synced
                - synchronize found collections
                - update representations - fills error messages for exceptions
                - waits X seconds and repeat
        Returns:

        """
        while self.is_running:
            # Skip work while paused instead of exiting: with the pause
            # check in the 'while' condition (previous behaviour) pausing
            # ended this coroutine permanently and resume never worked -
            # which is why the tray couldn't offer pause/resume.
            if self.addon.is_paused():
                await asyncio.sleep(5)
                continue
            try:
                start_time = time.time()
                self.addon.set_sync_project_settings()  # clean cache
                project_name = None
                enabled_projects = self.addon.get_enabled_projects()
                for project_name in enabled_projects:
                    # One broken project (bad site config, provider raising
                    # in its constructor, transient DB error) must not stop
                    # syncing for every other project - contain it here.
                    try:
                        # Heal representations that have NO site records
                        # (created by Push-to-project, editorial ingest, or
                        # a publish without sitesync) - an NA/NA pair is
                        # invisible to this loop forever otherwise. Once
                        # per project per tray session, only on machines
                        # whose pair actually syncs, and off the event
                        # loop (a large first-time backfill must not block
                        # 'check_shutdown'/'reset_timer' handling).
                        if project_name not in self._backfilled_projects:
                            self._backfilled_projects.add(project_name)
                            active = self.addon.get_active_site(project_name)
                            remote = self.addon.get_remote_site(project_name)
                            if active != remote:
                                await self.loop.run_in_executor(
                                    None,
                                    self._backfill_site_records,
                                    project_name,
                                )
                        # Un-stick files a dead tray left IN_PROGRESS -
                        # the loop's OK/QUEUED pair fetch can never see
                        # them again otherwise. Throttled per project,
                        # and only from machines that actually sync (a
                        # not-opted-in studio workstation must not
                        # hammer the endpoint) - the server sweeps all
                        # sites in one call, so any syncing tray heals
                        # everyone including dead machines' sites.
                        now = time.time()
                        last_requeue = self._stale_requeue_last.get(
                            project_name, 0)
                        if (
                            now - last_requeue > _STALE_REQUEUE_INTERVAL
                            and self.addon.get_active_site(project_name)
                            != self.addon.get_remote_site(project_name)
                        ):
                            self._stale_requeue_last[project_name] = now
                            await self.loop.run_in_executor(
                                None,
                                self._requeue_stale_transfers,
                                project_name,
                            )
                        # Queue missing assigned-task work first (throttled
                        # internally, never raises) so this very loop pass
                        # picks the new downloads up.
                        self.auto_downloader.process_project(project_name)
                        await self._sync_project(project_name)
                    except asyncio.exceptions.CancelledError:
                        raise
                    except Exception:
                        self.log.warning(
                            "Failed to process project '{}', skipping it"
                            " this loop".format(project_name),
                            exc_info=True
                        )

                duration = time.time() - start_time
                self.log.debug("One loop took {:.2f}s".format(duration))

                delay = self.addon.get_loop_delay(project_name)
                self.log.debug(
                    "Waiting for {} seconds to new loop".format(delay)
                )
                # A wake set at ANY point since the last clear (even
                # mid-pass) makes this return immediately - no window in
                # which a wake can be lost.
                if not self._reset_requested:
                    try:
                        await asyncio.wait_for(
                            self._wake_event.wait(), timeout=delay
                        )
                    except asyncio.TimeoutError:
                        pass
                self._wake_event.clear()
                self._reset_requested = False

            except ConnectionResetError:
                self.log.warning(
                    "ConnectionResetError in sync loop, trying next loop",
                    exc_info=True)
            except asyncio.exceptions.CancelledError:
                # shutdown is cancelling this coroutine's wait
                self._reset_requested = False
            except ResumableError:
                self.log.warning(
                    "ResumableError in sync loop, trying next loop",
                    exc_info=True)
            except Exception:
                # A silently stopped thread is the worst possible outcome:
                # nothing syncs anywhere and there is no user-visible signal.
                # Log loudly and try again next loop instead of stopping.
                self.log.warning(
                    "Unhandled except. in sync loop, trying next loop",
                    exc_info=True)
                # The failure may have happened before the loop's own timer
                # ran (e.g. fetching settings), so wait here to avoid
                # hammering an unreachable server in a hot loop.
                await asyncio.sleep(30)

    def _requeue_stale_transfers(self, project_name):
        """Best-effort server-side requeue of dead IN_PROGRESS files.

        One call sweeps every site of the project. Contained: a
        pre-requeueStale server (404) or transient error only logs
        (warn-once) and the next interval retries.
        """
        try:
            count = self.addon.requeue_stale_transfers(
                project_name, older_than_seconds=_STALE_REQUEUE_AGE
            )
            if count:
                self.log.info(
                    "Requeued {} stalled IN_PROGRESS file(s)"
                    " in '{}'".format(count, project_name)
                )
        except Exception:
            self._warn_once(
                (project_name, "requeue_stale"),
                (
                    "Couldn't requeue stale transfers for '{}'"
                    " (old server without the endpoint?)"
                ).format(project_name),
                exc_info=True
            )

    def _backfill_site_records(self, project_name):
        """Best-effort server-side backfill of zero-record representations.

        Contained like everything else in the per-project loop body - a
        failure (old server without the endpoint, transient error) only
        logs and the next tray session retries.
        """
        try:
            count = self.addon.backfill_missing_site_records(project_name)
            if count:
                self.log.info(
                    "Backfilled {} representation(s) without site records"
                    " in '{}' (stamped as available on the remote"
                    " site)".format(count, project_name)
                )
        except Exception:
            self.log.warning(
                "Couldn't backfill missing site records for '{}'".format(
                    project_name),
                exc_info=True
            )

    def stop(self):
        """Sets is_running flag to false, 'check_shutdown' shuts server down"""
        self.is_running = False

    async def check_shutdown(self):
        """ Future that is running and checks if server should be running
            periodically.
        """
        while self.is_running:
            # This coroutine is the only consumer of long-running tasks
            # AND the only path to a clean loop shutdown - an escaped
            # exception here would kill both, so contain everything.
            # (Scheduled funcs are supposed to never raise, but that
            # invariant lives in their authors' hands.)
            try:
                if self.addon.long_running_tasks:
                    task = self.addon.long_running_tasks.pop()
                    self.log.info("starting long running")
                    try:
                        await self.loop.run_in_executor(None, task["func"])
                    finally:
                        self.log.info("finished long running")
                        self.addon.projects_processed.discard(
                            task["project_name"])
            except asyncio.exceptions.CancelledError:
                raise
            except Exception:
                self.log.warning(
                    "Long running task failed", exc_info=True
                )
            await asyncio.sleep(0.5)

        tasks = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        ]
        list(map(lambda task: task.cancel(), tasks))  # cancel all the tasks
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.log.debug(
            f"Finished awaiting cancelled tasks, results: {results}...")
        await self.loop.shutdown_asyncgens()
        # to really make sure everything else has time to stop
        self.executor.shutdown(wait=True)
        await asyncio.sleep(0.07)
        self.loop.stop()

    def reset_timer(self):
        """Called when waiting for next loop should be skipped"""
        self.log.debug("Resetting timer")
        # Backstop for calls arriving before run() created the event -
        # the loop checks this flag before every wait. Plain attribute
        # write, atomic enough for the foreign threads calling this.
        self._reset_requested = True
        loop = self.loop
        event = self._wake_event
        if loop is not None and loop.is_running() and event is not None:
            # Callers live in other threads (tray UI, tray webserver
            # route) while the event belongs to this thread's loop -
            # setting it directly from a foreign thread is not safe.
            loop.call_soon_threadsafe(event.set)

    def _warn_once(self, key, message, exc_info=False):
        """Log a warning only once per thread lifetime for given 'key'.

        The sync loop revisits the same misconfiguration every iteration;
        without deduplication a persistent problem floods the log until the
        real message drowns.
        """
        if key in self._warned_keys:
            return
        self._warned_keys.add(key)
        # never pass exc_info=False through: logging stores the literal
        # False on the record and ayon-core's formatter subscripts
        # record.exc_info -> TypeError ('bool' object is not subscriptable)
        if exc_info:
            self.log.warning(message, exc_info=True)
        else:
            self.log.warning(message)

    def _working_sites(self, project_name, sync_config):
        if self.addon.is_project_paused(project_name):
            self.log.debug(
                "Project '{}' is paused, skipping".format(project_name))
            return None, None

        local_site = self.addon.get_active_site(project_name)
        remote_site = self.addon.get_remote_site(project_name)
        if local_site == remote_site:
            self.log.debug("{}-{} sites same, skipping".format(
                local_site, remote_site))
            return None, None

        sites_config = sync_config.get("sites") or {}
        for site_name in (local_site, remote_site):
            # A direct [site_name] KeyError here used to propagate and kill
            # the sync loop for every project.
            if site_name not in sites_config:
                self._warn_once(
                    (project_name, site_name, "unconfigured"),
                    (
                        "Site '{}' used by project '{}' is not configured"
                        " in its SiteSync 'sites' settings - skipping the"
                        " project."
                    ).format(site_name, project_name)
                )
                return None, None

        # Check each site separately so the warning can name the culprit.
        for site_name in (local_site, remote_site):
            try:
                site_working = _site_is_working(
                    self.addon, project_name, site_name,
                    sites_config[site_name]
                )
            except Exception:
                # Provider handlers (e.g. rclone) may raise from their
                # constructor on bad configuration - degrade to "site not
                # working" instead of letting it crash the loop.
                self._warn_once(
                    (project_name, site_name, "provider_error"),
                    (
                        "Misconfigured provider for site '{}' in project"
                        " '{}', skipping the project."
                    ).format(site_name, project_name),
                    exc_info=True
                )
                return None, None
            if not site_working:
                self._warn_once(
                    (project_name, site_name, "not_working"),
                    (
                        "Site '{}' of project '{}' is not working"
                        " (unreachable root, missing credentials or"
                        " misconfigured provider) - project is skipped"
                        " until it recovers. Look for a 'root' warning"
                        " above naming the exact path."
                    ).format(site_name, project_name)
                )
                return None, None

        return local_site, remote_site

    def _get_remote_provider_info(
        self, project_name, remote_site, site_preset
    ):
        remote_provider = self.addon.get_provider_for_site(site=remote_site)
        handler = lib.factory.get_provider(
            remote_provider,
            project_name,
            remote_site,
            presets=site_preset
        )
        limit = lib.factory.get_provider_batch_limit(remote_provider)

        return handler, remote_provider, limit

    async def _sync_project(self, project_name):
        self.log.info(f"Processing '{project_name}'")
        preset = self.addon.sync_project_settings[project_name]

        local_site, remote_site = self._working_sites(
            project_name, preset
        )
        if not local_site or not remote_site:
            return

        remote_site_preset = preset.get("sites")[remote_site]

        handler, remote_provider, limit = self._get_remote_provider_info(
            project_name,
            remote_site,
            remote_site_preset
        )

        repre_states = self.addon.get_sync_representations(
            project_name,
            local_site,
            remote_site,
            limit
        )

        task_files_to_process = []
        files_processed_info = []
        # process only unique file paths in one batch
        # multiple representation could have same file path
        # (textures),
        # upload process can find already uploaded file and
        # reuse same id
        processed_file_path = set()

        # first call to get_provider could be expensive, its
        # building folder tree structure in memory
        # call only if needed, eg. DO_UPLOAD or DO_DOWNLOAD
        for repre_state in repre_states:
            repre_id = repre_state["representationId"]
            # QUESTION Why is not project passed in?
            # QUESTION Why there is not option to check all representations
            #    in one batch?
            if self.addon.is_representation_paused(repre_id):
                continue
            file_states = repre_state.get("files") or []
            for file_state in file_states:
                # skip already processed files
                # WARNING Using empty string for path is dangerous!!!
                file_path = file_state.get("path", "")
                if file_path in processed_file_path:
                    continue
                status = self.addon.check_status(
                    file_state,
                    local_site,
                    remote_site,
                    preset.get("config")
                )
                if (status == SyncStatus.DO_UPLOAD and
                        len(task_files_to_process) < limit):
                    tree = handler.get_tree()
                    task = asyncio.create_task(
                        upload(
                            self.addon,
                            project_name,
                            file_state,
                            repre_state,
                            remote_provider,
                            remote_site,
                            tree,
                            remote_site_preset
                        )
                    )
                    task_files_to_process.append(task)
                    # store info for exception handlingy
                    files_processed_info.append((
                        file_state,
                        repre_state,
                        remote_site,
                        "remote",
                        project_name
                    ))
                    processed_file_path.add(file_path)

                if (status == SyncStatus.DO_DOWNLOAD and
                        len(task_files_to_process) < limit):
                    tree = handler.get_tree()
                    task = asyncio.create_task(
                        download(
                            self.addon,
                            project_name,
                            file_state,
                            repre_state,
                            remote_provider,
                            remote_site,
                            tree,
                            remote_site_preset
                        )
                    )
                    task_files_to_process.append(task)

                    files_processed_info.append((
                        file_state,
                        repre_state,
                        local_site,
                        "local",
                        project_name
                    ))
                    processed_file_path.add(file_path)

        self.log.debug("Sync tasks count {}".format(
            len(task_files_to_process)
        ))
        files_created = await asyncio.gather(
            *task_files_to_process,
            return_exceptions=True
        )

        for file_result, info in zip(files_created, files_processed_info):
            file_state, repre_status, site_name, side, project_name = info
            error = None
            if isinstance(
                file_result, (TransferPausedError, TransientTransferError)
            ):
                # A pause is deliberate and a quota/rate-limit heals on
                # its own - neither is a failure: requeue the file (its
                # progress posts left it IN_PROGRESS, which the loop's
                # OK/QUEUED pair fetch could never see again) without
                # counting a retry or storing an error.
                try:
                    self.addon.update_db(
                        project_name=project_name,
                        new_file_id=None,
                        file=file_state,
                        repre_status=repre_status,
                        site_name=site_name,
                        side=side,
                        requeue=True
                    )
                except Exception:
                    self.log.warning(
                        "Couldn't requeue paused file", exc_info=True)
                continue
            if isinstance(file_result, BaseException):
                error = str(file_result)
                self.log.warning(error, exc_info=True)
                file_result = None  # it is exception >> no id >> reset

            # One failed status POST (server blip) must not drop the
            # results of every remaining file in the batch - those files
            # DID transfer and would be transferred again next pass.
            try:
                self.addon.update_db(
                    project_name=project_name,
                    new_file_id=file_result,
                    file=file_state,
                    repre_status=repre_status,
                    site_name=site_name,
                    side=side,
                    error=error
                )

                repre_id = repre_status["representationId"]
                self.addon.handle_alternate_site(
                    project_name,
                    repre_id,
                    site_name,
                    file_state["fileHash"]
                )
            except Exception:
                self.log.warning(
                    "Couldn't update status of '{}' on '{}' - will retry"
                    " next loop".format(
                        file_state.get("path"), site_name),
                    exc_info=True
                )
