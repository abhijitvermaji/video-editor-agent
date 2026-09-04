"""
AI Video Editing Agent - Backend
=================================
Ye backend ek video leta hai, uske silence/dead parts detect karta hai
(FFmpeg ke through), aur un parts ko cut karke ek clean, tight video
banata hai.

Flow:
1. Mobile app / user video upload karta hai -> POST /edit
2. Backend FFmpeg se silence detect karta hai
3. Backend silence wale parts hata kar clips banata hai
4. Sab clips ko jodta hai -> ek final trimmed video
5. User ko download link milta hai

Deploy karne ke liye README.md dekho.
"""

import os
import uuid
import subprocess
import re
import shutil
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="AI Video Editing Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)


def run_cmd(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 and "silencedetect" not in " ".join(cmd):
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
    return result.stderr


def detect_silence(input_path: str, silence_db: str = "-30dB", min_duration: float = 0.5):
    cmd = [
        "ffmpeg", "-i", input_path,
        "-af", f"silencedetect=noise={silence_db}:d={min_duration}",
        "-f", "null", "-"
    ]
    log = run_cmd(cmd)
    starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", log)]
    silences = list(zip(starts, ends))
    return silences


def get_duration(input_path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noheader=1:noprint_wrappers=1", input_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return float(result.stdout.strip())


def build_keep_segments(duration: float, silences: list[tuple[float, float]], padding: float = 0.15):
    keep = []
    cursor = 0.0
    for start, end in silences:
        seg_start = cursor
        seg_end = min(start + padding, duration)
        if seg_end - seg_start > 0.05:
            keep.append((seg_start, seg_end))
        cursor = max(end - padding, seg_end)
    if cursor < duration:
        keep.append((cursor, duration))
    return keep


def cut_and_join(input_path: str, segments: list[tuple[float, float]], output_path: str):
    if not segments:
        shutil.copy(input_path, output_path)
        return

    tmp_dir = Path(output_path).parent / f"tmp_{uuid.uuid4().hex}"
    tmp_dir.mkdir(exist_ok=True)
    clip_paths = []

    try:
        for i, (start, end) in enumerate(segments):
            clip_path = tmp_dir / f"clip_{i:04d}.mp4"
            cmd = [
                "ffmpeg", "-y", "-i", input_path,
                "-ss", str(start), "-to", str(end),
                "-c:v", "libx264", "-c:a", "aac",
                "-avoid_negative_ts", "make_zero",
                str(clip_path)
            ]
            run_cmd(cmd)
            clip_paths.append(clip_path)

        concat_list = tmp_dir / "concat_list.txt"
        with open(concat_list, "w") as f:
            for p in clip_paths:
                f.write(f"file '{p.resolve()}'\n")

        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_list), "-c", "copy", output_path
        ]
        run_cmd(cmd)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.get("/")
def health_check():
    return {"status": "ok", "message": "AI Video Editing Agent chal raha hai"}


@app.post("/edit")
async def edit_video(
    file: UploadFile = File(...),
    silence_db: str = "-30dB",
    min_silence_duration: float = 0.5,
):
    if not file.filename.lower().endswith((".mp4", ".mov", ".mkv", ".avi")):
        raise HTTPException(400, "Sirf video files allowed hain (mp4, mov, mkv, avi)")

    job_id = uuid.uuid4().hex
    input_path = UPLOAD_DIR / f"{job_id}_{file.filename}"
    output_path = OUTPUT_DIR / f"{job_id}_edited.mp4"

    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        duration = get_duration(str(input_path))
        silences = detect_silence(str(input_path), silence_db, min_silence_duration)
        keep_segments = build_keep_segments(duration, silences)
        cut_and_join(str(input_path), keep_segments, str(output_path))
    except Exception as e:
        raise HTTPException(500, f"Editing fail hui: {e}")
    finally:
        input_path.unlink(missing_ok=True)

    return {
        "job_id": job_id,
        "original_duration_sec": round(duration, 2),
        "removed_silence_chunks": len(silences),
        "download_url": f"/download/{job_id}_edited.mp4"
    }


@app.get("/download/{filename}")
def download(filename: str):
    file_path = OUTPUT_DIR / filename
    if not file_path.exists():
        raise HTTPException(404, "File nahi mili")
    return FileResponse(file_path,media_type="video/mp4", filename=filename)
