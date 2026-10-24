import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sync  # noqa: E402

TODAY = dt.date(2026, 1, 1)


def movie(mid, order=0, votes=100, genres=None, char="Hero", date="2020-01-01", adult=False, **kw):
    return {"id": mid, "title": f"Movie {mid}", "release_date": date, "genre_ids": genres or [18],
            "vote_count": votes, "order": order, "character": char, "adult": adult, **kw}


class FilterTests(unittest.TestCase):
    def kept_ids(self, cast, **opts):
        return {m["id"] for m in sync.filter_cast(cast, opts, TODAY)}

    def test_cast_order(self):
        cast = [movie(1, order=2), movie(2, order=40)]
        self.assertEqual(self.kept_ids(cast, max_cast_order=15), {1})
        self.assertEqual(self.kept_ids(cast, max_cast_order=None), {1, 2})

    def test_votes_genres_self_adult(self):
        cast = [movie(1, votes=5), movie(2, genres=[99]), movie(3, char="Himself"),
                movie(4, char="Self (archive)"), movie(5, adult=True), movie(6)]
        self.assertEqual(self.kept_ids(cast, min_vote_count=20), {6})
        self.assertEqual(self.kept_ids(cast, min_vote_count=0, exclude_genre_ids=[]), {1, 2, 6})

    def test_upcoming(self):
        cast = [movie(1, date="2030-01-01"), movie(2, date=""), movie(3)]
        self.assertEqual(self.kept_ids(cast, include_upcoming=True), {1, 2, 3})
        self.assertEqual(self.kept_ids(cast, include_upcoming=False), {3})

    def test_manual_include_exclude(self):
        cast = [movie(1), movie(2, order=99, votes=0)]
        self.assertEqual(self.kept_ids(cast, exclude_tmdb_ids=[1], include_tmdb_ids=[2]), {2})

    def test_sort_empty_dates_first_then_desc(self):
        ms = [movie(1, date="2001-01-01"), movie(2, date=""), movie(3, date="2020-05-05")]
        self.assertEqual([m["id"] for m in sync.sort_movies(ms)], [2, 3, 1])

    def test_slug(self):
        self.assertEqual(sync.slugify("Penélope Cruz"), "penelope-cruz")
        self.assertEqual(sync.slugify("Keanu Reeves"), "keanu-reeves")

    def test_backdrop_most_voted(self):
        cast = [movie(1, votes=5, backdrop_path="/a.jpg"), movie(2, votes=50, backdrop_path="/b.jpg"),
                movie(3, votes=999)]
        self.assertEqual(sync.pick_backdrop(cast), "/b.jpg")


class ListLookupTests(unittest.TestCase):
    LISTS = [{"id": 232508, "name": "Swann Arlaud", "slug": "swann-arlaud"},
             {"id": 232510, "name": "Vincent Lindon", "slug": "vincent-lindon"}]

    def test_match_ignores_case_and_accents(self):
        self.assertEqual(sync.find_list_id(self.LISTS, "vincent lindon"), 232510)
        self.assertEqual(sync.find_list_id(self.LISTS, "Swann Arlaud"), 232508)

    def test_not_found_lists_names(self):
        with self.assertRaisesRegex(sync.SyncError, "Swann Arlaud"):
            sync.find_list_id(self.LISTS, "Keanu Reeves")

    def test_duplicate_names_rejected(self):
        with self.assertRaises(sync.SyncError):
            sync.find_list_id(self.LISTS + [{"id": 1, "name": "Vincent Lindon"}], "Vincent Lindon")

    def test_explicit_id_skips_lookup(self):
        with mock.patch.object(sync, "mdb_request") as req:
            self.assertEqual(sync.resolve_list_id({"mdblist_list_id": 42}, "X"), 42)
            req.assert_not_called()

    def test_lookup_defaults_to_actor_name(self):
        with mock.patch.object(sync, "mdb_request", return_value=self.LISTS):
            self.assertEqual(sync.resolve_list_id({}, "Vincent Lindon"), 232510)


