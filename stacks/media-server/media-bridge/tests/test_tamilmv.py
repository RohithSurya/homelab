"""Offline tests for the tamilmv crawler: synthetic IPS markup (the shapes measured on 1tamilmv
2026-09-30) and fake fetchers keyed by URL -- no network, no FlareSolverr."""

import contextlib
import hashlib
import http.client
import io
import json
import os
import threading
import pathlib
import sys
import tempfile
import time
import unittest
import urllib.parse

_TMP = tempfile.mkdtemp()
os.environ["TAMILMV_DB"] = os.path.join(_TMP, "tamilmv.db")
os.environ["TAMILMV_ARCHIVE_DB"] = os.path.join(_TMP, "tamilmv_archive.db")
os.environ["TAMILMV_PUBLIC_URL"] = "http://100.64.0.9:8005"
os.environ["TAMILMV_ERRORS_LOG"] = os.path.join(_TMP, "errors.jsonl")
os.environ["TAMILMV_HTTP_PROXY"] = "http://proxy.invalid:8888"
os.environ["TAMILMV_PLAIN_DELAY"] = "0"
os.environ["REQUEST_DELAY"] = "0"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import tamilmv as t  # noqa: E402

BASE = "https://www.1tamilmv.test"


def benc(x):
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, str):
        x = x.encode()
    if isinstance(x, bytes):
        return b"%d:%s" % (len(x), x)
    if isinstance(x, list):
        return b"l" + b"".join(benc(v) for v in x) + b"e"
    return b"d" + b"".join(benc(k) + benc(x[k]) for k in sorted(x)) + b"e"


def torrent(name, size, private=False):
    info = {"name": name, "length": size, "piece length": 16384, "pieces": b"\0" * 20}
    if private:
        info["private"] = 1
    data = benc({"announce": "udp://tracker.example:80/announce", "info": info})
    return data, hashlib.sha1(benc(info)).hexdigest()


def topic_url(pid, slug="x"):
    return f"{BASE}/index.php?/forums/topic/{pid}-{slug}/"


def topic_page(pid, slug, forum="25-hd-rips-dvd-rips-br-rips", title="T", body="", replies=(),
               posted="2023-08-09T03:30:04Z"):
    arts = [f"<article id='elComment_{pid}1' class='cPost'><time datetime='{posted}'></time>"
            f"<div data-role='commentContent'>{body}</div></article>"]
    arts += [f"<article id='elComment_{pid}{i + 2}'><time datetime='2024-01-01T00:00:00Z'></time>"
             f"<div data-role='commentContent'>{r}</div></article>" for i, r in enumerate(replies)]
    return (f'<html><head><meta property="og:title" content="{title}">'
            f'<link rel="canonical" href="{topic_url(pid, slug)}"></head><body>'
            f"<nav class='ipsBreadcrumb'><ul data-role=\"breadcrumbList\">"
            f"<li><a href='{BASE}/index.php?/forums/'>Forums</a></li>"
            f"<li><a href='{BASE}/index.php?/forums/forum/22-telugu-language/'>Telugu</a></li>"
            f"<li><a href='{BASE}/index.php?/forums/forum/{forum}/'>Rips</a></li>"
            f"<li>{title}</li></ul></nav>" + "".join(arts) +
            "<ul class='ipsList_inline'><li><a class='cShareLink' href='https://www.reddit.com/submit?url=x'"
            " title='Share on X'>r</a></li><li><a href='https://pinterest.com/pin/create?d=6.7GB'>p</a>"
            "</li></ul></body></html>")


def listing_page(pids, pages):
    rows = "".join(f"<a href='{topic_url(p, 'movie-' + str(p))}'>t</a>" for p in pids)
    return f"<div data-pages='{pages}'></div>{rows}"


class Fake:
    """URL -> (status, body) for fetch_page and fetch_plain; records what was fetched."""

    def __init__(self):
        self.pages, self.files, self.calls = {}, {}, []

    def page(self, url):
        self.calls.append(url)
        return self.pages.get(url, (404, ""))

    def plain(self, url, binary=False):
        self.calls.append(url)
        if binary:
            return self.files.get(url, (404, b""))
        return self.pages.get(url, (404, ""))


class Base(unittest.TestCase):
    def setUp(self):
        with t.DB_LOCK:
            c = t.db()
            for tbl in ("posts", "releases", "meta", "attachments", "weblinks", "forums",
                        "release_topics", "logged"):
                c.execute(f"DELETE FROM {tbl}")
            c.commit()
        with t.ARCH_LOCK:
            t.adb().execute("DELETE FROM pages")
            t.adb().commit()
        for f in (t.ERRORS_PATH, t.ERRORS_PATH + ".1"):
            if os.path.exists(f):
                os.remove(f)
        t.CONFIG = {"tamilmv": {"homepage": BASE, "forums": [
            ("25-hd-rips-dvd-rips-br-rips", "telugu"), ("24-web-hd-itunes-hd-bluray", "telugu")]}}
        self.fake = Fake()
        self._orig = (t.fetch_page, t.fetch_plain)
        t.fetch_page, t.fetch_plain = self.fake.page, self.fake.plain

    def tearDown(self):
        t.fetch_page, t.fetch_plain = self._orig

    def releases(self):
        with t.DB_LOCK:
            return [dict(r) for r in t.db().execute("SELECT * FROM releases ORDER BY title")]

    def errors(self):
        try:
            with open(t.ERRORS_PATH) as fh:
                return [json.loads(line) for line in fh]
        except FileNotFoundError:
            return []

    def post(self, pid):
        with t.DB_LOCK:
            r = t.db().execute("SELECT * FROM posts WHERE post_id=?", (pid,)).fetchone()
        return dict(r) if r else None


SEETHAMMA_DN = ("www.1TamilMV.mov%20-%20Seethamma%20Vakitlo%20Sirimalle%20Chettu%20(2013)%20Telugu"
                "%20-%20DVD9%20-%20UNTOUCHED%20-%20480p%20-%20(DTS%20-%20754Kbps%20%26%20DD5.1%20-"
                "%20448Kbps)%20-%206.7GB%20-%20ESub.mkv")


