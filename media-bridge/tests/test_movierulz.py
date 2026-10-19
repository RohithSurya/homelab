"""Offline tests for the movierulz crawler's domain-rotation handling and stale-feed alert (fake fetch,
temp sqlite -- no network, no FlareSolverr)."""

import os
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import movierulz as m  # noqa: E402

OLD = "https://www.5movierulz.solutions"
NEW = "https://www.5movierulz.institute"


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        m._conn = None
        m.DB_PATH = os.path.join(tmp, "movierulz.db")
        m.BASE = OLD
        m.REQUEST_DELAY = 0
        self.pages, self.sent, self.logs = {}, [], []
        self._orig = (m.fetch, m.ntfy, m.log)
        m.fetch = lambda url: self.pages.get(url, (404, ""))
        m.ntfy = lambda title, body, **kw: self.sent.append((title, body))
        m.log = lambda msg: self.logs.append(msg)

    def tearDown(self):
        m.fetch, m.ntfy, m.log = self._orig
        m._conn = None


class DomainRotation(Base):
    def listing(self, host, ids):
        return "".join(f"<a href='{host}/some-movie-2026-telugu-{i}.html'>x</a>" for i in ids)

    def test_links_on_the_new_domain_are_crawled_and_the_move_is_logged(self):
        # BASE still says .solutions (which redirects); the listing's post links are all .institute
        self.pages[f"{OLD}/movies/page/1"] = (200, self.listing(NEW, [7017, 7016]))
        for i in (7017, 7016):
            self.pages[f"{NEW}/some-movie-2026-telugu-{i}.html"] = (
                200, f"<a href='magnet:?xt=urn:btih:{i:040x}&amp;dn=Some.Movie.{i}.2026.Telugu.mkv'>m</a>")
        m.crawl_once()
        with m.DB_LOCK:
            posts = m.db().execute("SELECT count(*) FROM posts").fetchone()[0]
            rels = m.db().execute("SELECT count(*) FROM releases").fetchone()[0]
        self.assertEqual((posts, rels), (2, 2))
        self.assertEqual(m.get_meta("moved_to"), "www.5movierulz.institute")
        self.assertTrue(any("domain moved" in line and "institute" in line for line in self.logs))

    def test_same_domain_reports_no_move(self):
        m.BASE = NEW
        self.pages[f"{NEW}/movies/page/1"] = (200, self.listing(NEW, [1]))
        self.pages[f"{NEW}/some-movie-2026-telugu-1.html"] = (200, "no magnets yet")
        m.crawl_once()
        self.assertEqual(m.get_meta("moved_to"), "")
        self.assertFalse(any("domain moved" in line for line in self.logs))

    def test_incremental_pass_stops_despite_the_newest_posts_sidebar_on_every_page(self):
        m.BASE = NEW
        m.MIN_PAGES = 3
        m.set_meta("backfill_done", "1")
        with m.DB_LOCK:                                   # catalogue already indexed: posts 1..300
            m.db().executemany("INSERT INTO posts(post_id, url, scraped_at, magnets) VALUES (?,?,?,1)",
                               [(i, f"{NEW}/x-{i}.html", time.time() - 86400) for i in range(1, 301)])
            m.db().commit()
        sidebar = [402, 401]                              # two NEW posts, shown on every page
        listing = {1: [402, 401, 300, 299], 2: [298, 297, 296], 3: [295, 294, 293], 4: [292, 291], 5: [290]}
        for n, ids in listing.items():
            self.pages[f"{NEW}/movies/page/{n}"] = (200, self.listing(NEW, ids + sidebar))
        for i in sidebar:
            self.pages[f"{NEW}/some-movie-2026-telugu-{i}.html"] = (
                200, f"<a href='magnet:?xt=urn:btih:{i:040x}&amp;dn=Some.Movie.{i}.2026.mkv'>m</a>")
        m.crawl_once()
        self.assertTrue(any("scanned 3 page(s), 2 posts fetched" in line for line in self.logs))

    def test_ads_and_other_hosts_are_not_posts(self):
        html = "<a href='https://adexchangerapid.com/some-thing-12.html'>ad</a>" + self.listing(NEW, [5])
        self.assertEqual([g[1] for g in m.POST_RE.findall(html)], ["5"])


class StaleAlert(Base):
    def post_seen(self, pid, ago_days):
        with m.DB_LOCK:
            m.db().execute("INSERT INTO posts(post_id, url, scraped_at, magnets) VALUES (?,?,?,1)",
                           (pid, f"{NEW}/x-{pid}.html", time.time() - ago_days * 86400))
            m.db().commit()

    def test_alerts_after_four_days_repeats_daily_and_resets_on_new_posts(self):
        now = time.time()
        self.post_seen(1, 3.9)
        self.assertFalse(m.check_stale(now))                       # 3.9 days: fine
        with m.DB_LOCK:
            m.db().execute("UPDATE posts SET scraped_at=?", (now - 4.2 * 86400,))
            m.db().commit()
        m.set_meta("moved_to", "www.5movierulz.institute")
        self.assertTrue(m.check_stale(now))
        title, body = self.sent[-1]
        self.assertIn("no new posts for 4.2 days", title)
        self.assertIn("update MOVIERULZ_BASE", body)
        self.assertFalse(m.check_stale(now + 3600))                # not again within a day
        self.assertTrue(m.check_stale(now + 86400 + 1))            # daily repeat
        self.post_seen(2, 0)                                       # posts flow again
        self.assertFalse(m.check_stale(now + 2 * 86400))
        self.assertEqual(m.get_meta("stale_alert"), "0")
        self.assertEqual(len(self.sent), 2)

    def test_empty_db_never_alerts(self):
        self.assertFalse(m.check_stale())
        self.assertEqual(self.sent, [])


