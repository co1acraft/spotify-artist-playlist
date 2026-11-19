import contextlib
import io
import json
import unittest
from unittest import mock

import spotify_artist_playlist as sp


ARTIST_ID = "0TnOYISbd1XYRBk9myaseg"
OTHER_ARTIST_ID = "1vCWHaC5f2uS3yhpwWbIA6"


def make_track(
    track_id,
    name,
    release_date="2020-01-01",
    precision="day",
    album="Album",
    disc=1,
    number=1,
    artists=("Artist",),
    artist_ids=(ARTIST_ID,),
):
    return sp.Track(
        id=track_id,
        name=name,
        artist_names=tuple(artists),
        artist_ids=frozenset(artist_ids),
        album_id=f"album-{album}",
        album_name=album,
        album_type="album",
        release_date=release_date,
        release_date_precision=precision,
        disc_number=disc,
        track_number=number,
    )


class FakeClient:
    def __init__(
        self,
        artist_results=None,
        albums=None,
        album_tracks=None,
        artist_objects=None,
    ):
        self.artist_results = artist_results or []
        self.albums = albums or []
        self.album_tracks = album_tracks or {}
        self.artist_objects = artist_objects or {}
        self.calls = []
        self.created = []
        self.added = []
        self.playlist_counter = 0

    def api(self, method, path, payload=None, params=None, retries=6):
        self.calls.append((method, path, params, payload))
        if path == "/search":
            return {"artists": {"items": self.artist_results}}
        if path.startswith("/artists/"):
            if path.endswith("/albums"):
                offset = (params or {}).get("offset", 0)
                page_index = offset // sp.ALBUM_PAGE_SIZE
                page = self.albums[page_index] if page_index < len(self.albums) else []
                return {
                    "items": page,
                    "total": sum(len(album_page) for album_page in self.albums),
                }
            artist_id = path.rsplit("/", 1)[-1]
            return self.artist_objects.get(artist_id, {})
        if path == "/me/playlists":
            self.playlist_counter += 1
            playlist_id = f"playlist{self.playlist_counter}"
            playlist = {
                "id": playlist_id,
                "external_urls": {"spotify": f"https://open.spotify.com/playlist/{playlist_id}"},
            }
            self.created.append((payload, playlist))
            return playlist
        raise AssertionError(f"Unexpected API call: {method} {path}")

    def create_playlist(self, name, public, description):
        payload = {"name": name, "public": public, "description": description}
        self.playlist_counter += 1
        playlist_id = f"playlist{self.playlist_counter}"
        playlist = {
            "id": playlist_id,
            "external_urls": {"spotify": f"https://open.spotify.com/playlist/{playlist_id}"},
        }
        self.created.append((payload, playlist))
        return playlist

    def paged(self, path, params=None, key="items"):
        self.calls.append(("GET", path, params, None))
        if "/albums/" in path and path.endswith("/tracks"):
            album_id = path.split("/albums/", 1)[1].rsplit("/", 1)[0]
            for page in self.album_tracks.get(album_id, []):
                yield from page
            return
        raise AssertionError(f"Unexpected paged call: {path}")

    def add_items(self, playlist_id, uris):
        self.added.append((playlist_id, uris))
        return []


class NormalizeTests(unittest.TestCase):
    def test_normalized_title_strips_features_and_versions(self):
        self.assertEqual(sp.normalized_title("Song (Remastered 2019)"), "song")
        self.assertEqual(sp.normalized_title("Song feat. Someone"), "song")
        self.assertEqual(sp.normalized_title("Song (Live) (Remastered)"), "song")
        self.assertEqual(sp.normalized_title("SONG  —  EDIT"), "song edit")
        self.assertEqual(sp.normalized_title("With You"), "with you")

    def test_normalize_collapses_whitespace_and_case(self):
        self.assertEqual(sp.normalize("  The   Beatles "), "the beatles")


