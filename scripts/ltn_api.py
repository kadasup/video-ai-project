"""
LTN 內部 API 串接：後臺影音清單、文章內容 → 匯入 VideoAI 自動化測試用。

兩支 API：
  1. getvideos：依時間區段列出後臺影音清單（含影片實際存放位置）
  2. getESNewsDetail：依 articleNo 抓文章全文＋關鍵字
"""
import json
import re
import urllib.request
from pathlib import Path

from produce import BASE

INPUT = BASE / "input"

BACKLOG_API = "https://dev55.ltn.com.tw/staff/pelin/video/program/api/getvideos/{year}/{start}/{end}"
ARTICLE_API = "https://dev55.ltn.com.tw/staff/Chris/sportstestapi/getESNewsDetail/all/breakingnews/{article_no}"

_UA = {"User-Agent": "Mozilla/5.0"}


def _get_json(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8-sig"))


def fetch_backlog(year: str, start: str, end: str) -> list[dict]:
    """
    後臺影音清單。year=2026，start/end=MMDDHHmm（例：07070000 = 7/7 00:00）。
    每筆的 status 欄位是後臺自己的處理狀態（實測值：已完成／不處理不存），
    前端直接拿這個欄位篩選，不需要另外在本機記一份狀態。
    """
    data = _get_json(BACKLOG_API.format(year=year, start=start, end=end))
    return (data.get("data") or {}).get("items", [])


def _clean_content(raw: str) -> str:
    """去掉 <div class='ltnembed'...></div> 嵌入標籤與其他殘留 HTML，只留純文字內文"""
    text = re.sub(r"<div[^>]*class=['\"]ltnembed['\"][^>]*>.*?</div>", "", raw, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\r\n", "\n").strip()
    return text


def fetch_article(article_no: str) -> dict:
    """回傳 {content, keywords, source_url, photo_url}"""
    data = _get_json(ARTICLE_API.format(article_no=article_no))
    content = _clean_content(data.get("LTNA_Content", ""))
    keywords = [t.get("Name", "") for t in data.get("NewsArticleTag", []) if t.get("Name")]
    photo_url = (data.get("A_Photo") or {}).get("PathL", "")
    return {"content": content, "keywords": keywords,
            "source_url": data.get("LTRT_Url", ""), "photo_url": photo_url}


def download_photo(url: str, article_no: str) -> str:
    """下載新聞配圖到 input/，回傳本機路徑；已存在就跳過重下"""
    INPUT.mkdir(parents=True, exist_ok=True)
    ext = Path(url.split("?")[0]).suffix or ".jpg"
    dest = INPUT / f"photo-{article_no}{ext}"
    if not dest.exists():
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=60) as resp:
            dest.write_bytes(resp.read())
    return str(dest)


def download_video(url: str) -> str:
    """下載影片到 input/，回傳本機路徑；同檔名已存在就跳過重下（重複測試不用等）"""
    INPUT.mkdir(parents=True, exist_ok=True)
    name = url.rsplit("/", 1)[-1]
    dest = INPUT / name
    if not dest.exists():
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=120) as resp:
            dest.write_bytes(resp.read())
    return str(dest)
