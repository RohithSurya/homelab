import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="fresh-en-test-")
os.environ["FRESH_EN_DB"] = os.path.join(TMP, "fresh_en.db")
os.environ["FRESH_EN_DATA"] = os.path.join(TMP, "data")
os.environ["FRESH_EN_MIN_FREE_GB"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fresh  # noqa: E402
import fresh_en  # noqa: E402

E = fresh_en.E
GB = 1e9


def rel(title, size_gb, seeders=50, private=False, btih=None):
    r = fresh_en.parse_release_en(title, int(size_gb * GB), None, seeders)
    r.update(btih=btih or title.lower()[-24:], magnet="magnet:?xt=urn:btih:" + (btih or "x"), url="http://p/dl",
             private=private, site="Knaben", added=time.time())
    return r


class TwoInstances(unittest.TestCase):
    def test_own_state_and_names(self):
        self.assertIsNot(E, fresh)
        self.assertEqual((fresh.CATEGORY, E.CATEGORY), ("fresh", "fresh-en"))
        self.assertEqual(E.COLLECTION, "Latest Hollywood")
        self.assertEqual(fresh.COLLECTION, "Latest Releases")
        self.assertTrue(str(E.BLACKHOLE).endswith("blackhole/fresh-en"))
        self.assertEqual(E.DB_PATH, os.environ["FRESH_EN_DB"])
        self.assertEqual((E.WEEKLY, E.OVERFLOW, E.mode()), (7, 3, "auto"))
        # the Indian module keeps its own hooks
        self.assertIs(fresh.plan_for.__module__, "fresh")
        self.assertTrue(fresh.orig_allowed("tel"))
        self.assertFalse(E.orig_allowed("tel"))

    def test_routing(self):
        code, _, _ = fresh.handle("GET", "/fresh-en/status.json")
        self.assertEqual(code, 404)  # the Indian handler never answers for /fresh-en
        code, _, body = fresh_en.handle("GET", "/fresh-en/status.json")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["mode"], "auto")
        self.assertEqual(fresh_en.handle("GET", "/fresh/status.json")[0], 404)


