"""MusicBrainz enrichment for the downloader.

Given an artist + title (and optionally an album hint), look up the
canonical metadata the Echo wants: album, album artist, date, genre,
track and disc numbers, and a cover-art URL.

Selection policy:
    - Skim search hits down to recordings that have at least one
      'Album'-primary-type release with NO disfavoured secondary types
      (Compilation, Live, Soundtrack, Single, EP). With an album hint we
      relax this and trust the user.
    - Among the candidate's releases, pick the earliest date — the
      original studio appearance, not the 2015 remaster.
    - For genre, take the highest-voted tag on the recording; fall back to
      release-group tags, then artist tags. MB tags are community-supplied
      so they're noisy, but they're real-world labels ('rock',
      'progressive metal') and that's what the Echo will display.

Rate limit: MusicBrainz requires no more than one request per second from
unauthenticated callers. musicbrainzngs handles this for us once
configured.

Call pattern per song: 1 search + 1 recording lookup + 1 release lookup =
3 requests ≈ 3 seconds at the rate limit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import musicbrainzngs

# Cover Art Archive serves resized variants on these endpoints.
_COVER_ART_URL = "https://coverartarchive.org/release/{release_id}/front-500"

_UA_NAME = "echo-library-builder"
_UA_VERSION = "0.1"
_UA_CONTACT = "https://github.com/local/echo-library-builder"

# Secondary types we'd rather skip when no album hint was given. The
# release-group's primary-type is almost always 'Album' even for these
# (since they're album-shaped releases), so the secondary type is the
# real discriminator.
_DISFAVORED_SECONDARY = {
    "Compilation", "Live", "Soundtrack", "Interview", "DJ-mix",
    "Mixtape/Street", "Demo", "Audio drama", "Remix",
}


@dataclass(frozen=True)
class Enriched:
    artist: str
    album: str
    title: str
    album_artist: str
    track_no: int | None
    disc_no: int | None
    date: str | None         # YYYY or YYYY-MM-DD
    genre: str | None
    cover_art_url: str | None
    duration_ms: int | None  # for the downloader's duration sanity check
    musicbrainz_recording_id: str
    musicbrainz_release_id: str


_configured = False


def _ensure_configured() -> None:
    global _configured
    if _configured:
        return
    musicbrainzngs.set_useragent(_UA_NAME, _UA_VERSION, _UA_CONTACT)
    musicbrainzngs.set_rate_limit(limit_or_interval=1.0, new_requests=1)
    _configured = True


def enrich(artist: str, title: str, album_hint: str | None = None) -> Enriched | None:
    """Look up MB metadata for one song. Returns None if nothing matched
    or the MB service rejected the query."""
    _ensure_configured()

    query_parts = [f'recording:"{_escape(title)}"', f'artist:"{_escape(artist)}"']
    if album_hint:
        query_parts.append(f'release:"{_escape(album_hint)}"')
    query = " AND ".join(query_parts)

    try:
        search = musicbrainzngs.search_recordings(query=query, limit=10)
    except musicbrainzngs.WebServiceError:
        return None
    recordings = search.get("recording-list", [])
    if not recordings:
        return None

    # Pick a recording that has at least one acceptable release in the
    # search-response release-list. With no album hint and a popular live
    # track, every top recording can be a separate live show entity —
    # then we fall back to the top-scored hit and rely on the release
    # filter (over the full release-list from the lookup) to do better.
    # For best results on obscure tracks, callers should pass an album
    # hint — the 'Artist - Album - Title' input form does that.
    chosen = _pick_recording(recordings, album_hint)
    if chosen is None:
        return None

    # Full recording lookup gives us tags and the complete release list
    # with proper release-group typing. Search results lack both.
    try:
        full = musicbrainzngs.get_recording_by_id(
            chosen["id"],
            # 'release-groups' is invalid on the recording endpoint; the
            # release-list returned via 'releases' already nests release-
            # group info inline. We'd also like 'genres' (curated genre
            # tags) but this musicbrainzngs version doesn't expose it,
            # so we filter the community 'tags' list in _pick_genre to
            # drop the obvious chart-position noise ("1-4 Wochen", etc.).
            includes=["releases", "tags", "artist-credits"],
        )["recording"]
    except musicbrainzngs.WebServiceError:
        return None

    release = _pick_release(full.get("release-list", []), album_hint)
    if release is None:
        return None

    # Release lookup gives us the medium-list with track positions and the
    # release's own tags (sometimes richer than the recording's).
    try:
        release_full = musicbrainzngs.get_release_by_id(
            release["id"],
            includes=["recordings", "media", "release-groups",
                      "tags", "artist-credits"],
        )["release"]
    except musicbrainzngs.WebServiceError:
        release_full = release

    disc_no, track_no = _track_position(release_full, full["id"])
    album_artist = _artist_credit_phrase(release_full.get("artist-credit", []))
    recording_artist = _artist_credit_phrase(full.get("artist-credit", [])) or artist
    date = release_full.get("date") or full.get("first-release-date")
    duration_ms = int(full["length"]) if full.get("length") else None

    genre = _pick_genre(full, release_full)

    cover_url = _COVER_ART_URL.format(release_id=release_full["id"])

    return Enriched(
        artist=recording_artist,
        album=release_full.get("title") or "",
        title=full.get("title", title),
        album_artist=album_artist or recording_artist,
        track_no=track_no,
        disc_no=disc_no,
        date=date,
        genre=genre,
        cover_art_url=cover_url,
        duration_ms=duration_ms,
        musicbrainz_recording_id=full["id"],
        musicbrainz_release_id=release_full["id"],
    )


def _escape(s: str) -> str:
    """Lucene-escape special chars in a MB search query value."""
    return re.sub(r'([+\-!(){}\[\]^"~*?:\\/])', r"\\\1", s)


def _pick_recording(recordings: list[dict], album_hint: str | None) -> dict | None:
    """Among search hits, pick a recording with at least one acceptable
    release. Acceptable = primary 'Album' with no disfavored secondary.
    Falls back to the top-scored hit if no recording is acceptable."""
    if album_hint:
        return recordings[0]

    def is_acceptable(rec: dict) -> bool:
        for rel in rec.get("release-list", []):
            rg = rel.get("release-group") or {}
            if _is_acceptable_release_group(rg):
                return True
        return False

    for rec in recordings:
        if is_acceptable(rec):
            return rec
    return recordings[0]


def _is_acceptable_release_group(rg: dict) -> bool:
    primary = rg.get("primary-type") or rg.get("type") or ""
    if primary != "Album":
        return False
    secondary = set(rg.get("secondary-type-list") or [])
    if secondary & _DISFAVORED_SECONDARY:
        return False
    return True


def _pick_release(releases: list[dict], album_hint: str | None) -> dict | None:
    if not releases:
        return None
    if album_hint:
        h = album_hint.lower().strip()
        exact = [r for r in releases if (r.get("title") or "").lower() == h]
        if exact:
            return _earliest(exact)
        partial = [r for r in releases if h in (r.get("title") or "").lower()]
        if partial:
            return _earliest(partial)
        return _earliest(releases)

    acceptable = [r for r in releases
                  if _is_acceptable_release_group(r.get("release-group") or {})]
    if acceptable:
        return _earliest(acceptable)
    # Nothing passes the studio-album filter — accept any non-disfavored,
    # else accept anything.
    not_disfavored = [
        r for r in releases
        if not (set((r.get("release-group") or {})
                    .get("secondary-type-list") or [])
                & _DISFAVORED_SECONDARY)
    ]
    return _earliest(not_disfavored or releases)


def _earliest(releases: list[dict]) -> dict:
    return min(releases, key=_release_date_key)


def _release_date_key(release: dict) -> str:
    return release.get("date") or "9999"


def _track_position(release: dict, recording_id: str) -> tuple[int | None, int | None]:
    """Return (disc, track) of `recording_id` within `release`."""
    for medium in release.get("medium-list", []):
        for track in medium.get("track-list", []):
            if track.get("recording", {}).get("id") == recording_id:
                return (
                    _to_int(medium.get("position")),
                    _to_int(track.get("position") or track.get("number")),
                )
    return None, None


def _artist_credit_phrase(credits: list) -> str | None:
    """Reassemble an artist-credit array into a display string. MB credits
    are an alternating sequence of {artist} dicts and join-phrase strings
    like ' feat. '."""
    if not credits:
        return None
    parts: list[str] = []
    for item in credits:
        if isinstance(item, dict):
            if "name" in item:
                parts.append(item["name"])
            else:
                parts.append(item.get("artist", {}).get("name", ""))
            if item.get("joinphrase"):
                parts.append(item["joinphrase"])
        elif isinstance(item, str):
            parts.append(item)
    return "".join(parts).strip() or None


def _pick_genre(recording: dict, release: dict) -> str | None:
    """Walk the recording's, release-group's, and release's community
    `tag-list` in that priority order, taking the highest-voted tag
    that *looks* like a genre. Title-cases the result so
    'progressive rock' becomes 'Progressive Rock' (matches DAP display).

    The look-like-a-genre filter drops tags with digits (chart positions
    like "1-4 Wochen", "top 10", "2000s") and over-long descriptive notes,
    which is most of what makes MB user tags noisy. We'd prefer the
    curated `genre-list` but musicbrainzngs in this venv doesn't expose
    it.
    """
    for source in (recording,
                   release.get("release-group") or {},
                   release):
        tags = source.get("tag-list") or []
        if not tags:
            continue
        for t in sorted(tags, key=lambda t: -int(t.get("count", 0))):
            name = (t.get("name") or "").strip()
            if name and _looks_like_a_genre(name):
                return name.title()
    return None


def _looks_like_a_genre(name: str) -> bool:
    """Heuristic — reject obvious non-genre MB tags.

    Filters out, in priority:
    - Digits → chart positions ('1-4 Wochen'), year markers ('2000s'),
      playlist counts.
    - >40 chars → descriptive notes.
    - 4+ words → almost always album titles or descriptive phrases
      ('Wild Eyed And Live'). Real genres top out around 3 words
      ('Progressive Death Metal').
    - Ends in '!' or '?' → not a genre name format.
    """
    if not name:
        return False
    if any(ch.isdigit() for ch in name):
        return False
    if len(name) > 40:
        return False
    if name.endswith(("!", "?")):
        return False
    # Word count — hyphenated single-token compounds count as one word.
    word_count = len(name.split())
    if word_count >= 4:
        return False
    return True


def _to_int(val) -> int | None:
    if val is None:
        return None
    try:
        return int(str(val).split("/")[0])
    except (ValueError, TypeError):
        return None
