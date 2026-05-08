import os
import pathspec


class GitIgnoreMatcher:
    """
    Reads ignore files under root_path and provides is_ignored() matching
    exactly like Git.

    Supports a custom ignore file (e.g. .syncignore). When specified:
    - If the custom file exists at root, use it as the only ignore source
    - If the custom file does not exist, fall back to recursive .gitignore files
    """

    def __init__(self, root_path, ignore_file=None):
        self.root_path = os.path.abspath(root_path)
        self.ignore_file = ignore_file
        self._specs = {}  # dir_rel_path -> GitIgnoreSpec
        self._load()

    def _load(self):
        """Walk the directory tree and load ignore files."""
        # Always ignore .git directory
        base_spec = pathspec.GitIgnoreSpec.from_lines("gitignore", [".git/"])
        self._specs[""] = base_spec

        # If a custom ignore file is specified and exists at root, use it exclusively
        if self.ignore_file:
            custom_path = os.path.join(self.root_path, self.ignore_file)
            if os.path.isfile(custom_path):
                try:
                    with open(custom_path, "r", encoding="utf-8") as f:
                        lines = [line.rstrip("\n\r") for line in f]
                except (OSError, IOError):
                    lines = []
                custom_spec = pathspec.GitIgnoreSpec.from_lines("gitignore", lines)
                combined = pathspec.GitIgnoreSpec(base_spec.patterns + custom_spec.patterns)
                self._specs[""] = combined
                # Propagate the same spec to all subdirectories
                for dirpath, dirnames, _ in os.walk(self.root_path):
                    rel_dir = os.path.relpath(dirpath, self.root_path)
                    if rel_dir == ".":
                        rel_dir = ""
                    for dname in dirnames:
                        child_rel = os.path.join(rel_dir, dname) if rel_dir else dname
                        if child_rel not in self._specs:
                            self._specs[child_rel] = combined
                return

        # Fall back to recursive .gitignore files
        for dirpath, dirnames, filenames in os.walk(self.root_path):
            rel_dir = os.path.relpath(dirpath, self.root_path)
            if rel_dir == ".":
                rel_dir = ""

            if ".gitignore" in filenames:
                gitignore_path = os.path.join(dirpath, ".gitignore")
                try:
                    with open(gitignore_path, "r", encoding="utf-8") as f:
                        lines = [line.rstrip("\n\r") for line in f]
                except (OSError, IOError):
                    lines = []

                # Build spec for this directory: parent spec + local rules
                parent_spec = self._specs.get(rel_dir)
                local_spec = pathspec.GitIgnoreSpec.from_lines("gitignore", lines)
                if parent_spec is not None:
                    # Combine: parent rules first, then local rules (higher priority)
                    combined = pathspec.GitIgnoreSpec(parent_spec.patterns + local_spec.patterns)
                else:
                    combined = local_spec
                self._specs[rel_dir] = combined

            # Propagate spec to subdirectories that don't have their own .gitignore
            for dname in dirnames:
                child_rel = os.path.join(rel_dir, dname) if rel_dir else dname
                if child_rel not in self._specs:
                    parent_spec = self._specs.get(rel_dir)
                    if parent_spec is not None:
                        self._specs[child_rel] = parent_spec

    def is_ignored(self, rel_path, is_dir=False):
        """
        Check if a path (relative to root) is ignored.
        .gitignore files themselves are never ignored.
        """
        # Never ignore .gitignore files themselves
        basename = os.path.basename(rel_path)
        if basename == ".gitignore":
            return False

        # Check if any parent directory is ignored (e.g., .git/ ignores .git/config)
        parts = rel_path.split("/")
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            parent_dir = os.path.dirname(parent)
            if parent_dir == ".":
                parent_dir = ""
            parent_spec = self._specs.get(parent_dir)
            if parent_spec is not None:
                parent_result = parent_spec.check_file(parent + "/")
                if parent_result.include is True:
                    return True

        # Find the directory containing this path
        dir_rel = os.path.dirname(rel_path)
        if dir_rel == ".":
            dir_rel = ""

        spec = self._specs.get(dir_rel)
        if spec is None:
            return False

        # For directories, append trailing slash to match dir-only patterns
        check_path = rel_path + "/" if is_dir else rel_path
        result = spec.check_file(check_path)
        return result.include is True
