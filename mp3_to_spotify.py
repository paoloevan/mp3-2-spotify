#!/usr/bin/env python3
"""
mp3-2-spotify:
Automatically identify songs inside long MP3/MP4 files, podcast episodes, radio shows, or DJ mixes
using adaptive audio fingerprinting (Shazam) and sync them to Spotify playlists.
"""

import asyncio
import os
import json
import argparse
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from shazamio import Shazam, HTTPClient
from aiohttp_retry import ExponentialRetry
import spotipy
from spotipy.oauth2 import SpotifyOAuth

load_dotenv()


def str_to_bool(val: str, default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


def create_shazam_client() -> Shazam:
    """Create a Shazam client without aggressive internal retry bursts on HTTP 429."""
    return Shazam(
        http_client=HTTPClient(
            retry_options=ExponentialRetry(attempts=1)
        )
    )


def format_timestamp(seconds: float) -> str:
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    hours = int(mins // 60)
    mins = int(mins % 60)
    if hours > 0:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def get_media_duration(file_path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", file_path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(res.stdout.strip())


def extract_audio_chunk(media_path: str, start_sec: float, duration_sec: float = 15.0) -> str:
    """Extract a mono 44.1kHz MP3 audio sample to a temporary file."""
    temp_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    temp_path = temp_file.name
    temp_file.close()

    cmd = [
        "ffmpeg", "-y", "-ss", str(start_sec), "-i", media_path,
        "-t", str(duration_sec), "-vn", "-acodec", "libmp3lame",
        "-ar", "44100", "-ac", "1", temp_path
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return temp_path


def clean_name(s: str) -> str:
    if not s:
        return ""
    return s.strip().lower()


async def scan_audio_adaptive(
    media_path: str,
    base_step: int = 45,
    skip_step: int = 90,
    sample_len: int = 15,
    request_delay: float = 3.5,
    cooldown_sec: float = 65.0
) -> list:
    """
    Adaptive Audio Scanner:
    - Extracts `sample_len` seconds per sample (resilient to voiceovers).
    - Advances by `base_step` seconds on speech/silence to catch every new song.
    - Advances by `skip_step` seconds when a song is recognized and still playing.
    - Holds the exact same timestamp on rate-limit/timeout and waits `cooldown_sec`.
    """
    path = Path(media_path)
    if not path.is_file():
        print(f"❌ Error: file not found: {media_path}")
        return []

    total_duration = get_media_duration(str(path))
    print(f"--- Starting adaptive scan: {path.name} ---")
    print(f"Total duration: {format_timestamp(total_duration)} ({int(total_duration)}s)")
    print(f"Base step: {base_step}s | Skip step: {skip_step}s | Sample length: {sample_len}s\n")

    shazam = create_shazam_client()
    recognized_tracks = []
    seen_track_keys = set()

    current_sec = 0.0
    while current_sec < total_duration:
        ts_str = format_timestamp(current_sec)
        print(f"[{ts_str}] Sampling ({int(current_sec)}/{int(total_duration)}s)...", end=" ", flush=True)

        chunk_path = None
        step_to_add = base_step
        try:
            chunk_path = extract_audio_chunk(str(path), current_sec, duration_sec=sample_len)
            out = {}
            try:
                out = await asyncio.wait_for(shazam.recognize(chunk_path), timeout=12.0)
            except Exception:
                print(f"\n⏱️ (Shazam rate-limit/timeout: pausing {int(cooldown_sec)}s on the same timestamp)", flush=True)
                await asyncio.sleep(cooldown_sec)
                shazam = create_shazam_client()
                step_to_add = 0.0  # Stay on the exact same second

            track = out.get("track")

            if step_to_add == 0.0:
                pass
            elif track:
                title = track.get("title", "").strip()
                artist = track.get("subtitle", "").strip()
                key = f"{clean_name(title)} - {clean_name(artist)}"

                if key and key not in seen_track_keys:
                    seen_track_keys.add(key)

                    track_info = {
                        "timestamp": ts_str,
                        "time_sec": current_sec,
                        "title": title,
                        "artist": artist,
                        "shazam_url": track.get("url", ""),
                        "spotify_data": None
                    }

                    providers = track.get("hub", {}).get("providers", [])
                    for p in providers:
                        if p.get("type") == "SPOTIFY":
                            for a in p.get("actions", []):
                                if a.get("uri"):
                                    track_info["spotify_data"] = a.get("uri")

                    recognized_tracks.append(track_info)
                    print(f"🎵 FOUND: {artist} - {title}")
                    step_to_add = skip_step
                elif key:
                    print(f"⏩ (Still playing: {artist} - {title})")
                    step_to_add = skip_step
                else:
                    print("⚪ Unidentified track.")
            else:
                print("⚪ No track detected (speech or silence).")
        except Exception as e:
            print(f"⚠️ Sampling error: {e}")
            step_to_add = base_step
        finally:
            if chunk_path and os.path.exists(chunk_path):
                try:
                    os.remove(chunk_path)
                except Exception:
                    pass

        current_sec += step_to_add
        if step_to_add > 0:
            await asyncio.sleep(request_delay)

    print(f"\n✅ Scan complete. Identified {len(recognized_tracks)} unique tracks.")
    return recognized_tracks


def export_results_to_json(recognized_tracks: list, output_path: str):
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(recognized_tracks, f, indent=2, ensure_ascii=False)
    print(f"💾 Results saved to: {output_path}")


def get_spotify_client():
    client_id = os.getenv("SPOTIPY_CLIENT_ID")
    client_secret = os.getenv("SPOTIPY_CLIENT_SECRET")
    redirect_uri = os.getenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8501")

    if not client_id or not client_secret:
        print("⚠️ Missing SPOTIPY_CLIENT_ID or SPOTIPY_CLIENT_SECRET in .env file.")
        return None

    scope = "playlist-modify-public playlist-modify-private playlist-read-private"
    try:
        return spotipy.Spotify(auth_manager=SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope=scope,
            cache_path=".spotify_cache",
            open_browser=True
        ))
    except Exception as e:
        print(f"⚠️ Spotify initialization error: {e}")
        return None


def get_existing_playlist_uris(sp: spotipy.Spotify, playlist_id: str) -> set:
    """Fetch all track URIs currently in the playlist to prevent duplicates."""
    existing_uris = set()
    offset = 0
    limit = 100
    while True:
        res = sp.playlist_items(playlist_id, limit=limit, offset=offset)
        items = res.get("items", [])
        if not items:
            break
        for entry in items:
            track_obj = entry.get("item") or entry.get("track")
            if track_obj and track_obj.get("uri"):
                existing_uris.add(track_obj["uri"])
        if not res.get("next"):
            break
        offset += len(items)
    return existing_uris


def resolve_or_create_playlist(sp: spotipy.Spotify, user_id: str, target_playlist: str, is_public: bool = False):
    playlist_id = None
    playlist_name = target_playlist
    playlist_url = ""

    if target_playlist.startswith("https://open.spotify.com/playlist/"):
        playlist_id = target_playlist.split("playlist/")[1].split("?")[0]
    elif len(target_playlist) == 22 and " " not in target_playlist:
        playlist_id = target_playlist

    if playlist_id:
        try:
            p_data = sp.playlist(playlist_id)
            playlist_name = p_data.get("name", playlist_id)
            playlist_url = p_data.get("external_urls", {}).get("spotify", "")
            print(f"✅ Found target playlist: '{playlist_name}' ({playlist_url})")
        except Exception:
            playlist_id = None

    if not playlist_id:
        res = sp.current_user_playlists(limit=50)
        for p in res.get("items", []):
            owner_id = p.get("owner", {}).get("id")
            can_modify = (owner_id == user_id) or p.get("collaborative", False)
            if can_modify and p["name"].lower() == target_playlist.lower():
                playlist_id = p["id"]
                playlist_name = p["name"]
                playlist_url = p.get("external_urls", {}).get("spotify", "")
                print(f"✅ Found existing editable playlist: '{playlist_name}' ({playlist_url})")
                break

    if not playlist_id:
        print(f"✨ Playlist '{target_playlist}' not found. Creating it...")
        try:
            playlist = sp.current_user_playlist_create(
                name=target_playlist,
                public=is_public,
                description="Created by mp3-2-spotify"
            )
        except Exception:
            playlist = sp.user_playlist_create(
                user=user_id,
                name=target_playlist,
                public=is_public,
                description="Created by mp3-2-spotify"
            )
        playlist_id = playlist["id"]
        playlist_name = playlist["name"]
        playlist_url = playlist.get("external_urls", {}).get("spotify", "")
        try:
            sp.playlist_change_details(playlist_id, public=is_public)
        except Exception:
            pass
        visibility = "public" if is_public else "private"
        print(f"✅ Created new {visibility} playlist: '{playlist_name}' ({playlist_url})")

    return playlist_id, playlist_name, playlist_url


def sync_tracks_to_spotify(
    sp: spotipy.Spotify,
    target_playlist: str,
    tracks: list,
    create_dated_playlist: bool = True,
    dated_prefix: str = "mb",
    is_public: bool = False,
    episode_name: str = ""
):
    try:
        me = sp.me()
        user_id = me["id"]
        print(f"Connected to Spotify as: {me.get('display_name', user_id)}")
    except Exception as e:
        print(f"❌ Spotify authentication error: {e}")
        return None

    playlist_id, playlist_name, playlist_url = resolve_or_create_playlist(
        sp, user_id, target_playlist, is_public=is_public
    )

    existing_uris = get_existing_playlist_uris(sp, playlist_id)
    if existing_uris:
        print(f"ℹ️ Playlist '{playlist_name}' already has {len(existing_uris)} tracks. Duplicates will be skipped.")

    track_uris_for_main = []
    episode_uris = []
    seen_in_episode = set()

    print("\n🔍 Searching and matching tracks on Spotify...")
    for t in tracks:
        cand_uri = None
        if t.get("spotify_data") and t["spotify_data"].startswith("spotify:track:"):
            cand_uri = t["spotify_data"]
            found_desc = f"{t['artist']} - {t['title']}"
        else:
            query = f"track:{t['title']} artist:{t['artist']}"
            res = sp.search(q=query, type="track", limit=3)
            items = res.get("tracks", {}).get("items", [])

            if not items:
                fallback_q = f"{t['title']} {t['artist']}"
                res = sp.search(q=fallback_q, type="track", limit=3)
                items = res.get("tracks", {}).get("items", [])

            if items:
                best = items[0]
                cand_uri = best["uri"]
                artists_str = ", ".join(a["name"] for a in best.get("artists", []))
                found_desc = f"{artists_str} - {best['name']}"

        if cand_uri:
            if cand_uri not in seen_in_episode:
                episode_uris.append(cand_uri)
                seen_in_episode.add(cand_uri)

            if cand_uri in existing_uris:
                print(f"  ⏩ Already in '{playlist_name}': {found_desc}")
            else:
                track_uris_for_main.append(cand_uri)
                existing_uris.add(cand_uri)
                print(f"  [OK] {t['artist']} - {t['title']} -> {found_desc}")
        else:
            print(f"  ❌ Not found on Spotify: {t['artist']} - {t['title']}")

    if track_uris_for_main:
        for i in range(0, len(track_uris_for_main), 100):
            sp.playlist_add_items(playlist_id=playlist_id, items=track_uris_for_main[i:i + 100])
        print(f"\n✅ Added {len(track_uris_for_main)} new tracks to '{playlist_name}'!")
    else:
        print(f"\nℹ️ No new tracks to add to '{playlist_name}' (all already present or not found).")

    if create_dated_playlist and episode_uris:
        date_str = datetime.now().strftime("%Y-%m-%d")
        dated_playlist_name = f"{dated_prefix} {date_str}".strip()
        desc = f"Episode tracklist ({episode_name})" if episode_name else f"Episode tracklist ({date_str})"
        print(f"\n✨ Creating dedicated episode playlist: '{dated_playlist_name}'...")
        try:
            dated_pl = sp.current_user_playlist_create(
                name=dated_playlist_name,
                public=is_public,
                description=desc
            )
        except Exception:
            dated_pl = sp.user_playlist_create(
                user=user_id,
                name=dated_playlist_name,
                public=is_public,
                description=desc
            )
        dated_pl_id = dated_pl["id"]
        dated_pl_url = dated_pl.get("external_urls", {}).get("spotify", "")
        try:
            sp.playlist_change_details(dated_pl_id, public=is_public)
        except Exception:
            pass
        for i in range(0, len(episode_uris), 100):
            sp.playlist_add_items(playlist_id=dated_pl_id, items=episode_uris[i:i + 100])
        visibility = "public" if is_public else "private"
        print(f"✅ Created {visibility} playlist '{dated_playlist_name}' with {len(episode_uris)} tracks! ({dated_pl_url})")

    return playlist_url


async def main():
    default_audio_dir = os.getenv("AUDIO_DIR", "./episodes")
    default_playlist = os.getenv("SPOTIFY_MAIN_PLAYLIST", "MB")
    default_dated_prefix = os.getenv("SPOTIFY_DATED_PLAYLIST_PREFIX", "mb")
    default_create_dated = str_to_bool(os.getenv("SPOTIFY_CREATE_DATED_PLAYLIST"), default=True)
    default_public = str_to_bool(os.getenv("SPOTIFY_PLAYLIST_PUBLIC"), default=False)
    default_base_step = int(os.getenv("SCAN_BASE_STEP", "45"))
    default_skip_step = int(os.getenv("SCAN_SKIP_STEP", "90"))
    default_sample_len = int(os.getenv("SCAN_SAMPLE_LEN", "15"))
    default_req_delay = float(os.getenv("SCAN_REQUEST_DELAY", "3.5"))

    parser = argparse.ArgumentParser(
        description="Identify songs in podcast/radio episodes using Shazam and sync them to Spotify."
    )
    parser.add_argument("file", nargs="?", help="Path to audio/video file (defaults to newest file in AUDIO_DIR)")
    parser.add_argument("--base-step", type=int, default=default_base_step, help=f"Step in seconds on speech/silence (default: {default_base_step})")
    parser.add_argument("--skip-step", type=int, default=default_skip_step, help=f"Step in seconds after recognizing a song (default: {default_skip_step})")
    parser.add_argument("--sample-len", type=int, default=default_sample_len, help=f"Audio sample duration in seconds (default: {default_sample_len})")
    parser.add_argument("--spotify", action="store_true", help="Sync identified tracks to Spotify")
    parser.add_argument("--playlist", default=default_playlist, help=f"Target cumulative Spotify playlist name, ID, or URL (default: '{default_playlist}')")
    parser.add_argument("--dated-prefix", default=default_dated_prefix, help=f"Prefix for per-episode dated playlist (default: '{default_dated_prefix}')")
    parser.add_argument("--no-dated-playlist", action="store_true", help="Disable creating a dedicated 'prefix YYYY-MM-DD' playlist")
    parser.add_argument("--public", action="store_true", default=default_public, help="Create playlists as public instead of private")
    parser.add_argument("--from-json", help="Sync to Spotify directly from an existing .tracks.json file without re-scanning")
    args = parser.parse_args()

    create_dated = default_create_dated and (not args.no_dated_playlist)

    if args.from_json:
        json_path = Path(args.from_json)
        if not json_path.is_file():
            print(f"❌ File not found: {json_path}")
            return
        with open(json_path, "r", encoding="utf-8") as f:
            tracks = json.load(f)
        sp = get_spotify_client()
        if sp:
            sync_tracks_to_spotify(
                sp,
                args.playlist,
                tracks,
                create_dated_playlist=create_dated,
                dated_prefix=args.dated_prefix,
                is_public=args.public,
                episode_name=json_path.stem
            )
        return

    target_file = args.file
    if not target_file:
        folder = Path(default_audio_dir).expanduser()
        if not folder.is_dir():
            print(f"❌ Audio folder not found: {folder}. Set AUDIO_DIR in .env or pass a file path.")
            return
        media_files = sorted(
            [f for f in folder.iterdir() if f.suffix.lower() in [".mp4", ".mp3", ".m4a", ".aac", ".wav", ".ogg"]],
            key=lambda f: f.stat().st_mtime,
            reverse=True
        )
        if not media_files:
            print(f"❌ No audio/video files found in {folder}")
            return
        target_file = str(media_files[0])
        print(f"ℹ️ No file specified. Using newest file: {media_files[0].name}")

    tracks = await scan_audio_adaptive(
        target_file,
        base_step=args.base_step,
        skip_step=args.skip_step,
        sample_len=args.sample_len,
        request_delay=default_req_delay
    )

    if not tracks:
        print("⚪ No tracks identified.")
        return

    output_json = Path(target_file).with_suffix(".tracks.json")
    export_results_to_json(tracks, str(output_json))

    print("\n--- IDENTIFIED TRACKLIST ---")
    for t in tracks:
        print(f"{t['timestamp']} | {t['artist']} - {t['title']}")
    print("----------------------------\n")

    if args.spotify:
        sp = get_spotify_client()
        if sp:
            sync_tracks_to_spotify(
                sp,
                args.playlist,
                tracks,
                create_dated_playlist=create_dated,
                dated_prefix=args.dated_prefix,
                is_public=args.public,
                episode_name=Path(target_file).name
            )


if __name__ == "__main__":
    asyncio.run(main())
