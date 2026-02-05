import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
import base64
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from mutagen.id3 import APIC, COMM, ID3, TALB, TIT2, TLEN, TPE1, TPOS, TRCK, TDRC, TXXX, USLT
from mutagen.mp4 import MP4, MP4Cover
from mutagen.mp3 import MP3
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

APP_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIST = os.path.join(APP_DIR, "..", "frontend", "dist")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SPOTIFY_TOKEN_CACHE: dict[str, float | str | None] = {"access_token": None, "expires_at": 0.0}
PLAYLIST_JOBS: dict[str, dict] = {}
PLAYLIST_JOBS_LOCK = threading.Lock()
OUTPUT_FORMATS = {"best", "mp3", "m4a", "opus"}
FALLBACK_FORMAT_ORDER = ["best", "m4a", "opus", "mp3"]
_COOKIEFILE_CACHE_PATH: Optional[str] = None


@dataclass
class MediaMeta:
    input_text: str
    source: str
    title: str
    artist: Optional[str] = None
    album: Optional[str] = None
    cover_url: Optional[str] = None
    media_type: Optional[str] = None
    query: Optional[str] = None
    duration_seconds: Optional[int] = None
    release_date: Optional[str] = None
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    description: Optional[str] = None
    youtube_url: Optional[str] = None
    youtube_id: Optional[str] = None
    channel: Optional[str] = None
    lyrics: Optional[str] = None
    lyrics_source: Optional[str] = None
    extra_tags: dict[str, str] = field(default_factory=dict)


def _is_spotify_url(value: str) -> bool:
    return bool(re.search(r"https?://(open\.)?spotify\.com/", value, re.IGNORECASE))


def _is_youtube_url(value: str) -> bool:
    return bool(re.search(r"https?://(www\.)?(youtube\.com|youtu\.be)/", value, re.IGNORECASE))


def _is_spotify_playlist_url(value: str) -> bool:
    return bool(re.search(r"spotify\.com/playlist/", value, re.IGNORECASE))


def _fetch_soup(url: str) -> Optional[BeautifulSoup]:
    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code != 200:
        return None
    return BeautifulSoup(r.text, "html.parser")


def _get_oembed(url: str) -> dict:
    r = requests.get("https://open.spotify.com/oembed", params={"url": url}, timeout=15)
    if r.status_code != 200:
        return {}
    return r.json()


def _parse_open_graph(soup: Optional[BeautifulSoup]) -> dict:
    if not soup:
        return {}
    og = {}
    for tag in soup.find_all("meta"):
        prop = tag.get("property") or tag.get("name")
        content = tag.get("content")
        if not prop or not content:
            continue
        if prop.startswith("og:") or prop.startswith("music:"):
            og[prop] = content
    return og


def _parse_json_ld(soup: Optional[BeautifulSoup]) -> list[dict]:
    if not soup:
        return []
    items: list[dict] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = (script.string or "").strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            items.append(payload)
        elif isinstance(payload, list):
            items.extend([x for x in payload if isinstance(x, dict)])
    return items


