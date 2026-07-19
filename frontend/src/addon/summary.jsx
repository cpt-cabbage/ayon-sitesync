import axios from 'axios'
import { useState, useEffect } from 'react'
import { Button, Section, TablePanel } from '@ynput/ayon-react-components'

import { DataTable } from 'primereact/datatable'
import { Column } from 'primereact/column'
import { MultiSelect } from 'primereact/multiselect'
import { FilterMatchMode } from 'primereact/api'
import { Dropdown } from 'primereact/dropdown';

import { formatStatus, SYNC_STATES } from './common'
import SiteSyncDetail from './detail'

/*
 * Utils
 */

const defaultParams = {
  first: 0,
  rows: 25,
  page: 0,
  sortField: 'folder',
  sortOrder: 1,
  filters: {
    folder: { value: '', matchMode: 'contains' },
    product: { value: '', matchMode: 'contains' },
    version: { value: '', matchMode: 'contains' },
    representation: { value: null, matchMode: FilterMatchMode.IN },
    localStatus: { value: null, matchMode: FilterMatchMode.IN },
    remoteStatus: { value: null, matchMode: FilterMatchMode.IN },
  },
  bothOnly: true
}

const textMatchModes = [
  { label: 'Contains', matchMode: FilterMatchMode.CONTAINS },
]
const selectMatchModes = [{ label: 'In', matchMode: FilterMatchMode.IN }]

const buildQueryString = (localSite, remoteSite, lazyParams) => {
  // every user-influenced value is URI-encoded: site names may contain
  // spaces, and '&', '#', '+' or '%' typed into a filter used to
  // silently corrupt the query string (wrong results, no error)
  let url = `?localSite=${encodeURIComponent(localSite)}`
  url += `&remoteSite=${encodeURIComponent(remoteSite)}`
  url += `&pageLength=${lazyParams.rows}&page=${lazyParams.page + 1}`
  url += `&sortBy=${lazyParams.sortField}`
  url += `&sortDesc=${lazyParams.sortOrder === 1 ? 'true' : 'false'}`
  if (lazyParams.filters.folder && lazyParams.filters.folder.value)
    url += `&folderFilter=${encodeURIComponent(lazyParams.filters.folder.value)}`
  if (lazyParams.filters.product && lazyParams.filters.product.value)
    url += `&productFilter=${encodeURIComponent(lazyParams.filters.product.value)}`
  if (lazyParams.filters.version && lazyParams.filters.version.value)
    url += `&versionFilter=${encodeURIComponent(lazyParams.filters.version.value)}`
  if (
    lazyParams.filters.representation &&
    lazyParams.filters.representation.value
  ) {
    for (const val of lazyParams.filters.representation.value)
      url += `&repreNameFilter=${encodeURIComponent(val)}`
  }
  if (lazyParams.filters.localStatus && lazyParams.filters.localStatus.value) {
    for (const val of lazyParams.filters.localStatus.value)
      url += `&localStatusFilter=${val}`
  }
  if (
    lazyParams.filters.remoteStatus &&
    lazyParams.filters.remoteStatus.value
  ) {
    for (const val of lazyParams.filters.remoteStatus.value)
      url += `&remoteStatusFilter=${val}`
  }
  return url
}

/*
 * Main component
 */

