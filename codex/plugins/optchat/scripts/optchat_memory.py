"""Append-only OptChat store. JSONL is authoritative; SQLite is a lookup cache."""

import datetime
import heapq
import json
import os
import sqlite3
import tempfile
from fractions import Fraction
from pathlib import Path

NODE_BYTES = 512
VIEW_LOW, VIEW_HIGH = 64000, 128000
COMPACT_LOW, COMPACT_HIGH = 16000, 32000
MESSAGE_BYTES = 30000
KINDS = {"user", "unii", "tool", "echo", "work", "note"}
PLACEHOLDER = "(not summarized yet: zoom it)"


def byte_size(text):
    return len(text.encode("utf-8"))


def chunks(text, budget=MESSAGE_BYTES):
    """Split without dropping whitespace or cutting a UTF-8 codepoint."""
    raw = text.encode("utf-8")
    while raw:
        part = raw[:budget].decode("utf-8", errors="ignore")
        if not part:
            raise ValueError("Chunk budget is smaller than a UTF-8 character")
        yield part
        raw = raw[byte_size(part):]
    if not text:
        yield ""


def clip_output(text):
    if len(text) <= 30000:
        return text
    marker = "\n[... tool output clipped ...]\n"
    keep = 30000 - len(marker)
    return text[:keep // 2] + marker + text[-(keep - keep // 2):]


def atomic_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".optchat-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class FileLock:
    def __init__(self, path, blocking=True):
        self.stream = open(path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BaseException:
            self.stream.close()
            raise

    def close(self):
        if not self.stream.closed:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def node_shape(level, index):
    return (type(level) is int and type(index) is int and level >= 0 and index >= 0)


def shrink(pairs, get_text, total, target):
    """Merge built siblings by age from their LAST message, oldest tie first.

    A local linked list and heap avoid rescanning the view after each merge.
    Only the small view is visited; the historical tree is never scanned.
    """
    entries = [{"key": tuple(pair), "prev": k - 1, "next": k + 1,
                "alive": True, "version": 0} for k, pair in enumerate(pairs)]
    if not entries:
        return []
    entries[-1]["next"] = -1

    def line_size(key):
        level, index = key
        text = get_text(level, index)
        return byte_size("%d+%d|%s\n" % (index << level, 1 << level,
                                          (text if text is not None else PLACEHOLDER).replace("\n", " ")))

    size = byte_size("<chat>\n</chat>\n") + sum(line_size(e["key"]) for e in entries)
    ready = []

    def offer(k):
        if k < 0 or not entries[k]["alive"]:
            return
        left = entries[k]
        j = left["next"]
        if j < 0:
            return
        level, index = left["key"]
        if index % 2 or entries[j]["key"] != (level, index + 1):
            return
        if get_text(level + 1, index // 2) is None:
            return
        priority = Fraction(total + 1, 1 << level) - index
        heapq.heappush(ready, (-priority, index << level, k, j,
                               left["version"], entries[j]["version"]))

    for k in range(len(entries)):
        offer(k)
    while size > target and ready:
        _, _, k, j, version, right_version = heapq.heappop(ready)
        left, right = entries[k], entries[j]
        if (not left["alive"] or not right["alive"] or left["next"] != j
                or left["version"] != version or right["version"] != right_version):
            continue
        level, index = left["key"]
        parent = (level + 1, index // 2)
        size += line_size(parent) - line_size(left["key"]) - line_size(right["key"])
        left["key"], left["next"] = parent, right["next"]
        left["version"] += 1
        right["alive"] = False
        if left["next"] >= 0:
            entries[left["next"]]["prev"] = k
        offer(left["prev"])
        offer(k)
    return [list(e["key"]) for e in entries if e["alive"]]


class Store:
    """Hold the writer lock for a short transaction, never for a model call."""

    def __init__(self, directory):
        self.root = Path(directory).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(self.root / ".lock")
        self.db = None
        try:
            for directory in ("main", "tree"):
                (self.root / directory).mkdir(exist_ok=True)
            self.db = sqlite3.connect(str(self.root / "index.sqlite3"))
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS messages
                  (i INTEGER PRIMARY KEY, kind TEXT, text TEXT, size INTEGER, date TEXT, source TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS nodes
                  (l INTEGER, i INTEGER, text TEXT, size INTEGER, PRIMARY KEY(l,i));
                CREATE TABLE IF NOT EXISTS jobs (l INTEGER, i INTEGER, automatic INTEGER, PRIMARY KEY(l,i));
                CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(automatic,l,i);
                CREATE TABLE IF NOT EXISTS cursors (path TEXT PRIMARY KEY, offset INTEGER);
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
            """)
            self._sync("main")
            self._sync("tree")
            self.total = self.db.execute("SELECT coalesce(max(i)+1,0) FROM messages").fetchone()[0]
            count = self.db.execute("SELECT count(*) FROM messages").fetchone()[0]
            if count != self.total:
                raise ValueError("The message log has a gap in its permanent ids")
            path = self.root / "view.json"
            if path.exists():
                self.view = json.loads(path.read_text(encoding="utf-8"))
            elif self.total:
                raise ValueError("view.json is missing; restore it from backup. Views are never rebuilt.")
            else:
                self.view = []
                atomic_json(path, self.view)
            state_path = self.root / "view-state.json"
            if state_path.exists():
                self.views = json.loads(state_path.read_text(encoding="utf-8"))
            elif self.total:
                raise ValueError("view-state.json is missing; restore it from backup")
            else:
                self.views = {"batch": False, "compact_batch": False, "compact_view": []}
                atomic_json(state_path, self.views)
            self.covered = self._validate_view(self.view)
            # Complete an interrupted append without changing the saved prefix.
            if self.covered < self.total:
                self.view.extend([0, i] for i in range(self.covered, self.total))
                atomic_json(path, self.view)
            self.db.commit()
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        self.lock.close()

    def _validate_view(self, pairs):
        if not isinstance(pairs, list):
            raise ValueError("Invalid view.json: expected node pairs")
        end = 0
        for pair in pairs:
            if (not isinstance(pair, list) or len(pair) != 2 or not node_shape(*pair)
                    or pair[1] << pair[0] != end):
                raise ValueError("Invalid view.json: nodes do not cover a contiguous chat")
            level, index = pair
            end += 1 << level
            if end > self.total or (level and self.node(level, index) is None):
                raise ValueError("Invalid view.json: references a missing node or future message")
        return end

    def _sync(self, kind):
        for path in sorted((self.root / kind).glob("*.jsonl")):
            relative = str(path.relative_to(self.root))
            row = self.db.execute("SELECT offset FROM cursors WHERE path=?", (relative,)).fetchone()
            offset = row[0] if row else 0
            if path.stat().st_size < offset:
                raise ValueError("Append-only log was truncated: %s" % path)
            with path.open("rb") as stream:
                stream.seek(offset)
                for line in stream:
                    if not line.endswith(b"\n"):
                        raise ValueError("Incomplete JSONL write in %s; restore the log before appending" % path)
                    value = json.loads(line)
                    if kind == "main":
                        self._index_message(value)
                    else:
                        self._index_node(value)
                    offset += len(line)
            self.db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?)", (relative, offset))

    def _index_message(self, value):
        if (type(value.get("i")) is not int or value["i"] < 0 or value.get("kind") not in KINDS
                or not isinstance(value.get("text"), str) or value.get("size") != byte_size(value["text"])
                or not isinstance(value.get("date"), str)):
            raise ValueError("Invalid message in append-only log")
        self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)",
                        tuple(value.get(k) for k in ("i", "kind", "text", "size", "date", "source")))
        automatic = byte_size(value["kind"] + ": " + value["text"]) <= NODE_BYTES
        self.db.execute("INSERT OR IGNORE INTO jobs VALUES (0,?,?)", (value["i"], automatic))

    def _index_node(self, value):
        if (not node_shape(value.get("l"), value.get("i")) or not isinstance(value.get("text"), str)
                or not value["text"].strip() or value.get("size") != byte_size(value["text"])):
            raise ValueError("Invalid tree node in append-only log")
        level, index = value["l"], value["i"]
        self.db.execute("INSERT INTO nodes VALUES (?,?,?,?)", (level, index, value["text"], value["size"]))
        self.db.execute("DELETE FROM jobs WHERE l=? AND i=?", (level, index))
        if self.node(level, index ^ 1) is not None and self.node(level + 1, index // 2) is None:
            automatic = byte_size(self.source(level + 1, index // 2)) <= NODE_BYTES
            self.db.execute("INSERT OR IGNORE INTO jobs VALUES (?,?,?)", (level + 1, index // 2, automatic))

    def _write_log(self, kind, value):
        day = value.get("date", datetime.datetime.now(datetime.timezone.utc).isoformat())[:10]
        datetime.date.fromisoformat(day)
        path = self.root / kind / (day + ".jsonl")
        with path.open("ab") as stream:
            stream.write((json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
            offset = stream.tell()
        if kind == "main":
            self._index_message(value)
        else:
            self._index_node(value)
        self.db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?)", (str(path.relative_to(self.root)), offset))
        self.db.commit()

    def append(self, kind, text, date=None, source=None, defer_views=False):
        if kind not in KINDS or not isinstance(text, str):
            raise ValueError("Messages require a supported kind and text")
        date = date or datetime.datetime.now(datetime.timezone.utc).isoformat()
        datetime.datetime.fromisoformat(date.replace("Z", "+00:00"))
        parts = [clip_output(text)] if kind == "echo" else chunks(text)
        ids = []
        changed = False
        for part_number, part in enumerate(parts):
            key = "%s:%d" % (source, part_number) if source is not None else None
            existing = self.db.execute("SELECT i FROM messages WHERE source=?", (key,)).fetchone() if key else None
            if existing:
                ids.append(existing[0])
                continue
            i = self.total
            self._write_log("main", dict(i=i, kind=kind, text=part, size=byte_size(part), date=date, source=key))
            self.total += 1
            self.view.append([0, i])
            ids.append(i)
            changed = True
        if changed and not defer_views:
            self.settle_short()
            self.update_views()
        return ids

    def node(self, level, index):
        row = self.db.execute("SELECT text FROM nodes WHERE l=? AND i=?", (level, index)).fetchone()
        return row[0] if row else None

    def message(self, i):
        row = self.db.execute("SELECT i,kind,text,size,date FROM messages WHERE i=?", (i,)).fetchone()
        if row is None:
            raise ValueError("No message %s" % i)
        return dict(zip(("i", "kind", "text", "size", "date"), row))

    def source(self, level, index):
        if level == 0:
            message = self.message(index)
            return message["kind"] + ": " + message["text"]
        halves = (self.node(level - 1, 2 * index), self.node(level - 1, 2 * index + 1))
        if None in halves:
            raise ValueError("Cannot summarize a parent before its children")
        return "\n".join(halves)

    def put(self, level, index, text):
        if not node_shape(level, index) or (index + 1) << level > self.total:
            raise ValueError("Invalid node")
        if self.node(level, index) is not None:
            return False
        if not isinstance(text, str) or not text.strip():
            raise ValueError("A summary cannot be empty")
        self.source(level, index)  # validates that both children exist
        self._write_log("tree", dict(l=level, i=index, text=text, size=byte_size(text)))
        return True

    def settle_short(self):
        # Ready short sources have their own indexed queue; long pending
        # messages are never revisited on each append.
        while True:
            row = self.db.execute("SELECT l,i FROM jobs WHERE automatic=1 LIMIT 1").fetchone()
            if row is None:
                break
            self.put(*row, self.source(*row))

    def jobs(self, limit=8, exclude=()):
        ready = []
        # At most eight unbuilt message lines may precede a newly scheduled leaf.
        first = list(self.db.execute("SELECT i FROM jobs WHERE l=0 ORDER BY i LIMIT 8"))
        rows = list(self.db.execute("SELECT l,i FROM jobs WHERE l>0 ORDER BY l,i LIMIT ?", (limit + len(exclude),)))
        rows += [(0, row[0]) for row in first]
        for key in rows:
            if tuple(key) not in exclude:
                ready.append(tuple(key))
                if len(ready) == limit:
                    break
        return ready

    def render(self, pairs=None):
        lines = ["<chat>"]
        for level, index in self.view if pairs is None else pairs:
            text = self.node(level, index)
            lines.append("%d+%d|%s" % (index << level, 1 << level,
                                       (text if text is not None else PLACEHOLDER).replace("\n", " ").replace("\r", " ")))
        return "\n".join(lines) + "\n</chat>\n"

    def state(self, key, default=None):
        if key in ("batch", "compact_batch", "compact_view"):
            return self.views.get(key, default)
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key, value):
        if key in ("batch", "compact_batch", "compact_view"):
            self.views[key] = value
            atomic_json(self.root / "view-state.json", self.views)
            return
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))
        self.db.commit()

    def update_views(self):
        batch = self.state("batch", False) or byte_size(self.render()) > VIEW_HIGH
        before = self.view
        if batch:
            self.view = shrink(self.view, self.node, self.total, VIEW_LOW)
        self.set_state("batch", batch and byte_size(self.render()) > VIEW_LOW)
        atomic_json(self.root / "view.json", self.view)
        # Persist the smaller compaction view independently. It is bounded by
        # the live view and follows the same batches rather than being refit.
        compact = self.state("compact_view", [])
        covered = sum(1 << level for level, _ in compact)
        compact.extend([0, i] for i in range(covered, self.total))
        compact_batch = (self.state("compact_batch", False) or self.view != before
                         or byte_size(self.render(compact)) > COMPACT_HIGH)
        if compact_batch:
            compact = shrink(compact, self.node, self.total, COMPACT_LOW)
        self.set_state("compact_batch", compact_batch and byte_size(self.render(compact)) > COMPACT_LOW)
        self.set_state("compact_view", compact)

    def context(self, end):
        pairs = self.state("compact_view", [])
        out = []
        for level, index in pairs:
            start, stop = index << level, (index + 1) << level
            if start >= end:
                break
            if stop > end:
                # Split only the boundary node, never refit its earlier prefix.
                stack = [(level, index)]
                while stack:
                    l, i = stack.pop()
                    if i << l >= end:
                        continue
                    if (i + 1) << l > end and l:
                        stack.extend([(l - 1, i * 2 + 1), (l - 1, i * 2)])
                    elif self.node(l, i) is None:
                        return self.render(out)
                    else:
                        out.append([l, i])
            elif self.node(level, index) is None:
                break
            else:
                out.append([level, index])
        return self.render(out)
