from __future__ import annotations
import re
import time
import uuid
from typing import Any, Type
from nxtools import logging
import os
from fastapi import Path, Query, Response

from ayon_server.addons import BaseServerAddon
from ayon_server.exceptions import BadRequestException, ForbiddenException

from ayon_server.access.utils import folder_access_list
from ayon_server.api.dependencies import (
    CurrentUser,
    ProjectName,
    RepresentationID,
)

from ayon_server.entities.representation import RepresentationEntity
from ayon_server.lib.postgres import Postgres
from ayon_server.utils import SQLTool

from .settings.settings import SiteSyncSettings
from .settings.models import (
    FileModel,
    RepresentationStateModel,
    SiteSyncParamsModel,
    SiteSyncSummaryItem,
    SiteSyncSummaryModel,
    SortByEnum,
    StatusEnum,
    SyncStatusModel,
    RepresentationSiteStateModel
)


class SiteSync(BaseServerAddon):
    settings_model: Type[SiteSyncSettings] = SiteSyncSettings

    frontend_scopes: dict[str, Any] = {"project": {}}

    def initialize(self) -> None:

        self.add_endpoint(
            "/{project_name}/get_user_sites",
            self.get_user_sites,
            method="GET",
        )

        self.add_endpoint(
            "/{project_name}/params",
            self.get_site_sync_params,
            method="GET",
        )

        self.add_endpoint(
            "/{project_name}/state",
            self.get_site_sync_state,
            method="GET",
        )

        self.add_endpoint(
            "/{project_name}/state/representations",
            self.get_representations_site_sync_state,
            method="GET",
        )

        self.add_endpoint(
            "/{project_name}/state/resetFailed",
            self.reset_failed_representations,
            method="POST",
        )

        self.add_endpoint(
            "/{project_name}/state/backfill",
            self.backfill_missing_site_records,
            method="POST",
        )

        self.add_endpoint(
            "/{project_name}/state/requeueStale",
            self.requeue_stale_in_progress,
            method="POST",
        )

        # NOTE: registered before the generic
        # '/{project_name}/state/{representation_id}/{site_name}' POST so
        # the literal 'setPriority' segment wins route matching
        self.add_endpoint(
            "/{project_name}/state/setPriority/{representation_id}",
            self.set_representation_priority,
            method="POST",
        )

        self.add_endpoint(
            "/{project_name}/state/{representation_id}/{site_name}",  # noqa
            self.set_site_sync_representation_state,
            method="POST",
        )

        self.add_endpoint(
            "/{project_name}/state/{representation_id}/{site_name}",  # noqa
            self.remove_site_sync_representation_state,
            method="DELETE",
        )

    #
    # GET SITE SYNC PARAMS
    #

    async def get_site_sync_params(
        self,
        project_name: ProjectName,
        user: CurrentUser,
    ) -> SiteSyncParamsModel:

        access_list = await folder_access_list(user, project_name, "read")
        conditions = [
            "r.active IS TRUE",
            "v.active IS TRUE",
            "p.active IS TRUE",
        ]
        if access_list is not None:
            conditions.append(f"h.path like ANY ('{{ {','.join(access_list)} }}')")

        # 'count' MUST be the raw representation row count - the web
        # frontend feeds it straight into the DataTable's 'totalRecords',
        # so a DISTINCT-collapsed value (a handful of repre names) caps
        # the paginator at one page and everything past it becomes
        # unreachable. 'names' stays DISTINCT for the filter dropdown.
        # One aggregate query - the 4-table join runs once, not twice.
        query = f"""
            SELECT
                COUNT(*) as total_count,
                COALESCE(
                    array_agg(DISTINCT r.name), ARRAY[]::VARCHAR[]
                ) as names
            FROM project_{project_name}.representations as r
            INNER JOIN project_{project_name}.versions as v
                ON r.version_id = v.id
            INNER JOIN project_{project_name}.products as p
                ON v.product_id = p.id
            INNER JOIN project_{project_name}.hierarchy as h
                ON p.folder_id = h.id
            {SQLTool.conditions(conditions)}
        """

        total_count = 0
        names = []
        result = await Postgres.fetch(query)
        if result:
            total_count = result[0]["total_count"] or 0
            names = list(result[0]["names"] or [])

        return SiteSyncParamsModel(count=total_count, names=names)

    #
    # GET USER SYNC SITES
    #

    async def get_user_sites(
        self,
        project_name: ProjectName,
        user: CurrentUser,
    ) -> dict[str, list[str]]:
        sites = {"active_site": [], "remote_site": []}
        site_infos = await Postgres.fetch("select id, data from sites")
        for site_info in site_infos:
            site_data = site_info["data"] or {}
            site_users = site_data.get("users") or []
            settings = await self.get_project_site_settings(
                project_name, user.name, site_info["id"]
            )
            settings_dict = settings.dict()
            local_setting = settings_dict["local_setting"]
            config = settings_dict.get("config") or {}

            # Zero-touch default, mirroring the client's
            # `_get_zero_touch_role` exactly: synthesize local/studio only
            # when the addon is enabled for the project, the artist set
            # NEITHER side explicitly (all-or-nothing - a half-explicit
            # pair resolves through the project config, same as the
            # client), the project config pair is degenerate (an
            # admin-forced non-degenerate pair wins) and the machine is
            # opted in via 'sync_enabled'.
            config_pair_degenerate = (
                (config.get("active_site") or "studio")
                == (config.get("remote_site") or "studio")
            )
            zero_touch = (
                settings_dict.get("enabled")
                and not local_setting["active_site"]
                and not local_setting["remote_site"]
                and config_pair_degenerate
                and user.name in site_users
                and local_setting.get("sync_enabled")
            )
            for site_type in ["active_site", "remote_site"]:
                used_site = local_setting[site_type]
                if not used_site and zero_touch:
                    if site_type == "active_site":
                        used_site = "local"
                    else:
                        used_site = "studio"
                if not used_site and (
                    local_setting["active_site"]
                    or local_setting["remote_site"]
                ):
                    # Half-explicit pair: the unset side resolves through
                    # the project config, mirroring the client.
                    used_site = config.get(site_type)
                if not used_site:
                    continue

                if used_site == "local":
                    sites[site_type].append(site_info["id"])
                else:
                    sites[site_type].append(used_site)
        # multiple sites can resolve to the same value (e.g. 'studio')
        for site_type, values in sites.items():
            sites[site_type] = list(dict.fromkeys(values))
        return sites


    #
    # GET SITE SYNC OVERAL STATE
    #
    async def get_site_sync_state(
        self,
        project_name: ProjectName,
        user: CurrentUser,
        representationIds: list[str] | None = Query(
            None,
            description="Filter by representation ids",
            example="['57cf375c749611ed89de0242ac140004']",
        ),
        repreNameFilter: list[str] | None = Query(None,
            description="Filter by representation name"),
        localSite: str = Query(
            ...,
            description="Name of the local site",
            example="Machine42",
        ),
        remoteSite: str = Query(
            ...,
            description="Name of the remote site",
            example="GDrive",
        ),
        folderFilter: str | None = Query(
            None,
            description="Filter folders by name",
            example="sh042",
        ),
        folderIdsFilter: list[str] | None = Query(
            None,
            description="Filter folders by id, eg filtering by asset ids",
            example="['57cf375c749611ed89de0242ac140004']",
        ),
        productFilter: str | None = Query(
            None,
            description="Filter products by name",
            example="animation",
        ),
        versionFilter: int | None = Query(
            None,
            description="Filter products by version",
            example="1",
        ),
        versionIdsFilter: list[str] | None = Query(
            None,
            description="Filter versions by ids",
            example="['57cf375c749611ed89de0242ac140004']",
        ),
        localStatusFilter: list[StatusEnum] | None = Query(
            None,
            description=f"List of states to show. Available options: {StatusEnum.__doc__}",
            example=[StatusEnum.QUEUED, StatusEnum.IN_PROGRESS],
        ),
        remoteStatusFilter: list[StatusEnum] | None = Query(
            None,
            description=f"List of states to show. Available options: {StatusEnum.__doc__}",
            example=[StatusEnum.QUEUED, StatusEnum.IN_PROGRESS],
        ),
        sortBy: SortByEnum = Query(
            SortByEnum.folder,
            description="Sort the result by this value",
            example=SortByEnum.folder,
        ),
        sortDesc: bool = Query(
            False,
            name="Sort descending",
            description="Sort the result in descending order",
        ),
        bothOnly: bool = Query(
            False,
            name="Query only with both sites",
            description="Used for front end UI to show only repres with"
                        " both sides",
        ),
        # Pagination
        page: int = Query(1, ge=1),
        pageLength: int = Query(50, ge=1),
    ) -> SiteSyncSummaryModel:
        """Return a site sync state.

        Used for querying representations to be synchronized and state of
        versions and representations to show in Loader UI.
        """
        await check_sync_status_table(project_name)
        validate_site_name(localSite)
        validate_site_name(remoteSite)

        # Hide archived entities - a soft-deleted version must stop
        # syncing, not keep transferring until its hard delete cascades.
        conditions = [
            "f.active IS TRUE",
            "p.active IS TRUE",
            "v.active IS TRUE",
            "r.active IS TRUE",
        ]

        if representationIds is not None:
            conditions.append(
                f"r.id IN {sql_array(validate_id_list(representationIds))}"
            )

        if folderFilter:
            conditions.append(f"f.name ILIKE '%{escape_ilike(folderFilter)}%'")

        if folderIdsFilter:
            conditions.append(
                f"f.id IN {sql_array(validate_id_list(folderIdsFilter))}"
            )

        if productFilter:
            conditions.append(
                f"p.name ILIKE '%{escape_ilike(productFilter)}%'"
            )

        if versionFilter is not None:
            conditions.append(f"v.version = {versionFilter}")

        if versionIdsFilter:
            conditions.append(
                f"v.id IN {sql_array(validate_id_list(versionIdsFilter))}"
            )

        if localStatusFilter:
            statusFilter = [str(s.value) for s in localStatusFilter]
            conditions.append(f"local.status IN ({','.join(statusFilter)})")

        if remoteStatusFilter:
            statusFilter = [str(s.value) for s in remoteStatusFilter]
            conditions.append(f"remote.status IN ({','.join(statusFilter)})")

        if repreNameFilter:
            conditions.append(f"r.name IN {sql_array(repreNameFilter)}")

        access_list = await folder_access_list(user, project_name, "read")
        if access_list is not None:
            conditions.append(f"path like ANY ('{{ {','.join(access_list)} }}')")

        sites_join = "LEFT"
        if bothOnly:
            sites_join = "INNER"

        query = f"""
            SELECT
                f.name as folder,
                p.name as product,
                v.version as version,
                r.name as representation,
                h.path as path,

                r.id as representation_id,
                r.files as representation_files,
                local.data as local_data,
                remote.data as remote_data,
                local.status as localStatus,
                remote.status as remoteStatus,
                v.id as version_id,
                GREATEST(
                    COALESCE(local.priority, 50),
                    COALESCE(remote.priority, 50)
                ) as priority
            FROM
                project_{project_name}.folders as f
            INNER JOIN
                project_{project_name}.products as p
                ON p.folder_id = f.id
            INNER JOIN
                project_{project_name}.versions as v
                ON v.product_id = p.id
            INNER JOIN
                project_{project_name}.representations as r
                ON r.version_id = v.id
            INNER JOIN
                project_{project_name}.hierarchy as h
                ON f.id = h.id
            {sites_join} JOIN
                project_{project_name}.sitesync_files_status as local
                ON local.representation_id = r.id
                AND local.site_name = '{localSite}'
            {sites_join} JOIN
                project_{project_name}.sitesync_files_status as remote
                ON remote.representation_id = r.id
                AND remote.site_name = '{remoteSite}'

            {SQLTool.conditions(conditions)}

            ORDER BY {sortBy.value} {'DESC' if sortDesc else 'ASC'}, r.id ASC
            LIMIT {pageLength}
            OFFSET { (page-1) * pageLength }
        """
        # ', r.id' tiebreaker: every sort key is non-unique (most rows
        # share priority 50), and without a total order equal-key rows
        # shuffle across page boundaries - the sync loop and the UI both
        # page explicitly, so rows would be skipped or duplicated.
        repres = []

        async for row in Postgres.iterate(query):
            files = row["representation_files"]
            file_count = len(files)
            # 'or 0': a representation file without a size (or a manually
            # created status row) must not 500 the whole state page.
            total_size = sum(f.get("size") or 0 for f in files)

            ldata = row["local_data"] or {}
            lfiles = ldata.get("files") or {}
            lsize = sum(f.get("size") or 0 for f in lfiles.values())
            ltime = max(
                [f.get("timestamp") or 0 for f in lfiles.values()] or [0]
            )

            rdata = row["remote_data"] or {}
            rfiles = rdata.get("files") or {}
            rsize = sum(f.get("size") or 0 for f in rfiles.values())
            rtime = max(
                [f.get("timestamp") or 0 for f in rfiles.values()] or [0]
            )

            local_status = SyncStatusModel(
                status=StatusEnum.NOT_AVAILABLE
                if row["localstatus"] is None
                else row["localstatus"],
                totalSize=total_size,
                size=lsize,
                timestamp=ltime,
            )
            remote_status = SyncStatusModel(
                status=StatusEnum.NOT_AVAILABLE
                if row["remotestatus"] is None
                else row["remotestatus"],
                totalSize=total_size,
                size=rsize,
                timestamp=rtime,
            )

            file_list = []
            for file_info in files:
                file_id = file_info["id"]
                local_file = lfiles.get(file_id, {})
                remote_file = rfiles.get(file_id, {})

                file_list.append(
                    FileModel(
                        id=file_id,
                        fileHash=file_info["hash"],
                        size=file_info.get("size") or 0,
                        path=file_info["path"],
                        baseName=os.path.split(file_info["path"])[1],
                        localStatus=SyncStatusModel(
                            status=local_file.get("status",
                                                StatusEnum.NOT_AVAILABLE),
                            size=local_file.get("size", 0),
                            totalSize=file_info.get("size") or 0,
                            timestamp=local_file.get("timestamp", 0),
                            message=local_file.get("message", None),
                            retries=local_file.get("retries", 0),
                            progress=local_file.get("progress", None),
                        ),
                        remoteStatus=SyncStatusModel(
                            status=remote_file.get("status",
                                                StatusEnum.NOT_AVAILABLE),
                            size=remote_file.get("size", 0),
                            totalSize=file_info.get("size") or 0,
                            timestamp=remote_file.get("timestamp", 0),
                            message=remote_file.get("message", None),
                            retries=remote_file.get("retries", 0),
                            progress=remote_file.get("progress", None),
                        ),
                    )
                )

            repres.append(
                SiteSyncSummaryItem.construct(
                    folder=row["folder"],
                    product=row["product"],
                    version=row["version"],
                    representation=row["representation"],
                    representationId=row["representation_id"],
                    fileCount=file_count,
                    size=total_size,
                    localStatus=local_status,
                    remoteStatus=remote_status,
                    files=file_list,
                    version_id=row["version_id"],
                    priority=row["priority"],
                )
            )

        return SiteSyncSummaryModel(representations=repres)


    #
    # SET REPRESENTATION SYNC STATE
    #

    async def reset_failed_representations(
        self,
        project_name: ProjectName,
        user: CurrentUser,
        site_name: str = Query(..., alias="siteName"),
        representation_id: str | None = Query(
            None,
            alias="representationId",
            description="Limit the reset to a single representation",
        ),
    ) -> dict[str, int]:
        """Requeue FAILED files of a site so clients retry them.

        Without 'representationId' every failed representation of the
        site is reset ("retry all failed"); with it only that one.
        Only files in FAILED state are touched - their status goes back
        to QUEUED and 'retries'/'message' are cleared.
        """
        await check_sync_status_table(project_name)
        validate_site_name(site_name)

        access_condition = ""
        if representation_id:
            await ensure_representation_access(
                project_name, user, representation_id
            )
        else:
            # Users with restricted folder access may only batch-reset
            # representations inside their subtree - mirroring the folder
            # ACL the read endpoints already apply.
            access_list = await folder_access_list(user, project_name, "read")
            if access_list is not None:
                access_condition = (
                    "AND representation_id IN "
                    f"{acl_repre_subquery(project_name, access_list)}"
                )

        reset_count = 0
        async with Postgres.acquire() as conn:
            async with conn.transaction():
                if representation_id:
                    rows = await conn.fetch(
                        f"""
                        SELECT representation_id, data
                        FROM project_{project_name}.sitesync_files_status
                        WHERE site_name = $1 AND representation_id = $2
                        FOR UPDATE
                        """,
                        site_name,
                        representation_id,
                    )
                else:
                    # Match any row holding a FAILED file, not only rows
                    # whose roll-up is FAILED - the roll-up ranks
                    # IN_PROGRESS above FAILED, so a representation with
                    # one failed file and one still transferring would
                    # otherwise be skipped by "retry all failed".
                    # CASE guards jsonb_each: one legacy/malformed row
                    # whose 'files' is not a JSON object must not 500 the
                    # whole batch (AND does not guarantee evaluation
                    # order, CASE does). The jsonb equality avoids ::int
                    # casts raising on non-numeric statuses for the same
                    # reason.
                    rows = await conn.fetch(
                        f"""
                        SELECT representation_id, data
                        FROM project_{project_name}.sitesync_files_status
                        WHERE site_name = $1 AND (
                            status = $2
                            OR CASE
                                WHEN jsonb_typeof(data->'files') = 'object'
                                THEN EXISTS (
                                    SELECT 1
                                    FROM jsonb_each(data->'files') AS fs
                                    WHERE fs.value->'status'
                                        = to_jsonb($2::integer)
                                )
                                ELSE FALSE
                            END
                        )
                        {access_condition}
                        FOR UPDATE
                        """,
                        site_name,
                        StatusEnum.FAILED,
                    )

                for row in rows:
                    files = (row["data"] or {}).get("files") or {}
                    if not isinstance(files, dict):
                        continue
                    changed = False
                    for file_info in files.values():
                        if file_info.get("status") != StatusEnum.FAILED:
                            continue
                        file_info["status"] = StatusEnum.QUEUED
                        file_info.pop("retries", None)
                        file_info.pop("message", None)
                        changed = True
                    if not changed:
                        continue

                    status = get_overal_status(files)
                    await conn.execute(
                        f"""
                        UPDATE project_{project_name}.sitesync_files_status
                        SET status = $1, data = $2
                        WHERE representation_id = $3 AND site_name = $4
                        """,
                        status,
                        {"files": files},
                        row["representation_id"],
                        site_name,
                    )
                    reset_count += 1

        return {"resetCount": reset_count}

    async def backfill_missing_site_records(
        self,
        project_name: ProjectName,
        user: CurrentUser,
        site_name: str = Query(
            "studio",
            alias="siteName",
            description="Site stamped onto representations without any"
                        " sync record",
        ),
    ) -> dict[str, int]:
        """Create a site record for representations that have none at all.

        Representations created outside the normal publish flow (core's
        Push-to-project, editorial ingest, a publish run with the sitesync
        addon disabled or crashed) end up with ZERO rows in
        'sitesync_files_status'. An NA/NA pair is invisible to the sync
        loop forever and nothing else ever heals it. This stamps such
        representations as fully available on 'siteName' - clients pass
        the project's resolved remote site (the transfer source the sync
        loop matches against); 'studio' is only the fallback default.
        Idempotent: representations with ANY existing record are left
        untouched, so repeated calls are no-ops (and throttled).
        """
        await check_sync_status_table(project_name)
        validate_site_name(site_name)

        # Every participating tray triggers this once per session, so
        # throttle server-side: in the steady state the anti-join scan
        # proves "nothing to do" only by scanning, which is not free on
        # large projects. The throttle lives in 'sitesync_meta' (NOT in
        # module state, which is per uvicorn worker/replica and resets on
        # every deploy): one atomic cross-worker claim - the row is
        # inserted or updated only when the stored timestamp is older
        # than the window; RETURNING is empty when another worker already
        # claimed it. Timestamps are bound as bigints - asyncpg's numeric
        # codec rejects Python floats.
        now = int(time.time())
        claim = await Postgres.fetch(
            f"""
            INSERT INTO project_{project_name}.sitesync_meta AS meta
                (key, value)
            VALUES ($1, jsonb_build_object('last_run', $2::bigint))
            ON CONFLICT (key) DO UPDATE
                SET value = jsonb_build_object('last_run', $2::bigint)
                WHERE COALESCE(
                    (meta.value->>'last_run')::numeric, 0
                ) <= $3::bigint
            RETURNING key
            """,
            f"backfill:{site_name}",
            now,
            now - _BACKFILL_THROTTLE,
        )
        if not claim:
            return {"backfilledCount": 0}

        # Users with restricted folder access may only stamp records
        # inside their subtree (managers and unrestricted users scan the
        # whole project). Stamping availability on faith is the accepted
        # design limit of this endpoint - the ACL bounds WHO can do it
        # WHERE, not the faith itself. The hierarchy join exists ONLY
        # for this filter, so unrestricted users (the common case - the
        # caller is each artist's own tray) skip its cost entirely.
        acl_join = ""
        access_condition = ""
        access_list = await folder_access_list(user, project_name, "read")
        if access_list is not None:
            acl_join = f"""
            INNER JOIN project_{project_name}.hierarchy AS h
                ON p.folder_id = h.id
            """
            access_condition = f"""
              AND h.path like ANY ('{{ {','.join(access_list)} }}')
            """

        # One set-based statement: no per-row round-trips, no long-held
        # transaction, files JSON built in SQL. ON CONFLICT guards a
        # concurrent publish/add_site creating a row mid-statement.
        query = f"""
            INSERT INTO project_{project_name}.sitesync_files_status
                (representation_id, site_name, status, priority, data)
            SELECT
                r.id,
                $1,
                $2,
                50,
                jsonb_build_object('files', (
                    SELECT jsonb_object_agg(
                        f->>'id',
                        jsonb_build_object(
                            'hash', f->'hash',
                            'status', $2::integer,
                            'size', COALESCE((f->>'size')::bigint, 0),
                            'timestamp', $3::bigint
                        )
                    )
                    FROM jsonb_array_elements(r.files) AS f
                ))
            FROM project_{project_name}.representations AS r
            INNER JOIN project_{project_name}.versions AS v
                ON r.version_id = v.id
            INNER JOIN project_{project_name}.products AS p
                ON v.product_id = p.id
            {acl_join}
            WHERE r.active IS TRUE
              AND v.active IS TRUE
              AND p.active IS TRUE
              AND jsonb_array_length(r.files) > 0
              AND NOT EXISTS (
                  SELECT 1
                  FROM project_{project_name}.sitesync_files_status AS s
                  WHERE s.representation_id = r.id
              )
              {access_condition}
            ON CONFLICT DO NOTHING
        """

        status_tag = await Postgres.execute(
            query, site_name, int(StatusEnum.SYNCED), int(now)
        )
        # command tag looks like 'INSERT 0 <count>'; the count is
        # informational only, so parsing failures just report 0
        backfilled = 0
        try:
            backfilled = int(str(status_tag).rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            pass

        return {"backfilledCount": backfilled}

    async def requeue_stale_in_progress(
        self,
        project_name: ProjectName,
        user: CurrentUser,
        site_name: str | None = Query(
            None,
            alias="siteName",
            description="Limit the sweep to one site; omitted sweeps"
                        " every site of the project",
        ),
        older_than_seconds: int = Query(
            3600,
            alias="olderThanSeconds",
            ge=60,
            description="Requeue IN_PROGRESS files whose last update is"
                        " older than this many seconds",
        ),
    ) -> dict[str, int]:
        """Requeue IN_PROGRESS files that stopped receiving updates.

        A tray killed mid-transfer (crash, sleep, power loss) leaves its
        files IN_PROGRESS forever: the roll-up ranks IN_PROGRESS above
        everything, the sync loop only fetches OK/QUEUED pairs, and
        resetFailed only touches FAILED files - nothing else ever
        un-sticks them. The state POST stamps a SERVER-side timestamp on
        every IN_PROGRESS file (see set_site_sync_representation_state),
        so this comparison is same-clock and immune to artist-machine
        clock skew; providers post progress every few seconds while
        genuinely transferring, so a file 'olderThanSeconds' old is
        dead, not slow. Sweeping ALL sites by default matters: a wiped
        machine's own tray is exactly the one that will never call this
        for its site, so any tray's sweep must heal everyone. The
        staleness predicate lives in SQL so live rows are never fetched
        or FOR-UPDATE-locked (they are the normal occupants of
        IN_PROGRESS, and their owner posts progress against the same
        rows every few seconds).
        """
        await check_sync_status_table(project_name)
        site_condition = ""
        if site_name:
            validate_site_name(site_name)
            site_condition = "AND site_name = $3"

        cutoff = int(time.time()) - older_than_seconds
        requeued = 0
        async with Postgres.acquire() as conn:
            async with conn.transaction():
                # Roll-up IN_PROGRESS iff any file is IN_PROGRESS (see
                # get_overal_status). A row with ANY fresh file is a live
                # transfer and is skipped wholesale; jsonb_typeof CASEs
                # guard malformed rows (AND/EXISTS do not guarantee
                # evaluation order, CASE does).
                query_args = [StatusEnum.IN_PROGRESS, cutoff]
                if site_name:
                    query_args.append(site_name)
                rows = await conn.fetch(
                    f"""
                    SELECT representation_id, site_name, data
                    FROM project_{project_name}.sitesync_files_status
                    WHERE status = $1
                      {site_condition}
                      AND CASE
                        WHEN jsonb_typeof(data->'files') = 'object'
                        THEN NOT EXISTS (
                            SELECT 1
                            FROM jsonb_each(data->'files') AS fs
                            WHERE CASE
                                WHEN jsonb_typeof(fs.value->'timestamp')
                                    = 'number'
                                THEN (fs.value->>'timestamp')::numeric
                                ELSE 0
                            END > $2::bigint
                        )
                        ELSE FALSE
                      END
                    FOR UPDATE
                    """,
                    *query_args,
                )

                for row in rows:
                    files = (row["data"] or {}).get("files") or {}
                    if not isinstance(files, dict):
                        continue
                    changed = False
                    for file_info in files.values():
                        if file_info.get("status") != StatusEnum.IN_PROGRESS:
                            continue
                        file_info["status"] = StatusEnum.QUEUED
                        file_info.pop("progress", None)
                        changed = True
                    if not changed:
                        continue

                    status = get_overal_status(files)
                    await conn.execute(
                        f"""
                        UPDATE project_{project_name}.sitesync_files_status
                        SET status = $1, data = $2
                        WHERE representation_id = $3 AND site_name = $4
                        """,
                        status,
                        {"files": files},
                        row["representation_id"],
                        row["site_name"],
                    )
                    requeued += 1

        return {"requeuedCount": requeued}

    async def set_representation_priority(
        self,
        project_name: ProjectName,
        user: CurrentUser,
        representation_id: RepresentationID,
        priority: int = Query(..., ge=0, le=100),
    ) -> Response:
        """Set transfer priority on every site record of a representation.

        One UPDATE across all of the representation's rows, so the two
        sides of a sync pair can never diverge (the /state roll-up reads
        the highest of them). Never creates a row - a POST to the
        per-site state endpoint with an absent record would insert a
        spurious NOT_AVAILABLE row; this endpoint deliberately cannot.
        """
        await check_sync_status_table(project_name)
        await ensure_representation_access(
            project_name, user, representation_id
        )
        await Postgres.execute(
            f"""
            UPDATE project_{project_name}.sitesync_files_status
            SET priority = $1
            WHERE representation_id = $2
            """,
            priority,
            representation_id,
        )
        return Response(status_code=204)

    async def set_site_sync_representation_state(
        self,
        post_data: RepresentationStateModel,
        project_name: ProjectName,
        user: CurrentUser,
        representation_id: RepresentationID,
        site_name: str = Path(
            ...
        ),
        reset: bool = Query(False),  # reset existing
    ) -> Response:
        """Adds site information to representation.

        Called after integration to set initial state of representation files on
        sites.
        Called repeatedly during synchronization to update progress/store error
        message
        """
        DEFAULT_PRIORITY = 50
        await check_sync_status_table(project_name)
        validate_site_name(site_name)
        await ensure_representation_access(
            project_name, user, representation_id
        )

        priority = post_data.priority

        async with Postgres.acquire() as conn:
            async with conn.transaction():
                query = (
                    f"""
                    SELECT priority, data
                    FROM project_{project_name}.sitesync_files_status
                    WHERE representation_id = $1 AND site_name = $2
                    FOR UPDATE
                    """,
                    representation_id,
                    site_name,
                )

                result = await conn.fetch(*query)
                do_insert = not result

                if priority is None:
                    priority = DEFAULT_PRIORITY
                    # Keep priority from existing items
                    if result:
                        priority = result[0]["priority"]

                # reset with new files is required for hero versions
                if reset or do_insert:
                    repre = await RepresentationEntity.load(
                        project_name, representation_id, transaction=conn
                    )

                    files = {}
                    for file_info in repre._payload.files:
                        fhash = file_info.hash
                        files[file_info.id] = {
                            "hash": fhash,
                            "status": StatusEnum.NOT_AVAILABLE,
                            "size": 0,
                            "timestamp": 0,
                        }
                else:
                    files = result[0]["data"].get("files") or {}

                for posted_file in post_data.files:
                    posted_file_id = posted_file.id
                    if posted_file_id not in files:
                        logging.warning(f"{posted_file} not in files")
                        continue
                    # IN_PROGRESS files get a SERVER-clock timestamp: the
                    # stale-transfer requeue compares these against the
                    # server's own clock, and client-posted wall clocks
                    # (skewed machines, older client builds re-posting a
                    # copied old value) would flip live transfers back to
                    # QUEUED mid-flight - or mask genuinely dead ones.
                    if posted_file.status == StatusEnum.IN_PROGRESS:
                        files[posted_file_id]["timestamp"] = int(time.time())
                    else:
                        files[posted_file_id]["timestamp"] = (
                            posted_file.timestamp
                        )
                    files[posted_file_id]["status"] = posted_file.status
                    files[posted_file_id]["size"] = posted_file.size

                    # Live 0-1 fraction reported by providers during a
                    # transfer. Only meaningful while IN_PROGRESS - drop
                    # it on any other status so a finished/failed file
                    # doesn't carry a stale fraction.
                    if (
                        posted_file.progress is not None
                        and posted_file.status == StatusEnum.IN_PROGRESS
                    ):
                        files[posted_file_id]["progress"] = (
                            posted_file.progress
                        )
                    elif "progress" in files[posted_file_id]:
                        del files[posted_file_id]["progress"]

                    if posted_file.message:
                        files[posted_file_id]["message"] = posted_file.message
                    elif "message" in files[posted_file_id]:
                        del files[posted_file_id]["message"]

                    if posted_file.retries:
                        files[posted_file_id]["retries"] = posted_file.retries
                    elif "retries" in files[posted_file_id]:
                        del files[posted_file_id]["retries"]

                status = get_overal_status(files)

                if do_insert:
                    # ON CONFLICT: FOR UPDATE cannot lock a row that does
                    # not exist yet, so two concurrent first-POSTs for the
                    # same (repre, site) can both take the insert branch -
                    # without this the second one 500s on the primary key.
                    await conn.execute(
                        f"""
                        INSERT INTO project_{project_name}.sitesync_files_status
                        (representation_id, site_name, status, priority, data)
                        VALUES ($1, $2, $3, $4, $5)
                        ON CONFLICT (representation_id, site_name)
                        DO UPDATE SET
                            status = EXCLUDED.status,
                            priority = EXCLUDED.priority,
                            data = EXCLUDED.data
                        """,
                        representation_id,
                        site_name,
                        status,
                        priority,
                        {"files": files},
                    )
                else:
                    await conn.execute(
                        f"""
                        UPDATE project_{project_name}.sitesync_files_status
                        SET status = $1, data = $2, priority = $3
                        WHERE representation_id = $4 AND site_name = $5
                        """,
                        status,
                        {"files": files},
                        priority,
                        representation_id,
                        site_name,
                    )

                # An explicitly posted priority applies to ALL of the
                # representation's rows, exactly like setPriority - a
                # per-side write here would reintroduce the side
                # divergence setPriority exists to prevent (add_site can
                # pass 'priority', e.g. the launch hook's 99). The
                # IS DISTINCT FROM makes repeat posts (every DCC launch
                # re-posts 99) no-op writes.
                if post_data.priority is not None:
                    await conn.execute(
                        f"""
                        UPDATE project_{project_name}.sitesync_files_status
                        SET priority = $1
                        WHERE representation_id = $2
                          AND priority IS DISTINCT FROM $1
                        """,
                        post_data.priority,
                        representation_id,
                    )

        return Response(status_code=204)

    async def remove_site_sync_representation_state(
        self,
        project_name: ProjectName,
        user: CurrentUser,
        representation_id: RepresentationID,
        site_name: str = Path(...),
    ) -> Response:
        await check_sync_status_table(project_name)
        validate_site_name(site_name)
        await ensure_representation_access(
            project_name, user, representation_id
        )

        async with Postgres.acquire() as conn:
            async with conn.transaction():
                query = (
                    f"""
                    DELETE
                    FROM project_{project_name}.sitesync_files_status
                    WHERE representation_id = $1 AND site_name = $2
                    """,
                    representation_id,
                    site_name,
                )

                await conn.fetch(*query)

                return Response(status_code=204)

    async def get_representations_site_sync_state(
        self,
        project_name: ProjectName,
        user: CurrentUser,
        representationIds: list[str] = Query(
            None,
            description="Filter by representation ids",
            example="['57cf375c749611ed89de0242ac140004']",
        ),
        siteNames: list[str] | None = Query(
            None,
            description="Filter by site names",
            example="['studio']",
        ),
    ) -> list[RepresentationSiteStateModel]:
        """List all sites on all representations and their state"""
        await check_sync_status_table(project_name)
        if not representationIds:
            raise BadRequestException("'representationIds' is required")

        conditions = [
            "representation_id IN "
            f"{sql_array(validate_id_list(representationIds))}"
        ]

        if siteNames:
            for name in siteNames:
                validate_site_name(name)
            conditions.append(f"site_name IN {sql_array(siteNames)}")

        query = f"""
            SELECT representation_id, site_name, status
            FROM project_{project_name}.sitesync_files_status
            {SQLTool.conditions(conditions)}
        """
        repres = []

        async for row in Postgres.iterate(query):
            repres.append(
                RepresentationSiteStateModel(
                    representationId=row["representation_id"],
                    siteName=row["site_name"],
                    status=row["status"]
                )
            )
        return repres


# Site names and free-text filters are interpolated into SQL below (the
# joins and ILIKE conditions cannot use bind parameters the way the query
# is assembled), so they MUST be validated/escaped here. The tray's
# "All files" search box feeds arbitrary artist text into the filters.
_SITE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9 _.\-]+$")


