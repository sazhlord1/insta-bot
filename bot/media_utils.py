"""ffmpeg / ffprobe helpers (blocking — call through asyncio.to_thread)."""
import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}


def is_photo(path: str | Path) -> bool:
    return Path(path).suffix.lower() in PHOTO_EXT


def probe(path: str | Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    data = json.loads(out.stdout or "{}")
    info = {"duration": float(data.get("format", {}).get("duration", 0) or 0),
            "vcodec": None, "acodec": None, "pix_fmt": None, "width": 0, "height": 0}
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and info["vcodec"] is None:
            info.update(vcodec=s.get("codec_name"), pix_fmt=s.get("pix_fmt"),
                        width=s.get("width", 0), height=s.get("height", 0))
        elif s.get("codec_type") == "audio" and info["acodec"] is None:
            info["acodec"] = s.get("codec_name")
    return info


def ensure_instagram_video(path: str | Path) -> Path:
    """Re-encode to H.264/AAC mp4 only when the file isn't already compatible."""
    path = Path(path)
    info = probe(path)
    ok = (info["vcodec"] == "h264" and info["acodec"] in ("aac", None)
          and info["pix_fmt"] in ("yuv420p", None) and path.suffix.lower() == ".mp4"
          and max(info["width"], info["height"]) <= 1920)
    if ok:
        return path
    out = path.with_name(path.stem + "_ig.mp4")
    cmd = ["ffmpeg", "-y", "-i", str(path),
           "-vf", "scale='min(1080,iw)':-2", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart", str(out)]
    log.info("Re-encoding %s for Instagram", path.name)
    subprocess.run(cmd, capture_output=True, timeout=1800, check=True)
    path.unlink(missing_ok=True)
    return out


def thumbnail(path: str | Path) -> Path:
    path = Path(path)
    out = path.with_suffix(".thumb.jpg")
    for seek in ("1", "0"):  # very short clips have no frame at 1s
        subprocess.run(["ffmpeg", "-y", "-ss", seek, "-i", str(path), "-frames:v", "1",
                        "-vf", "scale=720:-2", str(out)],
                       capture_output=True, timeout=120)
        if out.exists() and out.stat().st_size > 0:
            break
    return out


def delete_media(path: str | Path) -> None:
    p = Path(path)
    for f in (p, p.with_suffix(".thumb.jpg")):
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass
