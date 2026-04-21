import os
import asyncio
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


class FileWatcher:
    def __init__(self, root_path, callback, loop):
        self.root_path = os.path.abspath(root_path)
        self.callback = callback
        self.loop = loop
        self.observer = Observer()
        self._pending = {}
        self._debounce = 0.5

    def start(self):
        handler = _Handler(self.root_path, self._on_event)
        self.observer.schedule(handler, self.root_path, recursive=True)
        self.observer.start()

    def stop(self):
        self.observer.stop()
        self.observer.join(timeout=5)

    def _on_event(self, event_type, rel_path, is_dir):
        key = (event_type, rel_path)

        def _schedule():
            if key in self._pending:
                self._pending[key].cancel()
            self._pending[key] = self.loop.call_later(
                self._debounce,
                lambda: asyncio.ensure_future(
                    self._fire(event_type, rel_path, is_dir), loop=self.loop
                ),
            )

        self.loop.call_soon_threadsafe(_schedule)

    async def _fire(self, event_type, rel_path, is_dir):
        self._pending.pop((event_type, rel_path), None)
        await self.callback(event_type, rel_path, is_dir)


class _Handler(FileSystemEventHandler):
    def __init__(self, root_path, callback):
        self.root_path = root_path
        self.cb = callback

    def _rel(self, path):
        return os.path.relpath(path, self.root_path)

    def on_created(self, event):
        self.cb("create", self._rel(event.src_path), event.is_directory)

    def on_modified(self, event):
        self.cb("modify", self._rel(event.src_path), event.is_directory)

    def on_deleted(self, event):
        self.cb("delete", self._rel(event.src_path), event.is_directory)

    def on_moved(self, event):
        self.cb("delete", self._rel(event.src_path), event.is_directory)
        self.cb("create", self._rel(event.dest_path), event.is_directory)