def _parse_artist_and_title(raw_title: str) -> tuple[Optional[str], str]:
    if " - " in raw_title:
        parts = [p.strip() for p in raw_title.split(" - ", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            return parts[1], parts[0]
    return None, raw_title.strip()


def _guess_artist_and_title(meta: MediaMeta) -> tuple[Optional[str], Optional[str]]:
    if meta.artist and meta.title:
        return meta.artist, meta.title
    if meta.title and " - " in meta.title:
        parsed_artist, parsed_title = _parse_artist_and_title(meta.title)
        if parsed_artist and parsed_title:
            return parsed_artist, parsed_title
    return meta.artist, meta.title


def _normalize_lyrics(text: str) -> str:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _fetch_lyrics_from_url(url: str) -> Optional[str]:
    try:
        res = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
    except requests.RequestException:
        return None
    if res.status_code != 200:
        return None
    try:
        payload = res.json()
    except ValueError:
        return None
    lyrics = (payload.get("lyrics") or "").strip()
    if not lyrics:
        return None
    return _normalize_lyrics(lyrics)


def _fetch_lyrics(meta: MediaMeta) -> tuple[Optional[str], Optional[str]]:
    artist, title = _guess_artist_and_title(meta)
    if not artist or not title:
        return None, None

    artist_q = quote(artist, safe="")
    title_q = quote(title, safe="")
    providers = [
        ("lyrics.ovh", f"https://api.lyrics.ovh/v1/{artist_q}/{title_q}"),
        ("lyrist", f"https://lyrist.vercel.app/api/{title_q}/{artist_q}"),
    ]

    for name, url in providers:
        lyrics = _fetch_lyrics_from_url(url)
        if lyrics:
            return lyrics, name

    return None, None


def _build_lrc(meta: MediaMeta) -> Optional[str]:
    if not meta.lyrics:
        return None
    lines = [line.strip() for line in meta.lyrics.split("\n")]
    header = []
    if meta.artist:
        header.append(f"[ar:{meta.artist}]")
    if meta.title:
        header.append(f"[ti:{meta.title}]")
    body = []
    for line in lines:
        if not line:
            body.append("")
        else:
            body.append(f"[00:00.00]{line}")
    content = "\n".join(header + body).strip()
    return content or None


def _spotify_id(url: str) -> Optional[str]:
    m = re.search(r"spotify\.com/(track|album|playlist|episode|show)/([a-zA-Z0-9]+)", url)
    if not m:
        return None
    return m.group(2)


def _spotify_kind(url: str) -> Optional[str]:
    m = re.search(r"spotify\.com/(track|album|playlist|episode|show)/", url)
    return m.group(1) if m else None


def _yt_cookiefile_path() -> Optional[str]:
    global _COOKIEFILE_CACHE_PATH
    if _COOKIEFILE_CACHE_PATH and os.path.exists(_COOKIEFILE_CACHE_PATH):
        return _COOKIEFILE_CACHE_PATH

    cookie_path = os.getenv("YTDLP_COOKIES")
    if cookie_path and os.path.exists(cookie_path):
        _COOKIEFILE_CACHE_PATH = cookie_path
        return cookie_path

    cookie_b64 = os.getenv("YTDLP_COOKIES_B64")
    if cookie_b64:
        try:
            data = base64.b64decode(cookie_b64).decode("utf-8")
            path = "/tmp/ytdlp-cookies.txt"
            with open(path, "w", encoding="utf-8") as f:
                f.write(data)
            _COOKIEFILE_CACHE_PATH = path
            return path
        except Exception:
            return None

    return None


def _is_yt_bot_block_error(err: Exception) -> bool:
    msg = str(err).lower()
    markers = [
        "sign in to confirm you're not a bot",
        "use --cookies-from-browser or --cookies",
        "confirm you’re not a bot",
    ]
    return any(token in msg for token in markers)


def _best_spotify_cover_url(oembed: dict, og: dict) -> Optional[str]:
    # og:image is usually higher quality than oEmbed thumbnail.
    return og.get("og:image") or oembed.get("thumbnail_url")


def _spotify_playlist_id(url: str) -> Optional[str]:
    m = re.search(r"spotify\.com/playlist/([a-zA-Z0-9]+)", url)
    if not m:
        return None
    return m.group(1)


def _extract_spotify_json_ld_info(json_ld_items: list[dict]) -> dict:
    info: dict[str, Optional[str]] = {}
    for item in json_ld_items:
        artist = item.get("byArtist")
        if isinstance(artist, dict) and artist.get("name") and not info.get("artist"):
            info["artist"] = artist.get("name")
        if item.get("duration") and not info.get("duration_iso"):
            info["duration_iso"] = item.get("duration")
        if item.get("datePublished") and not info.get("release_date"):
            info["release_date"] = item.get("datePublished")
        if item.get("description") and not info.get("description"):
            info["description"] = item.get("description")
    return info


def _iso8601_to_seconds(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    total = (hours * 3600) + (minutes * 60) + seconds
    return total or None


def _spotify_client_credentials() -> tuple[Optional[str], Optional[str]]:
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    return client_id, client_secret


def _spotify_access_token() -> Optional[str]:
    client_id, client_secret = _spotify_client_credentials()
    if not client_id or not client_secret:
        return None

    now = time.time()
    cached = SPOTIFY_TOKEN_CACHE.get("access_token")
    if cached and float(SPOTIFY_TOKEN_CACHE.get("expires_at", 0.0)) > now + 30:
        return str(cached)

    token_res = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        timeout=15,
    )
    if token_res.status_code != 200:
        return None

    payload = token_res.json()
    access_token = payload.get("access_token")
    expires_in = int(payload.get("expires_in", 3600))
    if not access_token:
        return None

    SPOTIFY_TOKEN_CACHE["access_token"] = access_token
    SPOTIFY_TOKEN_CACHE["expires_at"] = now + expires_in
    return str(access_token)


def resolve_spotify(url: str) -> MediaMeta:
    soup = _fetch_soup(url)
    oembed = _get_oembed(url)
    og = _parse_open_graph(soup)
    json_ld = _parse_json_ld(soup)
    ld_info = _extract_spotify_json_ld_info(json_ld)

    title = oembed.get("title") or og.get("og:title") or "Spotify Item"
    artist = oembed.get("author_name") or ld_info.get("artist")
    cover_url = _best_spotify_cover_url(oembed, og)
    media_type = oembed.get("type") or og.get("og:type") or _spotify_kind(url)
    album = og.get("music:album")

    if not artist and title:
        parsed_artist, parsed_title = _parse_artist_and_title(title)
        artist = parsed_artist
        title = parsed_title

    duration_seconds = None
    if og.get("music:duration") and str(og.get("music:duration", "")).isdigit():
        duration_seconds = int(og["music:duration"])
    if not duration_seconds:
        duration_seconds = _iso8601_to_seconds(ld_info.get("duration_iso"))

    release_date = og.get("music:release_date") or ld_info.get("release_date")
    description = og.get("og:description") or ld_info.get("description")

    query_parts = [title]
    if artist:
        query_parts.append(artist)

    extra_tags: dict[str, str] = {}
    spotify_id = _spotify_id(url)
    if spotify_id:
        extra_tags["Spotify ID"] = spotify_id
    extra_tags["Spotify URL"] = url

    return MediaMeta(
        input_text=url,
        source="spotify",
        title=title,
        artist=artist,
        album=album,
        cover_url=cover_url,
        media_type=media_type,
        query=" ".join(query_parts),
        duration_seconds=duration_seconds,
        release_date=release_date,
        description=description,
        extra_tags=extra_tags,
    )


def _youtube_info(target: str, search: bool = False) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,
        "skip_download": True,
    }

    cookiefile = _yt_cookiefile_path()
    if cookiefile:
        opts["cookiefile"] = cookiefile

    with YoutubeDL(opts) as ydl:
        if search:
            result = ydl.extract_info(f"ytsearch1:{target}", download=False)
            if not result or "entries" not in result or not result["entries"]:
                raise HTTPException(status_code=404, detail="No YouTube match found")
            return result["entries"][0]
        return ydl.extract_info(target, download=False)


def _best_youtube_cover_url(info: dict) -> Optional[str]:
    thumbs = info.get("thumbnails") or []
    best_url = None
    best_area = -1

    for item in thumbs:
        if not isinstance(item, dict):
            continue
        thumb_url = item.get("url")
        if not thumb_url:
            continue
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        area = width * height
        if area > best_area:
            best_area = area
            best_url = thumb_url

    if best_url:
        return best_url

    video_id = info.get("id")
    if video_id:
        # Fallback candidates in descending quality.
        for suffix in ["maxresdefault.jpg", "sddefault.jpg", "hqdefault.jpg"]:
            candidate = f"https://i.ytimg.com/vi/{video_id}/{suffix}"
            try:
                res = requests.head(candidate, timeout=5)
                if res.status_code == 200:
                    return candidate
            except requests.RequestException:
                continue

    return info.get("thumbnail")


def _normalize_text(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _score_youtube_entry(entry: dict, meta: Optional[MediaMeta], query: str) -> int:
    title = _normalize_text(entry.get("title"))
    channel = _normalize_text(entry.get("channel") or entry.get("uploader"))
    full_text = f"{title} {channel}"
    score = 0

    for bad in [" live ", " remix ", " slowed ", " sped up ", " karaoke ", " 8d ", " lyrics "]:
        if bad.strip() in full_text:
            score -= 8

    for good in [" official ", " topic ", " auto generated by youtube "]:
        if good.strip() in full_text:
            score += 6

    query_tokens = [x for x in _normalize_text(query).split() if len(x) > 2]
    for token in query_tokens:
        if token in title:
            score += 2

    if meta:
        if meta.artist and _normalize_text(meta.artist) in full_text:
            score += 8
        if meta.title:
            title_tokens = [x for x in _normalize_text(meta.title).split() if len(x) > 2]
            for token in title_tokens:
                if token in title:
                    score += 2
        if meta.duration_seconds and entry.get("duration"):
            diff = abs(int(entry["duration"]) - int(meta.duration_seconds))
            if diff <= 5:
                score += 8
            elif diff <= 15:
                score += 4
            elif diff > 45:
                score -= 6

    return score


def _search_best_youtube_entry(query: str, meta: Optional[MediaMeta] = None, count: int = 10) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,
        "skip_download": True,
    }

    cookiefile = _yt_cookiefile_path()
    if cookiefile:
        opts["cookiefile"] = cookiefile

    with YoutubeDL(opts) as ydl:
        result = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
    entries = result.get("entries") or []
    if not entries:
        raise HTTPException(status_code=404, detail="No YouTube match found")

    ranked = sorted(entries, key=lambda item: _score_youtube_entry(item, meta, query), reverse=True)
    return ranked[0]


def _populate_youtube_match(meta: MediaMeta) -> MediaMeta:
    query = _search_query(meta)
    best = _search_best_youtube_entry(query, meta=meta, count=10)
    meta.youtube_url = best.get("webpage_url") or best.get("url")
    meta.youtube_id = best.get("id")
    meta.channel = meta.channel or best.get("channel") or best.get("uploader")
    if not meta.duration_seconds:
        meta.duration_seconds = best.get("duration")
    if not meta.cover_url:
        meta.cover_url = _best_youtube_cover_url(best)
    if meta.youtube_id:
        meta.extra_tags["YouTube ID"] = str(meta.youtube_id)
    return meta


def resolve_youtube(url: str) -> MediaMeta:
    info = _youtube_info(url, search=False)
    artist = info.get("artist") or info.get("uploader") or info.get("channel")
    extra_tags: dict[str, str] = {}
    if info.get("id"):
        extra_tags["YouTube ID"] = str(info["id"])

    return MediaMeta(
        input_text=url,
        source="youtube",
        title=info.get("track") or info.get("title") or "YouTube Video",
        artist=artist,
        album=info.get("album"),
        cover_url=_best_youtube_cover_url(info),
        media_type="youtube",
        query=info.get("title") or url,
        duration_seconds=info.get("duration"),
        release_date=info.get("upload_date"),
        description=info.get("description"),
        youtube_url=info.get("webpage_url") or url,
        youtube_id=info.get("id"),
        channel=info.get("channel") or info.get("uploader"),
        extra_tags=extra_tags,
    )


def resolve_text(text: str) -> MediaMeta:
    info = _search_best_youtube_entry(text, meta=None, count=10)
    artist = info.get("artist") or info.get("uploader") or info.get("channel")

    return MediaMeta(
        input_text=text,
        source="text",
        title=info.get("track") or info.get("title") or text,
        artist=artist,
        album=info.get("album"),
        cover_url=_best_youtube_cover_url(info),
        media_type="search",
        query=text,
        duration_seconds=info.get("duration"),
        release_date=info.get("upload_date"),
        description=info.get("description"),
        youtube_url=info.get("webpage_url"),
        youtube_id=info.get("id"),
        channel=info.get("channel") or info.get("uploader"),
        extra_tags={"YouTube ID": str(info.get("id"))} if info.get("id") else {},
    )


def resolve_input(value: str) -> MediaMeta:
    cleaned = (value or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Missing input")

    if _is_spotify_url(cleaned):
        return resolve_spotify(cleaned)
    if _is_youtube_url(cleaned):
        return resolve_youtube(cleaned)
    return resolve_text(cleaned)


def _search_query(meta: MediaMeta) -> str:
    if meta.query:
        return meta.query
    parts = [meta.title]
    if meta.artist:
        parts.append(meta.artist)
    return " ".join(parts)


def _safe_filename(meta: MediaMeta) -> str:
    base = meta.title
    if meta.artist:
        base = f"{meta.artist} - {meta.title}"
    base = re.sub(r"[^a-zA-Z0-9 _\-\.]+", "", base).strip()
    return base or "download"


def _embed_metadata(mp3_path: str, meta: MediaMeta):
    ext = os.path.splitext(mp3_path)[1].lower()
    if ext == ".m4a":
        _embed_metadata_m4a(mp3_path, meta)
        return
    if ext == ".mp3":
        _embed_metadata_mp3(mp3_path, meta)
        return


def _download_cover_bytes(meta: MediaMeta) -> tuple[Optional[bytes], Optional[str]]:
    if not meta.cover_url:
        return None, None
    try:
        img = requests.get(meta.cover_url, timeout=15)
        if img.status_code == 200:
            return img.content, img.headers.get("Content-Type", "image/jpeg")
    except requests.RequestException:
        return None, None
    return None, None


def _embed_metadata_mp3(mp3_path: str, meta: MediaMeta):
    audio = MP3(mp3_path, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()

    if meta.title:
        audio.tags.add(TIT2(encoding=3, text=meta.title))
    if meta.artist:
        audio.tags.add(TPE1(encoding=3, text=meta.artist))
    if meta.album:
        audio.tags.add(TALB(encoding=3, text=meta.album))
    if meta.release_date:
        audio.tags.add(TDRC(encoding=3, text=meta.release_date))
    if meta.duration_seconds:
        audio.tags.add(TLEN(encoding=3, text=str(meta.duration_seconds * 1000)))
    if meta.track_number:
        audio.tags.add(TRCK(encoding=3, text=str(meta.track_number)))
    if meta.disc_number:
        audio.tags.add(TPOS(encoding=3, text=str(meta.disc_number)))

    for desc, value in meta.extra_tags.items():
        if value:
            audio.tags.add(TXXX(encoding=3, desc=desc, text=value))

    if meta.source in {"spotify", "youtube"}:
        audio.tags.add(COMM(encoding=3, desc="Source", text=meta.input_text))

    if meta.lyrics:
        audio.tags.add(USLT(encoding=3, lang="eng", desc="Lyrics", text=meta.lyrics))

    cover_bytes, cover_mime = _download_cover_bytes(meta)
    if cover_bytes:
        audio.tags.add(
            APIC(
                encoding=3,
                mime=cover_mime or "image/jpeg",
                type=3,
                desc="Cover",
                data=cover_bytes,
            )
        )

    audio.save()


def _embed_metadata_m4a(file_path: str, meta: MediaMeta):
    audio = MP4(file_path)
    if meta.title:
        audio["\xa9nam"] = [meta.title]
    if meta.artist:
        audio["\xa9ART"] = [meta.artist]
    if meta.album:
        audio["\xa9alb"] = [meta.album]
    if meta.release_date:
        audio["\xa9day"] = [meta.release_date]
    if meta.track_number:
        audio["trkn"] = [(int(meta.track_number), 0)]
    if meta.disc_number:
        audio["disk"] = [(int(meta.disc_number), 0)]

    source = meta.extra_tags.get("Spotify URL") or meta.input_text
    if source:
        audio["\xa9cmt"] = [str(source)]

    if meta.lyrics:
        audio["\xa9lyr"] = [meta.lyrics]

    cover_bytes, cover_mime = _download_cover_bytes(meta)
    if cover_bytes:
        cover_format = MP4Cover.FORMAT_JPEG
        if cover_mime and "png" in cover_mime.lower():
            cover_format = MP4Cover.FORMAT_PNG
        audio["covr"] = [MP4Cover(cover_bytes, imageformat=cover_format)]

    audio.save()


def _zip_audio_with_lrc(audio_path: str, audio_filename: str, lrc_content: str, lrc_filename: str) -> str:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        zip_path = tmp.name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(audio_path, arcname=audio_filename)
        zf.writestr(lrc_filename, lrc_content)
    return zip_path


def _yt_dlp_opts(fmt: str, output_format: str) -> dict:
    opts = {
        "format": fmt,
        "outtmpl": "%(id)s.%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "socket_timeout": 15,
        "http_chunk_size": 1024 * 1024,
    }
    if output_format == "mp3":
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
        ]
    elif output_format == "opus":
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "opus", "preferredquality": "0"},
        ]
    elif output_format == "m4a":
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "0"},
        ]

    cookiefile = _yt_cookiefile_path()
    if cookiefile:
        opts["cookiefile"] = cookiefile

    return opts


