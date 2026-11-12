import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="fresh-test-")
os.environ["FRESH_DB"] = os.path.join(TMP, "fresh.db")
os.environ["FRESH_DATA"] = os.path.join(TMP, "data")
os.environ["FRESH_MODE"] = "approval"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fresh  # noqa: E402

GB = 1e9


def rel(title, size_gb, btih=None, lang=None):
    r = fresh.parse_release(title, int(size_gb * GB), lang)
    r.update(btih=btih or title[-12:], magnet=f"magnet:?xt=urn:btih:{btih or 'x'}", site="tamilmv", added=time.time())
    return r


class ParseRelease(unittest.TestCase):
    def test_theatre_prints_rejected(self):
        for t in ("Mahasakthi (2026) Telugu DVDScr - x264 - AAC - 250MB",
                  "Mahasakthi (2026) Telugu HQ PreDVD - 1080p - x264 - HQ Clean - 2.6GB",
                  "Mookuthi Amman 2 (2026) Tamil HQ HDTS - 1080p - x264 - HQ Clean - AAC - 6GB",
                  "Scene (2026) Tamil HQ HDTS - 720p - x264 - HQ Clean - AAC - 950MB",
                  "Jailer 2 (2026) Telugu HDRip - 1080p - x264 - HQ Clean - AAC - 2.8GB"):
            self.assertIsNotNone(fresh.parse_release(t, 2 * GB)["reject"], t)

    def test_ott_rips_accepted(self):
        for t in ("The Dark Heaven (2026)\xa0TRUE WEB-DL - 1080p - AVC - [Tel + Hin] - AAC - 2GB - ESub",
                  "Yezhu Kadal Yezhu Malai (2026) Tamil HQ HDRip - 1080p - x264 - (DD5.1 - 384Kbps & AAC) - 2.7GB - ESub",
                  "The Odyssey (2026) 1080p BluRay\xa0- x264 - [Tel + Tam + Hin + Eng] - HQ Clean - 3.3GB - ESub",
                  "Jailer 2 (2026) Tamil HQ HDRip - 1080p - x264 - (AAC 2.0) - 2.8GB - HC- ESub"):
            self.assertIsNone(fresh.parse_release(t, 2 * GB)["reject"], t)

    def test_hc_flag_and_pack(self):
        self.assertTrue(fresh.parse_release("Jailer 2 (2026) Tamil HQ HDRip - x264 - AAC - 400MB - HC-ESub", 4e8)["hc"])
        pack = "Jailer 2 (2026) Tamil TRUE UHD + ESub - [4K, 1080p & 720p - AVC / HEVC - (DD+5.1 - 192Kbps) - 15GB + Rips]"
        self.assertEqual(fresh.parse_release(pack, 15 * GB)["reject"], "multi-resolution pack")

    def test_audio_languages(self):
        self.assertEqual(fresh.audio_langs("X (2026) TRUE WEB-DL - [Tam + Tel + Hin + Mal + Kan] - 9GB")[0],
                         {"tam", "tel", "hin", "mal", "kan"})
        self.assertEqual(fresh.audio_langs("X (2026) HQ HDRip - [Tamil + Telugu + Hindi + Eng] - 1GB")[0],
                         {"tam", "tel", "hin", "eng"})
        self.assertEqual(fresh.audio_langs("Jailer 2 (2026) Kannada HDRip - 1080p", "telugu")[0], {"kan"})
        langs, guessed = fresh.audio_langs("Jailer 2 (2026) HQ HDRip - 1080p - HEVC - 1.5GB", "tamil")
        self.assertEqual((langs, guessed), ({"tam"}, True))
        # a bracket that is not a language list is ignored
        self.assertEqual(fresh.audio_langs("X (2026) Telugu HDRip [Some Tag] 1GB")[0], {"tel"})

    def test_resolution_and_title(self):
        self.assertEqual(fresh.parse_release("X (2026) Tamil HQ HDRip - x264 - AAC - 400MB - ESub", 4e8)["res"], 480)
        self.assertEqual(fresh.parse_release("X (2026) TRUE WEB-DL - 4K SDR - HEVC - 6GB", 6 * GB)["res"], 2160)
        self.assertEqual(fresh.parse_title("The Dark Heaven (2026)\xa0HQ HDRip"), ("The Dark Heaven", 2026))
        self.assertEqual(fresh.parse_title("no year here"), (None, None))


