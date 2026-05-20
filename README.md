# Blog → 숏폼 변환기

블로그 URL 하나로 60초짜리 세로형 mp4를 자동 생성합니다.

## 파이프라인

```
블로그 URL
  → 크롤링 (httpx)
  → Claude Sonnet — 5장면 대본 생성
  → ElevenLabs TTS — 장면별 음성 (병렬)
  → DALL-E 3 — 장면별 이미지 1080×1920 (병렬)
  → FFmpeg — 이미지+음성+자막 합성 → mp4
```

## 설치

### 필수 사전 설치
```bash
# macOS
brew install ffmpeg

# Ubuntu
sudo apt install ffmpeg fonts-nanum
```

### 파이썬 패키지
```bash
pip install -r requirements.txt
```

### API 키 설정
```bash
cp .env.example .env
# .env 파일 열어서 키 3개 입력
```

| 키 | 발급처 |
|---|---|
| ANTHROPIC_API_KEY | https://console.anthropic.com |
| ELEVENLABS_API_KEY | https://elevenlabs.io |
| OPENAI_API_KEY | https://platform.openai.com |

## 실행

```bash
bash run.sh
# → http://localhost:8000 접속
```

## 비용 (60초 영상 1편 기준)

| 항목 | 단가 |
|---|---|
| Claude Sonnet (대본) | ~₩50 |
| ElevenLabs (TTS 5장면) | ~₩80 |
| DALL-E 3 (이미지 5장) | ~₩350 |
| **합계** | **~₩480** |

## 한국어 TTS 변경

ElevenLabs에서 한국어 지원 Voice ID를 찾아 `.env`의  
`ELEVENLABS_VOICE_ID` 값을 교체하면 됩니다.

또는 **Clova Voice**로 교체하려면 `main.py`의  
`text_to_speech()` 함수만 수정하면 됩니다.

## 구조

```
blog-shortform/
├── main.py          # FastAPI 앱 (파이프라인 전체)
├── templates/
│   └── index.html   # 웹 UI
├── output/          # 생성된 영상 저장
├── requirements.txt
├── .env.example
└── run.sh
```