def _find_generated_audio(tmpdir: str, fallback_id: Optional[str], output_format: str) -> Optional[str]:
    preferred_ext = {
        "mp3": ".mp3",
        "m4a": ".m4a",
        "opus": ".opus",
        "best": None,
    }.get(output_format)

    if fallback_id:
        if preferred_ext:
            by_id = os.path.join(tmpdir, f"{fallback_id}{preferred_ext}")
            if os.path.exists(by_id):
                return by_id
        for ext in [".m4a", ".opus", ".webm", ".mp3"]:
            by_id_any = os.path.join(tmpdir, f"{fallback_id}{ext}")
            if os.path.exists(by_id_any):
                return by_id_any
    for name in os.listdir(tmpdir):
        lower = name.lower()
        if lower.endswith((".m4a", ".opus", ".webm", ".mp3")):
            return os.path.join(tmpdir, name)
    return None


def _format_candidates(output_format: str) -> list[str]:
    if output_format == "best":
        return ["bestaudio/best"]
    return ["bestaudio/best"]


def _download_audio(meta: MediaMeta, tmpdir: str, output_format: str = "best") -> str:
    if output_format not in OUTPUT_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {output_format}")
    if meta.source != "youtube" and not meta.youtube_url:
        meta = _populate_youtube_match(meta)

    query = _search_query(meta)
    formats = _format_candidates(output_format)

    last_error = None
    source_id = meta.youtube_id
    target = meta.youtube_url if meta.source == "youtube" and meta.youtube_url else f"ytsearch1:{query}"

    for fmt in formats:
        ydl_opts = _yt_dlp_opts(fmt, output_format=output_format)
        ydl_opts["outtmpl"] = os.path.join(tmpdir, "%(id)s.%(ext)s")
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(target, download=True)
                if target.startswith("ytsearch1:"):
                    if not info or "entries" not in info or not info["entries"]:
                        raise HTTPException(status_code=404, detail="No YouTube match found")
                    entry = info["entries"][0]
                    source_id = entry.get("id")
                else:
                    source_id = info.get("id")
            file_path = _find_generated_audio(tmpdir, source_id, output_format=output_format)
            if file_path:
                return file_path
        except DownloadError as exc:
            if _is_yt_bot_block_error(exc):
                raise HTTPException(
                    status_code=429,
                    detail="YouTube blocked this server (bot check). Configure YTDLP_COOKIES or YTDLP_COOKIES_B64.",
                )
            last_error = exc
            continue

    detail = "YouTube download failed"
    if last_error:
        detail = f"YouTube download failed: {last_error}"
    raise HTTPException(status_code=502, detail=detail)


