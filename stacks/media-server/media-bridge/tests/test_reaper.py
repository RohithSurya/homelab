import datetime
import os
import unittest
from unittest import mock

for k, v in {"JELLYFIN_URL": "http://jf", "JELLYFIN_API_KEY": "k",
             "RADARR_URL": "http://ra", "RADARR_API_KEY": "k", "DRY_RUN": "false"}.items():
    os.environ.setdefault(k, v)

import reaper  # noqa: E402

UTC = datetime.timezone.utc


def ago(days):
    return datetime.datetime.now(UTC) - datetime.timedelta(days=days)


def movie(mid, tmdb, file_added):
    return {"id": mid, "tmdbId": tmdb, "title": f"m{mid}", "year": 2026, "hasFile": True,
            "movieFile": {"dateAdded": file_added.isoformat().replace("+00:00", "Z")}}


class ReapTest(unittest.TestCase):
    def run_reap(self, played, movies):
        deleted = []
        with mock.patch.object(reaper, "jf_users", return_value=[("u1", "rosurya")]), \
             mock.patch.object(reaper, "jf_played_movies", return_value=played), \
             mock.patch.object(reaper, "radarr_movies_by_tmdb",
                               return_value={m["tmdbId"]: m for m in movies}), \
             mock.patch.object(reaper, "delete_movie", side_effect=deleted.append), \
             mock.patch.object(reaper, "notify"), mock.patch.object(reaper, "log"):
            reaper.reap()
        return deleted

    def test_watched_long_ago_is_deleted(self):
        self.assertEqual(self.run_reap({1: ago(30)}, [movie(10, 1, ago(60))]), [10])

    def test_redownloaded_after_watch_is_kept(self):
        # watched 30 days ago, deleted, grabbed again 2 days ago
        self.assertEqual(self.run_reap({1: ago(30)}, [movie(10, 1, ago(2))]), [])

    def test_rewatched_redownload_follows_grace(self):
        self.assertEqual(self.run_reap({1: ago(20)}, [movie(10, 1, ago(25))]), [10])
        self.assertEqual(self.run_reap({1: ago(5)}, [movie(10, 1, ago(25))]), [])

    def test_missing_file_date_keeps_old_behaviour(self):
        m = movie(10, 1, ago(2))
        m["movieFile"] = {}
        self.assertEqual(self.run_reap({1: ago(30)}, [m]), [10])


if __name__ == "__main__":
    unittest.main()