class Parse(unittest.TestCase):
    def test_scene_titles(self):
        p = fresh_en.parse_title_en
        self.assertEqual(p("The.History.of.Concrete.2026.1080p.AMZN.WEB-DL.DDP5.1.H.264-Kitsune"),
                         ("The History of Concrete", 2026))
        self.assertEqual(p("The Odyssey (2026) [1080p] [BluRay] [x265] [10bit] [5.1]"), ("The Odyssey", 2026))
        self.assertEqual(p("2001.A.Space.Odyssey.1968.2160p.UHD.BluRay.x265-X"), ("2001 A Space Odyssey", 1968))
        self.assertEqual(p("Primetime 2026 1080p WEBRip 10Bit DDP5 1 x265 NeoNoir"), ("Primetime", 2026))

    def test_rejects(self):
        cases = {
            "A.Talent.for.Murder.2026.CAM.1080p.EN-CinemaCity": "theatre print",
            "The Odyssey 2026 MVO HDrS HDRip": None,  # some reject (MVO dub or HDRip)
            "The odyssey 2026 1080p dual lat cinecalidad my": "non-English dub",
            "The.Odyssey.2026.MULTi.TRUEFRENCH.1080p.BluRay.x264-Ulysse": "non-English dub",
            "Possible Love 2026 1080p (DUAL) WEB DL HEVC x265 5 1 BONE": "non-English dub",
            "Spider Man (2026) HQ HDRip - x264 - [Hin + Eng] - 1.4GB": None,
            "Show.S01E02.1080p.WEB-DL.H.264-FLUX": "TV episode",
        }
        for t, why in cases.items():
            r = fresh_en.parse_release_en(t, 3 * GB)
            self.assertIsNotNone(r["reject"], t)
            if why:
                self.assertEqual(r["reject"], why, t)

    def test_dry_run_findings(self):
        """Release names from the 2026-11-16 live dry run."""
        p = fresh_en.parse_release_en
        self.assertEqual(p("The Whisper Man (2026) (UHD WEBRip 1080p AV1 HDR10 EAC3 Atmos) [OEP]", 5 * GB)["res"], 1080)
        self.assertEqual(p("Coyote vs Acme 2026 2160P.AI.WEB-DL.60FPS.H265.SDR.DDP5 1 Atmos", 38 * GB)["reject"], "upscale")
        self.assertEqual(p("Your Mother Your Mother Your Mother (2026) EN DS4K 1080p AMZN 5.1 HEVC - SP1EG3L", 2.6 * GB)["source"],
                         "WEB-DL")
        tel = p("The Odyssey (2026) 1080p BluRay - x264 - [Tel + Tam + Hin + Eng] - HQ Clean - 3.3GB - ESub", 3.3 * GB)
        self.assertIsNone(tel["reject"])  # 'HQ Clean' wording on a BluRay is not a theatre print
        self.assertIsNotNone(p("Vibe (2026) HQ HDRip - 1080p - x264 - [Tamil + Telugu + Hindi] - 3.6GB", 3.6 * GB)["reject"])
        self.assertEqual(fresh_en.parse_title_en("Resident Evil 2026 2026 1080p AMZN WEB-DL DDP5 1 H 264-KyoGo"),
                         ("Resident Evil", 2026))
        self.assertEqual(fresh_en.parse_title_en("BEWARE BOI&Uacute;NA (2026) 2160p WEB"), ("BEWARE BOIÚNA", 2026))
        self.assertIn("no TMDB match", fresh_en.foreign_reason_en({}, None, []))

    def test_accepts_and_flags(self):
        r = fresh_en.parse_release_en("Resident.Evil.2026.REPACK.2160p.AMZN.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-FLUX", 18 * GB, None, 900)
        self.assertIsNone(r["reject"])
        self.assertEqual((r["res"], r["source"], r["group"], r["grp_rank"], r["hdr_rank"], r["audio"]),
                         (2160, "WEB-DL", "FLUX", 0, 0, 0))
        self.assertTrue(r["service"] and r["proper"])
        y = fresh_en.parse_release_en("Model Citizen (2026) [2160p] [4K] [WEB] [5.1] [YTS.MX]", 5 * GB)
        self.assertIsNone(y["reject"])
        self.assertEqual(y["grp_rank"], 3)  # YTS = low quality
        tel = fresh_en.parse_release_en("Spider-Man Brand New Day (2026) 1080p WEB-DL - [Tel + Tam + Hin + Eng] - 5GB", 5 * GB)
        self.assertIsNone(tel["reject"])
        self.assertIn("tel", tel["langs"])
        remux = fresh_en.parse_release_en("The.Odyssey.2026.2160p.UHD.BluRay.REMUX.HDR.HEVC.DTS-HD.MA.5.1-CiNEPHiLES", 89 * GB)
        self.assertTrue(remux["remux"])
        disc = fresh_en.parse_release_en("The.Odyssey.2026.COMPLETE.UHD.BLURAY-SURCODE", 90 * GB)
        self.assertTrue(disc["disc"])
        self.assertEqual(fresh_en.parse_release_en("X.2026.1080p.WEB-DL-GRP", 2 * GB, None, 1)["reject"], "too few seeders")