def _download_audio_with_fallback(meta: MediaMeta, tmpdir: str, preferred_format: str) -> str:
    base = [fmt for fmt in FALLBACK_FORMAT_ORDER if fmt in OUTPUT_FORMATS]
    order = []
    if preferred_format in OUTPUT_FORMATS:
        order.append(preferred_format)
    order.extend([fmt for fmt in base if fmt not in order])
    last_error: Optional[HTTPException] = None
    for fmt in order:
        try:
            return _download_audio(meta, tmpdir, output_format=fmt)
        except HTTPException as exc:
            if exc.status_code == 429:
                raise
            last_error = exc
            continue
    if last_error:
        raise last_error
    raise HTTPException(status_code=502, detail="YouTube download failed")


def download_from_input(value: str, output_format: str = "best") -> tuple[str, MediaMeta, str]:
    meta = resolve_input(value)
    lyrics, source = _fetch_lyrics(meta)
    meta.lyrics = lyrics
    meta.lyrics_source = source

    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = _download_audio_with_fallback(meta, tmpdir, preferred_format=output_format)
        _embed_metadata(file_path, meta)
        ext = os.path.splitext(file_path)[1].lower() or ".bin"

        final_name = _safe_filename(meta) + ext
        final_path = os.path.join(tmpdir, final_name)
        os.rename(file_path, final_path)

        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as stable:
            with open(final_path, "rb") as f:
                shutil.copyfileobj(f, stable)
            stable_path = stable.name

    return stable_path, meta, ext