def validate_site_name(site_name: str) -> str:
    """Allow only a safe charset for values interpolated into SQL."""
    if not site_name or not _SITE_NAME_PATTERN.match(site_name):
        raise BadRequestException(f"Invalid site name: {site_name!r}")
    return site_name


def escape_ilike(value: str) -> str:
    """Escape a user string for embedding inside an ILIKE '%...%' literal.

    Quotes are doubled (SQL literal), backslash/percent/underscore are
    escaped so they match literally instead of acting as wildcards.
    """
    value = value.replace("\\", "\\\\").replace("'", "''")
    return value.replace("%", "\\%").replace("_", "\\_")


def sql_array(values: list[str]) -> str:
    """Injection-safe replacement for SQLTool.array on value lists.

    SQLTool.array wraps string elements in single quotes WITHOUT
    escaping quotes inside them, so feeding it raw user input is an SQL
    injection. This is the ONLY sanctioned way to build an IN (...)
    list in this module - never call SQLTool.array directly on request
    data. Doubling single quotes is sufficient under Postgres'
    standard_conforming_strings (backslashes are literal in '...').
    """
    escaped = [str(value).replace("'", "''") for value in values]
    return SQLTool.array(escaped)


def acl_repre_subquery(project_name: str, access_list: list[str]) -> str:
    """Subquery of representation ids inside the user's folder subtree.

    The single source of the representations->versions->products->
    hierarchy ACL join used by every mutating endpoint - keep it in one
    place so a hardening change cannot miss a copy.
    """
    return f"""(
        SELECT r.id
        FROM project_{project_name}.representations AS r
        INNER JOIN project_{project_name}.versions AS v
            ON r.version_id = v.id
        INNER JOIN project_{project_name}.products AS p
            ON v.product_id = p.id
        INNER JOIN project_{project_name}.hierarchy AS h
            ON p.folder_id = h.id
        WHERE h.path like ANY ('{{ {','.join(access_list)} }}')
    )"""


