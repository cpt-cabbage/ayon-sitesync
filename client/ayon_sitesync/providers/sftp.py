import os
import os.path
import threading
import platform

from ayon_core.lib import Logger
from .abstract_provider import AbstractProvider

log = Logger.get_logger("SiteSync-SFTPHandler")

sftpretty = None
try:
    import sftpretty
    import paramiko
except (ImportError, SyntaxError):
    pass

    # handle imports from Python 2 hosts - in those only basic methods are used
    log.warning("Import failed, imported from Python 2, operations will fail.")

from .transfer_utils import make_tmp_path, cleanup_tmp, wait_for_transfer


class SFTPHandler(AbstractProvider):
    """
        Implementation of SFTP API.

        Authentication could be done in 2 ways:
            - user and password
            - ssh key file for user (optionally password for ssh key)

        Settings could be overwritten per project.

    """
    CODE = "sftp"
    LABEL = "SFTP"

    def __init__(self, project_name, site_name, tree=None, presets=None):
        self.presets = None
        self.project_name = project_name
        self.site_name = site_name
        self.root = None
        self._conn = None

        self.presets = presets
        if not self.presets:
            self.log.warning(
                "Sync Server: There are no presets for {}.".format(site_name)
            )
            return

        # store to instance for reconnect
        self.sftp_host = presets["sftp_host"]
        self.sftp_port = presets["sftp_port"]
        self.sftp_user = presets["sftp_user"]
        self.sftp_pass = presets["sftp_pass"]
        self.sftp_key = presets["sftp_key"]
        self.sftp_key_pass = presets["sftp_key_pass"]

        self._tree = None

    @property
    def conn(self):
        """SFTP connection, cannot be used in all places though."""
        if not self._conn:
            self._conn = self._get_conn()

        return self._conn

    def is_active(self):
        """
            Returns True if provider is activated, eg. has working credentials.
        Returns:
            (boolean)
        """
        if not self.presets or not self.presets.get("enabled"):
            return False
        try:
            return self.conn is not None
        except Exception:
            # _get_conn raises on connection failure - an unreachable or
            # misconfigured SFTP site is "not working", not a crash.
            return False

    def get_roots_config(self, anatomy=None):
        """
            Returns root values for path resolving

            Use only Settings as GDrive cannot be modified by Local Settings

        Returns:
            (dict) - {"root": {"root": "/My Drive"}}
                     OR
                     {"root": {"root_ONE": "value", "root_TWO":"value}}
            Format is importing for usage of python's format ** approach
        """
        # TODO implement multiple roots
        return {"root": {"work": self.presets["root"]}}

    def get_tree(self):
        """
            Building of the folder tree could be potentially expensive,
            constructor provides argument that could inject previously created
            tree.
            Tree structure must be handled in thread safe fashion!
        Returns:
             (dictionary) - url to id mapping
        """
        # not needed in this provider
        pass

    def create_folder(self, path):
        """
            Create all nonexistent folders and subfolders in 'path'.
            Updates self._tree structure with new paths

        Args:
            path (string): absolute path, starts with GDrive root,
                           without filename
        Returns:
            (string) folder id of lowest subfolder from 'path'
        """
        self.conn.mkdir_p(path)

        return os.path.basename(path)

    def upload_file(
        self,
        source_path,
        target_path,
        addon,
        project_name,
        file,
        repre_status,
        site_name,
        overwrite=False
    ):
        """
            Uploads single file from 'source_path' to destination 'path'.
            It creates all folders on the path if are not existing.

        Args:
            source_path (string): absolute path on provider
            target_path (string): absolute path with or without name of the file
            addon (SiteSyncAddon): addon instance to call update_db on
            project_name (str):
            file (dict): info about uploaded file (matches structure from db)
            repre_status (dict): complete representation containing
                sync progress
            site_name (str): site name
            overwrite (boolean): replace existing file

        Returns:
            (string) file_id of created/modified file ,
                throws FileExistsError, FileNotFoundError exceptions
        """
        if not os.path.isfile(source_path):
            raise FileNotFoundError("Source file {} doesn't exist."
                                    .format(source_path))

        if self.file_path_exists(target_path):
            if not overwrite:
                raise ValueError("File {} exists, set overwrite".
                                 format(target_path))

        remote_tmp = make_tmp_path(target_path)
        upload_error = {}
        thread = threading.Thread(
            target=self._upload,
            args=(source_path, target_path, remote_tmp, upload_error))
        thread.daemon = True
        thread.start()

        source_size = os.path.getsize(source_path)

        def _post_progress(fraction):
            self.log.debug(f"uploaded {int(fraction * 100)}%.")
            addon.update_db(
                project_name=project_name,
                new_file_id=None,
                file=file,
                repre_status=repre_status,
                site_name=site_name,
                side="remote",
                progress=fraction
            )

        def _remote_tmp_size():
            try:
                return self.conn.stat(remote_tmp).st_size
            except (FileNotFoundError, IOError, OSError):
                return None

        wait_for_transfer(
            source_size,
            _remote_tmp_size,
            _post_progress,
            addon.LOG_PROGRESS_SEC,
            thread=thread,
            error_holder=upload_error,
        )
        # The worker renames the temp file into place the INSTANT put()
        # returns, so a fast upload can complete without the poll ever
        # observing convergence (the tmp is already gone). The outcome
        # is therefore judged on the FINAL path, never on the poll.
        thread.join(60)
        if upload_error.get("error"):
            raise upload_error["error"]
        if thread.is_alive():
            raise OSError(
                "Upload of '{}' did not finalize".format(source_path))
        try:
            uploaded_size = self.conn.stat(target_path).st_size
        except (FileNotFoundError, IOError, OSError):
            uploaded_size = None
        if uploaded_size != source_size:
            raise OSError(
                "Upload of '{}' produced size {} instead of {}".format(
                    source_path, uploaded_size, source_size)
            )

        return os.path.basename(target_path)

    def _upload(self, source_path, target_path, tmp_path, error_holder=None):
        log.debug("copying {}->{}".format(source_path, target_path))
        try:
            conn = self._get_conn()
            conn.put(source_path, tmp_path)
            if conn.isfile(target_path):
                conn.remove(target_path)
            conn.rename(tmp_path, target_path)
        except Exception as exc:
            # The exception must reach the transfer's thread - dying
            # silently here is what used to wedge the size-poll forever.
            if error_holder is not None:
                error_holder["error"] = exc
            log.warning(
                "Upload {} -> {} failed".format(source_path, target_path),
                exc_info=True,
            )
            try:
                conn.remove(tmp_path)
            except Exception:
                pass

    def download_file(
        self,
        source_path,
        target_path,
        addon,
        project_name,
        file,
        repre_status,
        site_name,
        overwrite=False
    ):
        """
            Downloads single file from 'source_path' (remote) to 'target_path'.
            It creates all folders on the local_path if are not existing.
            By default existing file on 'target_path' will trigger an exception

        Args:
            source_path (string): absolute path on provider
            target_path (string): absolute path with or without name of the file
            addon (SiteSyncAddon): addon instance to call update_db on
            project_name (str):
            file (dict): info about uploaded file (matches structure from db)
            repre_status (dict): complete representation containing
                sync progress
            site_name (str): site name
            overwrite (boolean): replace existing file

        Returns:
            (string) file_id of created/modified file ,
                throws FileExistsError, FileNotFoundError exceptions
        """
        if not self.file_path_exists(source_path):
            raise FileNotFoundError("Source file {} doesn't exist."
                                    .format(source_path))

        if os.path.isfile(target_path):
            if not overwrite:
                raise ValueError("File {} exists, set overwrite".
                                 format(target_path))

        local_tmp = make_tmp_path(target_path)
        download_error = {}
        thread = threading.Thread(
            target=self._download,
            args=(source_path, local_tmp, download_error))
        thread.daemon = True
        thread.start()

        source_size = self.conn.stat(source_path).st_size

        def _post_progress(fraction):
            self.log.debug(f"downloaded {int(fraction * 100)}%.")
            addon.update_db(
                project_name=project_name,
                new_file_id=None,
                file=file,
                repre_status=repre_status,
                site_name=site_name,
                side="local",
                progress=fraction
            )

        def _local_tmp_size():
            try:
                return os.path.getsize(local_tmp)
            except OSError:
                return None

        try:
            wait_for_transfer(
                source_size,
                _local_tmp_size,
                _post_progress,
                addon.LOG_PROGRESS_SEC,
                thread=thread,
                error_holder=download_error,
            )
            thread.join(60)
            if download_error.get("error"):
                raise download_error["error"]
            if thread.is_alive():
                raise OSError(
                    "Download of '{}' did not finalize".format(source_path))
            if os.path.getsize(local_tmp) != source_size:
                raise OSError(
                    "Download of '{}' produced a size mismatch".format(
                        source_path)
                )
            os.replace(local_tmp, target_path)
        except Exception:
            cleanup_tmp(local_tmp, log)
            raise

        return os.path.basename(target_path)

    def _download(self, source_path, target_path, error_holder=None):
        log.debug("downloading {}->{}".format(source_path, target_path))
        try:
            conn = self._get_conn()
            conn.get(source_path, target_path)
        except Exception as exc:
            if error_holder is not None:
                error_holder["error"] = exc
            log.warning(
                "Download {} -> {} failed".format(source_path, target_path),
                exc_info=True,
            )

    def delete_file(self, path):
        """
            Deletes file from 'path'. Expects path to specific file.

        Args:
            path: absolute path to particular file

        Returns:
            None
        """
        if not self.file_path_exists(path):
            raise FileNotFoundError("File {} to be deleted doesn't exist."
                                    .format(path))

        self.conn.remove(path)

    def list_folder(self, folder_path):
        """
            List all files and subfolders of particular path non-recursively.

        Args:
            folder_path (string): absolut path on provider
        Returns:
             (list)
        """
        return list(sftpretty.path_advance(folder_path))

    def folder_path_exists(self, file_path):
        """
            Checks if path from 'file_path' exists. If so, return its
            folder id.
        Args:
            file_path (string): path with / as a separator
        Returns:
            (string) folder id or False
        """
        if not file_path:
            return False

        return self.conn.isdir(file_path)

    def file_path_exists(self, file_path):
        """
            Checks if 'file_path' exists on GDrive

        Args:
            file_path (string): separated by '/', from root, with file name
        Returns:
            (dictionary|boolean) file metadata | False if not found
        """
        if not file_path:
            return False

        return self.conn.isfile(file_path)

    def _get_conn(self):
        """
            Returns fresh sftp connection.

            It seems that connection cannot be cached into self.conn, at least
            for get and put which run in separate threads.

        Returns:
            sftpretty.Connection
        """
        if not sftpretty:
            raise ImportError(
                "Library for SFTP provider is not available, "
                "ask admin to update dependency package."
            )

        cnopts = sftpretty.CnOpts(knownhosts=None)
        cnopts.log_level = "error"

        conn_params = {
            "host": self.sftp_host,
            "port": self.sftp_port,
            "username": self.sftp_user,
            "cnopts": cnopts,
        }
        if self.sftp_pass and self.sftp_pass.strip():
            conn_params["password"] = self.sftp_pass
        if self.sftp_key:
            no_configured_file_exist = False  # expects .pem format, not .ppk!
            key_paths = self.sftp_key[platform.system().lower()]
            for key_path in key_paths:
                no_configured_file_exist = True
                if os.path.exists(key_path):
                    no_configured_file_exist = False
                    conn_params["private_key"] = key_path
                    break
            if no_configured_file_exist:
                raise ValueError(
                    f"Certificate at '{key_paths}' doesn't exist."
                )

        if self.sftp_key_pass:
            conn_params["private_key_pass"] = self.sftp_key_pass

        try:
            return sftpretty.Connection(**conn_params)
        except (
            paramiko.ssh_exception.SSHException,
            sftpretty.exceptions.ConnectionException,
        ) as exc:
            self.log.warning("Couldn't connect", exc_info=True)
            # Returning None here used to make the transfer threads die
            # instantly on 'conn.put' (AttributeError on None) while the
            # size-poll spun on 0 bytes forever - a connection failure
            # must be an exception the caller can turn into FAILED.
            raise ConnectionError(
                "SFTP connection to '{}:{}' failed: {}".format(
                    self.sftp_host, self.sftp_port, exc)
            )