def _resolve_playlist_with_spotify_api(url: str) -> tuple[dict, list[MediaMeta]]:
    token = _spotify_access_token()
    if not token:
        raise HTTPException(status_code=401, detail="Spotify credentials not configured")

    playlist_id = _spotify_playlist_id(url)
    if not playlist_id:
        raise HTTPException(status_code=400, detail="Invalid Spotify playlist URL")

    headers = {"Authorization": f"Bearer {token}"}
    meta_res = requests.get(
        f"https://api.spotify.com/v1/playlists/{playlist_id}",
        params={"fields": "name,images(url),external_urls(spotify),tracks(total)"},
        headers=headers,
        timeout=20,
    )
    if meta_res.status_code != 200:
        raise HTTPException(status_code=meta_res.status_code, detail="Spotify API playlist metadata failed")

    playlist = meta_res.json()
    tracks: list[MediaMeta] = []
    offset = 0
    limit = 100

    while True:
        tracks_res = requests.get(
            f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
            params={
                "offset": offset,
                "limit": limit,
                "fields": "items(track(name,id,artists(name),album(name,images(url),release_date),duration_ms,disc_number,track_number,external_ids(isrc),external_urls(spotify))),next",
            },
            headers=headers,
            timeout=20,
        )
        if tracks_res.status_code != 200:
            raise HTTPException(status_code=tracks_res.status_code, detail="Spotify API playlist tracks failed")

        payload = tracks_res.json()
        items = payload.get("items", [])
        for item in items:
            track = item.get("track") or {}
            if not track or not track.get("name"):
                continue

            artist_names = ", ".join([a.get("name") for a in track.get("artists", []) if a.get("name")])
            album = track.get("album", {})
            images = album.get("images", [])
            cover_url = images[0].get("url") if images else None
            spotify_url = (track.get("external_urls") or {}).get("spotify")
            spotify_id = track.get("id")
            isrc = (track.get("external_ids") or {}).get("isrc")

            tags: dict[str, str] = {}
            if spotify_id:
                tags["Spotify ID"] = str(spotify_id)
            if spotify_url:
                tags["Spotify URL"] = str(spotify_url)
            if isrc:
                tags["ISRC"] = str(isrc)

            tracks.append(
                MediaMeta(
                    input_text=spotify_url or url,
                    source="spotify",
                    title=track.get("name"),
                    artist=artist_names or None,
                    album=album.get("name"),
                    cover_url=cover_url,
                    media_type="spotify_track",
                    query=" ".join([x for x in [track.get("name"), artist_names] if x]),
                    duration_seconds=(track.get("duration_ms") or 0) // 1000 if track.get("duration_ms") else None,
                    release_date=album.get("release_date"),
                    track_number=track.get("track_number"),
                    disc_number=track.get("disc_number"),
                    extra_tags=tags,
                )
            )

        if not payload.get("next") or not items:
            break
        offset += limit

    playlist_info = {
        "title": playlist.get("name") or "Spotify Playlist",
        "cover_url": ((playlist.get("images") or [{}])[0]).get("url") if playlist.get("images") else None,
        "source_mode": "spotify_api",
        "track_count": len(tracks),
    }
    return playlist_info, tracks