const SiteSyncSummary = ({
  addonName,
  addonVersion,
  projectName,
  localSites,
  remoteSites,
  names,
  totalCount,
}) => {
  const baseUrl = `/api/addons/${addonName}/${addonVersion}/${projectName}/state`
  const [loading, setLoading] = useState(false)
  const [representations, setRepresentations] = useState([])
  const [selectedRepresentation, setSelectedRepresentation] = useState(null)
  const [selectedLocalSite, setSelectedLocalSite] =
    useState(localSites && localSites[0] && localSites[0]["value"])
  const [selectedRemoteSite, setSelectedRemoteSite] =
    useState(remoteSites && remoteSites[0] && remoteSites[0]["value"])
  const [lazyParams, setLazyParams] = useState(defaultParams)
  const [errorMessage, setErrorMessage] = useState(null)

  useEffect(() => {
    setLoading(true)
    axios
      .get(baseUrl + buildQueryString(selectedLocalSite,
                                      selectedRemoteSite,
                                      lazyParams))
      .then((response) => {
        setRepresentations(response.data.representations)
        setErrorMessage(null)
      })
      .catch(() => {
        // stale rows with no indication used to be the only "signal"
        setErrorMessage('Loading the sync state failed - check the server.')
      })
      .finally(() => {
        setLoading(false)
      })
    // the sites are dependencies too: switching a site dropdown must
    // refetch even when the params object happens to be unchanged
    // eslint-disable-next-line
  }, [lazyParams, selectedLocalSite, selectedRemoteSite])

  // live progress: while any visible row is transferring, silently
  // re-poll so the progress bars actually move (the client writes
  // progress to the DB every ~5s)
  useEffect(() => {
    const anyInProgress = representations.some(
      (repre) =>
        repre.localStatus.status === 0 || repre.remoteStatus.status === 0
    )
    if (!anyInProgress) return

    const timer = setInterval(() => {
      axios
        .get(baseUrl + buildQueryString(selectedLocalSite,
                                        selectedRemoteSite,
                                        lazyParams))
        .then((response) => {
          setRepresentations(response.data.representations)
        })
        .catch(() => {})
    }, 5000)
    return () => clearInterval(timer)
    // eslint-disable-next-line
  }, [representations])

  const updateSite = (event, site_type) => {
    /* Updates site after selection change, triggers refresh.
     *
     * A FRESH object is required: mutating and re-setting the
     * module-level `defaultParams` was a same-reference state update on
     * first load, so React bailed out, the fetch effect never re-ran
     * and the table kept showing the previous site pair's data. */
    if (site_type == "local"){
        setSelectedLocalSite(event.value)
    }else{
        setSelectedRemoteSite(event.value)
    }

    setLazyParams({
      ...defaultParams,
      filters: { ...defaultParams.filters },
      first: 0,
      page: 0,
    })
}

  const retryAllFailed = () => {
    // requeue every FAILED file on both selected sites; desktop clients
    // pick them up on their next loop
    setLoading(true)
    const sites = [
      ...new Set([selectedLocalSite, selectedRemoteSite].filter(Boolean)),
    ]
    Promise.allSettled(
      sites.map((site) =>
        axios.post(
          `${baseUrl}/resetFailed?siteName=${encodeURIComponent(site)}`
        )
      )
    ).then((results) => {
      // a failed POST used to look identical to success
      const failed = results.filter((r) => r.status === 'rejected')
      if (failed.length) {
        setErrorMessage('Retrying failed transfers did not succeed.')
      } else {
        setErrorMessage(null)
      }
      // refresh the table
      setLazyParams({ ...lazyParams })
    })
  }

  const onPage = (event) => {
    setLazyParams(event)
  }

  const onSort = (event) => {
    event['first'] = 0
    event['page'] = 0
    setLazyParams(event)
  }

  const onFilter = (event) => {
    event['first'] = 0
    event['page'] = 0
    setLazyParams(event)
  }

  const representationFilterTemplate = (options) => {
    return (
      <MultiSelect
        value={options.value}
        options={names}
        onChange={(e) => options.filterApplyCallback(e.value)}
        optionLabel="name"
        placeholder="Any"
        className="p-column-filter"
        maxSelectedLabels={1}
      />
    )
  }

  const statusFilterTemplate = (options) => {
    return (
      <MultiSelect
        value={options.value}
        options={SYNC_STATES}
        onChange={(e) => options.filterApplyCallback(e.value)}
        optionLabel="name"
        placeholder="Any"
        className="p-column-filter"
        maxSelectedLabels={1}
      />
    )
  }

  return (
    <Section>
      {selectedRepresentation && (
        <SiteSyncDetail
          projectName={projectName}
          addonName={addonName}
          addonVersion={addonVersion}
          localSite={selectedLocalSite}
          remoteSite={selectedRemoteSite}
          representationId={selectedRepresentation.representationId}
          onHide={() => {
            setSelectedRepresentation(null)
          }}
        />
      )}
      <Dropdown
            value={selectedLocalSite}
            onChange={(e) => updateSite(e, "local")}
            options={localSites} optionLabel="name"
            placeholder="Local site" className="w-full md:w-14rem" />
      <Dropdown
            value={selectedRemoteSite}
            onChange={(e) => updateSite(e, "remote")}
            options={remoteSites} optionLabel="name"
            placeholder="Remote site" className="w-full md:w-14rem" />
      <Button
            label="Retry all failed"
            icon="refresh"
            onClick={retryAllFailed}
            style={{ alignSelf: 'flex-start' }} />
      {errorMessage && (
        <span style={{ color: 'var(--color-hl-error, #ff6b6b)' }}>
          {errorMessage}
        </span>
      )}
        <TablePanel loading={loading}>
          <DataTable
            scrollable
            responsive
            scrollHeight="flex"
            responsiveLayout="scroll"
            resizableColumns
            value={representations}
            dataKey="representationId"
            selectionMode="single"
            selection={selectedRepresentation}
            onSelectionChange={(e) => setSelectedRepresentation(e.value)}
            lazy
            paginator
            filterDisplay="row"
            first={lazyParams.first}
            rows={lazyParams.rows}
            totalRecords={totalCount}
            sortField={lazyParams.sortField}
            sortOrder={lazyParams.sortOrder}
            filters={lazyParams.filters}
            onPage={onPage}
            onSort={onSort}
            onFilter={onFilter}
          >
            <Column
              field="folder"
              header="Folder"
              sortable
              filter
              filterMatchModeOptions={textMatchModes}
            />
            <Column
              field="product"
              header="Product"
              sortable
              filter
              filterMatchModeOptions={textMatchModes}
            />
            <Column
              field="version"
              header="Version"
              sortable
              filter
              filterMatchModeOptions={textMatchModes}
              style={{ maxWidth: 150 }}
            />
            <Column
              field="representation"
              header="Representation"
              filter
              filterElement={representationFilterTemplate}
              filterMatchModeOptions={selectMatchModes}
            />
            <Column
              field="fileCount"
              header="File count"
              style={{ maxWidth: 100 }}
            />
            <Column
              field="localStatus"
              header="Local status"
              sortable
              filter
              filterElement={statusFilterTemplate}
              filterMatchModeOptions={selectMatchModes}
              body={(val) => formatStatus(val.localStatus)}
              style={{ maxWidth: 250 }}
            />
            <Column
              field="remoteStatus"
              header="Remote status"
              sortable
              filter
              filterElement={statusFilterTemplate}
              filterMatchModeOptions={selectMatchModes}
              body={(val) => formatStatus(val.remoteStatus)}
              style={{ maxWidth: 250 }}
            />
          </DataTable>
        </TablePanel>
    </Section>
  )
}

export default SiteSyncSummary
