"""Per-call usage events for the hosted MCP server (docs/hosted.md, "Usage events").

One JSON object per tool call (`tool_call`) and per MCP `initialize` (`initialize`), written to
the logger `buildingactivity.mcp.usage` (stderr) and, when BUILDINGACTIVITY_MCP_USAGE_DIR is
set, appended to `<dir>/usage-YYYY-MM-DD.jsonl` (UTC day). Files older than
BUILDINGACTIVITY_MCP_USAGE_KEEP_DAYS (default 400) are deleted at start and once a day.

An event never holds the API key or any argument value: the caller is `key_ref`, the first
16 hex characters of sha256(key) (the API's key store keeps the full sha256, so an operator
maps a ref to a key label offline), and arguments are reduced to name -> size. No SDK import.
"""
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict

ENV_DIR = "BUILDINGACTIVITY_MCP_USAGE_DIR"
ENV_KEEP_DAYS = "BUILDINGACTIVITY_MCP_USAGE_KEEP_DAYS"
DEFAULT_KEEP_DAYS = 400
FILE_RX = re.compile(r"^usage-(\d{4}-\d{2}-\d{2})\.jsonl$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
OTHER_ARGS = "_other"

log = logging.getLogger("buildingactivity.mcp.usage")


def key_ref(key):
    """First 16 hex characters of sha256(key); None without a key. Never the key itself."""
    if not key:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def clean(value, limit):
    """A client-supplied string with control characters removed, cut to `limit`; None if empty."""
    if value is None:
        return None
    s = _CONTROL.sub("", str(value))[:limit]
    return s or None


def arg_shape(args, known=None):
    """Argument NAME -> SIZE only: len of a string, list or dict, None for anything else.
    Never a value. With `known` (the tool's schema property names), other names are not
    written; they are counted under `_other` so a name can never carry caller data."""
    out, other = {}, 0
    for name, v in (args or {}).items():
        if known is not None and name not in known:
            other += 1
            continue
        out[clean(name, 64) or "?"] = len(v) if isinstance(v, (str, list, dict, tuple)) else None
    if other:
        out[OTHER_ARGS] = other
    return out


def utc_now():
    return _dt.datetime.now(_dt.UTC)


def iso_ms(t):
    return t.astimezone(_dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ClientCache:
    """Bounded LRU: (key_ref, user agent) -> (client name, version) last seen at initialize.
    The server is stateless, so a tools/call carries no clientInfo; this joins it back."""

    def __init__(self, size=4096, ttl=24 * 3600, clock=time.monotonic):
        self.size, self.ttl, self.clock = size, ttl, clock
        self._d = OrderedDict()
        self._lock = threading.Lock()

    def put(self, ref, user_agent, name, version):
        with self._lock:
            k = (ref, user_agent)
            self._d[k] = (self.clock(), name, version)
            self._d.move_to_end(k)
            while len(self._d) > self.size:
                self._d.popitem(last=False)

    def get(self, ref, user_agent):
        with self._lock:
            k = (ref, user_agent)
            hit = self._d.get(k)
            if hit is None:
                return None, None
            if self.clock() - hit[0] > self.ttl:
                del self._d[k]
                return None, None
            self._d.move_to_end(k)
            return hit[1], hit[2]


class UsageSink:
    """Writes usage events. A write error logs one warning and never raises."""

    def __init__(self, directory=None, keep_days=DEFAULT_KEEP_DAYS, now=utc_now, clients=None):
        self.directory = directory or None
        self.keep_days = int(keep_days)
        self.now = now
        self.clients = clients or ClientCache()
        self._lock = threading.Lock()
        self._warned = False
        self._pruned_day = None
        if self.directory:
            self._maybe_prune(self.now())

    @classmethod
    def from_env(cls, environ=None, **kw):
        env = os.environ if environ is None else environ
        keep = env.get(ENV_KEEP_DAYS, "").strip()
        try:
            keep_days = int(keep) if keep else DEFAULT_KEEP_DAYS
        except ValueError:
            log.warning("usage: %s is not a whole number; keeping %d days", ENV_KEEP_DAYS,
                        DEFAULT_KEEP_DAYS)
            keep_days = DEFAULT_KEEP_DAYS
        return cls(env.get(ENV_DIR, "").strip() or None, keep_days, **kw)

    # events -------------------------------------------------------------------------

    def tool_call(self, *, key, key_via, tool, outcome, status, latency_ms, records_charged,
                  args, user_agent, protocol_version, server_version):
        ref, ua = key_ref(key), clean(user_agent, 120)
        name, version = self.clients.get(ref, ua)
        self.emit({"event": "tool_call", "key_ref": ref, "key_via": key_via, "tool": tool,
                   "outcome": outcome, "status": status, "latency_ms": latency_ms,
                   "records_charged": records_charged, "args": args, "client_name": name,
                   "client_version": version, "user_agent": ua,
                   "protocol_version": clean(protocol_version, 32),
                   "server_version": server_version})

    def initialize(self, *, key, key_via, client_name, client_version, protocol_version,
                   user_agent):
        ref, ua = key_ref(key), clean(user_agent, 120)
        name, version = clean(client_name, 64), clean(client_version, 64)
        self.clients.put(ref, ua, name, version)
        self.emit({"event": "initialize", "key_ref": ref, "key_via": key_via,
                   "client_name": name, "client_version": version,
                   "protocol_version": clean(protocol_version, 32), "user_agent": ua})

    def emit(self, event):
        t = self.now()
        line = json.dumps({"ts": iso_ms(t), **event}, separators=(",", ":"), ensure_ascii=True)
        log.info("%s", line)
        if not self.directory:
            return
        try:
            with self._lock:
                self._maybe_prune(t)
                path = os.path.join(self.directory, f"usage-{t.astimezone(_dt.UTC):%Y-%m-%d}.jsonl")
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError as exc:
            self._warn(exc)

    # retention ----------------------------------------------------------------------

    def _maybe_prune(self, t):
        day = t.astimezone(_dt.UTC).date()
        if self._pruned_day == day:
            return
        self._pruned_day = day
        self.prune(day)

    def prune(self, today):
        """Delete usage files whose UTC day is more than keep_days before `today`."""
        cutoff = today - _dt.timedelta(days=self.keep_days)
        try:
            names = os.listdir(self.directory)
        except OSError as exc:
            self._warn(exc)
            return []
        gone = []
        for n in sorted(names):
            m = FILE_RX.match(n)
            if not m:
                continue
            try:
                day = _dt.date.fromisoformat(m.group(1))
            except ValueError:
                continue
            if day < cutoff:
                try:
                    os.remove(os.path.join(self.directory, n))
                    gone.append(n)
                except OSError as exc:
                    self._warn(exc)
        return gone

    def _warn(self, exc):
        if not self._warned:
            self._warned = True
            log.warning("usage: cannot write events to %s (%s); logging to stderr only",
                        self.directory, type(exc).__name__)