def _resolve_playlist_without_credentials(url: str) -> tuple[dict, list[MediaMeta]]:
    page = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    if page.status_code != 200:
        raise HTTPException(status_code=page.status_code, detail="Playlist page is not accessible")

    track_ids = re.findall(r"spotify:track:([A-Za-z0-9]+)", page.text)
    if not track_ids:
        raise HTTPException(status_code=404, detail="No playlist tracks found in public page")

    # Keep order and remove duplicates from the page payload.
    ordered_track_ids = list(dict.fromkeys(track_ids))
    playlist_oembed = _get_oembed(url)
    playlist_title = playlist_oembed.get("title") or "Spotify Playlist"
    playlist_cover = playlist_oembed.get("thumbnail_url")

    tracks: list[MediaMeta] = []
    for idx, track_id in enumerate(ordered_track_ids, start=1):
        track_url = f"https://open.spotify.com/track/{track_id}"
        track_oembed = _get_oembed(track_url)
        raw_title = track_oembed.get("title") or f"Track {idx}"
        artist = track_oembed.get("author_name")
        title = raw_title

        if not artist and raw_title:
            parsed_artist, parsed_title = _parse_artist_and_title(raw_title)
            artist = parsed_artist
            title = parsed_title

        tags = {"Spotify ID": track_id, "Spotify URL": track_url}
        tracks.append(
            MediaMeta(
                input_text=track_url,
                source="spotify",
                title=title,
                artist=artist,
                album=None,
                cover_url=track_oembed.get("thumbnail_url") or playlist_cover,
                media_type="spotify_track",
                query=" ".join([x for x in [title, artist] if x]),
                track_number=idx,
                extra_tags=tags,
            )
        )

    playlist_info = {
        "title": playlist_title,
        "cover_url": playlist_cover,
        "source_mode": "public_html_fallback",
        "track_count": len(tracks),
    }
    return playlist_info, tracks


def resolve_playlist(url: str) -> tuple[dict, list[MediaMeta]]:
    if not _is_spotify_playlist_url(url):
        raise HTTPException(status_code=400, detail="Input must be a Spotify playlist URL")

    try:
        return _resolve_playlist_with_spotify_api(url)
    except HTTPException:
        return _resolve_playlist_without_credentials(url)


def _set_job(job_id: str, updates: dict):
    with PLAYLIST_JOBS_LOCK:
        if job_id not in PLAYLIST_JOBS:
            return
        PLAYLIST_JOBS[job_id].update(updates)


def _append_job_file(job_id: str, file_entry: dict):
    with PLAYLIST_JOBS_LOCK:
        if job_id not in PLAYLIST_JOBS:
            return
        PLAYLIST_JOBS[job_id]["files"].append(file_entry)