def validate_id_list(values: list[str]) -> list[str]:
    """Validate entity ids interpolated into SQL IN (...) lists.

    Anything that does not parse as a UUID is rejected with a 400 - this
    both closes the injection channel and gives callers a clear error
    instead of a Postgres cast failure.
    """
    validated = []
    for value in values:
        try:
            validated.append(uuid.UUID(str(value)).hex)
        except (ValueError, AttributeError, TypeError):
            raise BadRequestException(f"Invalid entity id: {value!r}")
    return validated


async def ensure_representation_access(
    project_name: str,
    user,
    representation_id: str,
) -> None:
    """Folder-ACL gate for representation-scoped sync mutations.

    The read endpoints already scope results by 'folder_access_list';
    without this the mutating endpoints (state POST/DELETE, setPriority,
    per-repre resetFailed) would let a user with restricted folder access
    rewrite sync state for representations OUTSIDE their subtree.
    Unrestricted users and managers pass without an extra query.
    """
    access_list = await folder_access_list(user, project_name, "read")
    if access_list is None:
        return
    result = await Postgres.fetch(
        f"""
        SELECT 1
        WHERE $1::uuid IN {acl_repre_subquery(project_name, access_list)}
        """,
        representation_id,
    )
    if not result:
        raise ForbiddenException(
            "You do not have access to this representation"
        )


