import axios from 'axios'
import { useState, useEffect, useCallback } from 'react'
import { Button, TablePanel } from '@ynput/ayon-react-components'

import { Dialog } from 'primereact/dialog'
import { DataTable } from 'primereact/datatable'
import { Column } from 'primereact/column'

import { formatStatus } from './common'

const formatFileSize = (bytes, si = false, dp = 1) => {
  const thresh = si ? 1000 : 1024
  if (Math.abs(bytes) < thresh) {
    return bytes + ' B'
  }
  const units = si
    ? ['kB', 'MB', 'GB', 'TB', 'PB', 'EB', 'ZB', 'YB']
    : ['KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB', 'ZiB', 'YiB']
  let u = -1
  const r = 10 ** dp
  do {
    bytes /= thresh
    ++u
  } while (
    Math.round(Math.abs(bytes) * r) / r >= thresh &&
    u < units.length - 1
  )
  return bytes.toFixed(dp) + ' ' + units[u]
}

const buildQueryString = (representationId, localSite, remoteSite) => {
  let url = `?localSite=${localSite}&remoteSite=${remoteSite}`
  url += `&representationIds=${representationId}`
  return url
}

const SiteSyncDetailTable = ({ data, localSite, remoteSite }) => (
  <DataTable
    value={data}
    scrollable="true"
    responsive="true"
    responsiveLayout="scroll"
    scrollHeight="flex"
    selectionMode="single"
    style={{ flexGrow: 1 }}
  >
    <Column field="baseName" header="Name" />
    <Column
      field="size"
      header="Size"
      body={(row) => formatFileSize(row.size)}
      style={{ maxWidth: 100 }}
    />
    {localSite && (
      <Column
        field="localStatus"
        header="Local"
        body={(val) => formatStatus(val.localStatus)}
        style={{ minWidth: 80, maxWidth:225, overflow: "visible" }}
      />
    )}
    {remoteSite && (
      <Column
        field="remoteStatus"
        header="Remote"
        body={(val) => formatStatus(val.remoteStatus)}
        style={{ minWidth: 80, maxWidth:225,  overflow: "visible"}}
      />
    )}
  </DataTable>
)

const SiteSyncDetail = ({
  projectName,
  addonName,
  addonVersion,
  representationId,
  localSite,
  remoteSite,
  onHide,
}) => {
  const baseUrl = `/api/addons/${addonName}/${addonVersion}/${projectName}/state`
  const [files, setFiles] = useState([])
  const [loading, setLoading] = useState(false)

  const loadFiles = useCallback(() => {
    setLoading(true)

    axios
      .get(baseUrl + buildQueryString(representationId,
                                      localSite,
                                      remoteSite))
      .then((response) => {
        if (
          !(response.data)
        ) {
          console.log('ERROR GETTING FILES')
          setFiles([])
        }

        let result = []
        for (const repre of response.data.representations) {
            for (const file of repre.files){
                result.push({
                    hash: file.fileHash,
                    size: file.size,
                    baseName: file.baseName,
                    localStatus: file.localStatus,
                    remoteStatus: file.remoteStatus,
                })
            }
        }
        setFiles(result)
      })
      .finally(() => {
        setLoading(false)
      })
    // eslint-disable-next-line
  }, [projectName, representationId, localSite, remoteSite])

  useEffect(() => {
    loadFiles()
  }, [loadFiles])

  const anyFailed = files.some(
    (file) =>
      file.localStatus.status === 2 || file.remoteStatus.status === 2
  )

  const retryFailed = () => {
    // requeue failed files of this representation on both sites; only
    // FAILED files are touched server-side
    setLoading(true)
    const sites = [...new Set([localSite, remoteSite].filter(Boolean))]
    Promise.allSettled(
      sites.map((site) =>
        axios.post(
          `${baseUrl}/resetFailed` +
            `?siteName=${site}&representationId=${representationId}`
        )
      )
    ).then(() => loadFiles())
  }

  return (
    <Dialog
      visible
      header="Site sync details"
      onHide={onHide}
      style={{ minHeight: '40%', minWidth: 900 }}
      footer={
        anyFailed && (
          <Button
            label="Retry failed files"
            icon="refresh"
            onClick={retryFailed}
          />
        )
      }
    >
      <TablePanel className="nopad transparent" loading={loading}>
        <SiteSyncDetailTable
          data={files}
          localSite={localSite}
          remoteSite={remoteSite}
        />
      </TablePanel>
    </Dialog>
  )
}

export default SiteSyncDetail
