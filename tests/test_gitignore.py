import os
import tempfile
import asyncio
import time
import pytest
from orcasync.gitignore import GitIgnoreMatcher
from orcasync.sync_engine import scan_directory
from orcasync.watcher import FileWatcher


class TestGitIgnoreMatcher:
    def test_basic_ignore(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, ".gitignore"), "w") as f:
                f.write("*.pyc\n")
            matcher = GitIgnoreMatcher(root)
            assert matcher.is_ignored("foo.pyc", is_dir=False) is True
            assert matcher.is_ignored("foo.py", is_dir=False) is False

    def test_dir_suffix(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, ".gitignore"), "w") as f:
                f.write("build/\n")
            matcher = GitIgnoreMatcher(root)
            assert matcher.is_ignored("build", is_dir=True) is True
            assert matcher.is_ignored("build", is_dir=False) is False

    def test_nested_gitignore(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, ".gitignore"), "w") as f:
                f.write("*.log\n")
            os.makedirs(os.path.join(root, "sub"))
            with open(os.path.join(root, "sub", ".gitignore"), "w") as f:
                f.write("!important.log\n")
            matcher = GitIgnoreMatcher(root)
            assert matcher.is_ignored("foo.log", is_dir=False) is True
            assert matcher.is_ignored("sub/important.log", is_dir=False) is False
            assert matcher.is_ignored("sub/other.log", is_dir=False) is True

    def test_default_git_ignore(self):
        with tempfile.TemporaryDirectory() as root:
            matcher = GitIgnoreMatcher(root)
            assert matcher.is_ignored(".git", is_dir=True) is True
            assert matcher.is_ignored(".git/config", is_dir=False) is True

    def test_gitignore_file_not_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, ".gitignore"), "w") as f:
                f.write(".gitignore\n")
            matcher = GitIgnoreMatcher(root)
            assert matcher.is_ignored(".gitignore", is_dir=False) is False

    def test_syncignore_takes_precedence(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, ".syncignore"), "w") as f:
                f.write("*.tmp\n")
            with open(os.path.join(root, ".gitignore"), "w") as f:
                f.write("*.pyc\n")
            matcher = GitIgnoreMatcher(root)
            # .syncignore takes precedence
            assert matcher.is_ignored("foo.tmp", is_dir=False) is True
            # .gitignore is ignored when .syncignore exists
            assert matcher.is_ignored("foo.pyc", is_dir=False) is False

    def test_syncignore_applies_recursively(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "sub"))
            with open(os.path.join(root, ".syncignore"), "w") as f:
                f.write("*.log\n")
            matcher = GitIgnoreMatcher(root)
            assert matcher.is_ignored("sub/debug.log", is_dir=False) is True

    def test_syncignore_fallback_to_gitignore(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, ".gitignore"), "w") as f:
                f.write("*.pyc\n")
            # .syncignore does not exist, fall back to .gitignore
            matcher = GitIgnoreMatcher(root)
            assert matcher.is_ignored("foo.pyc", is_dir=False) is True

    def test_syncignore_with_dir_suffix(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, ".syncignore"), "w") as f:
                f.write("build/\n")
            matcher = GitIgnoreMatcher(root)
            assert matcher.is_ignored("build", is_dir=True) is True
            assert matcher.is_ignored("build", is_dir=False) is False


class TestScanDirectoryWithGitignore:
    def test_scan_ignores_files(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "keep.py"), "w") as f:
                f.write("pass")
            with open(os.path.join(root, "ignore.pyc"), "w") as f:
                f.write("bytecode")
            with open(os.path.join(root, ".gitignore"), "w") as f:
                f.write("*.pyc\n")
            matcher = GitIgnoreMatcher(root)
            manifest = scan_directory(root, gitignore_matcher=matcher)
            assert "keep.py" in manifest
            assert "ignore.pyc" not in manifest

    def test_scan_ignores_directories(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "build", "sub"))
            with open(os.path.join(root, "build", "sub", "file.txt"), "w") as f:
                f.write("content")
            with open(os.path.join(root, ".gitignore"), "w") as f:
                f.write("build/\n")
            matcher = GitIgnoreMatcher(root)
            manifest = scan_directory(root, gitignore_matcher=matcher)
            assert "build" not in manifest
            assert "build/sub" not in manifest
            assert "build/sub/file.txt" not in manifest

    def test_scan_without_gitignore(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "file.txt"), "w") as f:
                f.write("content")
            manifest = scan_directory(root, gitignore_matcher=None)
            assert "file.txt" in manifest


class TestWatcherWithGitignore:
    def test_watcher_drops_ignored_events(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as root:
                with open(os.path.join(root, ".gitignore"), "w") as f:
                    f.write("*.tmp\n")
                matcher = GitIgnoreMatcher(root)
                events = []

                async def callback(event_type, rel_path, is_dir):
                    events.append((event_type, rel_path))

                loop = asyncio.get_running_loop()
                watcher = FileWatcher(root, callback, loop, gitignore_matcher=matcher)
                watcher.start()
                try:
                    await asyncio.sleep(0.3)
                    with open(os.path.join(root, "test.tmp"), "w") as f:
                        f.write("ignored")
                    with open(os.path.join(root, "test.txt"), "w") as f:
                        f.write("kept")
                    await asyncio.sleep(1.5)
                finally:
                    watcher.stop()

                txt_events = [e for e in events if e[1] == "test.txt"]
                tmp_events = [e for e in events if e[1] == "test.tmp"]
                assert len(txt_events) > 0, f"Expected test.txt events, got: {events}"
                assert len(tmp_events) == 0, f"Expected no test.tmp events, got: {tmp_events}"

        asyncio.run(run_test())
