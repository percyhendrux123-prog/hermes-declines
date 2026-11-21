"""hermes-declines core: pure logic, no Hermes imports.

Fingerprinting, similarity and the 0600 decline store. Nothing here touches
the network. The raw prompt is never stored: only a normalized form and a
sha256 of the raw text.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unicodedata import decimal

DEFAULT_THRESHOLD = 0.6
DEFAULT_EXPIRE_DAYS = 30
DAY = 86400.0
MAX_SNAPSHOT_BYTES = 256 * 1024


class SnapshotTooLarge(ValueError):
    """An otherwise potential snapshot exceeds the byte bound."""


_MONTHS = (
    "january february march april may june july august september october november december "
    "jan feb mar apr jun jul aug sep sept oct nov dec"
)
_WEEKDAYS = "monday tuesday wednesday thursday friday saturday sunday mon tue tues wed thu thur thurs fri sat sun"
# Glue words that only exist to introduce a date/time ("on friday", "at 9am", "next week").
_GLUE = "today tomorrow tonight yesterday on at by around next this coming until till am pm"
_DROP_WORDS = frozenset((_MONTHS + " " + _WEEKDAYS + " " + _GLUE).split())

_RE_ISO = re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}(?:[t ]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2})?)?\b")
_RE_SLASH_DATE = re.compile(r"\b\d{1,4}[/.]\d{1,2}(?:[/.]\d{1,4})?\b")
_RE_CLOCK = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?")
_RE_DIGIT_TOKEN = re.compile(r"\b\d\w*")  # 9am, 30m, 3rd, 2026 ...
_RE_PUNCT = re.compile(r"[\W_]+", re.UNICODE)

# Callers cap the input. The digit and whitespace runs are disjoint, so invalid
# padded durations take linear work; keep numeric conversion bounded separately.
_RE_DURATION = re.compile(r"(\d*)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)")
_int_string_limit = getattr(sys, "get_int_max_str_digits", lambda: 0)
_RE_CLOCK_TIME = re.compile(r"(\d{1,2})(?::(\d{2}))?(am|pm)?")
_DAY_SPECS = frozenset("day daily everyday weekday weekdays weekend weekends".split())
_WEEKDAY_NAMES = frozenset(_WEEKDAYS.split()) | {"weds"}
_CRON_MONTHS = frozenset("jan feb mar apr may jun jul aug sep oct nov dec".split())
_CRON_DAYS = frozenset("sun mon tue wed thu fri sat".split())
_CRON_LAST_WEEKDAYS = frozenset(f"l{day}" for day in range(7))
_CRON_FIELDS = ((0, 59, ()), (0, 23, ()), (1, 31, ("l",)),
                (1, 12, _CRON_MONTHS), (0, 7, _CRON_DAYS), (0, 59, ()))
# Delimiters separate the digit runs, so even a long padded member takes linear work.
_RE_CRON_PART = re.compile(r"(\*|[0-9]+|[a-z]{1,3})(?:-([0-9]+|[a-z]{1,3}))?(?:/([0-9]+))?")
_USER_DENY = frozenset({"deny", "denied", "decline", "declined", "reject", "rejected", "no"})


def normalize_prompt(text: Any) -> str:
    """Lowercase; strip dates, times, numbers, punctuation and date glue words; collapse whitespace."""
    if not isinstance(text, str):
        return ""
    s = text.lower()
    s = _RE_ISO.sub(" ", s)
    s = _RE_SLASH_DATE.sub(" ", s)
    s = _RE_CLOCK.sub(" ", s)
    s = _RE_DIGIT_TOKEN.sub(" ", s)
    s = _RE_PUNCT.sub(" ", s)
    tokens = [t for t in s.split() if t not in _DROP_WORDS]
    return " ".join(tokens)


def normalize_deliver(value: Any, default: str = "origin") -> str:
    """Canonical delivery token list, with the caller's create or stored-job default."""
    if isinstance(value, (list, tuple)):
        value = ",".join(str(v) for v in value)
    if not isinstance(value, str) or not value.strip():
        return default
    return re.sub(r"\s+", "", value.strip().lower())


def schedule_kind(schedule: Any) -> str:
    """Coarse kind of a schedule string: interval, oneshot, cron, natural or unknown."""
    if not isinstance(schedule, str) or not schedule.strip():
        return "unknown"
    s = schedule.strip().lower()
    if re.fullmatch(r"[\d*/,\-?]+(?:\s+[\d*/,\-?a-z]+){4,5}", s):
        return "cron"
    if s.startswith("in ") or re.match(r"\d{4}-\d{2}-\d{2}", s):
        return "oneshot"
    if re.fullmatch(r"(?:every\s+)?\d+\s*[a-z]+", s) or s in ("every hour", "every day", "hourly", "daily"):
        return "interval"
    if s.startswith(("every ", "weekdays", "weekends", "daily", "weekly")):
        return "natural"
    return "unknown"


def _duration_parts(duration: str) -> Optional[tuple[str, str]]:
    match = _RE_DURATION.fullmatch(duration.strip())
    if not match:
        return None
    amount, unit = match.groups()
    limit = _int_string_limit()
    return None if limit and len(amount) > limit else (amount, unit)