class TrackKeyTests(unittest.TestCase):
    def test_release_key_respects_precision(self):
        self.assertEqual(
            make_track("a", "A", "1999", "year").release_key, (1999, 1, 1)
        )
        self.assertEqual(
            make_track("b", "B", "2001-05", "month").release_key, (2001, 5, 1)
        )
        self.assertEqual(
            make_track("c", "C", "2003-07-09", "day").release_key, (2003, 7, 9)
        )

    def test_unknown_date_sorts_last(self):
        self.assertGreater(make_track("x", "X", "").release_key, (2020, 1, 1))

    def test_artists_property(self):
        track = make_track("a", "A", artists=("One", "Two"))
        self.assertEqual(track.artists, "One, Two")
        self.assertEqual(track.uri, "spotify:track:a")


class OrderAndDedupeTests(unittest.TestCase):
    def test_release_order_groups_same_release_by_disc_and_track(self):
        tracks = [
            make_track("c", "C", "2000-01-01", album="Later", number=1),
            make_track("b", "B", "1990-01-01", album="First", disc=2, number=1),
            make_track("a", "A", "1990-01-01", album="First", disc=1, number=2),
        ]
        ordered = sp.release_order(tracks)
        self.assertEqual([track.id for track in ordered], ["a", "b", "c"])

    def test_dedupe_uri_keeps_distinct_recordings(self):
        tracks = [
            make_track("a", "Song", "2000-01-01", album="One"),
            make_track("a", "Song", "2001-01-01", album="Two"),
            make_track("b", "Song (Live)", "2002-01-01", album="Three"),
        ]
        kept, removed = sp.dedupe(sp.release_order(tracks), "uri")
        self.assertEqual([track.id for track in kept], ["a", "b"])
        self.assertEqual(removed, 1)
        self.assertEqual(kept[0].album_name, "One")

    def test_dedupe_name_collapses_identical_titles(self):
        tracks = [
            make_track("a", "Song", "2000-01-01"),
            make_track("b", "Song", "2001-01-01"),
        ]
        kept, removed = sp.dedupe(sp.release_order(tracks), "name")
        self.assertEqual([track.id for track in kept], ["a"])
        self.assertEqual(removed, 1)

    def test_dedupe_normalized_ignores_features_and_versions(self):
        tracks = [
            make_track("a", "Song (Remastered 2011)", "2000-01-01"),
            make_track("b", "Song feat. Guest", "2001-01-01"),
            make_track("c", "Other Song", "2002-01-01"),
        ]
        kept, removed = sp.dedupe(sp.release_order(tracks), "normalized")
        self.assertEqual([track.id for track in kept], ["a", "c"])
        self.assertEqual(removed, 1)


class IdExtractionTests(unittest.TestCase):
    def test_extracts_id_from_id_uri_and_url(self):
        for value in (
            ARTIST_ID,
            f"spotify:artist:{ARTIST_ID}",
            f"https://open.spotify.com/artist/{ARTIST_ID}?si=abc",
        ):
            self.assertEqual(sp.extracted_id(value, "artist"), ARTIST_ID)

    def test_rejects_non_matching_values(self):
        self.assertIsNone(sp.extracted_id("radiohead", "artist"))
        self.assertIsNone(sp.extracted_id("spotify:track:" + ARTIST_ID, "artist"))
        self.assertEqual(
            sp.extracted_id(f"https://open.spotify.com/playlist/{ARTIST_ID}", "playlist"),
            ARTIST_ID,
        )


