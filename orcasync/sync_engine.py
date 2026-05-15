import os
import hashlib

BLOCK_SIZE = 128 * 1024  # 128KB

STATE_DIR = ".orcasync"  # internal state (manifest cache, staging) — never synced


def normalize_path(path):
    """Normalize path to use forward slashes for cross-platform consistency."""
    return path.replace(os.sep, "/")


def compute_file_blocks(filepath):
    blocks = []
    try:
        with open(filepath, "rb") as f:
            index = 0
            while True:
                data = f.read(BLOCK_SIZE)
                if not data:
                    break
                blocks.append(
                    {
                        "index": index,
                        "size": len(data),
                        "hash": hashlib.sha256(data).hexdigest(),
                    }
                )
                index += 1
    except (OSError, IOError):
        pass
    return blocks


def scan_directory(root_path, gitignore_matcher=None):
    root = os.path.abspath(root_path)
    os.makedirs(root, exist_ok=True)
    manifest = {}

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""

        # Always skip internal state directory, even without gitignore.
        if STATE_DIR in dirnames:
            dirnames.remove(STATE_DIR)

        # Filter out ignored directories to prevent descending into them
        if gitignore_matcher is not None:
            dirnames[:] = [
                dname for dname in dirnames
                if not gitignore_matcher.is_ignored(
                    normalize_path(os.path.join(rel_dir, dname)) if rel_dir else dname,
                    is_dir=True,
                )
            ]

        for dname in dirnames:
            dpath = os.path.join(dirpath, dname)
            rel_path = normalize_path(os.path.join(rel_dir, dname)) if rel_dir else dname
            try:
                stat = os.stat(dpath)
                manifest[rel_path] = {
                    "path": rel_path,
                    "is_dir": True,
                    "mtime": stat.st_mtime,
                }
            except (OSError, IOError):
                continue

        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            rel_path = normalize_path(os.path.join(rel_dir, fname)) if rel_dir else fname
            if gitignore_matcher is not None and gitignore_matcher.is_ignored(rel_path, is_dir=False):
                continue
            try:
                stat = os.stat(fpath)
                blocks = compute_file_blocks(fpath)
                manifest[rel_path] = {
                    "path": rel_path,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "is_dir": False,
                    "blocks": blocks,
                }
            except (OSError, IOError):
                continue

    return manifest


def diff_manifests(local_manifest, remote_manifest):
    needs = []
    for path, remote_info in remote_manifest.items():
        if remote_info.get("is_dir"):
            local_info = local_manifest.get(path)
            if local_info is None or not local_info.get("is_dir"):
                needs.append({"path": path, "is_dir": True})
            continue
        local_info = local_manifest.get(path)
        if local_info is None or local_info.get("is_dir"):
            needs.append({"path": path, "block_indices": None})
            continue
        if _same_blocks(local_info.get("blocks", []), remote_info.get("blocks", [])):
            continue
        if remote_info.get("mtime", 0) <= local_info.get("mtime", 0):
            continue
        local_hashes = {b["index"]: b["hash"] for b in local_info.get("blocks", [])}
        changed = [
            b["index"]
            for b in remote_info.get("blocks", [])
            if b["hash"] != local_hashes.get(b["index"])
        ]
        if changed:
            needs.append({"path": path, "block_indices": changed})
    return needs


def _same_blocks(a, b):
    if len(a) != len(b):
        return False
    return all(x["hash"] == y["hash"] for x, y in zip(a, b))


def ensure_parent_dir(root_path, rel_path):
    parent = os.path.dirname(os.path.join(root_path, rel_path.replace("/", os.sep)))
    if parent:
        os.makedirs(parent, exist_ok=True)


def ensure_dir(root_path, rel_path):
    full = os.path.join(root_path, rel_path.replace("/", os.sep))
    os.makedirs(full, exist_ok=True)


def read_block(root_path, rel_path, block_index):
    filepath = os.path.join(root_path, rel_path.replace("/", os.sep))
    try:
        with open(filepath, "rb") as f:
            f.seek(block_index * BLOCK_SIZE)
            return f.read(BLOCK_SIZE)
    except (OSError, IOError):
        return None


def write_blocks(root_path, rel_path, blocks_data, expected_size=None):
    filepath = os.path.join(root_path, rel_path.replace("/", os.sep))
    ensure_parent_dir(root_path, rel_path)
    if not os.path.exists(filepath):
        open(filepath, "wb").close()
    with open(filepath, "r+b") as f:
        for block_index, data in sorted(blocks_data, key=lambda x: x[0]):
            f.seek(block_index * BLOCK_SIZE)
            f.write(data)
        if expected_size is not None:
            f.truncate(expected_size)


def delete_path(root_path, rel_path):
    full = os.path.join(root_path, rel_path.replace("/", os.sep))
    try:
        if os.path.isdir(full) and not os.path.islink(full):
            import shutil

            shutil.rmtree(full)
        elif os.path.exists(full):
            os.remove(full)
    except (OSError, IOError):
        pass
