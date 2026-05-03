#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
import zipfile
from datetime import date
from pathlib import Path

VIDEOS_JSON = Path(__file__).parent.parent / "videos.json"
REPO = os.environ["REPO"]
COOKIES_FILE = os.environ.get("COOKIES_FILE", "").strip()
MAX_PART_BYTES = 1_900 * 1024 * 1024  # 1.9 GB


def run(cmd, **kwargs):
    return subprocess.run(cmd, shell=True, text=True, capture_output=True, **kwargs)


def is_playlist(url):
    return "playlist?list=" in url or "/playlist/" in url


def yt_dlp_cmd(url, output_template, playlist):
    """Generate yt-dlp command with JavaScript runtime support"""
    # Use 'qjs' runtime for GitHub Actions (installed automatically)
    # Fallback to auto-detection if qjs not found
    js_runtime = "--js-runtimes qjs"

    # Simplified format selection - let yt-dlp choose best available
    fmt = "best[height<=720]/bestvideo[height<=720]+bestaudio/best"

    no_playlist = "" if playlist else "--no-playlist"
    cookies = f'--cookies "{COOKIES_FILE}"' if COOKIES_FILE else ""

    # Simplified player clients - let yt-dlp use default with JS runtime
    return (
        f'yt-dlp -f "{fmt}" --merge-output-format mp4 '
        f'--extractor-args "youtube:player_client=android,ios" '
        f'--retries 5 --fragment-retries 5 --sleep-requests 1 '
        f'--no-check-certificates {no_playlist} {cookies} {js_runtime} '
        f'-o "{output_template}" "{url}"'
    )


def read_info_json(tmpdir):
    """Read the .info.json file created by yt-dlp"""
    info_jsons = sorted(Path(tmpdir).rglob("*.info.json"))
    if not info_jsons:
        return {}
    try:
        return json.loads(info_jsons[0].read_text())
    except Exception as e:
        print(f"  Warning: Failed to read info.json: {e}")
        return {}


def zip_files(files, zip_path):
    """Create a zip file containing the given files"""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    return zip_path


def split_and_zip(mp4, tmpdir):
    """Split large MP4 files into parts and zip each part"""
    prefix = str(mp4) + ".part"
    run(f'split -b {MAX_PART_BYTES} "{mp4}" "{prefix}"')
    parts = sorted(Path(tmpdir).glob(mp4.name + ".part*"))
    zips = []
    for i, part in enumerate(parts, 1):
        zip_path = Path(tmpdir) / f"{mp4.stem}.part{i}.zip"
        zip_files([part], zip_path)
        zips.append(zip_path)
    return zips


def release_exists(tag):
    """Check if a GitHub release already exists"""
    result = run(f'gh release view "{tag}" --repo "{REPO}"')
    return result.returncode == 0


def create_or_upload_release(tag, title, notes, files):
    """Create a new release or upload files to existing one"""
    files_str = " ".join(f'"{f}"' for f in files)
    notes_escaped = notes.replace('"', '\\"')

    if release_exists(tag):
        print(f"  Release {tag} exists, uploading files...")
        return run(f'gh release upload "{tag}" {files_str} --repo "{REPO}" --clobber')

    print(f"  Creating release {tag}...")
    return run(
        f'gh release create "{tag}" {files_str} '
        f'--repo "{REPO}" --title "{title}" --notes "{notes_escaped}"'
    )


def get_release_url(tag):
    """Get the URL for a GitHub release"""
    result = run(f'gh release view "{tag}" --repo "{REPO}" --json url -q .url')
    return result.stdout.strip() if result.returncode == 0 else ""