def get_overal_status(files: dict) -> StatusEnum:
    all_states = [v.get("status", StatusEnum.NOT_AVAILABLE) for v in files.values()]
    if all(stat == StatusEnum.NOT_AVAILABLE for stat in all_states):
        return StatusEnum.NOT_AVAILABLE
    elif all(stat == StatusEnum.SYNCED for stat in all_states):
        return StatusEnum.SYNCED
    # IN_PROGRESS outranks FAILED: while anything is still transferring the
    # representation is alive, and showing FAILED would mask the ongoing
    # transfer. Once nothing moves anymore, any failed file wins.
    elif any(stat == StatusEnum.IN_PROGRESS for stat in all_states):
        return StatusEnum.IN_PROGRESS
    elif any(stat == StatusEnum.FAILED for stat in all_states):
        return StatusEnum.FAILED
    elif any(stat == StatusEnum.PAUSED for stat in all_states):
        return StatusEnum.PAUSED
    elif any(stat == StatusEnum.QUEUED for stat in all_states):
        return StatusEnum.QUEUED
    return StatusEnum.NOT_AVAILABLE


# Projects whose status table was already ensured in this server process -
# without this every state poll (the tray polls every few seconds) would
# run three DDL statements per request.
_ensured_status_tables: set[str] = set()

