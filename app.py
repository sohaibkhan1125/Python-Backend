# app.py
import os
import re
import tempfile
import shutil
from flask import Flask, request, jsonify, send_file, abort, url_for
from yt_dlp import YoutubeDL
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Optional: let deploy environment set ffmpeg path via env var
FFMPEG_LOCATION = os.environ.get("FFMPEG_PATH", "ffmpeg")  # or "/usr/bin/ffmpeg"

# Desired resolutions to present to user
DESIRED_HEIGHTS = [1080, 720, 480, 360, 240, 144]

# Helper: safe title to filename
def clean_filename(s: str) -> str:
    s = re.sub(r'[\\/*?:"<>|]', "", s)
    s = s.strip()
    return s[:200]  # limit length

def build_format_options(info):
    """
    Build a compact list of recommended format strings for download.
    We prefer combined video+audio when possible by using yt-dlp format selector syntax.
    For each desired height we produce a format string like:
      bestvideo[height<=720]+bestaudio/best[height<=720]/best[height<=720]
    And add audio-only: 'bestaudio' (we will convert to mp3 if user wants).
    """
    formats = []

    # Add video resolutions (format selector strings)
    for h in DESIRED_HEIGHTS:
        # Create a selector that attempts to give video+audio merged; if yt-dlp can't
        # combine, it will fall back to best for that height.
        selector = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
        formats.append({
            "id": selector,
            "label": f"{h}p",
            "ext": "mp4",
            "type": "video"
        })

    # Add audio-only option (mp3)
    formats.append({
        "id": "bestaudio",
        "label": "MP3 (audio only)",
        "ext": "mp3",
        "type": "audio"
    })

    # Deduplicate while preserving order (in case)
    seen = set()
    final = []
    for f in formats:
        if f['id'] in seen:
            continue
        seen.add(f['id'])
        final.append(f)
    return final

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "ffmpeg": FFMPEG_LOCATION})

@app.route("/fetch_info", methods=["POST"])
def fetch_info():
    """
    POST JSON: { "url": "https://..." }
    Returns JSON with title, thumbnail, duration, and filtered formats list.
    """
    data = request.get_json(silent=True) or {}
    url = data.get("url") or request.args.get("url")
    if not url:
        return jsonify({"error": "Missing 'url' parameter"}), 400

    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "no_warnings": True,
        # prefer to not download anything
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"error": f"Failed to extract info: {str(e)}"}), 500

    # build filtered formats (we do not enumerate all formats, we provide our recommended strings)
    formats = build_format_options(info)

    result = {
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url"),
        "formats": formats
    }
    return jsonify(result)

@app.route("/download", methods=["GET"])
def download():
    """
    Downloads the requested format and streams the file to the browser as an attachment.
    Query params:
      - url (required)
      - format_id (required)  // this is the yt-dlp format selector (e.g. bestvideo[height<=720]+bestaudio/best)
      - filename (optional)  // suggested download filename
      - audio_only (optional, '1' or 'true') // if set and format_id=='bestaudio', convert to mp3
    """
    url = request.args.get("url")
    format_id = request.args.get("format_id")
    suggested_name = request.args.get("filename")  # optional
    audio_only_flag = request.args.get("audio_only", "").lower() in ("1", "true", "yes")

    if not url or not format_id:
        return jsonify({"error": "Missing 'url' or 'format_id' query parameters"}), 400

    # Create temporary directory for this download
    temp_dir = tempfile.mkdtemp(prefix="ydl_")
    try:
        # Prepare ytdlp options
        outtmpl = os.path.join(temp_dir, "%(title)s.%(ext)s")
        ydl_opts = {
            "format": format_id,
            "outtmpl": outtmpl,
            "merge_output_format": "mp4",
            "ffmpeg_location": FFMPEG_LOCATION,
            "postprocessors": [],
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }

        # If audio-only requested or format_id is 'bestaudio', configure mp3 conversion
        want_mp3 = audio_only_flag or (format_id == "bestaudio")
        if want_mp3:
            # Download best audio and convert to mp3
            ydl_opts["format"] = "bestaudio"
            ydl_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]
            # We will expect a .mp3 file
            output_ext = "mp3"
        else:
            # Video: ensure merging and mp4 output
            ydl_opts["postprocessors"] = [
                {
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                }
            ]
            output_ext = "mp4"

        # Run yt-dlp to download into temp_dir
        try:
            with YoutubeDL(ydl_opts) as ydl:
                # extract and download
                info = ydl.extract_info(url, download=True)
                # prepare filename used by yt-dlp (respect outtmpl)
                filename = ydl.prepare_filename(info)
                # if mp3 conversion, change extension accordingly
                if want_mp3:
                    filename = os.path.splitext(filename)[0] + ".mp3"
                else:
                    # ensure mp4 extension if merge_output_format used or convert applied
                    if not filename.lower().endswith(".mp4"):
                        filename = os.path.splitext(filename)[0] + ".mp4"
        except Exception as e:
            return jsonify({"error": f"Download failed: {str(e)}"}), 500

        if not os.path.exists(filename):
            # Safety check: find any file in temp_dir
            files = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if os.path.isfile(os.path.join(temp_dir, f))]
            if files:
                filename = files[0]
            else:
                return jsonify({"error": "Download completed but file not found"}), 500

        # Suggest a safe download name
        base = suggested_name or info.get("title") or "video"
        base = secure_filename(clean_filename(base))
        download_name = f"{base}.{output_ext}"

        # Serve the file as attachment
        return send_file(filename, as_attachment=True, download_name=download_name, mimetype="application/octet-stream")
    finally:
        # remove temp dir and files after send_file returns (send_file reads file first)
        # Note: send_file may delay serving until response is created; we still cleanup.
        # Use a short delay or rely on system temp cleanup if needed. Here we attempt immediate cleanup.
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass

if __name__ == "__main__":
    # For local development
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