def process_entry(entry, tmpdir):
    """Process a single video/playlist entry"""
    url = entry["url"]
    playlist = is_playlist(url)
    output_template = (
        "%(playlist_id)s/%(playlist_index)03d-%(id)s.%(ext)s"
        if playlist
        else "%(id)s.%(ext)s"
    )

    print(f"Downloading: {url}")
    result = run(yt_dlp_cmd(url, str(Path(tmpdir) / output_template), playlist))

    # Check for common errors
    if result.returncode != 0:
        error_msg = result.stderr[-500:].strip()
        print(f"  ERROR: {error_msg}")
        entry["status"] = "failed"
        entry["error"] = error_msg
        return entry

    # Find downloaded MP4 files
    mp4_files = sorted(Path(tmpdir).rglob("*.mp4"))
    if not mp4_files:
        print("  ERROR: No MP4 files produced")
        entry["status"] = "failed"
        entry["error"] = "No mp4 files produced by yt-dlp"
        return entry

    # Read metadata
    info = read_info_json(tmpdir)
    title = info.get("title") or info.get("playlist_title") or Path(mp4_files[0]).stem
    print(f"  Title: {title}")

    # Handle playlist vs single video
    if playlist:
        pl_id = info.get("playlist_id") or info.get("playlist") or Path(tmpdir).name
        tag = f"yt-playlist-{pl_id}"[:100]

        upload_files = []
        for mp4 in mp4_files:
            if mp4.stat().st_size > MAX_PART_BYTES:
                upload_files.extend(split_and_zip(mp4, tmpdir))
            else:
                zip_path = Path(tmpdir) / f"{mp4.stem}.zip"
                upload_files.append(zip_files([mp4], zip_path))

        notes = (
            f"Source: {url}\n"
            f"Total videos: {len(mp4_files)}\n\n"
            "Split parts: extract each zip, then concatenate:\n"
            "```bash\ncat <name>.part*.mp4 > <name>.mp4\n```"
        )
    else:
        # Single video - use video ID from info.json for consistency
        video_id = info.get("id") or mp4_files[0].stem
        tag = f"yt-{video_id}"[:100]
        mp4 = mp4_files[0]

        if mp4.stat().st_size > MAX_PART_BYTES:
            upload_files = split_and_zip(mp4, tmpdir)
            notes = (
                f"Source: {url}\n\n"
                f"Split parts: extract each zip, then concatenate:\n"
                f"```bash\ncat {mp4.stem}.part*.mp4 > {mp4.name}\n```"
            )
        else:
            zip_path = Path(tmpdir) / f"{video_id}.zip"
            upload_files = [zip_files([mp4], zip_path)]
            notes = f"Source: {url}"

    # Create release and upload files
    result = create_or_upload_release(tag, title, notes, upload_files)
    if result.returncode != 0:
        error_msg = result.stderr[-500:].strip()
        print(f"  ERROR uploading release: {error_msg}")
        entry["status"] = "failed"
        entry["error"] = error_msg
        return entry

    # Update entry with success data
    entry["status"] = "done"
    entry["title"] = title
    entry["release_tag"] = tag
    entry["release_url"] = get_release_url(tag)
    entry["downloaded_at"] = date.today().isoformat()
    print(f"  ✅ Done: {entry['release_url']}")
    return entry


def main():
    """Main entry point"""
    # Check if videos.json exists
    if not VIDEOS_JSON.exists():
        print(f"Error: {VIDEOS_JSON} not found")
        exit(1)

    # Load and filter pending videos
    videos = json.loads(VIDEOS_JSON.read_text())
    pending = [v for v in videos if v.get("status") == "pending"]

    if not pending:
        print("No pending videos.")
        return

    print(f"Processing {len(pending)} pending video(s)...")

    # Process each pending entry
    for i, entry in enumerate(pending, 1):
        print(f"\n[{i}/{len(pending)}]")
        with tempfile.TemporaryDirectory() as tmpdir:
            process_entry(entry, tmpdir)

        # Save progress after each entry
        VIDEOS_JSON.write_text(json.dumps(videos, indent=2, ensure_ascii=False) + "\n")

    print("\n✅ All done!")


if __name__ == "__main__":
    main()