def duration_minutes(duration: str) -> Optional[int]:
    """Bounded relative one-shot delay; no host reads."""
    parsed = _duration_parts(duration)
    if parsed is None:
        return None
    amount, unit = parsed
    first_nonzero = next((i for i, digit in enumerate(amount) if decimal(digit)), len(amount))
    if len(amount) - first_nonzero > 6:
        return None
    value = int(amount[first_nonzero:] or "0") if amount else 1
    return value * {"m": 1, "h": 60, "d": 1440}[unit[0]]


def _weekday_time(rest: str) -> bool:
    """Recognize the host's day/list + clock subset without croniter."""
    items = rest.replace(",", " ").split()
    if not items:
        return False
    if items[0] in _DAY_SPECS:
        i = 1
    else:
        i = 0
        has_day = False
        while i < len(items) and (items[i] in _WEEKDAY_NAMES or items[i] == "and"):
            has_day |= items[i] != "and"
            i += 1
        if not has_day:
            return False
    if i < len(items) and items[i] == "at":
        i += 1
    clock = "".join(items[i:])
    if clock in ("noon", "midday", "midnight"):
        return True
    match = _RE_CLOCK_TIME.fullmatch(clock)
    if not match:
        return False
    hour, minute, meridiem = match.groups()
    return int(minute or "0") <= 59 and ((1 <= int(hour) <= 12) if meridiem else int(hour) <= 23)


def _cron_number(text: str, lower: int, upper: int) -> bool:
    limit = _int_string_limit()
    if limit and len(text) > limit:
        return False
    digits = text.lstrip("0") or "0"
    return len(digits) <= 3 and digits.isdecimal() and lower <= int(digits) <= upper


def _cron_field(text, lower, upper, aliases) -> bool:
    if aliases is _CRON_DAYS and text in _CRON_LAST_WEEKDAYS:
        return True  # a standalone selector, not a member of a regular weekday list
    # The whole schedule is already capped; a single list may use that budget.
    for part in text.split(","):
        match = _RE_CRON_PART.fullmatch(part)
        if not match:
            return False
        start, end, step = match.groups()
        if step is not None and not _cron_number(step, 1, 999):
            return False
        if start == "*" and end is None:
            continue
        for value in (start, end):
            if value is not None and value not in aliases and not _cron_number(value, lower, upper):
                return False
    return True


def is_recurring_schedule(text: str) -> bool:
    """Classify reviewed recurring forms; Hermes still validates the schedule.

    Caller supplies stripped, lowercase text capped by the policy field limit.
    """
    if text.startswith("every "):
        rest = text[6:]
        return _duration_parts(rest) is not None or _weekday_time(rest)
    if _duration_parts(text) is not None or _weekday_time(text):
        return True
    fields = text.split(None, 6)
    return len(fields) in (5, 6) and all(_cron_field(field, *limits) for field, limits in zip(fields, _CRON_FIELDS))


def make_fingerprint(prompt: Any, deliver: Any, schedule: Any, *, origin_scope: str = "") -> Dict[str, str]:
    raw = prompt if isinstance(prompt, str) else ""
    return {
        "text": normalize_prompt(raw),
        "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "deliver": normalize_deliver(deliver),
        "schedule_kind": schedule_kind(schedule),
        "origin_scope": origin_scope,
    }


def jaccard(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(len(ta | tb))


def uses_origin(deliver: str) -> bool:
    return "origin" in deliver.split(",")


def scope_digest(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True).encode("ascii")).hexdigest()


def fingerprints_match(a: Dict[str, Any], b: Dict[str, Any], threshold: float) -> bool:
    """Same deliver target and token Jaccard >= threshold. Identical raw prompts always match."""
    deliver = b.get("deliver")
    if not isinstance(deliver, str) or a.get("deliver") != deliver:
        return False
    if uses_origin(deliver) and (not b.get("origin_scope") or a.get("origin_scope") != b["origin_scope"]):
        return False
    if a.get("sha256") and a.get("sha256") == b.get("sha256"):
        return True
    return jaccard(a.get("text", ""), b.get("text", "")) >= threshold


def is_user_deny(choice: Any) -> bool:
    return isinstance(choice, str) and choice.strip().lower() in _USER_DENY


def coerce_float(value: Any, default: float) -> float:
    try:
        if isinstance(value, bool):
            return default
        f = float(value)
        return f if 0.0 <= f <= 1.0 else default
    except (TypeError, ValueError):
        return default


def coerce_days(value: Any, default: float) -> float:
    try:
        if isinstance(value, bool):
            return default
        f = float(value)
        return f if f >= 0 else default
    except (TypeError, ValueError):
        return default


def coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
    return default


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home())
    except Exception:
        env = os.environ.get("HERMES_HOME")
        return Path(env).expanduser() if env else Path.home() / ".hermes"


def default_state_dir() -> Path:
    return hermes_home() / "plugins-state" / "hermes-declines"


