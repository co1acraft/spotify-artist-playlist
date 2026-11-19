# Spotify Artist Playlist

> [!WARNING]
> this completely vibecoded and there may be issues
> who knows

A dependency-free Python tool that creates a Spotify playlist containing one artist's songs in **release order**, with **no duplicate songs**. Requires Python 3.10 or newer and no third-party packages.

It reads the artist's full Spotify catalog (albums, singles, compilations, and optionally guest appearances), expands every release into tracks, keeps only tracks credited to that artist, sorts by release date, removes duplicates, and writes the result to a new playlist.

## Quick start

```bash
# Preview the ordered, deduplicated track list without changing Spotify
python3 spotify_artist_playlist.py create "Radiohead" --dry-run

# Create the playlist
python3 spotify_artist_playlist.py create "Radiohead" --client-id YOUR_SPOTIFY_CLIENT_ID
```

An ambiguous artist name is never guessed: you'll get a numbered list and can re-run with `--artist-index 2`, or pass the artist's Spotify ID, URI, or URL directly.

```bash
python3 spotify_artist_playlist.py create "https://open.spotify.com/artist/a74b1b7f71a521c9d35ee7" \
  --client-id YOUR_SPOTIFY_CLIENT_ID
```

The first run opens your browser for Spotify authorization and caches the token at `~/.config/spotify-unique-playlist/token.json`. That is the same cache used by the sibling `spotify_playlist.py` tool, so you only need to authorize once. Use `--token-cache` to change the location.

## Spotify app setup

Creating a playlist requires a Spotify app (free to create) and a user login:

1. Open the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and create an app.
2. Add this exact redirect URI in the app settings:
   ```
   http://127.0.0.1:8888/callback
   ```
3. Copy the app's **Client ID** and pass it with `--client-id`, or set `SPOTIFY_CLIENT_ID`.
4. Development Mode apps require the app owner to have an active Spotify Premium subscription. If you are using someone else's app, your account must be added under that app's user management.

No client secret is needed. The tool uses OAuth 2.0 Authorization Code with PKCE and requests only `playlist-modify-public` and `playlist-modify-private`.

## Commands

### `create`

```bash
python3 spotify_artist_playlist.py create <artist> --client-id ID [options]
```

| Option | Description |
| --- | --- |
| `--artist-index N` | Pick result N when a name search finds several artists. |
| `--include-groups GROUPS` | Release types to scan. Default: `album,single,compilation`. Add `appears_on` for guest features. |
| `--market CODE` | ISO 3166-1 alpha-2 market for catalog lookups (default: all markets). |
| `--dedupe MODE` | `uri`, `name`, or `normalized`. Default: `uri`. See below. |
| `--reverse` | Newest first instead of oldest first. |
| `--limit N` | Keep only the first N tracks after sorting and deduplication. |
| `--delay SECONDS` | Pause between release lookups (default: 0). |
| `--name NAME` | Playlist name. Default: `<Artist> (Release Order)`. |
| `--description TEXT` | Playlist description. |
| `--public` / `--private` | Visibility. Default: private. |
| `--append-to PLAYLIST_ID` | Append to an existing playlist instead of creating one. |
| `--max-tracks N` | Items per playlist; larger sets are split into numbered parts. Default/maximum: `10000`. |
| `--dry-run` | Show the ordered track list without writing to Spotify. |
| `--format text\|json\|csv` | Dry-run output format. Default: `text`. |
| `-o, --output FILE` | Write the dry-run track list to a file instead of stdout. |
| `--client-id ID` | Spotify app client ID. Can also use `SPOTIFY_CLIENT_ID`. |
| `--redirect-uri URI` | OAuth redirect URI. Default: `http://127.0.0.1:8888/callback`. |
| `--token-cache FILE` | Token cache location. |
| `--no-browser` | Print the authorization URL instead of opening a browser. |

Supplying `--output` or `--format json|csv` implies `--dry-run`.

### `auth`

Authorize once and cache the token without creating anything:

```bash
python3 spotify_artist_playlist.py auth --client-id YOUR_SPOTIFY_CLIENT_ID
```

## How release order works

Releases are ordered by Spotify's `release_date`, using `release_date_precision` so that `1988`, `1988-06`, and `1988-06-15` all compare correctly. Tracks within a release follow disc number, then track number. Releases with the same date are ordered by album name for stable results, and releases with no date go last.

Because Spotify assigns each release its own date, a reissue or compilation can place an older song later in the playlist. Use `--dry-run --format csv` to inspect the dates before creating anything.

## Duplicate handling

| Mode | Behavior |
| --- | --- |
| `uri` (default) | One entry per Spotify track ID. The same recording reused on a single and an album appears once; distinct recordings such as live versions and remasters are kept. |
| `name` | One entry per case-insensitive track title, regardless of recording. |
| `normalized` | Like `name`, but also ignores featuring credits and trailing parenthetical versions such as `(Remastered 2019)` or `(Live)`. |

The first (earliest-release) occurrence is kept in every mode. Duplicates are removed *after* sorting, so the playlist preserves the song's original release position.

## Filtering

Only tracks whose `artists` list contains the selected artist are included. This drops other artists' tracks from compilations and from the artist's own albums. By default the tool scans albums, singles, and compilations. Add `--include-groups appears_on` to include songs the artist guested on; those are still filtered to tracks credited to the artist.

## Limits and troubleshooting

- Spotify caps a playlist at 10,000 items. Larger sets are split automatically into `Name (1/3)`, `Name (2/3)`, and so on. `--append-to` cannot be combined with splitting.
- If a track is unavailable in your market, try `--market US` (or your own market code).
- **`403 Forbidden`**: confirm the app owner has Premium, your account is allowed to use the app, and the app has both playlist-modify scopes.
- **`INVALID_CLIENT: Invalid redirect URI`**: the URI in the dashboard must exactly match `--redirect-uri`.
- **Quota/rate limiting**: the tool waits on normal `429` responses. If Spotify reports `QUOTA_EXCEEDED`, stop and retry later rather than hammering the API.
- **Stale authorization**: delete the token cache file and run `auth` again.

## Testing

```bash
python3 -m unittest discover -s tests -t . -v
```

## License

MIT. See [LICENSE](LICENSE).
