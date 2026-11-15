"""Live updates: what runs log, handed to the pages that are watching.

The web UI used to ask every five seconds whether anything was new, and
download each visible chart again to find out. Instead, a page opens one
long-lived request (`GET /api/events`) and the server writes to it as things
are logged: new metric points as they are, and short notices for the things
a page fetches in its own way (a table's new step, a log line, a file, a
change to the run itself).

Nothing here touches the database. Ingest handlers publish what they just
stored; the hub copies it to whoever subscribed to that run or its project.
With nobody watching, publishing is a dictionary lookup.

Events, each a JSON object with "type" and "run":
  metrics  {"points": {name: [[step, time, value], ...]}}
  data     {"names": [...]}   tables / histograms that gained a step
  files    {}                 a file finished uploading
  logs     {}                 console lines arrived
  run      {}                 status, series list, graph or alerts changed,
                              or (for a project) a run was created or deleted
  resync   {}                 events were dropped; fetch the current state
"""

import queue
import threading

# A page that stops reading (asleep laptop, dead connection not yet noticed)
# must not hold memory: past this many undelivered events it is told to
# resync instead.
BACKLOG = 256
# A burst bigger than this (a client flushing a long offline buffer) is
# cheaper to fetch than to stream.
MAX_POINTS = 5000


class Subscription:
    def __init__(self, run=None, project=None):
        self.run, self.project = run, project
        self.events = queue.Queue()

    def put(self, event):
        if self.events.qsize() >= BACKLOG:
            with self.events.mutex:
                self.events.queue.clear()
            event = {"type": "resync"}
        self.events.put(event)

    def get(self, timeout):
        """The next event; None if none came within `timeout` seconds."""
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None


class Hub:
    def __init__(self):
        self._lock = threading.Lock()
        self._by_run = {}  # run id -> set of subscriptions
        self._by_project = {}  # project name -> set of subscriptions
        self.closed = False

    def subscribe(self, run=None, project=None):
        sub = Subscription(run, project)
        with self._lock:
            if run is not None:
                self._by_run.setdefault(run, set()).add(sub)
            else:
                self._by_project.setdefault(project, set()).add(sub)
        return sub

    def unsubscribe(self, sub):
        with self._lock:
            table, key = (
                (self._by_run, sub.run) if sub.run is not None else (self._by_project, sub.project)
            )
            subs = table.get(key)
            if subs:
                subs.discard(sub)
                if not subs:
                    del table[key]

    def watched(self, run_id, project):
        """Whether anyone would receive an event for this run. Publishers
        check first, so that collecting an event costs nothing unwatched."""
        return run_id in self._by_run or project in self._by_project

    def publish(self, run_id, project, type, **fields):
        with self._lock:
            subs = list(self._by_run.get(run_id, ())) + list(self._by_project.get(project, ()))
        if not subs:
            return
        event = {"type": type, "run": run_id, **fields}
        for sub in subs:
            sub.put(event)

    def close(self):
        """Wake every stream so its thread can end."""
        self.closed = True
        with self._lock:
            subs = [s for group in (self._by_run, self._by_project) for ss in group.values() for s in ss]
        for sub in subs:
            sub.events.put({"type": "close"})