def short_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def read_snapshot(path: Path, *, verify_parent: bool = False) -> Any:
    """Read one generation, optionally pinning a real immediate parent directory."""
    if not verify_parent:
        return _read_snapshot_file(path)
    try:
        parent = os.open(path.parent, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISDIR(os.fstat(parent).st_mode):
            raise ValueError("snapshot parent is not a directory")
        return _read_snapshot_file(path.name, dir_fd=parent)
    finally:
        os.close(parent)


def _read_snapshot_file(path, *, dir_fd: Optional[int] = None) -> Any:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("snapshot is not a regular file")
        if info.st_size > MAX_SNAPSHOT_BYTES:
            raise SnapshotTooLarge("snapshot exceeds the size limit")
        raw = fh.read(MAX_SNAPSHOT_BYTES + 1)
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise SnapshotTooLarge("snapshot exceeds the size limit")
    return json.loads(raw.decode("utf-8-sig"))


def active_records(records, expire_days: float, now: float):
    cutoff = expire_days * DAY
    return (r for r in records if isinstance(r.get("ts"), (int, float)) and now < r["ts"] + cutoff)


def find_match(records, fingerprint: Dict[str, Any], threshold: float, expire_days: float, now: float,
               *, deadline: float):
    for record in active_records(records, expire_days, now):
        if time.monotonic() >= deadline:
            break
        if fingerprints_match(record["fingerprint"], fingerprint, threshold):
            return record
    return None


class Store:
    """Decline records in <state_dir>/declines.json. Directory 0700, files 0600."""

    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir).expanduser()
        self.path = self.dir / "declines.json"
        self.lock_path = self.dir / "declines.lock"

    # -- file plumbing -------------------------------------------------
    def _ensure_dir(self) -> None:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass

    def _locked(self, *, blocking: bool = True):
        store = self

        class _Lock:
            def __enter__(self):
                store._ensure_dir()
                fd = os.open(str(store.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                    os.fchmod(fd, 0o600)
                except BaseException:
                    os.close(fd)
                    raise
                self.fd = fd
                return self

            def __exit__(self, *exc):
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
                finally:
                    os.close(self.fd)
                return False

        return _Lock()

    def _read(self) -> List[Dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return []
        except (ValueError, OSError):
            # Corrupt store: keep the evidence, start clean (fail open).
            try:
                bad = "%s.corrupt-%d" % (self.path, int(time.time()))
                os.replace(self.path, bad)
                os.chmod(bad, 0o600)
            except OSError:
                pass
            return []
        return self._records(data)

    @staticmethod
    def _records(data: Any) -> List[Dict[str, Any]]:
        recs = data.get("declines") if isinstance(data, dict) else None
        return [r for r in recs if isinstance(r, dict) and isinstance(r.get("fingerprint"), dict)] \
            if isinstance(recs, list) else []

    def _write(self, records: List[Dict[str, Any]], *, bounded: bool = False) -> None:
        raw = json.dumps({"version": 1, "declines": records}, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8", errors="backslashreplace")
        if bounded and len(raw) > MAX_SNAPSHOT_BYTES:
            raise SnapshotTooLarge("decline history exceeds the size limit")
        tmp = "%s.tmp-%s" % (self.path, uuid.uuid4().hex[:8])
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- operations ----------------------------------------------------
    def add(self, *, session_key: str, surface: str, fingerprint: Dict[str, str], now: Optional[float] = None,
            blocking: bool = True, expire_days: Optional[float] = None) -> Dict[str, Any]:
        now = time.time() if now is None else now
        rec = {
            "id": uuid.uuid4().hex[:8],
            "ts": now,
            "session_key": session_key or "",
            "surface": surface or "",
            "fingerprint": dict(fingerprint),
        }
        with self._locked(blocking=blocking):
            records = self._read()
            try:
                self._write([*records, rec], bounded=True)
            except SnapshotTooLarge:
                if expire_days is None:
                    raise
                active = list(active_records(records, expire_days, now))
                if len(active) == len(records):
                    raise  # keep the prior file intact if no space can be recovered
                self._write([*active, rec], bounded=True)
        return rec

    def snapshot(self):
        return tuple(self._records(read_snapshot(self.path)))

    def all(self) -> List[Dict[str, Any]]:
        with self._locked():
            return self._read()

    def active(self, expire_days: float, now: Optional[float] = None) -> List[Dict[str, Any]]:
        now = time.time() if now is None else now
        return list(active_records(self.all(), expire_days, now))

    def forget(self, ident: str) -> Optional[Dict[str, Any]]:
        """Remove one record by id or unique id prefix. Returns it, or None if not found/ambiguous."""
        ident = (ident or "").strip().lower()
        if not ident:
            return None
        with self._locked():
            records = self._read()
            hits = [r for r in records if str(r.get("id", "")) == ident] or \
                   [r for r in records if str(r.get("id", "")).startswith(ident)]
            if len(hits) != 1:
                return None
            records = [r for r in records if r is not hits[0]]
            self._write(records)
            return hits[0]

    def clear(self) -> int:
        with self._locked():
            n = len(self._read())
            self._write([])
            return n