class Parsing(Base):
    def test_mangled_magnet_is_rewritten(self):
        page = (f'<a href="http://magnet?xt=urn:btih:BFE99945A30A63045A6063789EDC5248B27BB19E'
                f'&amp;dn={SEETHAMMA_DN}">x</a>')
        mags = t.find_magnets(page)
        self.assertEqual(len(mags), 1)
        self.assertTrue(mags[0].startswith("magnet:?xt=urn:btih:BFE99945"))
        self.assertIn("&dn=www.1TamilMV.mov", mags[0])

    def test_magnet_forms_dedupe_by_infohash(self):
        h = "a" * 40
        page = (f'<a href="magnet:?xt=urn:btih:{h}&amp;dn=A">1</a>'
                f'<a href="http://magnet?xt=urn:btih:{h.upper()}&dn=A">2</a>')
        self.assertEqual(len(t.find_magnets(page)), 1)

    def test_breadcrumb_forum_date_title(self):
        page = topic_page(1, "s", forum="13-dvd9-dvd5", title="Mann &amp; Vasanai")
        self.assertEqual(t.topic_forum(page), "13")
        self.assertEqual(t.topic_title(page), "Mann & Vasanai")
        self.assertEqual(int(t.topic_posted_at(page)), 1691551804)

    def test_weblinks_opening_post_only_and_no_images_or_socials(self):
        body = ("Khaleja (2010) 1080p - 6GB <a href='https://tnlink.in/hR3fQed'>https://tnlink.in/hR3fQed</a>"
                "<a href='https://www.imageride.net/image/A1'><img src='x.png'></a>"
                "<a href='https://t.me/tmvog'>TELEGRAM</a>"
                f"<a href='{BASE}/index.php?/forums/topic/5-x/'>internal</a>"
                "<a href='https://gdlink.dev/file/abc'>Khaleja 2160p 23.4GB</a>")
        page = topic_page(169271, "khaleja", body=body,
                          replies=["<a href='https://shrinkme.us/Other'>other movie 8GB</a>"])
        links = t.find_weblinks(page, "www.1tamilmv.test")
        self.assertEqual([l["host"] for l in links], ["tnlink.in", "gdlink.dev"])
        self.assertIn("Khaleja (2010) 1080p - 6GB", links[0]["label"])   # context before a bare URL
        self.assertEqual(links[0]["size"], 6_000_000_000)
        self.assertEqual(links[1]["size"], 23_400_000_000)

    def test_single_post_page_share_buttons_are_not_weblinks(self):
        page = topic_page(1, "s", body="nothing to download here")
        self.assertEqual(t.find_weblinks(page, "www.1tamilmv.test"), [])

    def test_label_context_starts_after_previous_link_and_skips_partial_tags(self):
        body = ("<p style='text-align:center;'>1080p [1.96 GB] <a href='https://gplinks.co/a'>https://gplinks.co/a</a>"
                " 720p [906.99 MB] <a href='https://gplinks.co/b'>https://gplinks.co/b</a></p>")
        page = topic_page(2, "s", body=body)
        a, b = t.find_weblinks(page, "www.1tamilmv.test")
        self.assertEqual(b["label"], "720p [906.99 MB]")
        self.assertEqual((a["size"], b["size"]), (1_960_000_000, 906_990_000))
        self.assertNotIn("style", a["label"])

    def test_private_torrent_never_becomes_a_magnet(self):
        data, _ = torrent("x.mkv", 10, private=True)
        self.assertIsNone(t.magnet_from_torrent_bytes(data))
        self.assertIsNone(t.magnet_from_torrent_bytes(b"<html>login</html>"))

    def test_torrent_bytes_to_magnet(self):
        data, btih = torrent("Loukyam (2014) Telugu - DVD9 - 6.5GB - ESub.mkv", 6_500_000_000)
        got_btih, magnet = t.magnet_from_torrent_bytes(data)
        self.assertEqual(got_btih, btih)
        rec = t.parse_magnet(magnet, 180394, "telugu")
        self.assertEqual(rec["btih"], btih)
        self.assertEqual(rec["size"], 6_500_000_000)
        self.assertEqual(rec["title"], "Loukyam (2014) Telugu - DVD9 - 6.5GB - ESub")
        self.assertIn("tr=udp%3A%2F%2Ftracker.example", magnet)


