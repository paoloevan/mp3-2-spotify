#!/usr/bin/env python3
"""
Batch Processor for podcast-2-spotify:
1. (Optional) Downloads new podcast episodes from an RSS feed URL into AUDIO_DIR.
2. Automatically converts .mp4/.m4a files to compressed .mp3 and removes originals.
3. Scans all unprocessed episodes (those without a .tracks.json file) and syncs to Spotify.
"""

import os
import sys
import subprocess
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def str_to_bool(val: str, default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


def download_from_rss(rss_url: str, folder: Path, max_episodes: int = 5):
    """Download latest episodes from an RSS feed if not already present locally."""
    if not rss_url:
        return

    print(f"🌐 Checking RSS feed: {rss_url}")
    try:
        req = urllib.request.Request(rss_url, headers={"User-Agent": "podcast-2-spotify/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            xml_data = resp.read()
        root = ET.fromstring(xml_data)
    except Exception as e:
        print(f"⚠️ Failed to fetch or parse RSS feed: {e}")
        return

    items = root.findall(".//item")
    if not items:
        print("ℹ️ No items found in RSS feed.")
        return

    for item in items[:max_episodes]:
        enclosure = item.find("enclosure")
        if enclosure is None or not enclosure.get("url"):
            continue

        media_url = enclosure.get("url")
        parsed_path = urllib.parse.urlparse(media_url).path
        filename = Path(parsed_path).name or "episode.mp3"
        stem = Path(filename).stem

        # Skip if already downloaded (.mp4/.mp3) or already processed (.tracks.json)
        if (
            (folder / filename).exists()
            or (folder / f"{stem}.mp3").exists()
            or (folder / f"{stem}.tracks.json").exists()
        ):
            continue

        title_el = item.find("title")
        ep_title = title_el.text.strip() if (title_el is not None and title_el.text) else filename
        dest_path = folder / filename
        print(f"⬇️ Downloading episode: {ep_title} -> {dest_path.name}")
        try:
            urllib.request.urlretrieve(media_url, str(dest_path))
            print(f"✅ Downloaded: {dest_path.name}")
        except Exception as e:
            print(f"❌ Error downloading {media_url}: {e}")
            if dest_path.exists():
                dest_path.unlink()


def convert_to_mp3(folder: Path, bitrate: str = "160k", delete_original: bool = True):
    """Convert .mp4 / .m4a files to .mp3 and optionally remove the original files."""
    convertible = sorted(list(folder.glob("*.mp4")) + list(folder.glob("*.m4a")))
    for src_path in convertible:
        mp3_path = src_path.with_suffix(".mp3")
        if not mp3_path.exists():
            print(f"🔄 Converting {src_path.name} -> {mp3_path.name} ({bitrate})...")
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(src_path), "-vn",
                    "-c:a", "libmp3lame", "-b:a", bitrate,
                    str(mp3_path), "-nostats", "-loglevel", "error"
                ],
                check=True
            )
        if delete_original and mp3_path.exists() and mp3_path.stat().st_size > 0:
            src_path.unlink()
            print(f"🗑️ Removed original file: {src_path.name}")


def main():
    audio_dir = os.getenv("AUDIO_DIR", "./episodes")
    rss_url = os.getenv("RSS_FEED_URL", "").strip()
    rss_max = int(os.getenv("RSS_MAX_EPISODES", "5"))
    compress_mp3 = str_to_bool(os.getenv("COMPRESS_TO_MP3"), default=True)
    mp3_bitrate = os.getenv("MP3_BITRATE", "160k")
    delete_original = str_to_bool(os.getenv("DELETE_ORIGINAL_AFTER_CONVERT"), default=True)
    playlist = os.getenv("SPOTIFY_MAIN_PLAYLIST", "MB")

    folder = Path(audio_dir).expanduser()
    folder.mkdir(parents=True, exist_ok=True)

    if rss_url:
        download_from_rss(rss_url, folder, max_episodes=rss_max)

    if compress_mp3:
        convert_to_mp3(folder, bitrate=mp3_bitrate, delete_original=delete_original)

    media_files = sorted([
        f for f in folder.iterdir()
        if f.suffix.lower() in [".mp4", ".mp3", ".m4a", ".aac", ".wav", ".ogg"]
    ])

    seen_stems = set()
    pending = []
    for f in sorted(media_files, key=lambda x: (x.stem, x.suffix.lower() != ".mp3")):
        if f.stem in seen_stems:
            continue
        seen_stems.add(f.stem)
        json_file = f.with_suffix(".tracks.json")
        if not json_file.exists():
            pending.append(f)

    if not pending:
        print("✅ All episodes in AUDIO_DIR have already been processed!")
        return

    print(f"Found {len(pending)} episode(s) to process:\n" + "\n".join(f"- {p.name}" for p in pending))
    print("=" * 60 + "\n")

    script_path = Path(__file__).resolve().parent / "podcast2spotify.py"

    for idx, media_path in enumerate(pending, start=1):
        print(f"\n[{idx}/{len(pending)}] PROCESSING: {media_path.name}")
        cmd = [
            sys.executable,
            str(script_path),
            str(media_path),
            "--spotify",
            "--playlist", playlist
        ]

        try:
            subprocess.run(cmd, check=True)
            print(f"[{idx}/{len(pending)}] ✅ Completed: {media_path.name}\n")
        except subprocess.CalledProcessError as e:
            print(f"[{idx}/{len(pending)}] ❌ Error processing {media_path.name}: {e}\n")

    print("\n🎉 BATCH PROCESSING COMPLETED FOR ALL FILES!")


if __name__ == "__main__":
    main()