class Plan(unittest.TestCase):
    def test_dark_heaven_two_files_telugu_primary(self):
        rs = [rel("The Dark Heaven (2026) Tamil TRUE WEB-DL - 1080p - AVC - (DD+5.1 - 640Kbps & AAC) - 2.5GB - ESub", 2.5, "a"),
              rel("The Dark Heaven (2026) Tamil TRUE WEB-DL - 1080p - AVC - (DD+5.1 - 640Kbps & AAC) - 4.5GB - ESub", 4.5, "b"),
              rel("The Dark Heaven (2026)\xa0TRUE WEB-DL - 1080p - AVC - [Tel + Hin] - AAC - 2GB - ESub", 2.0, "c"),
              rel("The Dark Heaven (2026)\xa0HQ HDRip - 720p - x264 - [Tel + Hin] - AAC - 1.2GB - ESub", 1.2, "d")]
        p = fresh.plan_versions(rs, "tam")
        self.assertEqual(p["status"], "ok")
        self.assertEqual([(v["role"], v["lang"], v["rel"]["btih"]) for v in p["versions"]],
                         [("primary", "tel", "c"), ("secondary", "tam", "b")])  # biggest within the 5 GB cap

    def test_multi_audio_preferred(self):
        rs = [rel("Meesaya Murukku 2 (2026) TRUE WEB-DL - 1080p - AVC - [Tam + Tel + Hin + Mal + Kan] - 5.5GB - ESub", 5.5, "m"),
              rel("Meesaya Murukku 2 (2026) Tamil HQ HDRip - 1080p - x264 - 2.9GB - ESub", 2.9, "t"),
              rel("Meesaya Murukku 2 (2026) Telugu HQ HDRip - 1080p - x264 - 2.9GB - ESub", 2.9, "g")]
        p = fresh.plan_versions(rs, "tam")
        self.assertEqual([v["rel"]["btih"] for v in p["versions"]], ["m"])

    def test_multi_not_taken_when_only_4k(self):
        rs = [rel("X (2026) TRUE WEB-DL - 4K SDR - HEVC - [Tam + Tel] - 9GB", 9, "k"),
              rel("X (2026) Tamil TRUE WEB-DL - 1080p - 2GB", 2, "t"),
              rel("X (2026) Telugu TRUE WEB-DL - 1080p - 2GB", 2, "g")]
        self.assertEqual([v["rel"]["btih"] for v in fresh.plan_versions(rs, "tam")["versions"]], ["g", "t"])

    def test_original_only_hindi(self):
        rs = [rel("Last Man In Tower (2026) Hindi TRUE WEB-DL - 1080p - AVC - 1.4GB - ESub", 1.4, "h"),
              rel("Last Man In Tower (2026) Hindi TRUE WEB-DL - 4K SDR - 2160p HQ - HEVC", 3.4, "k")]
        p = fresh.plan_versions(rs, "hin")
        self.assertEqual([(v["role"], v["lang"], v["rel"]["btih"]) for v in p["versions"]], [("primary", "hin", "h")])

    def test_third_language_dub_waits(self):
        rs = [rel("7 Star Boys (2026) Tamil TRUE WEB-DL - 1080p - AVC - 2.6GB - ESub", 2.6, "s")]
        p = fresh.plan_versions(rs, "mal")
        self.assertEqual(p["status"], "waiting_lang")
        self.assertIn("Malayalam", p["why"])

    def test_size_cap_then_720_then_4k(self):
        big = rel("X (2026) Telugu TRUE WEB-DL - 1080p - 8GB", 8, "big")
        big2 = rel("X (2026) Telugu TRUE WEB-DL - 1080p - 12GB", 12, "big2")
        s720 = rel("X (2026) Telugu HQ HDRip - 720p - 1.4GB", 1.4, "720")
        uhd = rel("X (2026) Telugu TRUE WEB-DL - 4K - 15GB", 15, "4k")
        pick = lambda rs: fresh.plan_versions(rs, "tel")["versions"][0]["rel"]["btih"]  # noqa: E731
        self.assertEqual(pick([big, big2, s720, uhd]), "big")   # smallest 1080p above the cap
        self.assertEqual(pick([s720, uhd]), "720")
        self.assertEqual(pick([uhd]), "4k")

    def test_prefers_true_webdl_and_no_hc(self):
        a = rel("X (2026) Tamil HQ HDRip - 1080p - 2.8GB - HC-ESub", 2.8, "hc")
        b = rel("X (2026) Tamil HQ HDRip - 1080p - 2.6GB - ESub", 2.6, "soft")
        c = rel("X (2026) Tamil TRUE WEB-DL - 1080p - 2.9GB - ESub", 2.9, "web")
        self.assertEqual(fresh.plan_versions([a, b], "tam")["versions"][0]["rel"]["btih"], "soft")
        self.assertEqual(fresh.plan_versions([a, b, c], "tam")["versions"][0]["rel"]["btih"], "web")
        self.assertEqual(fresh.plan_versions([a], "tam")["versions"][0]["rel"]["btih"], "hc")  # HC is allowed

    def test_naming(self):
        self.assertEqual(fresh.file_name("Adaikalam (2026)", "primary", "tel", ".mkv"), "Adaikalam (2026).mkv")
        self.assertEqual(fresh.file_name("Adaikalam (2026)", "secondary", "tam", ".mkv"), "Adaikalam (2026) - Tamil.mkv")