class DiffTests(unittest.TestCase):
    def test_add_and_remove(self):
        current = [{"id": 1, "imdb_id": "tt1"}, {"id": 2, "imdb_id": "tt2"}]
        desired = [{"tmdb": 2, "imdb": "tt2"}, {"tmdb": 3, "imdb": "tt3"}, {"tmdb": 4, "imdb": None}]
        add, rem = sync.compute_diff(current, desired)
        self.assertEqual([d["tmdb"] for d in add], [3, 4])
        self.assertEqual(rem, ["tt1"])

    def test_no_change(self):
        current = [{"id": 1, "imdb_id": "tt1"}]
        add, rem = sync.compute_diff(current, [{"tmdb": 1, "imdb": "tt1"}])
        self.assertEqual((add, rem), ([], []))

    def test_missing_imdb_not_readded_when_tmdb_present(self):
        current = [{"id": 4, "imdb_id": "tt4"}]
        add, rem = sync.compute_diff(current, [{"tmdb": 4, "imdb": None}])
        self.assertEqual((add, rem), ([], []))


def fake_resp(status=200, payload=None, content=b""):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.content = content
    r.headers = {}
    r.text = ""
    return r


class MdbListTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict("os.environ", {"MDBLIST_API_KEY": "k", "TMDB_API_KEY": "t"})
        p.start()
        self.addCleanup(p.stop)

    def test_pagination_and_list_response(self):
        full = {"movies": [{"id": i, "imdb_id": f"tt{i}"} for i in range(sync.PAGE)]}
        last = {"movies": [{"id": 9999, "imdb_id": "tt9999"}]}
        with mock.patch("requests.request", side_effect=[fake_resp(payload=full), fake_resp(payload=last)]) as rq:
            items = sync.mdb_get_items(5)
        self.assertEqual(len(items), sync.PAGE + 1)
        self.assertEqual(rq.call_args_list[1].kwargs["params"]["offset"], sync.PAGE)
        with mock.patch("requests.request", return_value=fake_resp(payload=[{"id": 1}])):
            self.assertEqual(len(sync.mdb_get_items(5)), 1)

    def test_apply_chunks(self):
        add = [{"tmdb": i} for i in range(250)]
        with mock.patch("requests.request", return_value=fake_resp(payload={"added": {"movies": 1}})) as rq:
            sync.mdb_apply(5, add, ["tt1"])
        urls = [c.args[1] for c in rq.call_args_list]
        self.assertEqual(sum(u.endswith("/items/add") for u in urls), 3)
        self.assertEqual(sum(u.endswith("/items/remove") for u in urls), 1)

    def test_retry_on_429(self):
        with mock.patch("time.sleep"), mock.patch(
                "requests.request", side_effect=[fake_resp(429), fake_resp(200, {"ok": 1})]):
            self.assertEqual(sync.http("GET", "http://x").status_code, 200)
        with mock.patch("time.sleep"), mock.patch("requests.request", return_value=fake_resp(500)):
            with self.assertRaises(sync.SyncError):
                sync.http("GET", "http://x")


class TmdbTests(unittest.TestCase):
    def test_auth_modes(self):
        with mock.patch.dict("os.environ", {"TMDB_API_KEY": "eyJabc"}), \
                mock.patch("requests.request", return_value=fake_resp(payload={})) as rq:
            sync.tmdb_request("/x")
            kw = rq.call_args.kwargs
            self.assertEqual(kw["headers"]["Authorization"], "Bearer eyJabc")
            self.assertNotIn("api_key", kw["params"])
        with mock.patch.dict("os.environ", {"TMDB_API_KEY": "plain"}), \
                mock.patch("requests.request", return_value=fake_resp(payload={})) as rq:
            sync.tmdb_request("/x")
            self.assertEqual(rq.call_args.kwargs["params"]["api_key"], "plain")

    def test_imdb_cache(self):
        cache = {"1": "tt1"}
        with mock.patch("sync.tmdb_request", return_value={"imdb_id": "tt2"}) as tr:
            self.assertEqual(sync.get_imdb_id(1, cache), "tt1")
            tr.assert_not_called()
            self.assertEqual(sync.get_imdb_id(2, cache), "tt2")
        self.assertEqual(cache["2"], "tt2")