# Minimum seconds between backfill scans per (project, site) - enforced
# cross-worker via the 'sitesync_meta' claim row, never in module state.
_BACKFILL_THROTTLE = 3600  # seconds


async def check_sync_status_table(project_name: str) -> None:
    """Checks for existence of `sitesync_files_status` table, creates if not."""
    if project_name in _ensured_status_tables:
        return
    await Postgres.execute(
        f"CREATE TABLE IF NOT EXISTS project_{project_name}.sitesync_files_status ("
        f"""representation_id UUID NOT NULL REFERENCES project_{project_name}.representations(id) ON DELETE CASCADE,
            site_name VARCHAR NOT NULL,
            status INTEGER NOT NULL DEFAULT -1,
            priority INTEGER NOT NULL DEFAULT 50,
            data JSONB NOT NULL DEFAULT '{{}}'::JSONB,
            PRIMARY KEY (representation_id, site_name)
        );"""
    )
    await Postgres.execute(f"CREATE INDEX IF NOT EXISTS file_status_idx ON project_{project_name}.sitesync_files_status(status);")
    await Postgres.execute(f"CREATE INDEX IF NOT EXISTS file_priority_idx ON project_{project_name}.sitesync_files_status(priority desc);")
    # Small key/value side table for addon bookkeeping that must be
    # shared across server workers/replicas (e.g. the backfill throttle -
    # module-level state is per process and resets on every deploy).
    await Postgres.execute(
        f"""CREATE TABLE IF NOT EXISTS project_{project_name}.sitesync_meta (
            key VARCHAR PRIMARY KEY,
            value JSONB NOT NULL DEFAULT '{{}}'::JSONB
        );"""
    )
    _ensured_status_tables.add(project_name)