class ResolveArtistTests(unittest.TestCase):
    def test_exact_name_match(self):
        client = FakeClient(
            artist_results=[
                {"id": "other", "name": "Artist Too"},
                {"id": ARTIST_ID, "name": "Artist"},
            ]
        )
        artist = sp.resolve_artist(client, "Artist", None, None)
        self.assertEqual(artist["id"], ARTIST_ID)

    def test_direct_id_lookup(self):
        client = FakeClient(artist_objects={ARTIST_ID: {"id": ARTIST_ID, "name": "Artist"}})
        artist = sp.resolve_artist(client, f"spotify:artist:{ARTIST_ID}", None, None)
        self.assertEqual(artist["name"], "Artist")

    def test_ambiguous_match_raises(self):
        client = FakeClient(
            artist_results=[
                {"id": "one", "name": "Artist One"},
                {"id": "two", "name": "Artist Two"},
            ]
        )
        with self.assertRaises(sp.SpotifyError) as context:
            sp.resolve_artist(client, "Artist", None, None)
        self.assertIn("--artist-index", str(context.exception))

    def test_artist_index_selects_candidate(self):
        client = FakeClient(
            artist_results=[
                {"id": "one", "name": "Artist One"},
                {"id": "two", "name": "Artist Two"},
            ]
        )
        artist = sp.resolve_artist(client, "Artist", 2, None)
        self.assertEqual(artist["id"], "two")


class FetchTests(unittest.TestCase):
    def test_fetch_albums_follows_pages_and_dedupes(self):
        first_page = [
            {"id": f"alb{index}", "name": f"Album {index}"} for index in range(10)
        ]
        client = FakeClient(
            albums=[
                first_page,
                [{"id": "alb1", "name": "Album 1"}, {"id": "alb11", "name": "Album 11"}],
            ]
        )
        albums = sp.fetch_albums(client, ARTIST_ID, ["album", "single"], "US")
        expected = [f"alb{index}" for index in range(10)] + ["alb11"]
        self.assertEqual([album["id"] for album in albums], expected)
        self.assertEqual(client.calls[0][2]["include_groups"], "album,single")
        self.assertEqual(client.calls[0][2]["market"], "US")
        self.assertEqual(client.calls[0][2]["limit"], 10)
        self.assertEqual(client.calls[0][2]["offset"], 0)
        self.assertEqual(client.calls[1][2]["offset"], 10)
        self.assertEqual(client.calls[1][2]["include_groups"], "album,single")
        self.assertEqual(client.calls[1][2]["market"], "US")

    def test_fetch_tracks_filters_other_artists_and_reads_all_pages(self):
        client = FakeClient(
            album_tracks={
                "alb1": [
                    [
                        {
                            "id": "t1",
                            "name": "First",
                            "artists": [{"id": ARTIST_ID, "name": "Artist"}],
                            "disc_number": 1,
                            "track_number": 1,
                        },
                        {
                            "id": "other",
                            "name": "Guest Only",
                            "artists": [{"id": OTHER_ARTIST_ID, "name": "Other"}],
                            "disc_number": 1,
                            "track_number": 2,
                        },
                    ],
                    [
                        {
                            "id": "t2",
                            "name": "Second",
                            "artists": [{"id": ARTIST_ID, "name": "Artist"}],
                            "disc_number": 1,
                            "track_number": 2,
                        }
                    ],
                ]
            }
        )
        albums = [
            {
                "id": "alb1",
                "name": "One",
                "album_type": "album",
                "release_date": "1990-05-06",
                "release_date_precision": "day",
            }
        ]
        with contextlib.redirect_stderr(io.StringIO()):
            tracks, skipped = sp.fetch_tracks(client, albums, ARTIST_ID, None, 0)
        self.assertEqual([track.id for track in tracks], ["t1", "t2"])
        self.assertEqual([track.track_number for track in tracks], [1, 2])
        self.assertEqual(tracks[0].release_date, "1990-05-06")
        self.assertEqual(tracks[0].album_name, "One")
        self.assertEqual(skipped, 1)