class Ladder(unittest.TestCase):
    def setUp(self):
        self.k = lambda big=False: (lambda r: fresh_en.rank_key_en(r, big))

    def test_4k_first_then_1080p(self):
        hd = rel("Heart.of.the.Beast.2026.1080p.AMZN.WEB-DL.DDP5.1.H.264-FLUX", 7)
        uhd = rel("Heart.of.the.Beast.2026.2160p.AMZN.WEB-DL.DDP5.1.DV.HDR.H.265-FLUX", 17)
        yts4k = rel("Heart of the Beast (2026) [2160p] [4K] [WEB] [5.1] [YTS.MX]", 5)
        self.assertEqual(min([hd, uhd, yts4k], key=self.k()), uhd)
        self.assertEqual(min([hd, yts4k], key=self.k()), hd)  # a good 1080p beats a low-bitrate 4K

    def test_hdr_order_and_dv_only_last(self):
        dvhdr = rel("X.2026.2160p.WEB-DL.DV.HDR.H.265-FLUX", 15)
        hdr = rel("X.2026.2160p.WEB-DL.HDR.H.265-FLUX", 15, btih="b")
        dv = rel("X.2026.2160p.WEB-DL.DV.H.265-FLUX", 15, btih="c")
        sdr = rel("X.2026.2160p.WEB-DL.H.265-FLUX", 15, btih="d")
        self.assertEqual(sorted([dv, sdr, hdr, dvhdr], key=self.k()), [dvhdr, hdr, sdr, dv])

    def test_remux_only_for_big(self):
        remux = rel("X.2026.2160p.UHD.BluRay.REMUX.HDR.HEVC.TrueHD.Atmos-FraMeSToR", 70)
        uhd = rel("X.2026.2160p.WEB-DL.DV.HDR.H.265-FLUX", 17)
        self.assertEqual(fresh_en.tier_en(remux), fresh_en.NEVER)
        self.assertEqual(min([remux, uhd], key=self.k(True)), remux)
        too_big = rel("X.2026.2160p.UHD.BluRay.REMUX-Y", 130)
        self.assertEqual(fresh_en.tier_en(too_big, True), fresh_en.NEVER)

    def test_public_first_private_ranks_two_tiers_lower(self):
        pub_hd = rel("X.2026.1080p.WEB-DL.H.264-FLUX", 6)
        priv_4k = rel("X.2026.2160p.WEB-DL.HDR.H.265-FLUX", 16, private=True, btih="p")
        pub_4k = rel("X.2026.2160p.WEB-DL.HDR.H.265-NTb", 16, btih="q")
        yts_hd = rel("X (2026) [1080p] [WEBRip] [YTS.MX]", 1.6, btih="y")
        self.assertEqual(E.pick_public_first([pub_hd, priv_4k]), pub_hd)    # a good public 1080p wins
        self.assertEqual(E.pick_public_first([yts_hd, priv_4k]), priv_4k)   # ...a tiny one doesn't
        self.assertEqual(E.pick_public_first([pub_hd, priv_4k, pub_4k]), pub_4k)
        with mock.patch.object(fresh_en, "PRIVATE_PENALTY", 0):
            self.assertEqual(E.pick_public_first([pub_hd, priv_4k]), priv_4k)

    def test_new_means_out_on_digital_within_the_window(self):
        now = time.mktime((2026, 11, 16, 12, 0, 0, 0, 0, -1))
        fresh_rel = [dict(rel("X.2026.2160p.WEB-DL-FLUX", 15), added=now - 86400)]
        self.assertIn("not new", fresh_en.stale_reason_en({"digital": "2025-11-06"}, fresh_rel, now))
        self.assertIsNone(fresh_en.stale_reason_en({"digital": "2026-11-10"}, fresh_rel, now))
        self.assertIsNone(fresh_en.stale_reason_en({"digital": "2026-11-17"}, fresh_rel, now))  # disc broke street date
        old_rel = [dict(rel("X.2026.1080p.WEB-DL-FLUX", 6), added=now - 60 * 86400)]
        self.assertIn("first rip", fresh_en.stale_reason_en({}, old_rel, now))


class Plan(unittest.TestCase):
    m = {"orig": "eng", "big": 0}

    def test_english_main_and_telugu_second(self):
        eng = rel("Spider-Man.Brand.New.Day.2026.2160p.WEB-DL.DDP5.1.HDR.H.265-FLUX", 16)
        tel = rel("Spider-Man: Brand New Day (2026) TRUE WEB-DL - 1080p - [Tel + Tam + Hin + Eng] - 5GB", 5)
        plan = fresh_en.plan_for_en([eng, tel], self.m)
        self.assertEqual(plan["status"], "ok")
        self.assertEqual([(v["role"], v["lang"], v["rel"]) for v in plan["versions"]],
                         [("primary", "eng", eng), ("secondary", "tel", tel)])
        self.assertEqual(E.file_name("Spider-Man (2026)", "secondary", "tel", ".mkv"), "Spider-Man (2026) - Telugu.mkv")

    def test_english_only_and_no_waiting(self):
        eng = rel("X.2026.1080p.WEB-DL.H.264-FLUX", 6)
        plan = fresh_en.plan_for_en([eng], self.m)
        self.assertEqual([v["lang"] for v in plan["versions"]], ["eng"])
        self.assertIn("4K later", plan["why"])
        tel_only = rel("X (2026) WEB-DL - 1080p - [Tel + Hin] - 3GB", 3)
        self.assertEqual(fresh_en.plan_for_en([tel_only], self.m)["status"], "no_release")

    def test_foreign_rule(self):
        self.assertIsNone(fresh_en.foreign_reason_en({}, "eng", []))
        self.assertIn("not an English", fresh_en.foreign_reason_en({}, "korean", []))
        self.assertIn("not an English", fresh_en.foreign_reason_en({}, "tel", []))