class ProcessTopic(Base):
    def test_seethamma_mangled_magnet_indexed_and_twin_attachment_not_downloaded(self):
        att = f"{BASE}/applications/core/interface/file/attachment.php?id=106550&amp;key=k"
        name = urllib.parse.unquote(SEETHAMMA_DN)
        body = (f"<a href='{att}' data-fileext='torrent'>{name.replace('&', '&amp;')}.torrent</a>"
                f"<a href='http://magnet?xt=urn:btih:bfe99945a30a63045a6063789edc5248b27bb19e&amp;dn={SEETHAMMA_DN}'>m</a>")
        url = topic_url(174730, "seethamma")
        self.fake.pages[url] = (200, topic_page(174730, "seethamma", body=body))
        self.assertEqual(t.process_topic("tamilmv", 174730, url, "telugu"), 1)
        rel = self.releases()
        self.assertEqual(rel[0]["btih"], "bfe99945a30a63045a6063789edc5248b27bb19e")
        self.assertTrue(rel[0]["title"].startswith("Seethamma Vakitlo Sirimalle Chettu (2013)"))
        self.assertFalse(any("attachment.php" in c for c in self.fake.calls))
        p = self.post(174730)
        self.assertEqual((p["magnets"], p["forum"], p["parsed_v"]), (2, "25", t.PARSER_V))

    def test_attachment_only_topic_is_downloaded_once(self):
        data, btih = torrent("www.1TamilMV.eu - Loukyam (2014) Telugu - DVD9 - 6.5GB - ESub.mkv", 6_500_000_000)
        att = f"{BASE}/applications/core/interface/file/attachment.php?id=119590&key=k"
        body = f"<a href='{att.replace('&', '&amp;')}'>www.1TamilMV.eu - Loukyam (2014).mkv.torrent</a>"
        url = topic_url(180394, "loukyam")
        self.fake.pages[url] = (200, topic_page(180394, "loukyam", body=body))
        self.fake.files[att] = (200, data)
        self.assertEqual(t.process_topic("tamilmv", 180394, url, "telugu"), 1)
        self.assertEqual(self.releases()[0]["btih"], btih)
        self.assertEqual(self.releases()[0]["title"], "Loukyam (2014) Telugu - DVD9 - 6.5GB - ESub")
        t.process_topic("tamilmv", 180394, url, "telugu")       # re-read: cached, no 2nd download
        self.assertEqual(sum(1 for c in self.fake.calls if c == att), 1)

    def test_weblinks_served_only_as_ungrabbable_dl_link_items(self):
        url = topic_url(190371, "raja-rani")
        body = ("Raja Rani (2014) 1080p - Raja.Rani.2014.1080p.JHS.WEB-DL.AAC2.0.H264-Musugu.mkv [1.96 GB] "
                "<a href='https://gplinks.co/pDoBDC'>https://gplinks.co/pDoBDC</a>")
        self.fake.pages[url] = (200, topic_page(190371, "raja-rani", title="Raja Rani (2014)", body=body))
        self.assertEqual(t.process_topic("tamilmv", 190371, url, "telugu"), 0)
        self.assertEqual(t.query_releases("search", "Raja Rani", "", []), [])
        links = t.query_links("search", "Raja Rani", "", [])
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["title"], "Raja.Rani.2014.1080p.JHS.WEB-DL.AAC2.0.H264-Musugu")
        xml = t.results_xml([], links, "http://10.0.0.179:8005")
        self.assertIn("<title>[DL-LINK] Raja.Rani.2014.1080p.JHS.WEB-DL.AAC2.0.H264-Musugu</title>", xml)
        self.assertIn('name="seeders" value="0"', xml)
        self.assertNotIn("magneturl", xml)
        self.assertNotIn("gplinks", xml)                          # the link itself is only on the page
        self.assertIn(f"http://10.0.0.179:8005/nograb/{links[0]['lid']}.torrent", xml)
        self.assertIn(f"<comments>http://100.64.0.9:8005/t/tamilmv/190371#l{links[0]['lid']}</comments>", xml)
        self.assertEqual(t.query_links("search", "", "", []), [])  # never in the RSS (empty query)
        self.assertEqual(t.query_links("movie", "Unrelated Title", "", []), [])

    def test_direct_link_button_named_after_its_torrent_and_tied_to_it(self):
        h = "ab" * 20
        dn = "www.1TamilMV.gripe%20-%20Thaai%20Kizhavi%20(2026)%20TRUE%20WEB-DL%20-%204K%20-%2013GB%20-%20ESub.mkv"
        body = (f"<a data-fileext='torrent' href='{BASE}/applications/core/interface/file/attachment.php?id=1&amp;key=k'>"
                "www.1TamilMV.gripe - Thaai Kizhavi (2026) TRUE WEB-DL - 4K - 13GB - ESub.mkv.torrent</a><br>"
                f"<a class='skyblue-button' href='magnet:?xt=urn:btih:{h}&amp;dn={dn}&amp;xl=14207009369'>MAGNET</a><br>"
                "<a class='download-button' href='https://www.cyberloom.best/l/PFVJ3cGf'><svg></svg> DIRECT LINK </a>")
        url = topic_url(197578, "thaai")
        self.fake.pages[url] = (200, topic_page(197578, "thaai", body=body))
        t.process_topic("tamilmv", 197578, url, "tamil")
        with t.DB_LOCK:
            w = t.db().execute("SELECT * FROM weblinks").fetchone()
        self.assertEqual(w["label"], "www.1TamilMV.gripe - Thaai Kizhavi (2026) TRUE WEB-DL - 4K - 13GB - ESub.mkv")
        self.assertEqual((w["btih"], w["size"]), (h, 14207009369))
        self.assertEqual(w["title"], "Thaai Kizhavi (2026) TRUE WEB-DL - 4K - 13GB - ESub")

    def test_non_release_forum_is_archived_and_extracted_but_not_served(self):
        url = topic_url(3517, "news")
        mag = "<a href='magnet:?xt=urn:btih:" + "c" * 40 + "&amp;dn=News.Movie.2008.mkv'>m</a>"
        self.fake.pages[url] = (200, topic_page(3517, "news", forum="18-cinema-news", body=mag))
        self.assertEqual(t.process_topic("tamilmv", 3517, url, None), 1)
        self.assertEqual(len(self.releases()), 1)                       # extracted ...
        self.assertTrue(t.archived("tamilmv", 3517))                    # ... and archived ...
        self.assertEqual(t.query_releases("search", "News Movie", "", []), [])   # ... not served
        self.assertEqual(self.post(3517)["forum"], "18")
        self.assertIsNone(t.topic_action("tamilmv", 3517, url))
        # flags: a forum override serves it; a topic override beats the forum
        t.upsert_forums("tamilmv", [("18", "18-cinema-news")])
        with t.DB_LOCK:
            t.db().execute("UPDATE forums SET serve=1 WHERE forum_id='18'")
            t.db().commit()
        self.assertEqual(len(t.query_releases("search", "News Movie", "", [])), 1)
        with t.DB_LOCK:
            t.db().execute("UPDATE posts SET serve=0 WHERE post_id=3517")
            t.db().commit()
        self.assertEqual(t.query_releases("search", "News Movie", "", []), [])

    def test_share_forum_language_comes_from_the_title(self):
        url = topic_url(169271, "khaleja")
        body = ("<a href='magnet:?xt=urn:btih:" + "f" * 40 + "&amp;dn=Khaleja.2010.mkv'>m</a>"
                "6GB <a href='https://tnlink.in/hR3fQed'>https://tnlink.in/hR3fQed</a>")
        self.fake.pages[url] = (200, topic_page(169271, "khaleja", forum="92-post-share-here",
                                                title="KHALEJA (2010) Telugu SunNxt TRUE WEB-DL", body=body))
        self.assertEqual(t.process_topic("tamilmv", 169271, url, None), 1)
        self.assertEqual(self.releases()[0]["language"], "telugu")
        with t.DB_LOCK:
            wl = t.db().execute("SELECT host, language FROM weblinks").fetchall()
        self.assertEqual([tuple(r) for r in wl], [("tnlink.in", "telugu")])

    def test_sitemap_topic_in_extra_dvd9_forum_gets_its_language(self):
        url = topic_url(200026, "mann")
        mag = "<a href='magnet:?xt=urn:btih:" + "d" * 40 + "&amp;dn=Mann.Vasanai.1983.DVD9.mkv'>m</a>"
        self.fake.pages[url] = (200, topic_page(200026, "mann", forum="13-dvd9-dvd5", body=mag))
        self.assertEqual(t.process_topic("tamilmv", 200026, url, None), 1)
        self.assertEqual(self.releases()[0]["language"], "tamil")


