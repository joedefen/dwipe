#!/usr/bin/env python3
"""
Structured Logger with JSON Lines format and weighted-age trimming.
ERR entries age 10x slower than other entries, so they persist longer.
"""
import inspect
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ============================================================================
# Data Classes for Structured Logging
# ============================================================================

@dataclass
class LogEntry:
    """Structured log entry for JSON Lines format."""
    timestamp: str
    level: str  # 'ERR', 'OK', 'MSG', 'DBG', etc.
    file: str
    line: int
    function: str
    module: str = ""
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    _raw: str = ""  # Original raw message

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = asdict(self)
        # Remove private fields
        result.pop('_raw', None)
        return result

    @staticmethod
    def from_dict(data: dict[str, Any]) -> 'LogEntry':
        """Safely create LogEntry from dict, filtering unknown fields."""
        # Only extract known fields to avoid TypeError from extra fields
        known_fields = {
            'timestamp', 'level', 'file', 'line', 'function',
            'module', 'message', 'data', 'session_id'
        }
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return LogEntry(**filtered)

    @property
    def location(self) -> str:
        """Short location string for display."""
        return f"{self.file}:{self.line}"

    @property
    def display_summary(self) -> str:
        """
        Extract display-friendly summary from message.

        If message contains JSON with 'filebase' or 'filepath', use that.
        Otherwise return truncated message or location.
        """
        if not self.message:
            return f"{self.file}:{self.line} {self.function}()"

        # Try to extract filebase from JSON in message
        if '{' in self.message:
            try:
                json_start = self.message.index('{')
                json_str = self.message[json_start:]
                data = json.loads(json_str)
                filebase = data.get('filebase', data.get('filepath', None))
                if filebase:
                    return filebase
            except (json.JSONDecodeError, ValueError, KeyError):
                pass

        # Fall back to truncated message
        return self.message[:70]

    @staticmethod
    def format_time_delta(seconds: float, signed: bool = False) -> str:
        """
        Format time delta in compact form (e.g., '18h39m', '5d3h').

        Args:
            seconds: Time difference in seconds
            signed: If True, include '-' prefix for negative values

        Returns:
            Compact time string (e.g., '2h30m', '5d', '45s')
        """
        ago = int(max(0, abs(seconds)))
        divs = (60, 60, 24, 7, 52, 9999999)
        units = ('s', 'm', 'h', 'd', 'w', 'y')
        vals = (ago % 60, int(ago / 60))  # seed with secs, mins
        uidx = 1  # best units

        for div in divs[1:]:
            if vals[1] < div:
                break
            vals = (vals[1] % div, int(vals[1] / div))
            uidx += 1

        rv = '-' if signed and seconds < 0 else ''
        rv += f'{vals[1]}{units[uidx]}' if vals[1] else ''
        rv += f'{vals[0]:d}{units[uidx-1]}'
        return rv

    def format_ago(self) -> str:
        """
        Format this entry's timestamp as relative time (e.g., '5m', '2h39m').

        Returns:
            Compact relative time string, or '???' if timestamp is invalid
        """
        try:
            ts = datetime.fromisoformat(self.timestamp)
            now = datetime.now()
            delta = now - ts
            return LogEntry.format_time_delta(delta.total_seconds())
        except (ValueError, AttributeError):
            return "???"

# ============================================================================
# Main Logger Class
# ============================================================================

