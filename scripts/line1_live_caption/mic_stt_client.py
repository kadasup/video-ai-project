"""線1 PoC 階段①/③ —— 麥克風收音 → gpt-realtime-whisper 即時轉錄 → 推給 caption_server。

⚠️ 目前打的是 OpenAI 官方端點（wss://api.openai.com），需要一把一般的 OpenAI API Key
（不是 Azure key）。確認好要用 Azure AI Speech 還是 Azure OpenAI Realtime API 之後，
這支腳本的連線／認證部分需要另外改寫。

已知限制（見研究彙整-國內外參考資料.md）：session 超過 9 分鐘延遲會拉高，
這裡用「每 8 分鐘重啟一次 session」當 workaround。
"""
import asyncio
import base64
import json
import os
import time

import pyaudio
import websockets
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
CAPTION_SERVER_URI = "ws://localhost:8765"
SESSION_RESTART_SECONDS = 8 * 60
SAMPLE_RATE = 24000
CHUNK = 4096

HOTWORDS_PATH = os.path.join(os.path.dirname(__file__), "hotwords.txt")


def load_hotwords_prompt():
    if not os.path.exists(HOTWORDS_PATH):
        return ""
    with open(HOTWORDS_PATH, encoding="utf-8") as f:
        words = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    if not words:
        return ""
    return "以下是台灣新聞播報，涉及人名、地名、機構：" + "、".join(words)


async def run_session(caption_ws, seconds_budget):
    uri = "wss://api.openai.com/v1/realtime?model=gpt-realtime-whisper"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    async with websockets.connect(uri, extra_headers=headers) as ws:
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "modalities": ["text"],
                "input_audio_format": "pcm16",
                "input_audio_transcription": {
                    "model": "whisper-1",
                    "language": "zh",
                    "prompt": load_hotwords_prompt(),
                },
                "turn_detection": {
                    "type": "semantic_vad",
                    "eagerness": "medium",
                },
            },
        }))

        pa = pyaudio.PyAudio()
        stream = pa.open(format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE,
                          input=True, frames_per_buffer=CHUNK)
        start = time.monotonic()

        async def send_audio():
            while time.monotonic() - start < seconds_budget:
                chunk = stream.read(CHUNK, exception_on_overflow=False)
                await ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode(),
                }))
                await asyncio.sleep(0)

        async def recv_captions():
            async for msg in ws:
                data = json.loads(msg)
                if data.get("type") == "conversation.item.input_audio_transcription.completed":
                    text = data["transcript"]
                    print(text)
                    await caption_ws.send(json.dumps({"text": text}))

        try:
            await asyncio.wait_for(
                asyncio.gather(send_audio(), recv_captions()),
                timeout=seconds_budget + 5,
            )
        except asyncio.TimeoutError:
            pass
        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()


async def main():
    if not OPENAI_API_KEY:
        raise SystemExit("請先在 .env 設定 OPENAI_API_KEY（複製 .env.example）")

    async with websockets.connect(CAPTION_SERVER_URI) as caption_ws:
        print("[mic_stt_client] 開始收音，每 8 分鐘自動重啟 session 一次")
        while True:
            await run_session(caption_ws, SESSION_RESTART_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