class NeedsFetch(Base):
    def _mark(self, pid, magnets, posted_at, parsed_v, forum="25", archive=True):
        with t.DB_LOCK:
            t.db().execute("INSERT INTO posts(site, post_id, url, scraped_at, magnets, forum, posted_at, parsed_v)"
                           " VALUES ('tamilmv',?,?,?,?,?,?,?)",
                           (pid, topic_url(pid, "a"), time.time(), magnets, forum, posted_at, parsed_v))
            t.db().commit()
        if archive:
            t.archive_put("tamilmv", pid, topic_url(pid, "a"), t.archive_view(topic_page(pid, "a")))

    def test_never_archived_rows_are_fetched_older_parser_rows_reparsed_locally(self):
        self._mark(1, 3, None, t.PARSER_V, archive=False)             # read before the archive
        self.assertEqual(t.topic_action("tamilmv", 1, topic_url(1, "a")), "fetch")
        self._mark(2, 3, None, 0)                                      # archived, old parser
        self.assertEqual(t.topic_action("tamilmv", 2, topic_url(2, "a")), "reparse")
        self._mark(6, 3, None, t.PARSER_V)
        self.assertIsNone(t.topic_action("tamilmv", 6, topic_url(6, "a")))

    def test_empty_retry_anchored_on_post_date(self):
        self._mark(3, 0, time.time() - 400 * 86400, t.PARSER_V)     # backfilled 2025 topic: done
        self.assertFalse(t.post_needs_fetch("tamilmv", 3, topic_url(3, "a")))
        self._mark(4, 0, time.time() - 3600, t.PARSER_V)            # posted an hour ago: retry
        self.assertTrue(t.post_needs_fetch("tamilmv", 4, topic_url(4, "a")))

    def test_slug_change_still_rereads(self):
        self._mark(5, 3, None, t.PARSER_V)
        self.assertTrue(t.post_needs_fetch("tamilmv", 5, topic_url(5, "b")))


class Walk(Base):
    FORUM = "25-hd-rips-dvd-rips-br-rips"

    def _forum(self, pages):
        """pages: list of topic-id lists; each topic page carries one magnet."""
        for i, pids in enumerate(pages, 1):
            self.fake.pages[t.listing_url(BASE, self.FORUM, i)] = (200, listing_page(pids, len(pages)))
            for p in pids:
                mag = f"<a href='magnet:?xt=urn:btih:{p:040x}&amp;dn=Movie.{p}.2020.mkv'>m</a>"
                self.fake.pages[topic_url(p, f"movie-{p}")] = (200, topic_page(p, f"movie-{p}", body=mag))

    def test_listing_is_start_date_sorted(self):
        self.assertTrue(t.listing_url(BASE, "25-x", 3).endswith(
            "/forums/forum/25-x/page/3/&sortby=start_date&sortdirection=desc"))
        self.assertNotIn("/page/", t.listing_url(BASE, "25-x", 1))

    def test_backfill_resumes_across_passes_and_finishes(self):
        self._forum([[10, 9], [8, 7], [6, 5], [4, 3]])
        forums = [(self.FORUM, "telugu")]
        p1 = t.backfill("tamilmv", BASE, forums, 2)
        self.assertEqual(p1[2], 2)
        self.assertEqual(t.get_meta("walk:tamilmv:25"), "3")
        self.assertEqual(t.backfill_remaining("tamilmv", forums), 1)
        t.backfill("tamilmv", BASE, forums, 10)            # resumes at page 2 (one page overlap)
        self.assertEqual(t.get_meta("walkdone:tamilmv:25"), "1")
        self.assertEqual(t.backfill_remaining("tamilmv", forums), 0)
        self.assertEqual(len(self.releases()), 8)
        self.assertEqual(t.backfill("tamilmv", BASE, forums, 10)[2], 0)   # done: no more pages

    def test_backfill_stops_at_page_count_not_empty_page(self):
        # IPS answers page N+1 with page 1; the walk must never ask for it.
        self._forum([[2], [1]])
        t.backfill("tamilmv", BASE, [(self.FORUM, "telugu")], 10)
        self.assertNotIn(t.listing_url(BASE, self.FORUM, 3), self.fake.calls)

    def test_head_walk_stops_at_known_page(self):
        self._forum([[6, 5], [4, 3], [2, 1]])
        known = {6, 5, 4, 3}
        for p in known:
            t.mark_post("tamilmv", p, topic_url(p, f"movie-{p}"), 1, "25", None)
        t.crawl_forum("tamilmv", BASE, self.FORUM, "telugu", known)
        self.assertNotIn(t.listing_url(BASE, self.FORUM, 2), self.fake.calls)

    def test_sitemap_reads_only_unseen_recent_topics(self):
        now = time.time()
        recent = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 86400))
        old = "2019-01-01T00:00:00+00:00"
        t.mark_post("tamilmv", 30, topic_url(30, "known"), 1, "25", None)
        self.fake.pages[f"{BASE}/sitemap.php"] = (
            200, f"<sitemapindex><sitemap><loc>{BASE}/sitemap.php?file=sitemap_content_forums_Topic_1</loc>"
                 f"</sitemap><sitemap><loc>{BASE}/sitemap.php?file=sitemap_content_gallery_Image_1</loc>"
                 f"</sitemap></sitemapindex>")
        self.fake.pages[f"{BASE}/sitemap.php?file=sitemap_content_forums_Topic_1"] = (200, "".join(
            f"<url><loc>{topic_url(p, 'm')}</loc><lastmod>{lm}</lastmod></url>"
            for p, lm in ((31, recent), (30, recent), (29, old))))
        mag = "<a href='magnet:?xt=urn:btih:" + "e" * 40 + "&amp;dn=New.Movie.2026.mkv'>m</a>"
        self.fake.pages[topic_url(31, "m")] = (200, topic_page(31, "m", body=mag))
        self.assertEqual(t.sitemap_sweep("tamilmv", BASE, now), (1, 1))
        self.assertNotIn(topic_url(29, "m"), self.fake.calls)
        self.assertEqual(t.sitemap_sweep("tamilmv", BASE, now + 60), (0, 0))   # interval gate


