import typing
from pydantic import Field, validator

from ayon_server.settings import (
    BaseSettingsModel,
    ensure_unique_names,
    normalize_name,
)

from .providers.local_drive import LocalDriveSubmodel
from .providers.gdrive import GoogleDriveSubmodel
from .providers.dropbox import DropboxSubmodel
from .providers.sftp import SFTPSubmodel
from .providers.rclone import RCloneSubmodel

if typing.TYPE_CHECKING:
    from ayon_server.addons import BaseServerAddon


class GeneralSubmodel(BaseSettingsModel):
    """Properties for loop and module configuration"""
    retry_cnt: int = Field(3, title="Retry Count")
    loop_delay: int = Field(60, title="Loop Delay")
    always_accessible_on: list[str] = Field([],
                                            title="Always accessible on sites")
    active_site: str = Field("studio", title="User Default Active Site")
    remote_site: str = Field("studio", title="User Default Remote Site")
    enable_auto_download: bool = Field(
        True,
        title="Auto-download assigned work",
        description="On remote machines, automatically download the last"
                    " published workfile of each task assigned to the"
                    " logged-in user, plus the representations it"
                    " references."
    )
    auto_download_interval: int = Field(
        300,
        title="Auto-download check interval (s)",
        description="How often to look for new assigned work to download."
    )
    auto_download_link_depth: int = Field(
        2,
        ge=1,
        le=5,
        title="Auto-download link depth",
        description="How many levels of 'reference' links to follow from"
                    " an auto-downloaded workfile. 1 downloads only what"
                    " the workfile loaded directly; 2 (default) also"
                    " downloads what those inputs reference (e.g. a"
                    " loaded asset's own linked dependencies). Each extra"
                    " level costs additional link queries per task."
    )
    min_free_space_gb: int = Field(
        5,
        title="Auto-download minimum free space (GB)",
        description="Skip auto-download when free disk space under the"
                    " local roots drops below this."
    )
    opened_task_retention_days: int = Field(
        14,
        title="Keep opened tasks synced for (days)",
        description="Tasks an artist opened (assigned or not) keep"
                    " auto-downloading new published work for this many"
                    " days after the last open. 0 disables opened-task"
                    " tracking."
    )
    mirror_workarea_workfiles: bool = Field(
        True,
        title="Fetch work-area workfiles from studio",
        description="On remote machines with the studio share reachable"
                    " (VPN), copy missing work-area workfiles of assigned"
                    " and opened tasks to the local site by direct file"
                    " copy. Existing local files are never overwritten."
    )


class RootSubmodel(BaseSettingsModel):
    """Setup root paths for local site.

    Studio roots overrides are in separate `Roots` tab outside of Site Sync.
    """

    _layout: str = "expanded"

    name: str = Field(
        "work",
        title="Root name",
        regex="^[a-zA-Z0-9_]{1,}$",
        scope=["site"],
    )

    path: str = Field(
        "",
        title="Path",
        scope=["site"],
    )


def provider_resolver():
    """Return a list of value/label dicts for the enumerator.

    Returning a list of dicts is used to allow for a custom label to be
    displayed in the UI.
    """
    provider_dict = {
        "gdrive": "Google Drive",
        "local_drive": "Local Drive",
        "dropbox": "Dropbox",
        "sftp": "SFTP",
        "rclone": "Rclone"
    }
    return [{"value": f"{key}", "label": f"{label}"}
            for key, label in provider_dict.items()]


async def defined_sited_enum_resolver(
    addon: "BaseServerAddon",
    settings_variant: str = "production",
    project_name: str | None = None,
) -> list[str]:
    """Provides list of names of configured syncable sites."""
    if addon is None:
        return []

    if project_name:
        settings = await addon.get_project_settings(project_name=project_name,
                                                    variant=settings_variant)
    else:
        settings =  await addon.get_studio_settings(variant=settings_variant)

    sites = ["local", "studio"]
    for site_model in settings.sites:
        sites.append(site_model.name)

    return sites


provider_enum = provider_resolver()


class SitesSubmodel(BaseSettingsModel):
    """Configured additional sites and properties for their providers"""
    _layout = "expanded"

    alternative_sites: list[str] = Field(
        default_factory=list,
        title="Alternative sites",
        scope=["studio", "project"],
        description="Files on this site are/should physically present on these"
                    " sites. Example sftp site exposes files from 'studio' "
                    " site"
    )

    provider: str = Field(
        "",
        title="Provider",
        description="Switch between providers",
        enum_resolver=lambda: provider_enum,
        conditional_enum=True
    )

    local_drive: LocalDriveSubmodel = Field(
        default_factory=LocalDriveSubmodel,
        scope=["studio", "project", "site"]
    )
    gdrive: GoogleDriveSubmodel = Field(
        default_factory=GoogleDriveSubmodel,
        scope=["studio", "project", "site"]
    )
    dropbox: DropboxSubmodel = Field(
        default_factory=DropboxSubmodel,
        scope=["studio", "project", "site"]
    )
    sftp: SFTPSubmodel = Field(
        default_factory=SFTPSubmodel,
        scope=["studio", "project", "site"]
    )
    rclone: RCloneSubmodel = Field(
        default_factory=RCloneSubmodel,
        scope=["studio", "project", "site"]
    )

    name: str = Field(..., title="Site name",
                      scope=["studio", "project", "site"])

    @validator("name")
    def validate_name(cls, value):
        """Ensure name does not contain weird characters"""
        return normalize_name(value)


class LocalSubmodel(BaseSettingsModel):
    """Select your local and remote site"""
    # Site sync is opt-in per artist site: without this toggle (or an
    # explicit active/remote pair below) a machine behaves as a plain
    # studio workstation - no syncing, no local roots, no prompts.
    sync_enabled: bool = Field(
        False,
        title="Use site sync on this machine",
        scope=["site"],
        description="Work in a local folder and sync published files"
                    " with the studio in the background. Off: files are"
                    " used directly from the studio storage."
    )
    active_site: str = Field("",
                             title="My Active Site",
                             scope=["site"],
                             enum_resolver=defined_sited_enum_resolver)

    remote_site: str = Field("",
                             title="My Remote Site",
                             scope=["site"],
                             enum_resolver=defined_sited_enum_resolver)

    # Empty by default (used to be a 'C:/projects_local' placeholder,
    # wrong on every non-Windows machine): with no explicit override the
    # client synthesizes a platform-aware '~/AYON_local/<root>' default
    # for remote machines. Explicit artist values are stored as overrides
    # and are never affected by this default.
    local_roots: list[RootSubmodel] = Field(
        default_factory=list,
        title="Local roots overrides",
        scope=["site"],
        description="Overrides for local root(s). Leave empty to use the"
                    " automatic '~/AYON_local' folder on remote machines."
    )


class SiteSyncSettings(BaseSettingsModel):
    """Settings for synchronization process"""
    enabled: bool = Field(False)

    config: GeneralSubmodel = Field(
        default_factory=GeneralSubmodel,
        title="Config"
    )

    local_setting: LocalSubmodel = Field(
        default_factory=LocalSubmodel,
        title="Local setting",
        scope=["site"],
        description="This setting is only applicable for artist's site",
    )

    sites: list[SitesSubmodel] = Field(
        default_factory=list,
        scope=["studio", "project", "site"],
        title="Sites",
    )

    @validator("sites")
    def ensure_unique_names(cls, value):
        """Ensure name fields within the lists have unique names."""
        ensure_unique_names(value)
        return value