class BannerTests(unittest.TestCase):
    def test_generate_banners(self):
        from PIL import Image
        photo = Image.new("RGB", (500, 750), (120, 80, 60))
        backdrop = Image.new("RGB", (1280, 720), (30, 60, 120))
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("sync.download_image", side_effect=[photo, backdrop, photo, backdrop]):
            out = Path(d)
            first = sync.generate_banners("Keanu Reeves", "keanu-reeves", "/p.jpg", "/b.jpg", out)
            self.assertEqual(set(first), {"keanu-reeves.jpg", "keanu-reeves_poster.jpg",
                                          "keanu-reeves_photo.jpg"})
            self.assertEqual(Image.open(out / "keanu-reeves.jpg").size, (1920, 1080))
            self.assertEqual(Image.open(out / "keanu-reeves_poster.jpg").size, (1000, 1500))
            second = sync.generate_banners("Keanu Reeves", "keanu-reeves", "/p.jpg", "/b.jpg", out)
            self.assertEqual(second, [])

    def test_banners_without_images(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("sync.download_image", return_value=None):
            out = Path(d)
            sync.generate_banners("Un Nom Tres Tres Long Pour Tester Le Retour A La Ligne", "x", None, None, out)
            self.assertTrue((out / "x.jpg").exists())
            self.assertTrue((out / "x_poster.jpg").exists())


class AddonTests(unittest.TestCase):
    CONFIG = {"github_repo": "HichGSS/actor-lists",
              "actors": [{"name": "Vincent Lindon"}, {"name": "Swann Arlaud"}]}

    @staticmethod
    def film(n, date, imdb="auto", **kw):
        return {"title": f"Film {n}", "year": date[:4], "tmdb": n,
                "imdb": f"tt{n}" if imdb == "auto" else imdb, "release_date": date,
                "poster_path": f"/p{n}.jpg", "backdrop_path": None, "overview": "", **kw}

    def desired(self):
        return [self.film(1, "2010-05-01"), self.film(2, "2022-03-03"),
                self.film(3, "2015-01-01", imdb=None), self.film(4, ""), self.film(5, "2001-01-01")]

    def results(self):
        return [{"name": "Vincent Lindon", "slug": "vincent-lindon", "desired": self.desired()},
                {"name": "Swann Arlaud", "slug": "swann-arlaud", "desired": self.desired()[:1]}]

    def load(self, d, rel):
        return json.loads((Path(d) / rel).read_text(encoding="utf-8"))

    def test_manifest_and_catalogs(self):
        with tempfile.TemporaryDirectory() as d:
            sync.generate_addon(self.CONFIG, self.results(), Path(d))
            m = self.load(d, "manifest.json")
            self.assertEqual(m["id"], "community.actorlists.hichgss")
            self.assertEqual([c["id"] for c in m["catalogs"]], ["actorlists-movie", "actorlists-series"])
            self.assertEqual(m["idPrefixes"], ["actorlists-"])
            self.assertTrue(m["logo"].endswith("/banners/vincent-lindon_poster.jpg"))
            self.assertTrue((Path(d) / ".nojekyll").exists())
            cat = self.load(d, "catalog/series/actorlists-series.json")["metas"]
            self.assertEqual([c["id"] for c in cat],
                             ["actorlists-series-vincent-lindon", "actorlists-series-swann-arlaud"])
            self.assertEqual(cat[0]["description"], "Filmographie de Vincent Lindon (4 films)")
            self.assertEqual(cat[0]["background"],
                             "https://raw.githubusercontent.com/HichGSS/actor-lists/main/banners/vincent-lindon.jpg")

    def test_movie_order_and_missing_imdb(self):
        with tempfile.TemporaryDirectory() as d:
            sync.generate_addon(self.CONFIG, self.results(), Path(d))
            meta = self.load(d, "meta/movie/actorlists-movie-vincent-lindon.json")["meta"]
            self.assertEqual([v["id"] for v in meta["videos"]], ["tt4", "tt2", "tt1", "tt5"])
            self.assertNotIn("episode", meta["videos"][0])
            self.assertNotIn("released", meta["videos"][0])
            self.assertEqual(meta["videos"][1]["released"], "2022-03-03T00:00:00.000Z")
            self.assertEqual(meta["videos"][1]["title"], "Film 2 (2022)")
            self.assertEqual(meta["videos"][1]["thumbnail"], "https://image.tmdb.org/t/p/w500/p2.jpg")

    def test_series_numbering_chronological(self):
        with tempfile.TemporaryDirectory() as d:
            sync.generate_addon(self.CONFIG, self.results(), Path(d))
            meta = self.load(d, "meta/series/actorlists-series-vincent-lindon.json")["meta"]
            self.assertEqual(meta["type"], "series")
            self.assertEqual([v["id"] for v in meta["videos"]], ["tt5", "tt1", "tt2", "tt4"])
            self.assertEqual([(v["season"], v["episode"]) for v in meta["videos"]],
                             [(1, 1), (1, 2), (1, 3), (1, 4)])

    def test_no_colon_in_filenames(self):
        with tempfile.TemporaryDirectory() as d:
            sync.generate_addon(self.CONFIG, self.results(), Path(d))
            files = [p for p in Path(d).rglob("*") if p.is_file()]
            self.assertEqual(len(files), 8)
            self.assertFalse([p for p in files if ":" in p.name])

    def test_stale_cleanup_and_failed_actor_kept(self):
        with tempfile.TemporaryDirectory() as d:
            sync.generate_addon(self.CONFIG, self.results(), Path(d))
            stale = Path(d) / "meta/movie/actorlists-movie-old-actor.json"
            stale.write_text("{}", encoding="utf-8")
            other = Path(d) / "meta/movie/readme.json"
            other.write_text("{}", encoding="utf-8")
            # Swann Arlaud en echec : son fichier est conserve et reste au catalogue
            sync.generate_addon(self.CONFIG, self.results()[:1], Path(d))
            self.assertFalse(stale.exists())
            self.assertTrue(other.exists())
            self.assertTrue((Path(d) / "meta/movie/actorlists-movie-swann-arlaud.json").exists())
            cat = self.load(d, "catalog/movie/actorlists-movie.json")["metas"]
            self.assertEqual(len(cat), 2)
            # Acteur retire de la config : supprime
            sync.generate_addon({**self.CONFIG, "actors": self.CONFIG["actors"][:1]},
                                self.results()[:1], Path(d))
            self.assertFalse((Path(d) / "meta/movie/actorlists-movie-swann-arlaud.json").exists())
            self.assertFalse((Path(d) / "meta/series/actorlists-series-swann-arlaud.json").exists())

    def test_unchanged_not_rewritten_and_no_repo(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(sync.generate_addon(self.CONFIG, self.results(), Path(d)))
            self.assertEqual(sync.generate_addon(self.CONFIG, self.results(), Path(d)), [])
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(sync.generate_addon({"actors": []}, self.results(), Path(d)), [])
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_build_desired_carries_tmdb_fields(self):
        m = movie(7, date="2020-02-02", poster_path="/p.jpg", backdrop_path="/b.jpg", overview="Resume")
        d = sync.build_desired([m], {"7": "tt7"})[0]
        self.assertEqual((d["imdb"], d["release_date"], d["poster_path"], d["backdrop_path"],
                          d["overview"], d["year"]), ("tt7", "2020-02-02", "/p.jpg", "/b.jpg", "Resume", "2020"))


if __name__ == "__main__":
    unittest.main()