class ArchiveAndReparse(Base):
    def test_archive_keeps_posts_drops_chrome_and_reparse_matches_a_fresh_read(self):
        body = ("<a href='magnet:?xt=urn:btih:" + "1" * 40 + "&amp;dn=Jeeva.2014.1080p.mkv&amp;xl=4380000000'>m</a>"
                "Jeeva (2014) 1080p ESub.mkv - [4.38GB] <a href='https://nowshort.com/TqRKU6j'>https://nowshort.com/TqRKU6j</a>")
        page = topic_page(200038, "jeeva", forum="92-post-share-here", title="Jeeva (2014) Tamil", body=body,
                          replies=["thanks!"]).replace("</head>", "<script>evil()</script></head>")
        url = topic_url(200038, "jeeva")
        self.fake.pages[url] = (200, page)
        t.process_topic("tamilmv", 200038, url)
        saved_url, html_, _ = t.archive_get("tamilmv", 200038)
        self.assertEqual(saved_url, url)
        self.assertEqual(html_.count("<article"), 2)                  # opening post + reply
        self.assertNotIn("evil()", html_)
        self.assertNotIn("reddit", html_)                             # share buttons are page chrome
        with t.DB_LOCK:
            before = [tuple(r) for r in t.db().execute("SELECT btih, title, size FROM releases")]
            wl_before = [tuple(r) for r in t.db().execute("SELECT lid, title, size FROM weblinks")]
            t.db().execute("DELETE FROM releases")
            t.db().execute("DELETE FROM weblinks")
            t.db().commit()
        calls = len(self.fake.calls)
        t.reparse_topic("tamilmv", 200038)
        self.assertEqual(len(self.fake.calls), calls)                 # no network
        with t.DB_LOCK:
            self.assertEqual([tuple(r) for r in t.db().execute("SELECT btih, title, size FROM releases")], before)
            self.assertEqual([tuple(r) for r in t.db().execute("SELECT lid, title, size FROM weblinks")], wl_before)


