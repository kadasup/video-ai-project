"""線1 直播即時字幕 PoC —— 本地字幕轉播 server。

角色很單純：接收任何一個 client 送來的字幕文字，轉播給其他所有連線的 client。
- manual_test_producer.py / mic_stt_client.py 是「送字幕」的一方
- OBS 瀏覽器來源開的 caption.html 是「顯示字幕」的一方
兩邊都連到這支 server，server 本身不判斷字幕內容、不做任何 STT 邏輯。
"""
import asyncio
import json

import websockets

HOST = "localhost"
PORT = 8765

CLIENTS = set()


async def handler(ws):
    CLIENTS.add(ws)
    print(f"[caption_server] client 連線，目前 {len(CLIENTS)} 個連線")
    try:
        async for message in ws:
            dead = set()
            for client in CLIENTS:
                if client is ws:
                    continue
                try:
                    await client.send(message)
                except websockets.ConnectionClosed:
                    dead.add(client)
            CLIENTS.difference_update(dead)
    finally:
        CLIENTS.discard(ws)
        print(f"[caption_server] client 離線，剩 {len(CLIENTS)} 個連線")


async def main():
    async with websockets.serve(handler, HOST, PORT):
        print(f"[caption_server] 監聽 ws://{HOST}:{PORT}")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
