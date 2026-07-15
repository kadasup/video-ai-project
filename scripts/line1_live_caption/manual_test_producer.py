"""線1 PoC 階段②測試工具 —— 不用麥克風、不用任何 STT/雲端金鑰。

手動輸入文字，按 Enter 送出，驗證「caption_server → OBS 瀏覽器來源」這段顯示
管線本身（樣式、位置、有沒有正確更新）沒問題。等這段確定沒問題，
再換成 mic_stt_client.py 接真正的語音辨識。
"""
import asyncio
import json

import websockets

SERVER_URI = "ws://localhost:8765"


async def main():
    async with websockets.connect(SERVER_URI) as ws:
        print("已連上 caption_server，輸入文字按 Enter 送出字幕（Ctrl+C 結束）")
        loop = asyncio.get_event_loop()
        while True:
            text = await loop.run_in_executor(None, input, "> ")
            await ws.send(json.dumps({"text": text}))


if __name__ == "__main__":
    asyncio.run(main())
