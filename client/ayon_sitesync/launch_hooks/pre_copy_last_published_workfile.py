import os
import shutil

from ayon_core.lib import filter_profiles
from ayon_core.pipeline.template_data import get_template_data
from ayon_core.pipeline.workfile import get_workfile_template_key
from ayon_core.pipeline.workfile import should_use_last_workfile_on_launch

from ayon_applications import PreLaunchHook

from ayon_sitesync.sitesync import download_last_published_workfile
from ayon_sitesync.task_tracking import record_opened_task
from ayon_sitesync.utils import get_last_published_workfile_representation
from ayon_sitesync.workarea_mirror import mirror_workarea_files


class CopyLastPublishedWorkfile(PreLaunchHook):
    """Copy last published workfile as first workfile.

    Prelaunch hook works only if last workfile leads to not existing file.
        - That is possible only if it's first version.
    """

    # Before `AddLastWorkfileToLaunchArgs`
    order = -1
    # any DCC could be used but TrayPublisher and other specials
    app_groups = ["blender", "photoshop", "tvpaint", "aftereffects",
                  "nuke", "nukeassist", "nukex", "hiero", "nukestudio",
                  "maya", "harmony", "celaction", "flame", "fusion",
                  "houdini", "resolve", "unreal", "substancepainter",
                  "substancedesigner", "motionbuilder", "gaffer",
                  "openrv", "premiere"]

    def execute(self):
        """Check if local workfile doesn't exist, else copy it.

        1- Check if setting for this feature is enabled
        2- Check if workfile in work area doesn't exist
        3- Check if published workfile exists and is copied locally in publish
        4- Substitute copied published workfile as first workfile
           with incremented version by +1

        Returns:
            None: This is a void method.
        """
        workfile_path = self.data.get("workfile_path")
        if workfile_path:
            self.log.debug("Explicit workfile path to open is defined.")
            return

        project_name = self.data["project_name"]
        sitesync_addon = self.addons_manager.get("sitesync")
        if (
            not sitesync_addon
            or not sitesync_addon.enabled
            or not sitesync_addon.is_project_enabled(project_name, True)
        ):
            self.log.debug("Sync server module is not enabled or available")
            return

        # Opening a task expresses interest in it: track it so the
        # auto-download service keeps this shot and its dependencies
        # synced, assigned or not. Recorded before any early return below
        # (existing workfile, disabled seeding, ...) and never fatal.
        try:
            record_opened_task(
                project_name,
                self.data["task_entity"]["id"],
                self.data["folder_entity"]["id"],
            )
        except Exception:
            self.log.warning("Couldn't track opened task", exc_info=True)

        # Check there is no workfile available
        last_workfile = self.data.get("last_workfile_path")
        if os.path.exists(last_workfile):
            self.log.debug(
                "Last workfile exists."
                f" Skipping {self.__class__.__name__} process."
            )
            return

        host_name = self.application.host_name

        host_addon = self.addons_manager.get_host_addon(host_name)
        if host_addon is None:
            self.log.warning(
                f"Host addon not found for host '{host_name}'"
            )
            return

        workfile_extensions = host_addon.get_workfile_extensions()
        if not workfile_extensions:
            self.log.debug(
                "No workfile extensions defined by"
                f" host addon '{host_addon.name}'"
            )
            return

        # Get data
        project_settings = self.data["project_settings"]
        anatomy = self.data["anatomy"]
        task_id = self.data["task_entity"]["id"]
        folder_entity = self.data["folder_entity"]
        folder_id = folder_entity["id"]
        task_name = self.data["task_name"]
        task_type = self.data["task_type"]
        project_entity = self.data["project_entity"]
        task_entity = self.data["task_entity"]

        use_last_published_workfile = should_use_last_workfile_on_launch(
            project_name,
            host_name,
            task_name,
            task_type,
            project_settings=project_settings
        )
        if use_last_published_workfile is False:
            self.log.info(
                f'Project "{project_name}" has turned off to use last'
                ' published workfile as first workfile for host'
                f' "{host_name}"'
            )
            return

        if use_last_published_workfile is None:
            self.log.info(
                "Seems like old version of settings is used."
                f' Can\'t access custom templates in host "{host_name}".'
            )
            return

        # Prefer the artist's actual work-area scenes: fetch the task's
        # workfiles from the studio share by direct copy (work-area files
        # are not representations, sitesync can't transfer them) and open
        # the newest one. Falls through to the published-workfile flow
        # when the share is unreachable or the task has no work-area
        # workfile yet.
        sitesync_settings = sitesync_addon.get_sync_project_setting(
            project_name
        )
        if (sitesync_settings["config"] or {}).get(
            "mirror_workarea_workfiles", True
        ):
            try:
                workarea_paths = mirror_workarea_files(
                    sitesync_addon,
                    project_name,
                    [self.data["task_entity"]["id"]],
                    workfile_extensions,
                )
            except Exception:
                self.log.warning(
                    "Work-area workfile fetch failed", exc_info=True
                )
                workarea_paths = []
            if workarea_paths:
                newest_path = workarea_paths[-1]
                self.log.info(
                    "Using newest work-area workfile: {}".format(
                        newest_path)
                )
                self.data["last_workfile_path"] = newest_path
                return

        # 'should_use_last_workfile_on_launch' only returns the profile's
        # 'enabled' flag; the profile's dedicated
        # 'use_last_published_workfile' toggle was read nowhere in core,
        # so turning it off in Studio Settings did nothing. Honor it here.
        if not self._use_last_published_workfile_enabled(
            host_name, task_name, task_type, project_settings
        ):
            self.log.info(
                f'Profile for host "{host_name}" disables using last'
                " published workfile as first workfile."
            )
            return

        self.log.info("Trying to fetch last published workfile...")

        workfile_representation = get_last_published_workfile_representation(
            project_name, folder_id, task_id, workfile_extensions
        )

        if not workfile_representation:
            self.log.info("Couldn't find published workfile representation")
            return

        max_retries = int(
            sitesync_addon.sync_project_settings
            [project_name]
            ["config"]
            ["retry_cnt"]
        )

        # Copy file and substitute path
        last_published_workfile_path = download_last_published_workfile(
            host_name,
            project_name,
            task_name,
            workfile_representation,
            max_retries,
            anatomy=anatomy,
            sitesync_addon=sitesync_addon,
        )
        if not last_published_workfile_path:
            self.log.debug(
                f"Couldn't download {last_published_workfile_path}"
            )
            return

        # Get workfile data
        workfile_data = get_template_data(
            project_entity, folder_entity, task_entity, host_name,
            project_settings
        )

        extension = last_published_workfile_path.split(".")[-1]
        workfile_data["version"] = (
                workfile_representation["context"]["version"] + 1)
        workfile_data["ext"] = extension

        template_key = get_workfile_template_key(
            task_name, host_name, project_name, project_settings
        )
        template = anatomy.get_template_item("work", template_key, "path")
        local_workfile_path = template.format_strict(workfile_data)

        # Copy last published workfile to local workfile directory
        shutil.copy(
            last_published_workfile_path,
            local_workfile_path,
        )

        self.data["last_workfile_path"] = local_workfile_path
        # Keep source filepath for further path conformation
        self.data["source_filepath"] = last_published_workfile_path

    def _use_last_published_workfile_enabled(
        self, host_name, task_name, task_type, project_settings
    ):
        """Read the profile's 'use_last_published_workfile' toggle.

        Missing settings structures default to enabled - matching the
        profile field's own default.
        """
        try:
            profiles = (
                project_settings
                ["core"]
                ["tools"]
                ["Workfiles"]
                ["last_workfile_on_startup"]
            )
        except (KeyError, TypeError):
            return True
        if not profiles:
            return True
        matching_profile = filter_profiles(
            profiles,
            {
                "task_names": task_name,
                "task_types": task_type,
                "host_names": host_name,
            }
        )
        if not matching_profile:
            return True
        return matching_profile.get("use_last_published_workfile", True)
