"""
一次性維護：清掉舊版 speech 快取裡的 Whisper 幻覺逐字稿（initial_prompt 回吐），
並把受影響影片從搜尋索引移除，讓 indexer 下次重轉重建。

背景：2026-07-10 上午修掉 initial_prompt 之前轉的逐字稿，無人聲片段會出現
「請用繁體中文。」這類幻覺文字，殘留在 analysis_cache.json 的 speech1 命名空間。
"""
import json
import re
import sqlite3
from pathlib import Path

CACHE = Path(r"D:\VideoAI\logs\analysis_cache.json")
DB    = Path(r"D:\VideoAI\search\index.db")
VIDEOS_DIR = Path(r"D:\VideoAI\input\videos")

BAD = ["請用繁體中文", "字幕由Amara", "谢谢观看", "謝謝觀看"]


def main():
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    purged = []
    for key in list(cache):
        if not key.startswith("speech1:"):
            continue
        val = cache[key] or {}
        full = val.get("full_text", "")
        if any(b in full for b in BAD):
            purged.append(key)
            del cache[key]
    if purged:
        CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    print(f"清除 {len(purged)} 筆幻覺快取")

    if not DB.exists():
        return
    con = sqlite3.connect(DB)
    removed = 0
    for key in purged:
        # key 格式 speech1:<絕對路徑>|<mtime>|<size>
        m = re.match(r"speech1:(.+)\|\d+\|\d+$", key)
        if not m:
            continue
        try:
            rel = str(Path(m.group(1)).relative_to(VIDEOS_DIR))
        except ValueError:
            continue
        row = con.execute("SELECT id FROM videos WHERE relpath=?", (rel,)).fetchone()
        if row:
            con.execute("DELETE FROM segments WHERE video_id=?", (row[0],))
            con.execute("DELETE FROM videos WHERE id=?", (row[0],))
            removed += 1
            print(f"  移出索引待重建：{rel}")
    con.commit()
    con.close()
    print(f"索引移除 {removed} 支，跑一次 indexer.py 會重轉重建")


if __name__ == "__main__":
    main()
