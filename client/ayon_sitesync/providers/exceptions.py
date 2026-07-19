class TransferPausedError(Exception):
    """A transfer stopped because the artist paused it mid-file.

    Deliberately NOT a failure: the sync loop must neither count a retry
    nor mark the file FAILED for a pause - a representation paused and
    resumed a few times during chunked transfers used to hit the retry
    limit and land in FAILED.
    """


class TransientTransferError(Exception):
    """A transfer failed for a reason that heals on its own (quota,
    rate limit).

    Treated like a pause by the sync loop: the file goes back to QUEUED
    without counting a retry or storing an error, so a day-long quota
    exhaustion cannot burn files into permanent FAILED - they simply
    retry on later passes until the condition clears.
    """
