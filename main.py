import os
import re
import json
import time
import uuid
import asyncio
import subprocess
from pathlib import Path

import httpx
import anthropic
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()
app = FastAPI()

Path("output").mkdir(exist_ok=True)

app.mount("/output", StaticFiles(directory="output"), name="output")

# ── API Keys (환경변수로 주입) ───────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")  # Adam

jobs: dict = {}  # job_id → status dict


# ── Request Model ────────────────────────────────────────────
class GenerateRequest(BaseModel):
    url: str


# ── 1. 블로그 크롤링 ─────────────────────────────────────────
async def fetch_blog_text(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(url, headers=headers)
        html = r.text

    # 간단 태그 제거
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:8000]


# ── 2. Claude 대본 생성 ──────────────────────────────────────
def generate_script(raw_text: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""아래 블로그 글을 60초짜리 한국어 숏폼 영상 대본으로 만들어줘.

규칙:
- 총 5개 장면(scene)으로 구성
- 각 장면: narration(한국어, 10~15초 분량) + image_prompt(반드시 영어로만, 50단어 이내, 사람 얼굴/실존인물 제외, 풍경/사물/추상 위주)
- JSON만 출력, 마크다운 없이

출력 형식:
{{
  "title": "영상 제목",
  "scenes": [
    {{"narration": "...", "image_prompt": "..."}},
    ...
  ]
}}

블로그 내용:
{raw_text}"""

    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)


# ── 3. ElevenLabs TTS ────────────────────────────────────────
async def text_to_speech(text: str, out_path: str):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        Path(out_path).write_bytes(r.content)


# ── 4. DALL-E 3 이미지 생성 ──────────────────────────────────
async def generate_image(prompt: str, out_path: str):
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "gpt-image-1",
        "prompt": prompt[:800] + ". Vertical 9:16, cinematic, vibrant.",
        "size": "1024x1792",
        "quality": "low",
        "output_format": "png",
        "n": 1,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://api.openai.com/v1/images/generations",
            headers=headers,
            json=payload,
        )
        if r.status_code != 200:
            error_detail = r.text
            raise RuntimeError(f"DALL-E 오류 {r.status_code}: {error_detail}")
        import base64
        b64 = r.json()["data"][0]["b64_json"]
        Path(out_path).write_bytes(base64.b64decode(b64))


# ── 5. FFmpeg 영상 합성 ──────────────────────────────────────
def merge_to_video(scenes_data: list, work_dir: str, out_path: str):
    """각 장면: 이미지 + 오디오 → 클립 → 최종 concat"""
    clips = []
    for i, scene in enumerate(scenes_data):
        img = f"{work_dir}/img_{i}.png"
        audio = f"{work_dir}/audio_{i}.mp3"
        clip = f"{work_dir}/clip_{i}.mp4"

        # 오디오 길이 측정
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio],
            capture_output=True, text=True,
        )
        duration = float(result.stdout.strip() or "10")

        # 자막 텍스트 파일
        narration = scene["narration"].replace("'", "\\'")
        subtitle_file = f"{work_dir}/sub_{i}.txt"
        Path(subtitle_file).write_text(narration, encoding="utf-8")

        # 이미지 + 오디오 → 클립 (자막 포함)
        drawtext = (
            f"fontfile=/usr/share/fonts/truetype/nanum/NanumGothic.ttf:"
            f"textfile={subtitle_file}:"
            f"fontsize=36:fontcolor=white:"
            f"x=(w-text_w)/2:y=h-150:"
            f"box=1:boxcolor=black@0.6:boxborderw=12:"
            f"line_spacing=8:fix_bounds=1"
        )

        subprocess.run([
            "ffmpeg", "-y",
            "-loop", "1", "-i", img,
            "-i", audio,
            "-vf", f"scale=1080:1920,setsar=1,{drawtext}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-t", str(duration),
            "-pix_fmt", "yuv420p",
            clip,
        ], check=True)
        clips.append(clip)

    # concat
    list_file = f"{work_dir}/list.txt"
    with open(list_file, "w") as f:
        for c in clips:
            f.write(f"file '{c}'\n")

    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_file, "-c", "copy", out_path,
    ], check=True)


# ── 백그라운드 파이프라인 ────────────────────────────────────
async def run_pipeline(job_id: str, url: str):
    work_dir = f"output/{job_id}"
    Path(work_dir).mkdir(parents=True, exist_ok=True)

    def update(step: str, pct: int):
        jobs[job_id].update({"step": step, "pct": pct})

    try:
        update("블로그 크롤링 중...", 5)
        raw_text = await fetch_blog_text(url)

        update("AI 대본 작성 중...", 15)
        script = generate_script(raw_text)
        jobs[job_id]["title"] = script.get("title", "숏폼 영상")
        scenes = script["scenes"]

        # 병렬로 TTS + 이미지 생성
        total = len(scenes)
        for i, scene in enumerate(scenes):
            pct = 20 + int((i / total) * 55)
            update(f"장면 {i+1}/{total} — 음성·이미지 생성 중...", pct)
            await asyncio.gather(
                text_to_speech(scene["narration"], f"{work_dir}/audio_{i}.mp3"),
                generate_image(scene["image_prompt"], f"{work_dir}/img_{i}.png"),
            )

        update("영상 합성 중 (FFmpeg)...", 80)
        out_path = f"{work_dir}/result.mp4"
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, merge_to_video, scenes, work_dir, out_path)

        jobs[job_id].update({
            "step": "완료",
            "pct": 100,
            "done": True,
            "file": f"/output/{job_id}/result.mp4",
        })

    except Exception as e:
        jobs[job_id].update({"step": f"오류: {e}", "pct": 0, "error": True})


# ── API 엔드포인트 ───────────────────────────────────────────
@app.post("/generate")
async def generate(req: GenerateRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"step": "시작...", "pct": 0, "done": False}
    background_tasks.add_task(run_pipeline, job_id, req.url)
    return {"job_id": job_id}


@app.get("/status/{job_id}")
async def status(job_id: str):
    return jobs.get(job_id, {"error": "job not found"})


@app.get("/download/{job_id}")
async def download(job_id: str):
    path = f"output/{job_id}/result.mp4"
    if Path(path).exists():
        return FileResponse(path, media_type="video/mp4", filename="shortform.mp4")
    return JSONResponse({"error": "not ready"}, status_code=404)


# ── UI ───────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index():
    return Path("templates/index.html").read_text(encoding="utf-8")