class StructuredLogger:
    """
    Structured logger using JSON Lines format with single log file
    and weighted-age trimming (ERR entries age 10x slower).
    """

    # Size limits (adjust as needed)
    MAX_LOG_SIZE = 10 * 1024 * 1024  # 10 MB
    TRIM_TO_RATIO = 0.67  # Trim to 67% when max exceeded
    ERR_AGE_WEIGHT = 10  # ERR entries age 10x slower

    def __init__(self, app_name: str = 'rmbloat',
                 log_dir: Path | None = None,
                 session_id: str = ""):
        """
        Initialize the structured logger.

        Args:
            app_name: Application name for log directory
            log_dir: Optional override for log directory
            session_id: Optional session identifier for log correlation
        """
        self.app_name = app_name
        self.session_id = session_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self._setup_paths(log_dir)

        # Statistics
        self.stats = {
            'entries_written': 0,
            'last_trim': datetime.now()
        }

    def _fix_ownership(self, path: Path) -> None:
        """Fix file/directory ownership to real user when running with sudo."""
        real_user = os.environ.get('SUDO_USER')
        if real_user:
            try:
                import pwd
                pw_record = pwd.getpwnam(real_user)
                uid, gid = pw_record.pw_uid, pw_record.pw_gid
                os.chown(path, uid, gid)
            except (OSError, KeyError):
                pass  # Ignore permission errors and missing users

    def _setup_paths(self, log_dir: Path | None) -> None:
        """Set up log directory and file paths."""
        try:
            if log_dir:
                # Use provided directory exactly as specified
                self.log_dir = Path(log_dir)
            else:
                # Construct default path from app_name
                base_dir = Path.home() / '.config'
                self.log_dir = base_dir / self.app_name

            self.log_dir.mkdir(parents=True, exist_ok=True)
            self._fix_ownership(self.log_dir)

            # Single log file (JSON Lines format)
            self.log_file = self.log_dir / "events.jsonl"

        except Exception as e:
            print(f"FATAL: Cannot setup log directory: {e}", file=sys.stderr)
            # Fallback to current directory
            self.log_dir = Path.cwd()
            self.log_file = Path("events.jsonl")

    def _get_caller_info(self, depth: int = 3) -> tuple:
        """Get caller information from stack frame."""
        try:
            frame = inspect.currentframe()
            for _ in range(depth):
                if frame:
                    frame = frame.f_back

            if frame:
                return (
                    Path(frame.f_code.co_filename).name,
                    frame.f_lineno,
                    frame.f_code.co_name,
                    frame.f_code.co_filename.split('/')[-2] if '/' in frame.f_code.co_filename else ""
                )
        except Exception:
            pass
        return ("unknown", 0, "unknown", "")

    def _create_log_entry(self, level: str, *args,
                         data: dict | None = None,
                         **kwargs) -> LogEntry:
        """Create a structured log entry."""
        file, line, function, module = self._get_caller_info()
        timestamp = datetime.now().isoformat()
        message = " ".join(str(arg) for arg in args)

        return LogEntry(
            timestamp=timestamp,
            level=level,
            file=file,
            line=line,
            function=function,
            module=module,
            message=message,
            data=data or {},
            session_id=self.session_id,
            _raw=message
        )

    def _append_log(self, entry: LogEntry) -> None:
        """
        Append entry to log file, trimming if necessary.

        Args:
            entry: Log entry to append
        """
        # Check if we need to trim
        if self.log_file.exists() and self.log_file.stat().st_size >= self.MAX_LOG_SIZE:
            self._trim_log_file()

        # Write the entry
        try:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                json_line = json.dumps(entry.to_dict())
                f.write(json_line + '\n')

            self.stats['entries_written'] += 1

            # Fix ownership after writing
            self._fix_ownership(self.log_file)

        except Exception as e:
            print(f"LOG WRITE ERROR: {e}", file=sys.stderr)

    def _trim_log_file(self) -> None:
        """
        Trim log file by removing oldest entries (by weighted age) until size < MAX_LOG_SIZE * TRIM_TO_RATIO.
        ERR entries have age/ERR_AGE_WEIGHT, so they're kept longer.
        """
        if not self.log_file.exists():
            return

        try:
            # Read all entries
            entries = []  # (timestamp, level, line, line_size)
            with open(self.log_file, encoding='utf-8') as f:
                while True:
                    line = f.readline()
                    if not line:
                        break
                    if line.strip():
                        try:
                            data = json.loads(line)
                            timestamp = datetime.fromisoformat(data['timestamp'])
                            level = data.get('level', 'OK')
                            line_size = len(line.encode('utf-8'))
                            entries.append((timestamp, level, line, line_size))
                        except (json.JSONDecodeError, KeyError, ValueError):
                            pass  # Skip malformed entries

            # Calculate effective age for each entry
            now = datetime.now()
            weighted = []  # (effective_age_seconds, line, line_size)
            for timestamp, level, line, line_size in entries:
                age_seconds = (now - timestamp).total_seconds()
                # ERRs age slower (kept longer)
                effective_age = age_seconds / self.ERR_AGE_WEIGHT if level == 'ERR' else age_seconds
                weighted.append((effective_age, line, line_size))

            # Sort by effective age (newest first = lowest age)
 
            weighted.sort(key=lambda x: x[0])

            # Target: keep newest entries until total size <= MAX_LOG_SIZE * TRIM_TO_RATIO
            target_size = int(self.MAX_LOG_SIZE * self.TRIM_TO_RATIO)
            kept = []
            total_size = 0

            # Keep newest entries (lowest effective age) until we hit target
            for _, line, line_size in weighted:
                if total_size + line_size <= target_size:
                    kept.append(line)
                    total_size += line_size
                # else: discard (too old with weighted age)

            # Write back (reverse to restore chronological order)
            kept.reverse()
            with open(self.log_file, 'w', encoding='utf-8') as f:
                f.writelines(kept)

            self.stats['last_trim'] = datetime.now()

            # Fix ownership after trimming
            self._fix_ownership(self.log_file)

        except Exception as e:
            print(f"TRIM ERROR: {e}", file=sys.stderr)


    # ========================================================================
    # Public API
    # ========================================================================

    def event(self, *args, data: dict | None = None, **kwargs) -> None:
        """Log an event (successful operation)."""
        entry = self._create_log_entry("OK", *args, data=data, **kwargs)
        self._append_log(entry)

    def error(self, *args, data: dict | None = None, **kwargs) -> None:
        """Log an error."""
        entry = self._create_log_entry("ERR", *args, data=data, **kwargs)
        self._append_log(entry)

        # Also print to stderr for immediate visibility
        print(f"ERROR: {args[0] if args else ''}", file=sys.stderr)
        if data:
            print(f"  Data: {json.dumps(data, indent=2)[:200]}...", file=sys.stderr)

    def info(self, *args, data: dict | None = None, **kwargs) -> None:
        """Log informational message."""
        entry = self._create_log_entry("MSG", *args, data=data, **kwargs)
        self._append_log(entry)

    def debug(self, *args, data: dict | None = None, **kwargs) -> None:
        """Log debug message."""
        entry = self._create_log_entry("DBG", *args, data=data, **kwargs)
        self._append_log(entry)

    # ========================================================================
    # Backward Compatibility Aliases (for RotatingLogger API)
    # ========================================================================

    def lg(self, *args, **kwargs) -> None:
        """
        Alias for info() - backward compatibility with RotatingLogger.

        Logs an ordinary message with a 'MSG' tag.
        Supports both simple messages and lists of strings.
        """
        # Handle list of strings like RotatingLogger did
        if args and isinstance(args[0], list):
            list_message = '\n'.join(str(item) for item in args[0])
            args = (list_message,) + args[1:]

        self.info(*args, **kwargs)

    def err(self, *args, **kwargs) -> None:
        """
        Alias for error() - backward compatibility with RotatingLogger.

        Logs an error message with an 'ERR' tag.
        Supports both simple messages and lists of strings.
        """
        # Handle list of strings like RotatingLogger did
        if args and isinstance(args[0], list):
            list_message = '\n'.join(str(item) for item in args[0])
            args = (list_message,) + args[1:]

        self.error(*args, **kwargs)

    def put(self, message_type: str, *args, **kwargs) -> None:
        """
        Alias for custom level logging - backward compatibility with RotatingLogger.

        Logs a message with an arbitrary MESSAGE_TYPE tag.
        Supports both simple messages and lists of strings.
        """
        # Handle list of strings like RotatingLogger did
        if args and isinstance(args[0], list):
            list_message = '\n'.join(str(item) for item in args[0])
            args = (list_message,) + args[1:]

        # Create entry with custom level
        # Extract data from kwargs to avoid passing it twice
        data = kwargs.pop('data', None)
        entry = self._create_log_entry(str(message_type).upper(), *args, data=data)
        self._append_log(entry)

    # ========================================================================
    # Filtering and Search Methods
    # ========================================================================

    @staticmethod
    def filter_entries(entries: list[LogEntry], pattern: str,
                      deep: bool = False) -> tuple[list[LogEntry], set]:
        """
        Filter log entries by pattern (case-insensitive).

        Args:
            entries: List of LogEntry objects to filter
            pattern: Search pattern (case-insensitive)
            deep: If True, also search within JSON data in messages

        Returns:
            (filtered_entries, deep_match_timestamps): Tuple of filtered list and
            set of timestamps that matched only in deep (JSON) search
        """
        if not pattern:
            return entries, set()

        pattern_lower = pattern.lower()
        filtered = []
        deep_matches = set()

        for entry in entries:
            # Build visible text (what shows in collapsed view)
            timestamp_short = entry.timestamp[:19]
            level = entry.level
            summary = entry.display_summary

            # Shallow search: check visible text
            visible_text = f"{timestamp_short} {level} {summary}".lower()
            shallow_match = pattern_lower in visible_text

            # Deep search: check JSON content if requested
            deep_match = False
            if deep:
                try:
                    # Search in the structured data field
                    if entry.data:
                        json_str = json.dumps(entry.data)
                        deep_match = pattern_lower in json_str.lower()
                    # Also search in message if it contains JSON
                    if not deep_match and '{' in entry.message:
                        json_start = entry.message.index('{')
                        json_str = entry.message[json_start:]
                        deep_match = pattern_lower in json_str.lower()
                except (ValueError, IndexError, TypeError):
                    pass

            # Include if either match
            if shallow_match or deep_match:
                filtered.append(entry)
                # Mark as deep-only match
                if deep_match and not shallow_match:
                    deep_matches.add(entry.timestamp)

        return filtered, deep_matches

    # ========================================================================
    # Query Methods - Window-based for efficient incremental reads
    # ========================================================================

    def get_window_of_entries(self, window_size: int = 1000):
        """
        Get a window of log entries (newest first).
        Returns: (entries_dict, window_state)
            entries_dict: OrderedDict keyed by timestamp
            window_state: dict with 'file_size' and 'last_position'
        """
        from collections import OrderedDict

        entries = OrderedDict()
        if not self.log_file.exists():
            return entries, {'file_size': 0, 'last_position': 0}

        file_size = self.log_file.stat().st_size

        try:
            with open(self.log_file, encoding='utf-8') as f:
                while True:
                    line = f.readline()
                    if not line:
                        break
                    if line.strip():
                        try:
                            entry_dict = json.loads(line)
                            entry = LogEntry.from_dict(entry_dict)
                            entries[entry.timestamp] = entry
                        except (json.JSONDecodeError, TypeError, KeyError):
                            pass
        except Exception:
            pass

        # Keep only newest window_size entries
        if len(entries) > window_size:
            # OrderedDict preserves insertion order (chronological)
            # Keep last window_size items
            items = list(entries.items())
            entries = OrderedDict(items[-window_size:])

        window_state = {
            'file_size': file_size,
            'last_position': file_size  # Next read starts here
        }

        return entries, window_state

    def refresh_window(self, window, window_state, window_size: int = 1000):
        """
        Refresh window with new entries from log file.
        Returns: updated (entries_dict, window_state)
        """
        from collections import OrderedDict

        if not self.log_file.exists():
            return window, window_state

        current_file_size = self.log_file.stat().st_size
        last_file_size = window_state.get('file_size', 0)
        last_position = window_state.get('last_position', 0)

        # File was trimmed (size dropped), reset and re-read
        if current_file_size < last_file_size:
            return self.get_window_of_entries(window_size)

        # No new data
        if current_file_size == last_position:
            return window, window_state

        # Read new entries from last position
        try:
            with open(self.log_file, encoding='utf-8') as f:
                f.seek(last_position)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    if line.strip():
                        try:
                            entry_dict = json.loads(line)
                            entry = LogEntry.from_dict(entry_dict)
                            window[entry.timestamp] = entry
                        except (json.JSONDecodeError, TypeError, KeyError):
                            pass

                new_position = f.tell()
        except Exception:
            new_position = last_position

        # Trim window if too large (keep newest)
        if len(window) > window_size:
            items = list(window.items())
            window = OrderedDict(items[-window_size:])

        window_state = {
            'file_size': current_file_size,
            'last_position': new_position
        }

        return window, window_state

    # ========================================================================
    # Properties
    # ========================================================================

    @property
    def log_paths(self) -> list[str]:
        """Return list of log file paths (for backward compatibility with -L option)."""
        return [str(self.log_file)]


