import os
import re
import json
import uuid
import base64
import asyncio
from pathlib import Path

import httpx
import anthropic
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()
Path("output").mkdir(exist_ok=True)
app.mount("/output", StaticFiles(directory="output"), name="output")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")

jobs: dict = {}


class GenerateRequest(BaseModel):
    url: str


# ── 1. 블로그 크롤링 ─────────────────────────────────────────
async def fetch_blog_text(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(url, headers=headers)
        html = r.text
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
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        Path(out_path).write_bytes(r.content)


# ── 4. gpt-image-1 이미지 생성 ───────────────────────────────
async def generate_image(prompt: str, out_path: str):
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "gpt-image-1",
        "prompt": prompt[:800] + ". Vertical 9:16, cinematic, vibrant, no people.",
        "size": "1024x1536",
        "quality": "low",
        "n": 1,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://api.openai.com/v1/images/generations",
            headers=headers,
            json=payload,
        )
        if r.status_code != 200:
            raise RuntimeError(f"이미지 오류 {r.status_code}: {r.text}")
        b64 = r.json()["data"][0]["b64_json"]
        Path(out_path).write_bytes(base64.b64decode(b64))


# ── 5. moviepy 영상 합성 ─────────────────────────────────────
def merge_to_video(scenes_data: list, work_dir: str, out_path: str):
    import subprocess

    scene_videos = []
    for i in range(len(scenes_data)):
        img = f"{work_dir}/img_{i}.png"
        audio = f"{work_dir}/audio_{i}.mp3"
        scene_out = f"{work_dir}/scene_{i}.mp4"
        subprocess.run([
         "ffmpeg", "-y",
         "-loop", "1", "-i", img,
         "-i", audio,
         "-vf", "scale=480:854:force_original_aspect_ratio=decrease,pad=480:854:(ow-iw)/2:(oh-ih)/2",
         "-c:v", "libx264",
         "-preset", "ultrafast",
         "-crf", "28",
         "-c:a", "aac",
         "-shortest", scene_out
   ], check=True)
        scene_videos.append(scene_out)

    list_file = f"{work_dir}/list.txt"
    with open(list_file, "w") as f:
        for v in scene_videos:
            f.write(f"file '{v}'\n")

    subprocess.run([
    "ffmpeg", "-y", "-f", "concat", "-safe", "0",
    "-i", list_file,
    "-c:v", "libx264",
    "-preset", "ultrafast",
    "-crf", "28",
    "-c:a", "aac",
    out_path
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

        total = len(scenes)
        for i, scene in enumerate(scenes):
            pct = 20 + int((i / total) * 55)
            update(f"장면 {i+1}/{total} — 음성·이미지 생성 중...", pct)
            await asyncio.gather(
                text_to_speech(scene["narration"], f"{work_dir}/audio_{i}.mp3"),
                generate_image(scene["image_prompt"], f"{work_dir}/img_{i}.png"),
            )

        update("영상 합성 중...", 80)
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


# ── API ──────────────────────────────────────────────────────
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


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index():
    return Path("templates/index.html").read_text(encoding="utf-8")
