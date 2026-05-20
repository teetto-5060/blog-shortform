#!/bin/bash
# blog-shortform 서버 실행 스크립트

echo "📦 패키지 설치 중..."
pip install -r requirements.txt -q

echo "🔍 FFmpeg 확인..."
if ! command -v ffmpeg &> /dev/null; then
  echo "⚠️  FFmpeg가 없습니다. 설치하세요:"
  echo "   macOS: brew install ffmpeg"
  echo "   Ubuntu: sudo apt install ffmpeg"
  exit 1
fi

echo "🔑 .env 파일 확인..."
if [ ! -f .env ]; then
  cp .env.example .env
  echo "✅ .env 파일 생성됨 — API 키를 입력하세요"
  echo "   nano .env"
  exit 0
fi

# .env 로드
export $(grep -v '^#' .env | xargs)

echo "🚀 서버 시작 → http://localhost:8000"
uvicorn main:app --reload --host 0.0.0.0 --port 8000