class SplitAndFormatTests(unittest.TestCase):
    def test_split_playlists(self):
        tracks = [make_track(str(index), str(index)) for index in range(5)]
        groups = sp.split_playlists(tracks, 2, None)
        self.assertEqual([len(group) for group in groups], [2, 2, 1])

    def test_append_to_rejects_overflow(self):
        tracks = [make_track(str(index), str(index)) for index in range(3)]
        with self.assertRaises(sp.SpotifyError):
            sp.split_playlists(tracks, 2, "playlist123")

    def test_formats(self):
        tracks = [make_track("a", "Song", album="One")]
        text = sp.format_tracks(tracks, "text")
        self.assertIn("Song", text)
        payload = json.loads(sp.format_tracks(tracks, "json"))
        self.assertEqual(payload[0]["uri"], "spotify:track:a")
        csv_text = sp.format_tracks(tracks, "csv")
        self.assertIn("position,uri,name,artists", csv_text)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient(
            artist_results=[{"id": ARTIST_ID, "name": "Artist"}],
            albums=[
                [
                    {
                        "id": "alb1",
                        "name": "Old",
                        "album_type": "album",
                        "release_date": "1990-01-01",
                        "release_date_precision": "day",
                    },
                    {
                        "id": "alb2",
                        "name": "New",
                        "album_type": "single",
                        "release_date": "2000-01-01",
                        "release_date_precision": "day",
                    },
                ]
            ],
            album_tracks={
                "alb1": [
                    [
                        {
                            "id": "t1",
                            "name": "Song",
                            "artists": [{"id": ARTIST_ID, "name": "Artist"}],
                            "disc_number": 1,
                            "track_number": 1,
                        }
                    ]
                ],
                "alb2": [
                    [
                        {
                            "id": "t1",
                            "name": "Song",
                            "artists": [{"id": ARTIST_ID, "name": "Artist"}],
                            "disc_number": 1,
                            "track_number": 1,
                        },
                        {
                            "id": "t2",
                            "name": "Later",
                            "artists": [{"id": ARTIST_ID, "name": "Artist"}],
                            "disc_number": 1,
                            "track_number": 2,
                        },
                    ]
                ],
            },
        )
        self.patcher = mock.patch.object(sp, "SpotifyClient", return_value=self.client)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def base_args(self):
        return [
            "create",
            "Artist",
            "--client-id",
            "client",
            "--no-browser",
        ]

    def test_dry_run_output_is_release_ordered_and_deduplicated(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = sp.main(self.base_args() + ["--dry-run"])
        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertEqual(output.count("Song"), 1)
        self.assertLess(output.index("Song"), output.index("Later"))
        self.assertEqual(self.client.created, [])

    def test_create_adds_tracks_in_release_order(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            result = sp.main(self.base_args() + ["--name", "My List"])
        self.assertEqual(result, 0)
        self.assertEqual(len(self.client.created), 1)
        self.assertEqual(self.client.created[0][0]["name"], "My List")
        self.assertEqual(
            self.client.added[0][1], ["spotify:track:t1", "spotify:track:t2"]
        )

    def test_name_dedupe_removes_remaster(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        args = self.base_args() + ["--dry-run", "--dedupe", "normalized", "--format", "json"]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = sp.main(args)
        self.assertEqual(result, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual([track["name"] for track in payload], ["Song", "Later"])

    def test_limit_applied_after_dedupe(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        args = self.base_args() + ["--dry-run", "--format", "json", "--limit", "1"]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = sp.main(args)
        self.assertEqual(result, 0)
        self.assertEqual(len(json.loads(stdout.getvalue())), 1)

    def test_split_creates_numbered_playlists(self):
        stderr = io.StringIO()
        args = self.base_args() + ["--max-tracks", "1"]
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            result = sp.main(args)
        self.assertEqual(result, 0)
        names = [created[0]["name"] for created in self.client.created]
        self.assertEqual(names, ["Artist (Release Order) (1/2)", "Artist (Release Order) (2/2)"])


class ClientTests(unittest.TestCase):
    def test_rejects_non_spotify_absolute_url(self):
        client = sp.SpotifyClient("id", sp.DEFAULT_REDIRECT_URI, sp.Path("/tmp/nonexistent-token.json"))
        with self.assertRaises(sp.SpotifyError):
            client.api("GET", "https://example.com/v1/albums")


if __name__ == "__main__":
    unittest.main()