class Serving(Base):
    def _release(self, pid, dn, forum="25"):
        url = topic_url(pid, "x")
        mag = f"<a href='magnet:?xt=urn:btih:{pid:040x}&amp;dn={dn}'>m</a>"
        self.fake.pages[url] = (200, topic_page(pid, "x", forum=forum + "-f", body=mag))
        t.process_topic("tamilmv", pid, url, "telugu")

    def test_dvd9_single_file_served_as_dvd_remux_disc_image_kept(self):
        self._release(1, "Seethamma.Vakitlo.Sirimalle.Chettu.(2013).Telugu.-.DVD9.-.UNTOUCHED.-.480p.mkv")
        self._release(2, "Seethamma.Vakitlo.Sirimalle.Chettu.(2013).Telugu.DVD9.ISO")
        xml = t.results_xml(t.query_releases("movie", "Seethamma Vakitlo Sirimalle Chettu", "", []))
        self.assertIn("Telugu.-.DVD Remux.-.UNTOUCHED", xml)
        self.assertIn("Telugu.DVD9.ISO", xml)
        self.assertIn("<comments>http://100.64.0.9:8005/t/tamilmv/1</comments>", xml)

    def test_sanitize_strips_active_content_and_rewrites_old_domains(self):
        dirty = ("<p onclick='x()'>hi</p><script>a()</script><iframe src='//e'></iframe>"
                 "<a href='javascript:alert(1)'>j</a><img src='https://i/x.png' onerror='y()'>"
                 "<a href='https://www.1tamilmv.promo/index.php?/forums/topic/1-x/'>t</a><svg><script>b()</script></svg>")
        clean = t.sanitize(dirty, "tamilmv", "https://www.1tamilmv.lease")
        for bad in ("onclick", "a()", "iframe", "javascript:", "onerror", "b()", "<svg"):
            self.assertNotIn(bad, clean)
        self.assertIn("https://www.1tamilmv.lease/index.php?/forums/topic/1-x/", clean)

    def test_topic_page_escapes_scraped_text(self):
        body = "x <a href='https://gplinks.co/a'>Movie (2020) &lt;b&gt; 1GB</a>"
        url = topic_url(9, "x")
        self.fake.pages[url] = (200, topic_page(9, "x", title="A &lt;script&gt; title", body=body))
        t.process_topic("tamilmv", 9, url, "tamil")
        code, page = t.topic_page("tamilmv", 9)
        self.assertEqual(code, 200)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("gplinks.co ↗", page)
        self.assertEqual(t.topic_page("tamilmv", 12345)[0], 404)

    def test_http_nograb_is_404_topic_page_has_csp(self):
        url = topic_url(9, "x")
        self.fake.pages[url] = (200, topic_page(9, "x", title="Movie (2020)", body="y"))
        t.process_topic("tamilmv", 9, url, "tamil")
        srv = t.ThreadingHTTPServer(("127.0.0.1", 0), t.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            c.request("GET", "/nograb/abcdef.torrent")
            r = c.getresponse()
            self.assertEqual(r.status, 404)
            r.read()
            c.request("GET", "/t/tamilmv/9")
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            self.assertIn("default-src 'none'", r.getheader("Content-Security-Policy"))
            r.read()
            c.close()
        finally:
            srv.shutdown()
            srv.server_close()


class BackfillThread(Base):
    def setUp(self):
        super().setUp()
        t.fetch_page = self._orig[0]              # the real fetch_page over a fake fetch_plain
        self.flare = []
        self._orig_fetch = t.fetch
        t.fetch = lambda url: (self.flare.append(url), (200, "<html>flare</html>"))[1]

    def tearDown(self):
        t.fetch = self._orig_fetch
        t._CTX.plain_only = False
        super().tearDown()

    def test_plain_only_raises_on_challenge_but_returns_404(self):
        t._CTX.plain_only = True
        self.fake.pages["https://a/challenge"] = (403, "<title>Just a moment...</title>")
        self.fake.pages["https://a/down"] = (522, "error code: 522")
        with self.assertRaises(t.PlainUnavailable):
            t.fetch_page("https://a/challenge")
        with self.assertRaises(t.PlainUnavailable):
            t.fetch_page("https://a/down")
        self.assertEqual(t.fetch_page("https://a/deleted-topic")[0], 404)     # not a reason to pause
        self.assertEqual(self.flare, [])                                         # never FlareSolverr

    def test_head_walk_thread_still_falls_back_to_flaresolverr(self):
        self.fake.pages["https://a/challenge"] = (403, "<title>Just a moment...</title>")
        self.assertEqual(t.fetch_page("https://a/challenge")[1], "<html>flare</html>")

    def test_order_release_then_extras_then_everything_else(self):
        t.upsert_forums("tamilmv", [("2", "2-introduction"), ("92", "92-post-share-here"),
                                    ("25", "25-hd-rips-dvd-rips-br-rips"), ("15", "15-mp3-songs")])
        ids = [t.forum_id(p) for p, _ in t.backfill_order("tamilmv")]
        self.assertEqual(ids, ["25", "24", "38", "60", "13", "92", "2", "15"])

    def test_walk_epoch_resets_progress_once(self):
        t.set_meta("walk:tamilmv:11", 61)
        t.set_meta("walkdone:tamilmv:10", 1)
        t.set_meta("sitemap:tamilmv", 5)
        self.assertTrue(t.reset_walk_if_needed())
        self.assertIsNone(t.get_meta("walk:tamilmv:11"))
        self.assertIsNone(t.get_meta("walkdone:tamilmv:10"))
        self.assertEqual(t.get_meta("sitemap:tamilmv"), "5")                    # other meta kept
        t.set_meta("walk:tamilmv:11", 3)
        self.assertFalse(t.reset_walk_if_needed())
        self.assertEqual(t.get_meta("walk:tamilmv:11"), "3")

    def test_forum_discovery_from_sitemap(self):
        self.fake.pages[f"{BASE}/sitemap.php?file=sitemap_content_forums_Forum"] = (200, (
            f"<url><loc>{BASE}/index.php?/forums/forum/25-hd-rips-dvd-rips-br-rips/</loc></url>"
            f"<url><loc>{BASE}/index.php?/forums/forum/92-post-share-here/</loc></url>"))
        self.assertEqual(t.discover_forums("tamilmv", BASE), 2)
        self.assertEqual(sorted(t.known_forums("tamilmv")),
                         [("25", "25-hd-rips-dvd-rips-br-rips"), ("92", "92-post-share-here")])


def mag(pid, dn=None):
    return f"<a href='magnet:?xt=urn:btih:{pid:040x}&amp;dn={dn or f'Movie.{pid}.2020.mkv'}'>m</a>"


class RealFetch(Base):
    """The real fetch_page (plain-only, as in the backfill thread) over the fake fetch_plain."""

    def setUp(self):
        super().setUp()
        t.fetch_page = self._orig[0]
        self._orig_fetch = t.fetch
        t.fetch = lambda url: (200, "<html>flare</html>")
        t._CTX.plain_only, t._CTX.role = True, "backfill"

    def tearDown(self):
        t.fetch = self._orig_fetch
        t._CTX.plain_only, t._CTX.role = False, "head"
        super().tearDown()

    def page(self, pid, body=None, forum="25-hd-rips-dvd-rips-br-rips", status=200, **kw):
        self.fake.pages[topic_url(pid, f"movie-{pid}")] = (
            status, topic_page(pid, f"movie-{pid}", forum=forum, body=body if body is not None else mag(pid), **kw)
            if status == 200 else "")


class ErrorLog(RealFetch):
    def test_transient_fetch_raises_and_logs_retry(self):
        self.fake.pages[topic_url(1, "a")] = (522, "error code: 522")
        with self.assertRaises(t.PlainUnavailable):
            t.process_topic("tamilmv", 1, topic_url(1, "a"))
        (e,) = self.errors()
        self.assertEqual((e["type"], e["outcome"], e["status"], e["thread"], e["post_id"]),
                         ("server_error", "retry", 522, "backfill", 1))
        self.assertIsNone(self.post(1))                                   # nothing recorded: retried
        self.fake.pages[topic_url(2, "a")] = (403, "<title>Just a moment...</title>")
        with self.assertRaises(t.PlainUnavailable):
            t.process_topic("tamilmv", 2, topic_url(2, "a"))
        self.assertEqual(self.errors()[-1]["type"], "challenge")

    def test_not_found_and_no_article_mark_gone(self):
        t.process_topic("tamilmv", 3, topic_url(3, "a"))                  # fake: unknown url -> 404
        self.fake.pages[topic_url(4, "a")] = (200, "<html><head><meta property='og:title' content='Home'></head></html>")
        t.process_topic("tamilmv", 4, topic_url(4, "a"))
        self.assertEqual([(e["type"], e["outcome"]) for e in self.errors()],
                         [("not_found", "gone"), ("no_article", "gone")])
        self.assertEqual((self.post(3)["gone"], self.post(4)["gone"]), (1, 1))
        self.assertIsNone(t.topic_action("tamilmv", 4, topic_url(4, "a")))
        self.assertEqual(t.topic_action("tamilmv", 4, topic_url(4, "renamed")), "fetch")  # slug moved on

    def test_attachment_failure_pending_then_retried_from_archive(self):
        data, btih = torrent("Loukyam (2014) Telugu - DVD9 - 6.5GB - ESub.mkv", 6_500_000_000)
        att = f"{BASE}/applications/core/interface/file/attachment.php?id=77&key=k"
        self.page(5, body=f"<a href='{att.replace('&', '&amp;')}'>Loukyam (2014).mkv.torrent</a>")
        t.process_topic("tamilmv", 5, topic_url(5, "movie-5"), "telugu")   # attachment: 404
        self.assertEqual(self.post(5)["pending"], 1)
        self.assertEqual(self.errors()[-1]["type"], "attachment_failed")
        self.assertEqual(t.topic_action("tamilmv", 5, topic_url(5, "movie-5")), "reparse")
        self.fake.files[att] = (200, data)
        fetched = len(self.fake.calls)
        t._read_topics("tamilmv", [(5, topic_url(5, "movie-5"))], "telugu")
        self.assertEqual(self.fake.calls[fetched:], [att])                 # only the attachment
        self.assertEqual((self.post(5)["pending"], self.releases()[0]["btih"]), (0, btih))

    def test_one_off_findings_logged_once(self):
        priv, _ = torrent("x.mkv", 10, private=True)
        att = f"{BASE}/applications/core/interface/file/attachment.php?id=78&key=k"
        body = (f"<a href='{att.replace('&', '&amp;')}'>x.mkv.torrent</a>"
                "<a href='magnet:?xt=urn:btih:nothex&amp;dn=Broken.mkv'>bad</a>"
                + mag(6, "Show.2024.S01E09.mkv"))
        self.page(6, body=body)
        self.fake.files[att] = (200, priv)
        t.process_topic("tamilmv", 6, topic_url(6, "movie-6"))
        t.reparse_topic("tamilmv", 6)                                      # same findings again
        types = sorted(e["type"] for e in self.errors())
        self.assertEqual(types, ["magnet_skipped", "magnet_unparsed", "private_torrent"])

    def test_empty_served_topic_and_extract_error(self):
        self.page(7, body="just text")
        t.process_topic("tamilmv", 7, topic_url(7, "movie-7"))
        self.assertEqual(self.errors()[-1]["type"], "empty")
        self.page(8, body="just text", forum="18-cinema-news")            # not served: not 'empty'
        t.process_topic("tamilmv", 8, topic_url(8, "movie-8"))
        self.assertEqual(len(self.errors()), 1)
        orig = t.find_weblinks
        t.find_weblinks = lambda *a, **k: 1 / 0
        try:
            self.page(9)
            t.process_topic("tamilmv", 9, topic_url(9, "movie-9"))
        finally:
            t.find_weblinks = orig
        e = self.errors()[-1]
        self.assertEqual((e["type"], e["outcome"]), ("extract_error", "recorded"))
        self.assertIn("ZeroDivisionError", e["detail"])
        self.assertEqual((self.post(9)["parsed_v"], self.post(9)["last_error"]), (0, "extract_error"))
        self.assertTrue(t.archived("tamilmv", 9))                          # fixable by `reparse`

    def test_concurrent_writes_are_whole_lines_and_rotation(self):
        def burst(n):
            t._CTX.role = f"w{n}"
            for i in range(200):
                t.log_error("tamilmv", i, "network", "fetch", "retry", detail="x" * 50)
        threads = [threading.Thread(target=burst, args=(n,)) for n in range(3)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(len(self.errors()), 600)                         # every line parses
        old = t.ERRORS_MAX_MB
        t.ERRORS_MAX_MB = 0.0001
        try:
            t.log_error("tamilmv", 1, "network", "fetch", "retry")
        finally:
            t.ERRORS_MAX_MB = old
        self.assertTrue(os.path.exists(t.ERRORS_PATH + ".1"))
        self.assertEqual(len(self.errors()), 1)

    def test_errors_command_groups_and_knows_unresolved(self):
        t.process_topic("tamilmv", 3, topic_url(3, "a"))                  # 404 -> gone (unresolved)
        self.page(5)
        t.log_error("tamilmv", 5, "network", "fetch", "retry", topic_url(5, "movie-5"))
        t.process_topic("tamilmv", 5, topic_url(5, "movie-5"))             # later read fine: resolved
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            t.errors_cli([])
        text = out.getvalue()
        self.assertIn("not_found: 1 total, 1 in 24h, 1 unresolved", text)
        self.assertIn("network: 1 total, 1 in 24h, 0 unresolved", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            t.errors_cli(["--unresolved"])
        self.assertNotIn("network", out.getvalue())


class ParallelWalk(RealFetch):
    FORUM = "25-hd-rips-dvd-rips-br-rips"

    def listing(self, pages):
        for i, pids in enumerate(pages, 1):
            self.fake.pages[t.listing_url(BASE, self.FORUM, i)] = (200, listing_page(pids, len(pages)))

    def test_three_workers_each_topic_once_all_archived(self):
        pids = list(range(100, 125))
        self.listing([pids])
        for p in pids:
            self.page(p)
        t.backfill("tamilmv", BASE, [(self.FORUM, "telugu")], 5, workers=3)
        fetched = [c for c in self.fake.calls if "/topic/" in c]
        self.assertEqual(sorted(fetched), sorted(set(fetched)))           # none twice
        self.assertEqual(len(fetched), 25)
        self.assertTrue(all(t.archived("tamilmv", p) for p in pids))
        self.assertEqual(len(self.releases()), 25)
        self.assertEqual(t.get_meta("walkdone:tamilmv:25"), "1")

    def test_transient_failure_blocks_page_then_retry_completes_without_gaps(self):
        self.listing([[10, 9, 8, 7], [6, 5, 4, 3]])
        for p in (10, 9, 8, 7, 6, 5, 4, 3):
            self.page(p)
        self.fake.pages[topic_url(8, "movie-8")] = (522, "error code: 522")
        with self.assertRaises(t.PlainUnavailable):
            t.backfill("tamilmv", BASE, [(self.FORUM, "telugu")], 5, workers=3)
        self.assertIsNone(t.get_meta("walk:tamilmv:25"))                   # page 1 not passed
        self.page(8)                                                       # site back
        t.backfill("tamilmv", BASE, [(self.FORUM, "telugu")], 5, workers=3)
        self.assertTrue(all(t.archived("tamilmv", p) for p in (10, 9, 8, 7, 6, 5, 4, 3)))
        self.assertEqual(t.get_meta("walkdone:tamilmv:25"), "1")
        topic_fetches = [c for c in self.fake.calls if "/topic/" in c]
        self.assertEqual(topic_fetches.count(topic_url(10, "movie-10")), 1)   # done ones not re-fetched

    def test_gone_topic_does_not_block_the_page(self):
        self.listing([[12, 11], [2, 1]])
        for p in (12, 2, 1):
            self.page(p)                                                   # 11 -> 404
        t.backfill("tamilmv", BASE, [(self.FORUM, "telugu")], 5, workers=3)
        self.assertEqual(t.get_meta("walkdone:tamilmv:25"), "1")
        self.assertEqual(self.post(11)["gone"], 1)

    def test_audit_retries_unarchived_and_gone_logs_what_stays_missing(self):
        for p in (21, 22, 24):
            self.page(p)
        t.process_topic("tamilmv", 21, topic_url(21, "movie-21"))           # archived already
        t.process_topic("tamilmv", 23, topic_url(23, "movie-23"))           # 404 -> gone
        self.fake.pages[f"{BASE}/sitemap.php"] = (
            200, f"<loc>{BASE}/sitemap.php?file=sitemap_content_forums_Topic_1</loc>")
        self.fake.pages[f"{BASE}/sitemap.php?file=sitemap_content_forums_Topic_1"] = (200, "".join(
            f"<url><loc>{topic_url(p, f'movie-{p}')}</loc><lastmod>2019-01-01T00:00:00+00:00</lastmod></url>"
            for p in (21, 22, 23, 24)))
        t.set_meta("walk_epoch", 2)
        self.assertEqual(t.audit("tamilmv", BASE, workers=3), (4, 1, 3, 1))
        self.assertTrue(t.archived("tamilmv", 22) and t.archived("tamilmv", 24))
        missing = [e for e in self.errors() if e["type"] == "audit_missing"]
        self.assertEqual([(e["post_id"], e["detail"], e["thread"]) for e in missing], [(23, "not_found", "audit")])


class Commands(Base):
    def test_refetch_by_id_or_url_clears_gone(self):
        t.mark_gone("tamilmv", 31, topic_url(31, "old"), "no_article")
        self.fake.pages[f"{BASE}/index.php?/forums/topic/31-old/"] = (200, topic_page(31, "old", body=mag(31)))
        t.set_meta("base:tamilmv", BASE)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            t.refetch_cli(["31"])
        self.assertIn("[tamilmv 31] ok", out.getvalue())
        self.assertEqual(self.post(31)["gone"], 0)
        self.assertEqual(len(self.releases()), 1)
        self.assertEqual(t._parse_topic_arg(f"{BASE}/index.php?/forums/topic/174730-seethamma/"),
                         (174730, "seethamma"))

    def test_recrawl_resets_walk_and_refetch_forces_archived_topics(self):
        for k, v in (("walk:tamilmv:11", 61), ("walkdone:tamilmv:25", 1), ("audit:tamilmv", 2)):
            t.set_meta(k, v)
        with contextlib.redirect_stdout(io.StringIO()):
            t.recrawl_cli(["25"])
        self.assertIsNone(t.get_meta("walkdone:tamilmv:25"))
        self.assertEqual(t.get_meta("walk:tamilmv:11"), "61")                 # other forum untouched
        self.assertIsNone(t.get_meta("audit:tamilmv"))
        self.fake.pages[topic_url(40, "a")] = (200, topic_page(40, "a", body=mag(40)))
        t.process_topic("tamilmv", 40, topic_url(40, "a"))
        self.assertIsNone(t.topic_action("tamilmv", 40, topic_url(40, "a")))
        time.sleep(0.01)
        with contextlib.redirect_stdout(io.StringIO()):
            t.recrawl_cli(["--refetch"])
        self.assertIsNone(t.get_meta("walk:tamilmv:11"))
        self.assertEqual(t.topic_action("tamilmv", 40, topic_url(40, "a")), "fetch")

    def test_same_torrent_in_two_topics_one_release_both_topic_pages(self):
        for pid in (51, 52):
            self.fake.pages[topic_url(pid, "a")] = (200, topic_page(pid, "a", body=mag(999, "Shared.Movie.2020.mkv")))
            t.process_topic("tamilmv", pid, topic_url(pid, "a"))
        self.assertEqual(len(self.releases()), 1)
        self.assertEqual(self.releases()[0]["post_id"], 51)                  # first topic keeps it
        self.assertIn("Shared.Movie.2020", t.topic_page("tamilmv", 52)[1])


if __name__ == "__main__":
    unittest.main()