def _run_playlist_job(job_id: str, playlist_url: str, output_format: str, include_lrc: bool):
    job_dir = tempfile.mkdtemp(prefix=f"downtify-{job_id[:8]}-")
    files_dir = os.path.join(job_dir, "files")
    os.makedirs(files_dir, exist_ok=True)

    _set_job(job_id, {"status": "running", "workdir": job_dir})

    try:
        playlist_info, tracks = resolve_playlist(playlist_url)
        total = len(tracks)
        _set_job(
            job_id,
            {
                "playlist_title": playlist_info.get("title"),
                "cover_url": playlist_info.get("cover_url"),
                "source_mode": playlist_info.get("source_mode"),
                "total": total,
                "done": 0,
                "failed": 0,
            },
        )

        success_count = 0
        failed_count = 0

        for idx, track in enumerate(tracks, start=1):
            _set_job(job_id, {"current": f"{idx}/{total}: {track.title}"})
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    lyrics, source = _fetch_lyrics(track)
                    track.lyrics = lyrics
                    track.lyrics_source = source
                    file_path = _download_audio_with_fallback(track, tmpdir, preferred_format=output_format)
                    _embed_metadata(file_path, track)
                    ext = os.path.splitext(file_path)[1].lower() or ".bin"
                    output_name = f"{idx:03d} - {_safe_filename(track)}{ext}"
                    output_path = os.path.join(files_dir, output_name)
                    shutil.copy2(file_path, output_path)
                    lrc_path = None
                    if include_lrc and track.lyrics:
                        lrc_content = _build_lrc(track)
                        if lrc_content:
                            lrc_name = f"{idx:03d} - {_safe_filename(track)}.lrc"
                            lrc_path = os.path.join(files_dir, lrc_name)
                            with open(lrc_path, "w", encoding="utf-8") as f:
                                f.write(lrc_content)
                _append_job_file(
                    job_id,
                    {
                        "id": str(idx),
                        "index": idx,
                        "title": track.title,
                        "artist": track.artist,
                        "filename": output_name,
                        "path": output_path,
                        "lrc_path": lrc_path,
                        "lyrics_found": bool(track.lyrics),
                        "lyrics_source": track.lyrics_source,
                    },
                )
                success_count += 1
                _set_job(job_id, {"done": success_count})
            except HTTPException as exc:
                if exc.status_code == 429:
                    _set_job(
                        job_id,
                        {
                            "status": "failed",
                            "error": str(exc.detail),
                            "current": None,
                            "failed": failed_count + 1,
                            "done": success_count,
                        },
                    )
                    return
                failed_count += 1
                _set_job(job_id, {"failed": failed_count, "done": success_count})
                continue
            except Exception:
                failed_count += 1
                _set_job(job_id, {"failed": failed_count, "done": success_count})
                continue

        created_files = [
            name
            for name in os.listdir(files_dir)
            if name.lower().endswith((".mp3", ".m4a", ".opus", ".webm", ".lrc"))
        ]
        if not created_files:
            _set_job(job_id, {"status": "failed", "error": "No tracks were downloaded"})
            return

        zip_name = re.sub(r"[^a-zA-Z0-9 _\-\.]+", "", str(playlist_info.get("title") or "playlist")).strip() or "playlist"
        zip_path = os.path.join(job_dir, f"{zip_name}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in sorted(created_files):
                zf.write(os.path.join(files_dir, name), arcname=name)

        _set_job(
            job_id,
            {
                "status": "done",
                "zip_path": zip_path,
                "current": None,
                "done": success_count,
                "failed": failed_count,
            },
        )
    except Exception as exc:
        _set_job(job_id, {"status": "failed", "error": str(exc), "current": None})


@app.post("/api/preview")
async def preview(payload: dict):
    value = payload.get("input") or payload.get("url")
    if not value:
        raise HTTPException(status_code=400, detail="Missing input")

    meta = resolve_input(value)
    lyrics, source = _fetch_lyrics(meta)
    return JSONResponse(
        {
            "source": meta.source,
            "title": meta.title,
            "artist": meta.artist,
            "album": meta.album,
            "cover_url": meta.cover_url,
            "media_type": meta.media_type,
            "query": _search_query(meta),
            "duration_seconds": meta.duration_seconds,
            "release_date": meta.release_date,
            "channel": meta.channel,
            "youtube_url": meta.youtube_url,
            "lyrics_found": bool(lyrics),
            "lyrics_source": source,
        }
    )


@app.post("/api/download")
async def download(payload: dict, background_tasks: BackgroundTasks):
    value = payload.get("input") or payload.get("url")
    output_format = str(payload.get("format") or "best").lower()
    include_lrc = bool(payload.get("include_lrc") or payload.get("with_lrc"))
    if not value:
        raise HTTPException(status_code=400, detail="Missing input")
    if output_format not in OUTPUT_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {output_format}")

    if _is_spotify_playlist_url(str(value)):
        raise HTTPException(status_code=400, detail="Use /api/playlist/start for Spotify playlists")

    file_path, meta, ext = download_from_input(value, output_format=output_format)
    filename = _safe_filename(meta) + ext
    if include_lrc and meta.lyrics:
        lrc_content = _build_lrc(meta)
        if lrc_content:
            lrc_filename = _safe_filename(meta) + ".lrc"
            zip_path = _zip_audio_with_lrc(file_path, filename, lrc_content, lrc_filename)
            background_tasks.add_task(lambda p: os.remove(p), file_path)
            background_tasks.add_task(lambda p: os.remove(p), zip_path)
            zip_name = _safe_filename(meta) + ".zip"
            return FileResponse(zip_path, filename=zip_name, media_type="application/zip")

    background_tasks.add_task(lambda p: os.remove(p), file_path)
    media_type = "audio/mpeg"
    if ext == ".m4a":
        media_type = "audio/mp4"
    elif ext in {".opus", ".webm"}:
        media_type = "audio/ogg"
    return FileResponse(file_path, filename=filename, media_type=media_type)


@app.post("/api/playlist/start")
async def playlist_start(payload: dict):
    value = payload.get("input") or payload.get("url")
    output_format = str(payload.get("format") or "best").lower()
    include_lrc = bool(payload.get("include_lrc") or payload.get("with_lrc"))
    if not value:
        raise HTTPException(status_code=400, detail="Missing input")
    if output_format not in OUTPUT_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {output_format}")
    if not _is_spotify_playlist_url(str(value)):
        raise HTTPException(status_code=400, detail="Input must be a Spotify playlist URL")

    job_id = str(uuid.uuid4())
    with PLAYLIST_JOBS_LOCK:
        PLAYLIST_JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "input": value,
            "total": 0,
            "done": 0,
            "failed": 0,
            "current": None,
            "error": None,
            "zip_path": None,
            "files": [],
            "playlist_title": None,
            "cover_url": None,
            "source_mode": None,
            "output_format": output_format,
            "include_lrc": include_lrc,
            "created_at": int(time.time()),
        }

    thread = threading.Thread(
        target=_run_playlist_job,
        args=(job_id, str(value), output_format, include_lrc),
        daemon=True,
    )
    thread.start()

    return JSONResponse({"job_id": job_id, "status": "queued"})


