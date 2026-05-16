"""Conflict detection and `.sync-conflict-*` preservation.

orcasync's M1 conflict policy is a heuristic over the existing
`(mtime, blocks)` manifest:

* A pair (local, remote) is a conflict when both sides have non-deletion
  changes, hashes differ, and modification times are close enough that
  we can't confidently pick a winner by mtime alone.
* The loser is renamed to:
      <stem>.sync-conflict-<YYYYMMDD-HHMMSS>-<hostname>.<ext>
  before the winner is applied, so no user data is silently overwritten.

M3 replaces this heuristic with proper version vectors; the file naming
convention is kept stable so existing conflict files keep their meaning.
"""

import logging
import os
import socket
import time

from .logging_util import log_event

logger = logging.getLogger("orcasync.conflict")

# When both sides' mtimes fall within this window we treat the change as
# concurrent and refuse to silently overwrite the loser. Outside the
# window we fall back to last-write-wins (the legacy behavior).
CONCURRENT_MTIME_WINDOW_S = 5.0


def _local_host_tag():
    try:
        return socket.gethostname().replace(" ", "_")[:32] or "host"
    except Exception:
        return "host"


def conflict_filename(orig_rel_path, now=None, host=None):
    """Return the `.sync-conflict-*` name for `orig_rel_path`."""
    if now is None:
        now = time.time()
    if host is None:
        host = _local_host_tag()
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    base = os.path.basename(orig_rel_path)
    parent = os.path.dirname(orig_rel_path)
    stem, ext = os.path.splitext(base)
    suffix = f".sync-conflict-{ts}-{host}"
    new_name = f"{stem}{suffix}{ext}"
    if parent:
        # Use forward slash to stay consistent with manifest path keys.
        return f"{parent}/{new_name}"
    return new_name


def detect_conflict(local_info, remote_info, *, window_s=CONCURRENT_MTIME_WINDOW_S):
    """Return True when local and remote both modified the file concurrently.

    Inputs are manifest entries (dicts) for a single path. Either may be
    None (file missing on that side).
    """
    if not local_info or not remote_info:
        return False
    if local_info.get("is_dir") or remote_info.get("is_dir"):
        return False
    local_blocks = local_info.get("blocks", []) or []
    remote_blocks = remote_info.get("blocks", []) or []
    # Same content is never a conflict.
    if [b.get("hash") for b in local_blocks] == [b.get("hash") for b in remote_blocks]:
        return False
    local_mtime = local_info.get("mtime", 0) or 0
    remote_mtime = remote_info.get("mtime", 0) or 0
    return abs(local_mtime - remote_mtime) <= window_s


def pick_loser(local_info, remote_info, *, local_host=None, remote_host=None):
    """Return "local" or "remote" — whichever side loses the conflict.

    Older mtime loses; on exact tie, the side with the lexicographically
    larger host tag loses (deterministic across both peers without
    needing extra coordination).
    """
    local_mtime = local_info.get("mtime", 0) or 0
    remote_mtime = remote_info.get("mtime", 0) or 0
    if local_mtime < remote_mtime:
        return "local"
    if remote_mtime < local_mtime:
        return "remote"
    lh = (local_host or _local_host_tag())
    rh = (remote_host or "")
    return "local" if lh >= rh else "remote"


def preserve_local_as_conflict(root_path, rel_path, now=None):
    """Rename the local file at rel_path to a conflict filename.

    Returns the conflict relative path on success, or None if the file
    was missing or could not be renamed.
    """
    src = os.path.join(root_path, rel_path.replace("/", os.sep))
    if not os.path.isfile(src):
        return None
    conflict_rel = conflict_filename(rel_path, now=now)
    dst = os.path.join(root_path, conflict_rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(dst) or root_path, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError as e:
        log_event(
            logger, logging.ERROR, "conflict.rename_failed",
            path=rel_path, error=str(e),
        )
        return None
    log_event(
        logger, logging.WARNING, "conflict.kept_both",
        original=rel_path, conflict_file=conflict_rel, loser="local",
    )
    return conflict_rel