class Headlines(unittest.TestCase):
    def test_title_and_date(self):
        h = fresh_en.headline_title
        self.assertEqual(h("James McAvoy’s CALIFORNIA SCHEMIN’ Sets Digital Streaming Release Date"), "California Schemin’")
        self.assertEqual(h("Horror Thriller BREEDER Sets Digital Streaming Release Date"), "Breeder")
        self.assertEqual(h("YOUR MOTHER YOUR MOTHER YOUR MOTHER Gets MGM+ Streaming Premiere Date"),
                         "Your Mother Your Mother Your Mother")
        now = time.mktime((2026, 11, 16, 12, 0, 0, 0, 0, -1))
        self.assertEqual(fresh_en.headline_date("arrives on Digital on November 24th.", now), "2026-11-24")
        self.assertEqual(fresh_en.headline_date("streaming January 6", now), "2027-01-06")


class Flow(unittest.TestCase):
    def setUp(self):
        E._conn = None
        fresh_en._tables_ready = False
        for f in pathlib.Path(TMP).glob("fresh_en.db*"):
            f.unlink()
        E._RELS.clear()
        E.BLACKHOLE = pathlib.Path(TMP) / "data" / "blackhole" / "fresh-en"
        E.DELIVERED = pathlib.Path(TMP) / "data" / "downloads" / "fresh-en"

    def feed(self, title, size_gb, private=False, seeders=80, published=None):
        fresh_en._tables()
        E.x("INSERT INTO feed(guid, title, size, seeders, indexer, private, magnet, url, infohash, published, first_seen, via)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (title.lower(), title, int(size_gb * GB), seeders, "Knaben", 1 if private else 0,
             None if private else f"magnet:?xt=urn:btih:{abs(hash(title))}", "http://prowlarr/dl/1", f"h{abs(hash(title))}",
             published or time.time(), time.time(), "rss"))

    def test_build_decide_start_private_as_torrent(self):
        self.feed("Heart of the Beast (2026) [1080p] [WEBRip] [5.1] [YTS.MX]", 1.6)
        self.feed("Heart.of.the.Beast.2026.2160p.AMZN.WEB-DL.DDP5.1.DV.HDR.H.265-FLUX", 17, private=True)
        self.feed("Leviticus.2026.1080p.WEBRip.x265-NeoNoir", 2.5, published=time.time() - 120 * 86400)  # old rip
        match = {"tmdb": 1263337, "orig": "eng", "has_file": False, "radarr_id": None, "monitored": None,
                 "title": "Heart of the Beast", "year": 2026}
        with mock.patch.object(E, "lookup", return_value=(match, [])), \
             mock.patch.object(fresh_en, "_scraper_releases", return_value={}):
            E.build_candidates()
        ms = E.q("SELECT * FROM movies")
        self.assertEqual([m["title"] for m in ms], ["Heart of the Beast"])  # Leviticus is too old
        E.apply_decisions([{"id": ms[0]["id"], "verdict": "download", "score": 8, "tmdb_id": 1263337, "orig_lang": "en"}])
        self.assertEqual(E.q("SELECT status FROM movies")[0]["status"], "approved")  # auto mode
        got = mock.Mock(is_redirect=False, content=b"d8:announce...e", headers={})
        got.raise_for_status = lambda: None
        with mock.patch.object(E.requests, "get", return_value=got) as g:
            E.start_approved(now=time.mktime((2026, 11, 16, 12, 0, 0, 0, 0, -1)))
        g.assert_called_once()  # the private 4K (beats a tiny public YTS) was fetched as a .torrent
        drops = sorted(p.name for p in E.BLACKHOLE.iterdir())
        self.assertEqual(drops, ["Heart.of.the.Beast.2026.2160p.AMZN.WEB-DL.DDP5.1.DV.HDR.H.265-FLUX.torrent"])

    def test_upgrade_swaps_the_main_file(self):
        self.feed("Musk.2026.1080p.WEB-DL.HEVC.x265.5.1-BONE", 3)
        match = {"tmdb": 1096228, "orig": "eng", "has_file": False, "radarr_id": None, "monitored": None}
        with mock.patch.object(E, "lookup", return_value=(match, [])), \
             mock.patch.object(fresh_en, "_scraper_releases", return_value={}):
            E.build_candidates()
        mid = E.q("SELECT id FROM movies")[0]["id"]
        E.apply_decisions([{"id": mid, "verdict": "download", "score": 7, "tmdb_id": 1096228, "orig_lang": "en"}])
        E.start_approved(now=time.mktime((2026, 11, 16, 12, 0, 0, 0, 0, -1)))
        folder = pathlib.Path(TMP) / "movies" / "Musk (2026)"
        stem = "Musk.2026.1080p.WEB-DL.HEVC.x265.5.1-BONE"
        (E.DELIVERED / stem).mkdir(parents=True)
        (E.DELIVERED / stem / "musk.mkv").write_bytes(b"1080")
        with mock.patch.object(E, "ensure_radarr_movie", return_value=(5, str(folder))), \
             mock.patch.object(E, "radarr"), mock.patch.object(E, "jellyfin"):
            E.check_versions()
        self.assertEqual((folder / "Musk (2026).mkv").read_bytes(), b"1080")
        self.assertEqual(E.q("SELECT status FROM movies")[0]["status"], "done")
        # the 4K shows up -> queued as an upgrade, placed under the same name, old version retired
        self.feed("Musk.2026.2160p.WEB-DL.DV.HDR.H.265-FLUX", 15)
        with mock.patch.object(E, "lookup", return_value=(match, [])), \
             mock.patch.object(fresh_en, "_scraper_releases", return_value={}):
            E.build_candidates()
        E.add_missing_versions()
        up = E.q("SELECT * FROM versions WHERE role='primary_upgrade'")
        self.assertEqual(len(up), 1)
        (E.DELIVERED / up[0]["stem"]).mkdir(parents=True)
        (E.DELIVERED / up[0]["stem"] / "musk.4k.mkv").write_bytes(b"2160")
        with mock.patch.object(E, "ensure_radarr_movie", return_value=(5, str(folder))), \
             mock.patch.object(E, "radarr"), mock.patch.object(E, "jellyfin"):
            E.check_versions()
        self.assertEqual((folder / "Musk (2026).mkv").read_bytes(), b"2160")
        states = sorted((v["role"], v["state"]) for v in E.q("SELECT role, state FROM versions"))
        self.assertEqual(states, [("primary", "placed"), ("primary", "replaced")])
        self.assertEqual(E.q("SELECT status FROM movies")[0]["status"], "done")
        self.assertEqual(list(folder.iterdir()), [folder / "Musk (2026).mkv"])