@app.get("/api/playlist/status/{job_id}")
async def playlist_status(job_id: str):
    with PLAYLIST_JOBS_LOCK:
        job = PLAYLIST_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Playlist job not found")

    return JSONResponse(
        {
            "id": job["id"],
            "status": job["status"],
            "total": job["total"],
            "done": job["done"],
            "failed": job["failed"],
            "current": job["current"],
            "error": job["error"],
            "playlist_title": job["playlist_title"],
            "cover_url": job["cover_url"],
            "source_mode": job["source_mode"],
            "output_format": job.get("output_format"),
            "include_lrc": job.get("include_lrc"),
            "files": [
                {
                    "id": item["id"],
                    "index": item["index"],
                    "title": item["title"],
                    "artist": item["artist"],
                    "filename": item["filename"],
                    "lyrics_found": item.get("lyrics_found"),
                    "lyrics_source": item.get("lyrics_source"),
                }
                for item in (job.get("files") or [])
            ],
            "ready": bool(job["zip_path"] and job["status"] == "done"),
        }
    )


@app.get("/api/playlist/file/{job_id}/{file_id}")
async def playlist_file_download(job_id: str, file_id: str, background_tasks: BackgroundTasks, include_lrc: bool = False):
    with PLAYLIST_JOBS_LOCK:
        job = PLAYLIST_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Playlist job not found")

    file_entry = None
    for item in (job.get("files") or []):
        if item.get("id") == file_id:
            file_entry = item
            break
    if not file_entry:
        raise HTTPException(status_code=404, detail="Track file not found")

    file_path = str(file_entry.get("path") or "")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=410, detail="Track file is no longer available")

    ext = os.path.splitext(file_entry["filename"])[1].lower()
    if include_lrc and file_entry.get("lrc_path"):
        lrc_path = str(file_entry.get("lrc_path") or "")
        if os.path.exists(lrc_path):
            with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
                zip_path = tmp.name
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(file_path, arcname=file_entry["filename"])
                zf.write(lrc_path, arcname=os.path.basename(lrc_path))
            zip_name = os.path.splitext(file_entry["filename"])[0] + ".zip"
            background_tasks.add_task(lambda p: os.remove(p), zip_path)
            return FileResponse(zip_path, filename=zip_name, media_type="application/zip")

    media_type = "audio/mpeg"
    if ext == ".m4a":
        media_type = "audio/mp4"
    elif ext in {".opus", ".webm"}:
        media_type = "audio/ogg"
    return FileResponse(file_path, filename=file_entry["filename"], media_type=media_type)


@app.get("/api/playlist/download/{job_id}")
async def playlist_download(job_id: str):
    with PLAYLIST_JOBS_LOCK:
        job = PLAYLIST_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Playlist job not found")
    if job.get("status") != "done" or not job.get("zip_path"):
        raise HTTPException(status_code=409, detail="Playlist is not ready yet")

    zip_path = str(job["zip_path"])
    if not os.path.exists(zip_path):
        raise HTTPException(status_code=410, detail="Playlist file is no longer available")

    playlist_title = job.get("playlist_title") or "playlist"
    safe_title = re.sub(r"[^a-zA-Z0-9 _\-\.]+", "", str(playlist_title)).strip() or "playlist"
    filename = f"{safe_title}.zip"
    return FileResponse(zip_path, filename=filename, media_type="application/zip")


@app.get("/api/health")
async def health():
    return {"ok": True}


if os.path.isdir(FRONTEND_DIST):
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="static")