class Foreign(unittest.TestCase):
    def test_rules(self):
        dub = [rel("Resident Evil (2026) HQ HDRip - 1080p - x264 - [Tamil + Telugu + Hindi + Eng] - 3GB", 3)]
        self.assertIn("English", fresh.foreign_reason({}, "eng", dub))
        # TMDB matched the wrong (English) film, but a plain Tamil rip says it's a Tamil original
        grand = [rel("The Grandmaster (2026) Tamil TRUE WEB-DL - 1080p - AVC - 3.2GB - ESub", 3.2)]
        self.assertIsNone(fresh.foreign_reason({}, "eng", grand))
        self.assertIn("France", fresh.foreign_reason({"Country": "France"}, "tam", grand))
        self.assertIsNone(fresh.foreign_reason({"Country": "India"}, "tam", grand))
        eng = [rel("The Dog Stars (2026) English HQ HDRip - 1080p - 2.3GB", 2.3)]
        self.assertIsNotNone(fresh.foreign_reason({}, "eng", eng))


class Mkv(unittest.TestCase):
    def test_default_audio_args(self):
        tracks = [{"type": "video"}, {"type": "audio", "properties": {"language": "tam"}},
                  {"type": "audio", "properties": {"language": "tel"}}, {"type": "subtitles"}]
        self.assertEqual(fresh.default_audio_args(tracks),
                         ["--edit", "track:a1", "--set", "flag-default=0", "--edit", "track:a2", "--set", "flag-default=1"])
        self.assertIsNone(fresh.default_audio_args([{"type": "audio", "properties": {"language": "tel"}}]))
        self.assertIsNone(fresh.default_audio_args([{"type": "audio", "properties": {"language": "tam"}},
                                                    {"type": "audio", "properties": {"language": "hin"}}]))
        named = [{"type": "audio", "properties": {"language": "und", "track_name": "Tamil"}},
                 {"type": "audio", "properties": {"language": "und", "track_name": "Telugu DD+5.1"}}]
        self.assertIn("flag-default=1", fresh.default_audio_args(named)[-1])


class InfoBlock(unittest.TestCase):
    def test_parse(self):
        page = ("<h2>The Good Lawyer Movie Information</h2><p><b>Directed by:</b></p><p>Tristan</p>"
                "<p><b>Starring by:</b></p><a>Laurent Lafitte</a>,<a>Mélanie Laurent</a>"
                "<p><b>Country:</b></p><p>France</p><p><b>Language:</b></p><p>Telugu Dubbed</p>"
                "<p><b>Quality:</b></p><p>HDRip</p><h3>Synopsis</h3><p>text</p>")
        info = fresh.parse_info_block(page)
        self.assertEqual(info["Country"], "France")
        self.assertEqual(info["Language"], "Telugu Dubbed")
        self.assertEqual(info["Starring by"], "Laurent Lafitte, Mélanie Laurent")


