"""Read source tags and write Echo-friendly tags on the output.

Echo quirks we care about:
- FLAC must use Vorbis comments only; embedded ID3v2 confuses some firmwares
- TRACKNUMBER must be the bare number, not 'N/Total'
- Cover art is embedded as a FLAC METADATA_BLOCK_PICTURE (front cover)
- MP3 mirror uses ID3v2.3 (Echo reads it; v2.4 sometimes mis-parses)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK
from mutagen.mp3 import MP3

# Common filename patterns we fall back to when tags are missing.
# Examples:
#   "03 - The Beatles - Yesterday.flac"  -> n=3, artist=The Beatles, title=Yesterday
#   "12 - Stairway to Heaven.flac"       -> n=12, title=Stairway to Heaven
_FNAME_RE_FULL = re.compile(r"^\s*(\d+)\s*[-_.]\s*(.+?)\s*[-_.]\s*(.+?)\s*$")
_FNAME_RE_SHORT = re.compile(r"^\s*(\d+)\s*[-_.]\s*(.+?)\s*$")


@dataclass
class SourceTags:
    artist: str = "Unknown Artist"
    album: str = "Unknown Album"
    title: str = "Unknown Title"
    track_no: int | None = None
    disc_no: int | None = None
    date: str | None = None
    genre: str | None = None
    album_artist: str | None = None


def _first(d, *keys) -> str | None:
    for k in keys:
        v = d.get(k)
        if v:
            if isinstance(v, list):
                v = v[0]
            return str(v).strip() or None
    return None


def _track_int(raw) -> int | None:
    if raw is None:
        return None
    s = str(raw).split("/")[0].strip()
    try:
        return int(s)
    except ValueError:
        return None


def from_filename(path: Path) -> tuple[int | None, str | None, str | None]:
    stem = path.stem
    m = _FNAME_RE_FULL.match(stem)
    if m:
        return int(m.group(1)), m.group(2), m.group(3)
    m = _FNAME_RE_SHORT.match(stem)
    if m:
        return int(m.group(1)), None, m.group(2)
    return None, None, stem


def read_source(path: Path, fallback_album: str, disc_hint: int | None) -> SourceTags:
    """Read tags from any mutagen-recognised audio file (FLAC, MP3, M4A,
    OGG, APE, WAV) and backfill missing values from the filename.

    Different formats use different key cases and tag layers; we normalise
    by trying both Vorbis-style ("ARTIST"/"artist") and ID3 frame IDs
    ("TPE1") for each field. mutagen.File() auto-detects the format.
    """
    import mutagen
    try:
        f = mutagen.File(path)
    except Exception:
        f = None
    if f is None or f.tags is None:
        t = _PseudoTags({})
    else:
        t = _normalise_tags(f.tags)

    fn_n, fn_artist, fn_title = from_filename(path)
    # Three tag schemes to cover: Vorbis (FLAC/OGG, case varies),
    # ID3 frames (MP3), and Apple atoms (M4A — '©' is the iTunes prefix
    # for standard metadata atoms).
    out = SourceTags(
        artist=_first(t, "artist", "ARTIST", "TPE1", "\xa9ART")
               or fn_artist or "Unknown Artist",
        album=_first(t, "album", "ALBUM", "TALB", "\xa9alb")
              or fallback_album,
        title=_first(t, "title", "TITLE", "TIT2", "\xa9nam")
              or fn_title or path.stem,
        track_no=_track_int(_first(t, "tracknumber", "TRACKNUMBER", "TRCK", "trkn")) or fn_n,
        disc_no=_track_int(_first(t, "discnumber", "DISCNUMBER", "TPOS", "disk")) or disc_hint,
        date=_first(t, "date", "DATE", "year", "YEAR", "TDRC", "\xa9day"),
        genre=_first(t, "genre", "GENRE", "TCON", "\xa9gen"),
        album_artist=_first(t, "albumartist", "ALBUMARTIST", "album artist",
                            "TPE2", "aART"),
    )
    return out


class _PseudoTags(dict):
    """Dict-with-a-get that mutagen.File() returns are sometimes typed
    as — keeps _first() happy."""


def _normalise_tags(raw) -> _PseudoTags:
    """Turn whatever mutagen returns into a flat dict whose `get(key)`
    returns a string or list of strings. Handles Vorbis comments, ID3
    frames, MP4 atoms (including (N, total) tuples for trkn/disk), and
    APEv2 — all of which expose slightly different surfaces."""
    if hasattr(raw, "items"):
        out: dict = {}
        for k, v in raw.items():
            if hasattr(v, "text"):
                # ID3 frame: TPE1, TALB, etc. → .text is a list of strings
                val = list(v.text) if v.text else []
            elif isinstance(v, (list, tuple)):
                val = []
                for x in v:
                    if isinstance(x, (list, tuple)) and x:
                        # MP4 trkn/disk: take the leading number, drop
                        # the (optional) "of total".
                        val.append(str(x[0]))
                    else:
                        val.append(str(x))
            else:
                val = [str(v)]
            out[k] = val
        return _PseudoTags(out)
    return _PseudoTags({})


def write_flac(target: Path, tags: SourceTags, picture_bytes: bytes | None) -> None:
    """Write clean Vorbis comments on an output FLAC. Removes any ID3v2 block."""
    f = FLAC(target)
    # Ensure a fresh Vorbis comment block. f.delete() empties but does NOT
    # remove the block, so add_tags() would raise FLACVorbisError on the
    # second pass — clear by hand instead.
    if f.tags is None:
        f.add_tags()
    else:
        f.tags.clear()

    f.tags["ARTIST"] = tags.artist
    f.tags["ALBUM"] = tags.album
    f.tags["TITLE"] = tags.title
    if tags.track_no is not None:
        f.tags["TRACKNUMBER"] = str(tags.track_no)
    if tags.disc_no is not None:
        f.tags["DISCNUMBER"] = str(tags.disc_no)
    if tags.date:
        f.tags["DATE"] = tags.date
    if tags.genre:
        f.tags["GENRE"] = tags.genre
    if tags.album_artist:
        f.tags["ALBUMARTIST"] = tags.album_artist

    f.clear_pictures()
    if picture_bytes:
        pic = Picture()
        pic.type = 3  # front cover
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        pic.data = picture_bytes
        f.add_picture(pic)

    f.save()


def write_mp3(target: Path, tags: SourceTags, picture_bytes: bytes | None) -> None:
    """Write ID3v2.3 tags on an output MP3."""
    audio = MP3(target, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()
    # Clear in-memory frames (e.g. the ffmpeg-written TSSE). Don't call
    # tags.delete() — in mutagen >=1.46 it requires a filename arg and the
    # bare call raises 'Missing filename or fileobj argument'.
    audio.tags.clear()
    audio.tags.add(TPE1(encoding=3, text=tags.artist))
    audio.tags.add(TALB(encoding=3, text=tags.album))
    audio.tags.add(TIT2(encoding=3, text=tags.title))
    if tags.track_no is not None:
        audio.tags.add(TRCK(encoding=3, text=str(tags.track_no)))
    if tags.disc_no is not None:
        audio.tags.add(TPOS(encoding=3, text=str(tags.disc_no)))
    if tags.album_artist:
        audio.tags.add(TPE2(encoding=3, text=tags.album_artist))
    if tags.date:
        audio.tags.add(TDRC(encoding=3, text=tags.date))
    if tags.genre:
        audio.tags.add(TCON(encoding=3, text=tags.genre))
    if picture_bytes:
        audio.tags.add(
            APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=picture_bytes)
        )
    # v2.3 for max compatibility on small DAPs
    audio.tags.update_to_v23()
    audio.save(v2_version=3)


def write_m4a(target: Path, tags: SourceTags, picture_bytes: bytes | None) -> None:
    """Write iTunes-style metadata atoms on an M4A/MP4 file."""
    from mutagen.mp4 import MP4, MP4Cover
    audio = MP4(target)
    if audio.tags is None:
        audio.add_tags()
    audio.tags.clear()
    audio.tags["\xa9ART"] = tags.artist
    audio.tags["\xa9alb"] = tags.album
    audio.tags["\xa9nam"] = tags.title
    if tags.track_no is not None:
        audio.tags["trkn"] = [(int(tags.track_no), 0)]
    if tags.disc_no is not None:
        audio.tags["disk"] = [(int(tags.disc_no), 0)]
    if tags.date:
        audio.tags["\xa9day"] = tags.date
    if tags.genre:
        audio.tags["\xa9gen"] = tags.genre
    if tags.album_artist:
        audio.tags["aART"] = tags.album_artist
    if picture_bytes:
        audio.tags["covr"] = [
            MP4Cover(picture_bytes, imageformat=MP4Cover.FORMAT_JPEG),
        ]
    audio.save()


def write_ogg(target: Path, tags: SourceTags, picture_bytes: bytes | None) -> None:
    """Write Vorbis comments on an OGG Vorbis file. Cover art uses the
    same METADATA_BLOCK_PICTURE convention as FLAC, base64-encoded."""
    import base64
    from mutagen.oggvorbis import OggVorbis
    audio = OggVorbis(target)
    if audio.tags is None:
        audio.add_tags()
    audio.tags.clear()
    audio.tags["ARTIST"] = tags.artist
    audio.tags["ALBUM"] = tags.album
    audio.tags["TITLE"] = tags.title
    if tags.track_no is not None:
        audio.tags["TRACKNUMBER"] = str(tags.track_no)
    if tags.disc_no is not None:
        audio.tags["DISCNUMBER"] = str(tags.disc_no)
    if tags.date:
        audio.tags["DATE"] = tags.date
    if tags.genre:
        audio.tags["GENRE"] = tags.genre
    if tags.album_artist:
        audio.tags["ALBUMARTIST"] = tags.album_artist
    if picture_bytes:
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        pic.data = picture_bytes
        audio.tags["METADATA_BLOCK_PICTURE"] = [
            base64.b64encode(pic.write()).decode("ascii"),
        ]
    audio.save()


# Format → writer dispatch. Used by the preserve build path to retag
# whatever ffmpeg or shutil dropped on disk.
_WRITERS = {
    "flac": write_flac,
    "mp3": write_mp3,
    "m4a": write_m4a,
    "mp4": write_m4a,
    "ogg": write_ogg,
}


def write_tags(target: Path, tags: SourceTags,
               picture_bytes: bytes | None) -> bool:
    """Dispatch to the right tag writer based on the target's extension.
    Returns True if a writer was found and ran, False if we don't have
    one for this format (DSF, APE, WAV — caller can decide whether to
    silently skip or raise)."""
    ext = target.suffix.lower().lstrip(".")
    writer = _WRITERS.get(ext)
    if writer is None:
        return False
    writer(target, tags, picture_bytes)
    return True
