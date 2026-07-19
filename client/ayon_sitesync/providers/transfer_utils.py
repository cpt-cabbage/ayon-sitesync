"""Shared helpers for provider file transfers.

Must stay Python 3.7 compatible and dependency-free (os/time/uuid only):
`workarea_mirror.py` imports this from DCC launch-hook processes.
"""
import os
import time
import uuid

# Seconds without a single new byte before a transfer counts as stalled.
# A transfer that neither fails nor moves must FAIL instead of spinning:
# a wedged wait loop permanently occupies one of the sync loop's few
# executor slots until no transfer can run at all.
STALL_TIMEOUT = 300


def make_tmp_path(target_path):
    """Unique in-flight name next to 'target_path'.

    Bytes are written here and os.replace()d into place only once
    complete, so an interrupted transfer never leaves a truncated file
    at the final path (which every os.path.exists() consumer trusts).
    The name is unique PER ATTEMPT: a stalled attempt's abandoned writer
    thread may still hold (and write) its own temp file, and a retry
    reusing a fixed name would interleave two writers into one file -
    which can even pass a size check while being torn.
    """
    return "{}.ayon_tmp.{}".format(target_path, uuid.uuid4().hex[:8])


def cleanup_tmp(path, log=None):
    """Best-effort removal of an in-flight temp file."""
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        if log is not None:
            log.warning("Couldn't remove temp file '{}'".format(path))


def wait_for_transfer(
    source_size,
    get_target_size,
    post_progress,
    progress_interval,
    thread=None,
    error_holder=None,
    stall_timeout=STALL_TIMEOUT,
):
    """Poll a background transfer until its byte count converges.

    'get_target_size' returns the current byte count of the in-flight
    target (or None when unreadable); 'post_progress' receives a 0-1
    fraction every 'progress_interval' seconds. Raises the worker's
    captured exception, or OSError on a 'stall_timeout' without a single
    new byte. A worker thread that exits CLEANLY before the size
    converges makes this return normally - the caller must then verify
    the outcome itself (the sftp worker, for example, renames the temp
    file away the instant the upload completes, so the poll may simply
    never observe convergence on a fast transfer).
    """
    target_size = 0
    last_tick = 0
    last_growth = time.time()
    while source_size != target_size:
        now = time.time()
        if now - last_tick >= progress_interval:
            last_tick = now
            post_progress(target_size / source_size)
        time.sleep(0.5)
        new_size = get_target_size()
        if new_size is not None and new_size != target_size:
            target_size = new_size
            last_growth = time.time()
            continue
        if error_holder is not None and error_holder.get("error"):
            raise error_holder["error"]
        if thread is not None and not thread.is_alive():
            # clean worker exit - outcome is the caller's to verify
            return
        if time.time() - last_growth > stall_timeout:
            raise OSError(
                "Transfer stalled - no new bytes for {}s".format(
                    stall_timeout)
            )