# ============================================================================
# Aliases for Backward Compatibility
# ============================================================================

# Alias for standard use (matches RotatingLogger pattern: Log = RotatingLogger)
Log = StructuredLogger

# ============================================================================
# Example Usage
# ============================================================================

def example_usage():
    """Example of how to use the structured logger."""

    # Create logger
    logger = StructuredLogger(
        app_name="VideoProcessor",
        session_id="session_12345"
    )

    print(f"Logs will be written to: {logger.log_dir}")
    print(f"Log file: {logger.log_file}")

    # Log some events
    logger.info("Starting video processing batch")

    # Simulate processing
    for i in range(5):
        if i == 2:
            # Log an error with structured data
            logger.error(
                "Failed to encode video",
                data={
                    "filepath": f"/videos/video_{i}.mp4",
                    "error_code": 183,
                    "ffmpeg_output": ["Error opening input", "Invalid data"],
                    "attempts": 3
                }
            )
        else:
            # Log a successful event
            logger.event(
                f"Successfully encoded video_{i}",
                data={
                    "filepath": f"/videos/video_{i}.mp4",
                    "original_size": 1000000,
                    "encoded_size": 500000,
                    "reduction": "50%",
                    "duration_seconds": 120.5
                }
            )

    logger.info("Batch processing complete")

    # Get recent entries programmatically using window
    print("\n" + "="*60)
    print("Recent Log Entries (window-based access):")
    window, _ = logger.get_window_of_entries(window_size=10)
    for _, entry in window.items():
        print(f"{entry.timestamp} [{entry.level}] {entry.location}: {entry.message}")
        if entry.data:
            print(f"  Data keys: {list(entry.data.keys())}")

if __name__ == "__main__":
    example_usage()