class SonarrNudge(Base):
    """nudge_sonarr: refresh a library show only when the feed has episodes Sonarr doesn't list."""

    def setUp(self):
        super().setUp()
        self.calls, self.episodes = [], {17: [(3, e) for e in range(1, 37)]}
        test = self

        class Resp:
            def __init__(self, data):
                self.data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self.data

        class FakeRequests:
            @staticmethod
            def get(url, params=None, **kw):
                if url.endswith("/series"):
                    return Resp([{"id": 17, "title": "Heart Beat", "year": 2024}])
                return Resp([{"seasonNumber": s_, "episodeNumber": e} for s_, e in test.episodes[params["seriesId"]]])

            @staticmethod
            def post(url, json=None, **kw):
                test.calls.append(json)
                return Resp({})

        self._req = (m.requests, m.SONARR_URL, m.SONARR_API_KEY)
        m.requests, m.SONARR_URL, m.SONARR_API_KEY = FakeRequests, "http://sonarr", "k"
        m._SERIES_YEAR_TS = 0
        m._NUDGED.clear()

    def tearDown(self):
        m.requests, m.SONARR_URL, m.SONARR_API_KEY = self._req
        m._SERIES_YEAR_TS = 0
        super().tearDown()

    def test_missing_episodes_refresh_once_per_interval(self):
        titles = ["Heart Beat (2026) S03E37-E40 TRUE WEB-DL - 1080p", "Heart Beat (2026) S03E37-E40 TRUE WEB-DL - 720p"]
        self.assertEqual(m.nudge_sonarr(titles), [17])
        self.assertEqual(self.calls, [{"name": "RefreshSeries", "seriesId": 17}])
        self.assertEqual(m.nudge_sonarr(titles), [])  # throttled
        self.assertEqual(len(self.calls), 1)

    def test_known_episodes_and_unknown_shows_do_nothing(self):
        self.assertEqual(m.nudge_sonarr(["Heart Beat (2026) S03E33-E36 TRUE WEB-DL - 1080p",
                                         "Some Other Show (2026) S01E01-E08 1080p",
                                         "Karuppu (2026) Tamil 1080p"]), [])
        self.assertEqual(self.calls, [])


class Search(Base):
    """Query matching + paging: a short alias must not match the whole catalogue, the query's own
    title outranks alias matches, and offset/limit are honoured (Athadu, 2026-10-16)."""

    def setUp(self):
        super().setUp()
        self._alias = (m._ALIAS, m._ALIAS_TS, m.RADARR_URL)
        m.RADARR_URL = ""   # _radarr_aliases() then returns m._ALIAS as is
        m._ALIAS = {"athadu": ["Athadu", "He", "Nandhu", "The Target"]}

    def tearDown(self):
        m._ALIAS, m._ALIAS_TS, m.RADARR_URL = self._alias
        super().tearDown()

    def add(self, i, title, added):
        m.upsert_release({"btih": f"{i:040x}", "post_id": str(i), "title": title, "kind": "movie",
                          "season": None, "language": "telugu", "year": 2005, "quality": "480p",
                          "size": 1, "magnet": f"magnet:?xt=urn:btih:{i:040x}", "added_at": added})

    def test_short_words_must_be_whole_title_words(self):
        self.assertFalse(m.title_matches("He 2005", "The Heart (2005) Telugu 1080p"))
        self.assertTrue(m.title_matches("He 2005", "He (2005) Telugu 1080p"))
        self.assertTrue(m.title_matches("Kill Bill Vol 2", "Kill.Bill.Vol.2.2004.1080p"))
        self.assertTrue(m.title_matches("Athadu 2005", "ATHADU (2005) Telugu - DVD9 - 480p"))

    def test_query_title_ranks_first_and_pages_are_honoured(self):
        self.add(1, "ATHADU (2005) Telugu - DVD9 - UNTOUCHED - 480p", added=1)
        for i in range(2, 152):   # newer releases matching only the alias 'The Target 2005'
            self.add(i, f"The Target {i} (2005) Hindi 720p", added=i)
        self.assertEqual(m.search_terms("Athadu 2005")[0], "Athadu 2005")
        first = m.query_releases("search", "Athadu 2005", "", [])
        self.assertEqual(len(first), 100)
        self.assertEqual(first[0]["title"], "ATHADU (2005) Telugu - DVD9 - UNTOUCHED - 480p")
        rest = m.query_releases("search", "Athadu 2005", "", [], offset=100, limit=100)
        self.assertEqual(len(rest), 51)
        self.assertEqual(m.query_releases("search", "Athadu 2005", "", [], offset=200), [])
        self.assertFalse({r["btih"] for r in first} & {r["btih"] for r in rest})

    def test_page_args(self):
        self.assertEqual(m.page_args({}), (0, 100))
        self.assertEqual(m.page_args({"offset": ["300"], "limit": ["50"]}), (300, 50))
        self.assertEqual(m.page_args({"offset": ["x"], "limit": ["1000"]}), (0, 100))


if __name__ == "__main__":
    unittest.main()