class Flow(unittest.TestCase):
    def setUp(self):
        fresh._conn = None
        if os.path.exists(fresh.DB_PATH):
            os.remove(fresh.DB_PATH)
        fresh._RELS.clear()
        fresh.BLACKHOLE = pathlib.Path(TMP) / "data" / "blackhole" / "fresh"
        shutil_rm(fresh.BLACKHOLE)

    def add_movie(self, title, orig="tam", score=8.0, big=0, n_rel=1):
        self.n = getattr(self, "n", 0) + 1
        mid = fresh.x("INSERT INTO movies(mkey, title, year, tmdb, orig, status, first_seen, first_good) "
                      "VALUES (?,?,?,?,?,?,?,?)", (fresh.norm(title) + "2026", title, 2026, 1000 + self.n,
                                                   orig, "new", time.time(), time.time()))
        fresh._RELS[mid] = [rel(f"{title} (2026) Telugu TRUE WEB-DL - 1080p - 2GB - ESub", 2, f"{mid}tel"),
                            rel(f"{title} (2026) Tamil TRUE WEB-DL - 1080p - 2GB - ESub", 2, f"{mid}tam")][:n_rel + 1]
        return mid

    def test_decide_approve_start_within_cap(self):
        ids = [self.add_movie(f"Movie {i}") for i in range(17)]
        fresh.apply_decisions([{"id": i, "verdict": "download", "score": 7} for i in ids], "sonnet")
        self.assertEqual({r["status"] for r in fresh.q("SELECT status FROM movies")}, {"proposed"})
        fresh.approve(ids)
        started = fresh.start_approved()
        self.assertEqual(len(started), fresh.WEEKLY)
        drops = sorted(p.name for p in fresh.BLACKHOLE.glob("*.magnet"))
        self.assertEqual(len(drops), 2 * fresh.WEEKLY)  # Telugu + Tamil file each, one movie each
        # a big movie may go over the cap
        big = self.add_movie("Ramayana Big")
        fresh.apply_decisions([{"id": big, "verdict": "download", "score": 9, "big": True}])
        fresh.approve([big])
        self.assertEqual(fresh.start_approved(), ["Ramayana Big"])

    def test_waiting_lang_and_watchlist(self):
        mid = self.add_movie("Seven Boys", orig="mal")
        fresh._RELS[mid] = [rel("Seven Boys (2026) Tamil TRUE WEB-DL - 1080p - 2GB", 2, "only-tam")]
        fresh.apply_decisions([{"id": mid, "verdict": "download", "score": 6}])
        self.assertEqual(fresh.q("SELECT status FROM movies WHERE id=?", (mid,))[0]["status"], "waiting_lang")
        fresh._RELS[mid].append(rel("Seven Boys (2026) Malayalam TRUE WEB-DL - 1080p - 2GB", 2, "mal"))
        fresh.start_approved()
        self.assertEqual(fresh.q("SELECT status FROM movies WHERE id=?", (mid,))[0]["status"], "proposed")

        w = self.add_movie("Theatre Hit", orig="hin")
        fresh._RELS[w] = [rel("Theatre Hit (2026) Hindi DVDScr - 1GB", 1, "scr")]
        fresh.apply_decisions([{"id": w, "verdict": "watchlist", "score": 9}])
        fresh.start_approved()
        self.assertEqual(fresh.q("SELECT status FROM movies WHERE id=?", (w,))[0]["status"], "watchlist")
        fresh._RELS[w].append(rel("Theatre Hit (2026) Hindi TRUE WEB-DL - 1080p - 2GB", 2, "web"))
        fresh.start_approved()
        self.assertEqual(fresh.q("SELECT status FROM movies WHERE id=?", (w,))[0]["status"], "active")

    def test_corrected_language_replans(self):
        mid = self.add_movie("Grand", orig="eng")
        fresh.apply_decisions([{"id": mid, "verdict": "download", "score": 6, "orig_lang": "tamil"}])
        m = fresh.q("SELECT status, orig FROM movies WHERE id=?", (mid,))[0]
        self.assertEqual((m["status"], m["orig"]), ("proposed", "tam"))

    def test_block_and_auto_mode(self):
        mid = self.add_movie("Bad One")
        fresh.block([mid])
        self.assertEqual(fresh.q("SELECT status FROM movies WHERE id=?", (mid,))[0]["status"], "blocked")
        fresh.meta_set("mode", "auto")
        m2 = self.add_movie("Auto One")
        fresh.apply_decisions([{"id": m2, "verdict": "download", "score": 8}])
        self.assertEqual(fresh.q("SELECT status FROM movies WHERE id=?", (m2,))[0]["status"], "approved")
        fresh.meta_set("mode", "approval")

    def test_failed_version_retries_next_release_then_gives_up(self):
        mid = self.add_movie("Retry Me", orig="tel", n_rel=0)
        fresh._RELS[mid] = [rel("Retry Me (2026) Telugu TRUE WEB-DL - 1080p - 3GB", 3, "r1"),
                            rel("Retry Me (2026) Telugu HQ HDRip - 1080p - 2GB", 2, "r2")]
        fresh.apply_decisions([{"id": mid, "verdict": "download", "score": 8}])
        fresh.approve([mid])
        fresh.start_approved()
        v = fresh.q("SELECT * FROM versions WHERE movie_id=?", (mid,))[0]
        self.assertEqual(v["btih"], "r1")
        with mock.patch.object(fresh, "prowlarr_version", return_value=None):
            fresh.retry_version(v, "stalled")
            v = fresh.q("SELECT * FROM versions WHERE movie_id=?", (mid,))[0]
            self.assertEqual((v["btih"], v["state"]), ("r2", "dropped"))
            fresh.retry_version(v, "stalled")
            v = fresh.q("SELECT * FROM versions WHERE movie_id=?", (mid,))[0]
            self.assertEqual(v["state"], "failed")  # no more releases
        fresh.add_missing_versions()  # a failed language is not re-queued forever
        self.assertEqual(len(fresh.q("SELECT * FROM versions WHERE movie_id=?", (mid,))), 1)

    def test_expiry_guards(self):
        mid = self.add_movie("Old One")
        keep = self.add_movie("Fav One")
        part = self.add_movie("Half One")
        old = time.time() - 40 * 86400
        for i, jid in ((mid, "j1"), (keep, "j2"), (part, "j3")):
            fresh.x("UPDATE movies SET status='done', placed_at=?, radarr_added=1, radarr_id=?, jf_id=? WHERE id=?",
                    (old, i, jid, i))

        def fake_jf(method, path, **kw):
            if path == "Users":
                return [{"Id": "u1", "Name": "rosurya"}, {"Id": "u2", "Name": "guest"}]
            if path == "Users/u2/Items":
                return {"Items": [{"Id": "j2", "UserData": {"IsFavorite": True}},
                                  {"Id": "j3", "UserData": {"PlaybackPositionTicks": 5, "Played": False}}]}
            return {"Items": []}
        deleted = []
        with mock.patch.object(fresh, "jellyfin", side_effect=fake_jf), \
             mock.patch.object(fresh, "EXPIRE_DRY_RUN", False), \
             mock.patch.object(fresh.requests, "delete", side_effect=lambda url, **k: deleted.append(url) or mock.Mock()), \
             mock.patch.object(fresh, "ntfy"):
            gone = fresh.expire()
        self.assertEqual(gone, ["Old One (2026)"])
        self.assertEqual(len(deleted), 1)
        self.assertTrue(deleted[0].endswith(f"/movie/{mid}"))

    def test_expiry_aborts_on_jellyfin_error(self):
        mid = self.add_movie("Old Two")
        fresh.x("UPDATE movies SET status='done', placed_at=?, radarr_added=1, radarr_id=1, jf_id='j' WHERE id=?",
                (time.time() - 40 * 86400, mid))
        with mock.patch.object(fresh, "jellyfin", side_effect=RuntimeError("down")), \
             mock.patch.object(fresh.requests, "delete") as d:
            self.assertEqual(fresh.expire(), [])
            d.assert_not_called()

    def test_two_titles_one_film_merge(self):
        a = self.add_movie("Mookuthi Amman 2", orig="tam", n_rel=0)
        fresh._RELS[a] = [rel("Mookuthi Amman 2 (2026) Tamil TRUE WEB-DL - 1080p - 2GB", 2, "tam")]
        b = self.add_movie("Mahasakthi", orig=None, n_rel=0)
        fresh._RELS[b] = [rel("Mahasakthi (2026) Telugu TRUE WEB-DL - 1080p - 2GB", 2, "tel")]
        fresh.x("UPDATE movies SET tmdb=1327835 WHERE id IN (?,?)", (a, b))
        fresh.merge_duplicates()
        self.assertEqual(fresh.q("SELECT status FROM movies WHERE id=?", (b,))[0]["status"], "duplicate")
        plan = fresh.current_plan(fresh.q("SELECT * FROM movies WHERE id=?", (a,))[0])
        self.assertEqual([(v["role"], v["lang"]) for v in plan["versions"]], [("primary", "tel"), ("secondary", "tam")])

    def test_digest_lists_proposals(self):
        mid = self.add_movie("Digest Me")
        fresh.apply_decisions([{"id": mid, "verdict": "download", "score": 7.5, "reason": "Good reviews"}])
        d = fresh.digest()
        self.assertIn(f"#{mid} Digest Me (2026)", d["text"])
        fresh.mark_notified(d["ids"])
        self.assertEqual(fresh.digest(only_new=True)["ids"], [])


def shutil_rm(p):
    import shutil
    shutil.rmtree(p, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
