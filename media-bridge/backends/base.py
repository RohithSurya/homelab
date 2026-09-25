"""
The contract every download backend implements for bridge.py.

The bridge owns everything the *arr apps see -- the blackhole scan, state files, delivery
under the grabbed release name, stall/missing/unfetchable deadlines, supersede, failing a
grab back to Radarr/Sonarr, and the private-tracker hold. A backend only knows how to run a
torrent somewhere and hand its bytes back:

  add          start a transfer               -> the ids the bridge stores in the state file
  locate       find a transfer the backend already holds (after Exists)
  begin_loop   take one snapshot per poll for the status() calls that follow
  status       downloading / done (+ an opaque result) / not visible yet
  fetch        copy a finished result to a local dir
  cancel       drop an in-progress transfer (stall, supersede)
  discard      drop a finished result
  start_hold / hold_over / end_hold   keep a delivered private result seeding, then drop it
  reap         clean a tombstoned (superseded/stalled/missing) transfer's late leftovers

State records are plain dicts, persisted as JSON by the bridge; a backend may add its own
keys to them (Seedr keeps user_torrent_id / hold_folder_id / hold_file_id).
"""

from common import clog


class Full(Exception):
    """No room for this transfer right now. `free` = bytes free, or None if unknown."""

    def __init__(self, free=None):
        super().__init__(f"backend full ({free} B free)")
        self.free = free


class Exists(Exception):
    """The backend already holds this torrent (added out of band, or a lost response)."""


class Rejected(Exception):
    """The backend refused the torrent outright; retrying the same drop cannot help."""


# status() states
DOWNLOADING = "downloading"
DONE = "done"
NOT_VISIBLE = "not_visible"  # finished, but its result isn't listed (yet)


class Backend:
    name = ""  # BRIDGE_ROUTE key, and rec["backend"]
    label = ""  # human name used in log lines and notifications
    tag = ""  # log tag
    # Longest seeding rule this backend can honour; the router refuses torrents whose tracker
    # needs more (a hit-and-run otherwise). None = no limit.
    max_seed_hours = None
    # wording for the unfetchable-result log line and ntfy body
    fetch_fail_log = "fetch failing"
    fetch_fail_note = "reported it complete but its files could not be fetched"

    def log(self, msg):
        clog(self.tag, msg)

    def startup(self):
        """Log reachability/capacity once at start; never raise."""

    def add(self, drop, magnet):
        """drop: the blackhole file (.magnet/.txt/.torrent); magnet: its magnet URI (for a
        .torrent, synthesized for its btih only -- upload the file itself).
        Returns {"title", "torrent_hash", ...backend keys}; raises Full / Exists / Rejected."""
        raise NotImplementedError

    def locate(self, btih, title):
        """After Exists: (backend keys dict incl. torrent_hash, human 'where') or None."""
        raise NotImplementedError

    def begin_loop(self, records):
        """records: [(state_path, rec)] of this backend's live transfers, incl. held/tombstoned."""

    def status(self, rec):
        """(DOWNLOADING, progress%) | (DONE, result) | (NOT_VISIBLE, None). `result` is opaque
        to the bridge except result["name"] (the backend's name for it, for logs)."""
        raise NotImplementedError

    def fetch(self, rec, result, dest_dir):
        """Copy the finished result into dest_dir (= downloads/<cat>/<release_name>), which the
        *arr imports from. Must leave only complete files under their final names."""
        raise NotImplementedError

    def cancel(self, rec):
        raise NotImplementedError

    def discard(self, rec, result):
        raise NotImplementedError

    def start_hold(self, rec, result):
        """Private result delivered: keep it seeding. Store what end_hold needs in rec and
        return the hold deadline as an epoch, or None when the backend decides by itself."""
        raise NotImplementedError

    def hold_over(self, rec):
        raise NotImplementedError

    def end_hold(self, rec):
        raise NotImplementedError

    def reap(self, rec):
        """Called every loop for a tombstoned rec until the bridge's grace window closes."""