class IndianFallbackFixes(unittest.TestCase):
    def test_privacy_comes_from_the_indexer_list(self):
        with mock.patch.object(fresh, "indexer_privacy", return_value={36: True, 2: False}):
            self.assertTrue(fresh.is_private_hit({"indexerId": 36}))
            self.assertFalse(fresh.is_private_hit({"indexerId": 2}))
            self.assertTrue(fresh.is_private_hit({"indexerId": 99}))  # unknown -> treated as private
        with mock.patch.object(fresh, "indexer_privacy", side_effect=RuntimeError("down")):
            self.assertTrue(fresh.is_private_hit({"indexerId": 2}))

    def test_private_prowlarr_hit_is_fetched_as_torrent_and_untagged_counts_as_original(self):
        rows = [{"title": "Kara.2026.1080p.WEB-DL.DDP5.1.H.264-TEL", "size": 3 * GB, "seeders": 9, "infoHash": "aa",
                 "magnetUrl": "magnet:?xt=urn:btih:aa", "downloadUrl": "http://p/1", "indexerId": 36,
                 "indexer": "IPTorrents"}]
        sr = mock.Mock(json=lambda: rows)
        sr.raise_for_status = lambda: None
        tor = mock.Mock(is_redirect=False, content=b"d4:infod7:privatei1eee", headers={})
        tor.raise_for_status = lambda: None
        with mock.patch.object(fresh, "PROWLARR_URL", "http://p"), mock.patch.object(fresh, "PROWLARR_API_KEY", "k"), \
             mock.patch.object(fresh, "indexer_privacy", return_value={36: True}), \
             mock.patch.object(fresh.requests, "get", side_effect=[sr, tor]):
            v = fresh.prowlarr_version({"title": "Kara", "year": 2026, "orig": "tel"}, "tel", set())
        self.assertIsNotNone(v)
        self.assertEqual(v["rel"]["torrent"], b"d4:infod7:privatei1eee")


if __name__ == "__main__":
    unittest.main()
