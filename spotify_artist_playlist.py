#!/usr/bin/env python3
"""Create a Spotify playlist of one artist's songs in release order, without duplicates."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import http.server
import io
import json
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

API_BASE = "https://api.spotify.com/v1"
ACCOUNTS_BASE = "https://accounts.spotify.com"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = ("playlist-modify-public", "playlist-modify-private")
BATCH_SIZE = 100
PLAYLIST_MAX = 10_000
ALBUM_PAGE_SIZE = 10
SEARCH_PAGE_SIZE = 10
TRACK_PAGE_SIZE = 50
ALBUM_GROUPS = ("album", "single", "compilation", "appears_on")
DEFAULT_GROUPS = ("album", "single", "compilation")
DEDUPE_CHOICES = ("uri", "name", "normalized")
SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
FEATURING_RE = re.compile(r"\s*\(?(?:feat\.?|featuring|ft\.)\s+[^()]*?\)?\s*$", re.IGNORECASE)
VERSION_RE = re.compile(r"\s*\([^()]*\)\s*$")
MAX_DESCRIPTION = 300


class SpotifyError(RuntimeError):
    pass


def normalize(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", (value or "").strip()).casefold()
    return re.sub(r"\s+", " ", text)


def normalized_title(value: str | None) -> str:
    text = normalize(value)
    text = FEATURING_RE.sub("", text)
    while True:
        shorter = VERSION_RE.sub("", text)
        if shorter == text:
            break
        text = shorter
    text = re.sub(r"[-‐-―_/\\]+", " ", text)
    return re.sub(r"\s+", " ", text).strip(" .-")


@dataclass
class Track:
    id: str
    name: str
    artist_names: tuple[str, ...]
    artist_ids: frozenset[str]
    album_id: str
    album_name: str
    album_type: str
    release_date: str
    release_date_precision: str
    disc_number: int
    track_number: int

    @property
    def uri(self) -> str:
        return f"spotify:track:{self.id}"

    @property
    def artists(self) -> str:
        return ", ".join(self.artist_names)

    @property
    def release_key(self) -> tuple[int, int, int]:
        parts = (self.release_date or "").split("-")
        try:
            year = int(parts[0]) if parts and parts[0] else 0
        except ValueError:
            year = 0
        month = 1
        day = 1
        if self.release_date_precision in ("month", "day") and len(parts) > 1:
            try:
                month = int(parts[1])
            except ValueError:
                month = 1
        if self.release_date_precision == "day" and len(parts) > 2:
            try:
                day = int(parts[2])
            except ValueError:
                day = 1
        if year <= 0:
            return (9999, 12, 31)
        return (year, min(max(month, 1), 12), min(max(day, 1), 31))

    @property
    def sort_key(self) -> tuple[Any, ...]:
        return (
            self.release_key,
            normalize(self.album_name),
            self.disc_number,
            self.track_number,
            normalize(self.name),
            self.id,
        )

    def dedupe_key(self, mode: str) -> str:
        if mode == "name":
            return f"name:{normalize(self.name)}"
        if mode == "normalized":
            return f"normalized:{normalized_title(self.name)}"
        return f"uri:{self.id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "name": self.name,
            "artists": self.artist_names,
            "album": self.album_name,
            "album_type": self.album_type,
            "release_date": self.release_date,
            "release_date_precision": self.release_date_precision,
            "disc_number": self.disc_number,
            "track_number": self.track_number,
        }


def default_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "spotify-unique-playlist"


def default_token_path() -> Path:
    return default_config_dir() / "token.json"


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def http_call(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 30.0,
) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def error_detail(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")[:500]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        if error:
            return str(error)
        if payload.get("error_description"):
            return str(payload["error_description"])
    return str(payload)[:500]


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def exchange_code(client_id: str, code: str, redirect_uri: str, verifier: str) -> dict[str, Any]:
    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
    ).encode("ascii")
    try:
        status, raw, _ = http_call(
            f"{ACCOUNTS_BASE}/api/token",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=payload,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SpotifyError(f"Token exchange failed: {error}") from error
    if status != 200:
        raise SpotifyError(f"Token exchange failed ({status}): {error_detail(raw)}")
    token = json.loads(raw.decode("utf-8"))
    token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60
    return token


def refresh_access_token(client_id: str, refresh: str) -> dict[str, Any]:
    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        }
    ).encode("ascii")
    try:
        status, raw, _ = http_call(
            f"{ACCOUNTS_BASE}/api/token",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=payload,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SpotifyError(f"Token refresh failed: {error}") from error
    if status != 200:
        raise SpotifyError(f"Token refresh failed ({status}): {error_detail(raw)}")
    token = json.loads(raw.decode("utf-8"))
    token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60
    if not token.get("refresh_token"):
        token["refresh_token"] = refresh
    return token


class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        expected = getattr(self.server, "callback_path", "/callback")
        if parsed.path != expected:
            self.send_error(404)
            return
        self.server.auth_result = urllib.parse.parse_qs(parsed.query)
        body = (
            "<!doctype html><html><body style='font-family:sans-serif;padding:2rem'>"
            "<h2>Spotify authorization received</h2>"
            "<p>You can close this tab and return to the terminal.</p>"
            "</body></html>"
        )
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
        self.server.done.set()

    def log_message(self, format: str, *args: Any) -> None:
        return


def manual_redirect(redirect_uri: str) -> dict[str, list[str]]:
    print(
        "Paste the full URL you were redirected to after approving access.",
        file=sys.stderr,
    )
    pasted = input("Redirect URL: ").strip()
    if not pasted:
        raise SpotifyError("No redirect URL supplied.")
    parsed = urllib.parse.urlparse(pasted)
    if not parsed.query and redirect_uri.rstrip("/") not in pasted:
        parsed = urllib.parse.urlparse(redirect_uri + "?" + pasted)
    return urllib.parse.parse_qs(parsed.query)


def authorize(client_id: str, redirect_uri: str, no_browser: bool) -> dict[str, Any]:
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    )
    auth_url = f"{ACCOUNTS_BASE}/authorize?{query}"

    parsed_redirect = urllib.parse.urlparse(redirect_uri)
    host = parsed_redirect.hostname or ""
    port = parsed_redirect.port or (443 if parsed_redirect.scheme == "https" else 80)
    callback_path = parsed_redirect.path or "/callback"
    loopback = host in ("127.0.0.1", "localhost") and not no_browser

    result: dict[str, list[str]] | None = None
    if loopback:
        try:
            server = http.server.HTTPServer(("127.0.0.1", port), OAuthCallbackHandler)
        except OSError as error:
            print(
                f"Could not listen on 127.0.0.1:{port} ({error}); falling back to manual entry.",
                file=sys.stderr,
            )
        else:
            server.callback_path = callback_path
            server.auth_result = None
            server.done = threading.Event()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            print(f"Open this URL to authorize:\n{auth_url}\n", file=sys.stderr)
            webbrowser.open(auth_url)
            if not server.done.wait(timeout=300):
                server.shutdown()
                raise SpotifyError("Timed out waiting for Spotify authorization.")
            result = server.auth_result
            server.shutdown()

    if result is None:
        print(f"Open this URL to authorize:\n{auth_url}\n", file=sys.stderr)
        if not no_browser:
            webbrowser.open(auth_url)
        result = manual_redirect(redirect_uri)

    if result is None:
        raise SpotifyError("Spotify authorization was not completed.")
    if result.get("error"):
        raise SpotifyError(f"Spotify authorization failed: {result['error'][0]}")
    if result.get("state", [""])[0] != state:
        raise SpotifyError("Authorization state mismatch; please try again.")
    if not result.get("code"):
        raise SpotifyError("Spotify did not return an authorization code.")
    return exchange_code(client_id, result["code"][0], redirect_uri, verifier)


class SpotifyClient:
    def __init__(
        self,
        client_id: str,
        redirect_uri: str,
        token_cache: Path,
        no_browser: bool = False,
    ) -> None:
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.token_cache = token_cache
        self.no_browser = no_browser
        self.token: dict[str, Any] = {}
        self._load_token()

    def _load_token(self) -> None:
        cached = load_json_file(self.token_cache)
        if cached.get("client_id") and cached.get("client_id") != self.client_id:
            return
        self.token = cached

    def _save_token(self) -> None:
        payload = dict(self.token)
        payload["client_id"] = self.client_id
        save_json_file(self.token_cache, payload)

    def _authenticate(self) -> None:
        token = authorize(self.client_id, self.redirect_uri, self.no_browser)
        token["client_id"] = self.client_id
        token["redirect_uri"] = self.redirect_uri
        self.token = token
        self._save_token()

    def _refresh(self) -> bool:
        refresh = self.token.get("refresh_token")
        if not refresh:
            return False
        try:
            token = refresh_access_token(self.client_id, refresh)
        except SpotifyError:
            return False
        token["client_id"] = self.client_id
        token["redirect_uri"] = self.redirect_uri
        self.token = token
        self._save_token()
        return True

    def access_token(self) -> str:
        if self.token.get("access_token") and float(self.token.get("expires_at", 0)) > time.time():
            return str(self.token["access_token"])
        if self._refresh():
            return str(self.token["access_token"])
        self._authenticate()
        return str(self.token["access_token"])

    def force_refresh(self) -> None:
        if not self._refresh():
            self._authenticate()

    def api(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        retries: int = 6,
    ) -> Any:
        if path.startswith("https://"):
            if not path.startswith(API_BASE):
                raise SpotifyError(f"Refusing non-Spotify API URL: {path}")
            url = path
        else:
            url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        last_error = "request failed"
        for attempt in range(retries):
            headers = {"Authorization": f"Bearer {self.access_token()}"}
            body = None
            if payload is not None:
                body = json.dumps(payload).encode("utf-8")
                headers["Content-Type"] = "application/json"

            try:
                status, raw, response_headers = http_call(
                    url, method=method, headers=headers, body=body
                )
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = str(error)
                time.sleep(min(2**attempt, 30))
                continue

            if status == 429:
                detail = error_detail(raw)
                try:
                    wait = float(response_headers.get("Retry-After", "1"))
                except ValueError:
                    wait = 1.0
                if "QUOTA_EXCEEDED" in detail.upper() or wait > 300:
                    raise SpotifyError(f"Spotify API quota exceeded: {detail}")
                print(f"\nRate limited by Spotify; waiting {wait:.0f}s...", file=sys.stderr)
                time.sleep(wait + 0.5)
                continue
            if status == 401 and attempt < 2:
                self.force_refresh()
                continue
            if 500 <= status < 600:
                last_error = error_detail(raw)
                time.sleep(min(2**attempt, 30))
                continue
            if 200 <= status < 300:
                return json.loads(raw.decode("utf-8")) if raw else None
            raise SpotifyError(f"Spotify API {method} {path} failed ({status}): {error_detail(raw)}")

        raise SpotifyError(f"Spotify API {method} {path} failed: {last_error}")

    def paged(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        key: str = "items",
    ) -> Iterator[dict[str, Any]]:
        url: str | None = path
        seen: set[str] = set()
        page_params = params
        while url and url not in seen:
            seen.add(url)
            data = self.api("GET", url, params=page_params)
            page_params = None
            if not isinstance(data, dict):
                return
            items = data.get(key)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        yield item
            next_url = data.get("next")
            url = str(next_url) if isinstance(next_url, str) and next_url else None

    def search(self, query: str, kind: str, limit: int = 10) -> list[dict[str, Any]]:
        data = self.api(
            "GET",
            "/search",
            params={"q": query, "type": kind, "limit": min(limit, SEARCH_PAGE_SIZE)},
        )
        if not isinstance(data, dict):
            return []
        section = data.get(kind + "s") or {}
        items = section.get("items") if isinstance(section, dict) else None
        return items if isinstance(items, list) else []

    def create_playlist(self, name: str, public: bool, description: str) -> dict[str, Any]:
        return self.api(
            "POST",
            "/me/playlists",
            payload={
                "name": name,
                "public": public,
                "description": description[:MAX_DESCRIPTION],
            },
        )

    def add_items(self, playlist_id: str, uris: list[str]) -> list[str]:
        failed: list[str] = []
        for start in range(0, len(uris), BATCH_SIZE):
            batch = uris[start : start + BATCH_SIZE]
            try:
                self.api("POST", f"/playlists/{playlist_id}/items", payload={"uris": batch})
            except SpotifyError as error:
                message = str(error)
                if "(400)" not in message and "(404)" not in message:
                    raise
                for uri in batch:
                    try:
                        self.api(
                            "POST",
                            f"/playlists/{playlist_id}/items",
                            payload={"uris": [uri]},
                        )
                    except SpotifyError:
                        failed.append(uri)
        return failed


def extracted_id(value: str, kind: str) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.startswith(f"spotify:{kind}:"):
        text = text.split(":", 2)[2]
    elif "://" in text:
        parts = urllib.parse.urlparse(text).path.strip("/").split("/")
        if len(parts) >= 2 and parts[-2] == kind:
            text = parts[-1]
    text = text.split("?")[0]
    return text if SPOTIFY_ID_RE.fullmatch(text) else None


def resolve_artist(
    client: SpotifyClient,
    query: str,
    index: int | None,
    market: str | None,
) -> dict[str, Any]:
    artist_id = extracted_id(query, "artist")
    if artist_id:
        artist = client.api("GET", f"/artists/{urllib.parse.quote(artist_id, safe='')}")
        if not isinstance(artist, dict) or not artist.get("id"):
            raise SpotifyError(f"No Spotify artist found for {query!r}.")
        return artist

    params: dict[str, Any] = {"q": query, "type": "artist", "limit": SEARCH_PAGE_SIZE}
    if market:
        params["market"] = market
    data = client.api("GET", "/search", params=params)
    section = data.get("artists") if isinstance(data, dict) else None
    items = section.get("items") if isinstance(section, dict) else None
    candidates = [item for item in (items or []) if isinstance(item, dict) and item.get("id")]
    if not candidates:
        raise SpotifyError(f"No Spotify artist found for {query!r}.")

    if index is not None:
        if not 1 <= index <= len(candidates):
            raise SpotifyError(f"--artist-index must be between 1 and {len(candidates)}.")
        return candidates[index - 1]

    wanted = normalize(query)
    exact = [item for item in candidates if normalize(item.get("name")) == wanted]
    if len(exact) == 1:
        return exact[0]
    if len(candidates) == 1:
        return candidates[0]
    listing = "\n".join(
        f"  {position}. {item.get('name')} ({item.get('id')})"
        for position, item in enumerate(candidates, start=1)
    )
    raise SpotifyError(
        f"Multiple artists matched {query!r}. Re-run with --artist-index N "
        "or pass the artist ID/URL:\n" + listing
    )


def fetch_albums(
    client: SpotifyClient,
    artist_id: str,
    include_groups: Iterable[str],
    market: str | None,
) -> list[dict[str, Any]]:
    path = f"/artists/{urllib.parse.quote(artist_id, safe='')}/albums"
    albums: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    while True:
        params: dict[str, Any] = {
            "include_groups": ",".join(include_groups),
            "limit": ALBUM_PAGE_SIZE,
            "offset": offset,
        }
        if market:
            params["market"] = market
        data = client.api("GET", path, params=params)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            break
        for album in items:
            if not isinstance(album, dict):
                continue
            album_id = album.get("id")
            if not isinstance(album_id, str) or album_id in seen:
                continue
            seen.add(album_id)
            albums.append(album)
        total = data.get("total") if isinstance(data, dict) else 0
        offset += ALBUM_PAGE_SIZE
        if not isinstance(total, int) or offset >= total:
            break
    return albums


def fetch_tracks(
    client: SpotifyClient,
    albums: list[dict[str, Any]],
    artist_id: str,
    market: str | None,
    delay: float,
) -> tuple[list[Track], int]:
    tracks: list[Track] = []
    skipped = 0
    total = len(albums)
    for position, album in enumerate(albums, start=1):
        album_id = album.get("id") or ""
        params: dict[str, Any] = {"limit": TRACK_PAGE_SIZE}
        if market:
            params["market"] = market
        path = f"/albums/{urllib.parse.quote(str(album_id), safe='')}/tracks"
        for item in client.paged(path, params):
            track_id = item.get("id")
            if not isinstance(track_id, str) or not track_id or item.get("is_local"):
                skipped += 1
                continue
            raw_artists = item.get("artists") or []
            names = tuple(
                str(artist.get("name"))
                for artist in raw_artists
                if isinstance(artist, dict) and artist.get("name")
            )
            ids = frozenset(
                str(artist.get("id"))
                for artist in raw_artists
                if isinstance(artist, dict) and artist.get("id")
            )
            if artist_id not in ids:
                skipped += 1
                continue
            try:
                disc = int(item.get("disc_number") or 1)
                number = int(item.get("track_number") or 1)
            except (TypeError, ValueError):
                disc = 1
                number = 1
            tracks.append(
                Track(
                    id=track_id,
                    name=str(item.get("name") or track_id),
                    artist_names=names,
                    artist_ids=ids,
                    album_id=str(album_id),
                    album_name=str(album.get("name") or ""),
                    album_type=str(album.get("album_type") or ""),
                    release_date=str(album.get("release_date") or ""),
                    release_date_precision=str(album.get("release_date_precision") or "year"),
                    disc_number=disc,
                    track_number=number,
                )
            )
        if position % 25 == 0 or position == total:
            print(
                f"\rScanned releases: {position:,}/{total:,} (kept {len(tracks):,} tracks)",
                end="",
                file=sys.stderr,
                flush=True,
            )
        if delay > 0:
            time.sleep(delay)
    if total:
        print(file=sys.stderr)
    return tracks, skipped


def release_order(tracks: Iterable[Track]) -> list[Track]:
    return sorted(tracks, key=lambda track: track.sort_key)


def dedupe(tracks: Iterable[Track], mode: str) -> tuple[list[Track], int]:
    seen: set[str] = set()
    kept: list[Track] = []
    total = 0
    for track in tracks:
        total += 1
        key = track.dedupe_key(mode)
        if key in seen:
            continue
        seen.add(key)
        kept.append(track)
    return kept, total - len(kept)


def format_tracks(tracks: list[Track], style: str) -> str:
    if style == "json":
        return json.dumps([track.to_dict() for track in tracks], indent=2, ensure_ascii=False)
    if style == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(
            [
                "position",
                "uri",
                "name",
                "artists",
                "release_date",
                "release_date_precision",
                "album",
                "album_type",
                "disc_number",
                "track_number",
            ]
        )
        for index, track in enumerate(tracks, start=1):
            writer.writerow(
                [
                    index,
                    track.uri,
                    track.name,
                    track.artists,
                    track.release_date,
                    track.release_date_precision,
                    track.album_name,
                    track.album_type,
                    track.disc_number,
                    track.track_number,
                ]
            )
        return buffer.getvalue()
    lines = []
    for index, track in enumerate(tracks, start=1):
        date = track.release_date or "unknown date"
        lines.append(
            f"{index:4d}. {track.name} — {track.artists} "
            f"[{date}; {track.album_name}]"
        )
    return "\n".join(lines)


def split_playlists(tracks: list[Track], max_tracks: int, append_to: str | None) -> list[list[Track]]:
    if append_to:
        if len(tracks) > max_tracks:
            raise SpotifyError(
                f"{len(tracks):,} tracks exceed --max-tracks {max_tracks:,}; "
                "remove --append-to to split into multiple playlists."
            )
        return [tracks] if tracks else []
    return [tracks[start : start + max_tracks] for start in range(0, len(tracks), max_tracks)]


def write_output(content: str, path: str | None) -> None:
    if path and path != "-":
        output_path = Path(path).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
        print(f"Wrote track list to {output_path}.", file=sys.stderr)
    else:
        sys.stdout.write(content)
        if not content.endswith("\n"):
            sys.stdout.write("\n")


def add_with_progress(client: SpotifyClient, playlist_id: str, uris: list[str]) -> list[str]:
    failed: list[str] = []
    total = len(uris)
    done = 0
    for start in range(0, total, BATCH_SIZE):
        batch = uris[start : start + BATCH_SIZE]
        failed.extend(client.add_items(playlist_id, batch))
        done += len(batch)
        print(f"\rAdding items: {done:,}/{total:,}", end="", file=sys.stderr, flush=True)
    if total:
        print(file=sys.stderr)
    return failed


def playlist_url(playlist: dict[str, Any]) -> str:
    url = (playlist.get("external_urls") or {}).get("spotify")
    if isinstance(url, str) and url:
        return url
    return f"https://open.spotify.com/playlist/{playlist.get('id', '')}"


def cmd_auth(args: argparse.Namespace) -> int:
    client_id = args.client_id or os.environ.get("SPOTIFY_CLIENT_ID")
    if not client_id:
        raise SpotifyError("Set SPOTIFY_CLIENT_ID or pass --client-id.")
    client = SpotifyClient(
        client_id=client_id,
        redirect_uri=args.redirect_uri,
        token_cache=Path(args.token_cache).expanduser(),
        no_browser=args.no_browser,
    )
    client.access_token()
    print(f"Authorized. Token cached at {Path(args.token_cache).expanduser()}.")
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    client_id = args.client_id or os.environ.get("SPOTIFY_CLIENT_ID")
    if not client_id:
        raise SpotifyError("Set SPOTIFY_CLIENT_ID or pass --client-id.")
    client = SpotifyClient(
        client_id=client_id,
        redirect_uri=args.redirect_uri,
        token_cache=Path(args.token_cache).expanduser(),
        no_browser=args.no_browser,
    )

    artist = resolve_artist(client, args.artist, args.artist_index, args.market)
    artist_name = str(artist.get("name") or args.artist)
    artist_id = str(artist["id"])
    print(f"Artist: {artist_name} ({artist_id})", file=sys.stderr)

    albums = fetch_albums(client, artist_id, args.include_groups, args.market)
    print(f"Found {len(albums):,} release(s). Reading track lists...", file=sys.stderr)
    tracks, skipped = fetch_tracks(client, albums, artist_id, args.market, args.delay)
    tracks = release_order(tracks)
    tracks, duplicates = dedupe(tracks, args.dedupe)
    if args.reverse:
        tracks.reverse()
    if args.limit is not None:
        tracks = tracks[: args.limit]

    print(
        f"Selected {len(tracks):,} unique track(s) from {len(albums):,} release(s); "
        f"dropped {duplicates:,} duplicate(s) and {skipped:,} non-matching/unavailable item(s).",
        file=sys.stderr,
    )
    if not tracks:
        raise SpotifyError("No playable tracks were found for this artist.")

    dry_run = args.dry_run or args.output is not None or args.format != "text"
    if dry_run:
        groups = split_playlists(tracks, args.max_tracks, args.append_to)
        print(
            f"Dry run: would write {len(tracks):,} track(s) "
            f"across {len(groups)} playlist(s).",
            file=sys.stderr,
        )
        write_output(format_tracks(tracks, args.format), args.output)
        return 0

    groups = split_playlists(tracks, args.max_tracks, args.append_to)
    base_name = args.name or f"{artist_name} (Release Order)"
    description = args.description or f"Every {artist_name} song in release order, no duplicates."

    if args.append_to:
        playlist_id = extracted_id(args.append_to, "playlist") or args.append_to
        print(f"Adding {len(tracks):,} track(s) to playlist {playlist_id}...", file=sys.stderr)
        failed = add_with_progress(client, playlist_id, [track.uri for track in tracks])
        print(f"Playlist: https://open.spotify.com/playlist/{playlist_id}")
        return 1 if failed else 0

    failures = 0
    urls: list[str] = []
    for part, group in enumerate(groups, start=1):
        name = base_name if len(groups) == 1 else f"{base_name} ({part}/{len(groups)})"
        part_description = description
        if len(groups) > 1:
            part_description = f"{description} Part {part} of {len(groups)}."
        print(f"Creating playlist {name!r}...", file=sys.stderr)
        playlist = client.create_playlist(name, args.public, part_description)
        playlist_id = playlist.get("id")
        if not playlist_id:
            raise SpotifyError("Spotify did not return a playlist ID.")
        failures += len(add_with_progress(client, str(playlist_id), [t.uri for t in group]))
        url = playlist_url(playlist)
        urls.append(url)
        print(f"Playlist: {url}", file=sys.stderr)

    print("\nDone.")
    for url in urls:
        print(url)
    return 1 if failures else 0


def add_auth_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--client-id",
        default=os.environ.get("SPOTIFY_CLIENT_ID"),
        help="Spotify app client ID (or set SPOTIFY_CLIENT_ID).",
    )
    parser.add_argument(
        "--redirect-uri",
        default=DEFAULT_REDIRECT_URI,
        help=f"Registered redirect URI (default: {DEFAULT_REDIRECT_URI}).",
    )
    parser.add_argument(
        "--token-cache",
        default=str(default_token_path()),
        help="Where to cache OAuth tokens.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the authorization URL instead of opening a browser.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spotify_artist_playlist.py",
        description=(
            "Create a Spotify playlist of an artist's songs in release order with no duplicates."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth_parser = subparsers.add_parser(
        "auth",
        help="Authorize with Spotify and cache a token.",
    )
    add_auth_args(auth_parser)
    auth_parser.set_defaults(func=cmd_auth)

    create_parser = subparsers.add_parser(
        "create",
        help="Build a release-ordered playlist for an artist.",
    )
    create_parser.add_argument("artist", help="Artist name, Spotify ID, URI, or URL.")
    create_parser.add_argument(
        "--artist-index",
        type=int,
        metavar="N",
        help="Pick result N when a name search returns several artists.",
    )
    create_parser.add_argument(
        "--include-groups",
        default=",".join(DEFAULT_GROUPS),
        metavar="GROUPS",
        help=(
            "Comma-separated release types to scan: album,single,compilation,appears_on "
            f"(default: {','.join(DEFAULT_GROUPS)})."
        ),
    )
    create_parser.add_argument(
        "--market",
        metavar="CODE",
        help="ISO 3166-1 alpha-2 market for catalog lookups (default: all markets).",
    )
    create_parser.add_argument(
        "--dedupe",
        choices=DEDUPE_CHOICES,
        default="uri",
        help=(
            "Duplicate definition: uri keeps distinct recordings, name collapses identical "
            "titles, normalized also ignores featuring/version suffixes (default: uri)."
        ),
    )
    create_parser.add_argument(
        "--reverse",
        action="store_true",
        help="Newest first instead of oldest first.",
    )
    create_parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Keep only the first N tracks after ordering and deduplication.",
    )
    create_parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Delay between release lookups (default: 0).",
    )
    create_parser.add_argument(
        "--name",
        help="Playlist name (default: '<Artist> (Release Order)').",
    )
    create_parser.add_argument(
        "--description",
        help="Playlist description.",
    )
    create_parser.add_argument(
        "--public",
        dest="public",
        action="store_true",
        help="Make the playlist public.",
    )
    create_parser.add_argument(
        "--private",
        dest="public",
        action="store_false",
        help="Make the playlist private (default).",
    )
    create_parser.set_defaults(public=False)
    create_parser.add_argument(
        "--append-to",
        metavar="PLAYLIST_ID",
        help="Append to an existing playlist instead of creating one.",
    )
    create_parser.add_argument(
        "--max-tracks",
        type=int,
        default=PLAYLIST_MAX,
        metavar="N",
        help=f"Max items per playlist; larger sets are split (default: {PLAYLIST_MAX}).",
    )
    create_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the ordered track list without changing Spotify.",
    )
    create_parser.add_argument(
        "--format",
        choices=("text", "json", "csv"),
        default="text",
        help="Dry-run output format (default: text).",
    )
    create_parser.add_argument(
        "--output",
        "-o",
        help="Write the dry-run track list to this file instead of stdout.",
    )
    add_auth_args(create_parser)
    create_parser.set_defaults(func=cmd_create)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "max_tracks", None) is not None and not 1 <= args.max_tracks <= PLAYLIST_MAX:
        parser.error(f"--max-tracks must be between 1 and {PLAYLIST_MAX}")
    if getattr(args, "limit", None) is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if getattr(args, "delay", None) is not None and args.delay < 0:
        parser.error("--delay cannot be negative")
    if getattr(args, "artist_index", None) is not None and args.artist_index < 1:
        parser.error("--artist-index must be at least 1")
    if getattr(args, "include_groups", None) is not None:
        groups = [group.strip() for group in args.include_groups.split(",") if group.strip()]
        invalid = [group for group in groups if group not in ALBUM_GROUPS]
        if invalid or not groups:
            parser.error(
                "--include-groups must be a comma-separated list of: " + ",".join(ALBUM_GROUPS)
            )
        args.include_groups = groups
    try:
        return int(args.func(args))
    except (SpotifyError, FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
