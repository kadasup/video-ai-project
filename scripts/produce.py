"""
社會新聞短影音產製腳本（監視器 / 社會案件用）
輸出格式：1080×1920 直式，68 秒（片頭 3s + 主畫面 60s + 片尾 5s）

用法：
  # 無影片先測流程（黑底）
  python produce.py --article D:/VideoAI/input/news.txt

  # 有監視器影片（可指定多支，依序銜接補滿主畫面秒數）
  python produce.py --article D:/VideoAI/input/news.txt --video D:/VideoAI/input/cctv1.mp4 D:/VideoAI/input/cctv2.mp4 --start 145 0

安裝依賴：
  pip install openai edge-tts
  winget install Gyan.FFmpeg  （ffmpeg 要在 PATH）

環境變數：
  OPENAI_API_KEY=sk-...
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from openai import AzureOpenAI
import edge_tts

# ── 路徑設定 ─────────────────────────────────────────────────────────────────
BASE      = Path(r"D:\VideoAI")
TEMPLATES = BASE / "templates"
OUTPUT    = BASE / "output"
TMP       = BASE / "tmp"

INTRO_PNG = TEMPLATES / "intro.png"    # 片頭圖卡（設計師提供）
OUTRO_PNG = TEMPLATES / "outro.png"    # 片尾圖卡（設計師提供）
LOGO_PNG  = TEMPLATES / "logo.png"     # 角標（標準命名，找不到時退回下方實際素材）
BGM_MP3   = TEMPLATES / "bgm.mp3"     # 背景音樂
FONT_TTF  = TEMPLATES / "font.ttf"    # 字幕字型
# 沒有設計師素材時，自動用黑底 / 系統字型暫代，流程照跑

# 設計師實際提供的角標素材（檔名跟標準命名對不上，依序找）
LOGO_CANDIDATES = [
    LOGO_PNG,
    TEMPLATES / "自由時報1080Logo.png",
    TEMPLATES / "自由時報720Logo.png",
    TEMPLATES / "自由電子報LOGO.png",
]

# ── 影片規格 ──────────────────────────────────────────────────────────────────
W, H       = 1080, 1920
INTRO_SEC  = 3
OUTRO_SEC  = 5
MAIN_SEC   = 60   # 主畫面目標秒數；來源影片不足時後段補黑幕並提醒
TTS_VOICE  = "zh-TW-HsiaoChenNeural"
TTS_RATE   = "+8%"    # 新聞播報語速略快於預設，貼近主播腔
TTS_PITCH  = "+0Hz"

# 可選台灣腔聲音（edge-tts 目前只有這三個 zh-TW）
TTS_VOICES = {
    "hsiaochen": "zh-TW-HsiaoChenNeural",  # 女聲（預設）
    "hsiaoyu":   "zh-TW-HsiaoYuNeural",    # 女聲
    "yunjhe":    "zh-TW-YunJheNeural",     # 男聲，較穩重
}

# ── Windows fallback 字型（沒有 font.ttf 時用）────────────────────────────────
SYSTEM_FONT = r"C:/Windows/Fonts/msjh.ttc"  # 微軟正黑體

def _client() -> AzureOpenAI:
    """延遲建立 AzureOpenAI client，讀 D:\\VideoAI\\.env 的設定"""
    return AzureOpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"),
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step 1：生成腳本
# ─────────────────────────────────────────────────────────────────────────────
def generate_script(article: str) -> dict:
    """新聞稿 → JSON 腳本（標題、地點、旁白、字幕、語意分段）"""
    prompt = f"""你是台灣電視新聞影音編輯。根據以下新聞稿，產出一支 {MAIN_SEC} 秒短影音的播報腳本。

輸出 JSON（嚴格依此格式，不要加任何說明）：
{{
  "title": "片頭標題（20 字內，吸睛但不煽情）",
  "location": "地點（縣市＋路段，10 字內）",
  "narration": "旁白全文（繁體中文，約 {int(MAIN_SEC * 4.5)} 字）",
  "subtitles": [
    {{"index": 1, "start": "00:00:00,000", "end": "00:00:03,500", "text": "第一句"}},
    {{"index": 2, "start": "00:00:03,500", "end": "00:00:07,000", "text": "第二句"}}
  ],
  "segments": [
    {{"start": 0, "end": 18, "desc": "這段旁白在講什麼（例：案發經過，嫌犯持刀進入超商）"}},
    {{"start": 18, "end": 40, "desc": "嫌犯特徵與逃逸方向"}},
    {{"start": 40, "end": {MAIN_SEC}, "desc": "警方調閱監視器追查"}}
  ]
}}

旁白播報文體規則（台灣電視新聞播報腔）：
- 字數 {int(MAIN_SEC * 4.3)}~{int(MAIN_SEC * 4.7)} 字（新聞播報語速約每秒 4.5 字，唸完剛好 {MAIN_SEC} 秒，過短會有無聲空檔）
- 第一句先破題：時間＋地點＋發生什麼事，一句話講完（如「台南安平一間超商昨天下午遭搶」）
- 結構：破題 → 經過細節 → 傷亡損失 → 警方處置 → 收尾（後續發展或呼籲）
- 全部用口語短句，每句不超過 20 字，避免書面語（「肇事」可以，「肇因於」不行）
- 數字用口語唸法：「三萬元」不寫「30,000元」、「七十歲」不寫「70歲」
- 平鋪直敘、不帶評論、不煽情，但語句要有播報的節奏感
- 匿名原則：嫌疑人用「王姓男子」「張姓嫌犯」，不寫全名

字幕規則：
- 每句 8~12 字（嚴格上限 12 字，超過會爆出畫面）
- 時間戳從 00:00:00,000 到 {MAIN_SEC} 秒，涵蓋整段旁白
- 文字與旁白一字不差

segments 規則（給後續自動選片配畫面用）：
- 把旁白依內容切成 3~5 個語意段落，start/end 為秒數，必須從 0 連續涵蓋到 {MAIN_SEC}
- desc 描述該段旁白的畫面需求，具體寫出人事物與動作（如「嫌犯亮刀威脅店員」而非「案發」）

新聞稿：
{article}"""

    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.2")
    resp = _client().chat.completions.create(
        model=deployment,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_completion_tokens=4096,
    )
    usage = {}
    if resp.usage:
        usage = {
            "prompt_tokens":     resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "total_tokens":      resp.usage.total_tokens,
        }
    return json.loads(resp.choices[0].message.content), usage


# ─────────────────────────────────────────────────────────────────────────────
# Step 2：TTS（逐句合成＋句間停頓，播報節奏更像真人）
# ─────────────────────────────────────────────────────────────────────────────
PAUSE_SENTENCE_MS = 280   # 句號/驚嘆/問號後追加的停頓（edge-tts 本身已有小停頓，這是額外的）

async def _tts_async(text: str, path: Path, voice: str, rate: str, pitch: str):
    comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    await comm.save(str(path))


def _split_sentences(text: str) -> list[str]:
    """依句末標點切句（。！？；），標點跟著前句；
    過濾掉沒有實質文字內容的片段（純標點/引號會讓 edge-tts 回 No audio）"""
    parts = re.split(r"(?<=[。！？；])", text.replace("\n", ""))
    return [p.strip() for p in parts
            if p.strip() and re.search(r"[一-鿿A-Za-z0-9]", p)]


async def _tts_sentences_async(sentences: list[str], voice: str,
                                rate: str, pitch: str) -> list[Path]:
    """句子並行合成（最多 4 條同時，避免服務拒絕），單句失敗自動重試一次"""
    TMP.mkdir(parents=True, exist_ok=True)
    paths = [TMP / f"_tts_sent_{i}.mp3" for i in range(len(sentences))]
    sem = asyncio.Semaphore(4)

    async def one(s: str, p: Path):
        async with sem:
            for attempt in (1, 2):
                try:
                    comm = edge_tts.Communicate(s, voice, rate=rate, pitch=pitch)
                    await comm.save(str(p))
                    return
                except Exception:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.8)

    await asyncio.gather(*(one(s, p) for s, p in zip(sentences, paths)))
    return paths


def generate_tts(text: str, path: Path, voice: str = TTS_VOICE,
                  rate: str = TTS_RATE, pitch: str = TTS_PITCH):
    """
    逐句合成＋句間插入 PAUSE_SENTENCE_MS 靜音再串接。
    句子只有一句、切句失敗、或逐句合成出錯時，一律退回整段單次合成（確保不炸流程）。
    """
    sentences = _split_sentences(text)
    if len(sentences) <= 1:
        asyncio.run(_tts_async(text, path, voice, rate, pitch))
        return

    try:
        pieces = asyncio.run(_tts_sentences_async(sentences, voice, rate, pitch))
        pieces = [p for p in pieces if p.exists() and p.stat().st_size > 0]
    except Exception:
        pieces = []
    if not pieces:
        asyncio.run(_tts_async(text, path, voice, rate, pitch))
        return

    silence = TMP / "_tts_silence.mp3"
    try:
        # 句間靜音檔（產生一次重複使用）
        ff("-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=24000",
           "-t", f"{PAUSE_SENTENCE_MS / 1000:.3f}", "-c:a", "libmp3lame", "-q:a", "9", silence)

        # 交錯串接：句1 靜音 句2 靜音 ... 句N
        inputs: list = []
        for i, p in enumerate(pieces):
            if i > 0:
                inputs.append(silence)
            inputs.append(p)

        args: list = []
        for p in inputs:
            args += ["-i", str(p)]
        ff(*args,
           "-filter_complex",
           "".join(f"[{i}:a]" for i in range(len(inputs))) + f"concat=n={len(inputs)}:v=0:a=1[a]",
           "-map", "[a]", "-c:a", "libmp3lame", "-q:a", "4", path)
    except Exception:
        # 串接失敗 → 退回整段單次合成，不讓產製流程掛掉
        asyncio.run(_tts_async(text, path, voice, rate, pitch))
    finally:
        for p in pieces + [silence]:
            try:
                p.unlink()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# 字幕時間軸縮放（旁白實際長度 ≠ 腳本假設的 MAIN_SEC 時用）
# ─────────────────────────────────────────────────────────────────────────────
def _ts_to_sec(t: str) -> float:
    """'00:00:03,500' → 3.5"""
    h, m, rest = t.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _sec_to_ts(sec: float) -> str:
    """3.5 → '00:00:03,500'"""
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def rescale_subtitles(subtitles: list, from_total: float, to_total: float) -> list:
    """把 0~from_total 的字幕時間軸等比縮放到 0~to_total"""
    if from_total <= 0 or abs(from_total - to_total) < 0.3:
        return subtitles
    k = to_total / from_total
    out = []
    for s in subtitles:
        out.append({**s,
                    "start": _sec_to_ts(_ts_to_sec(s["start"]) * k),
                    "end":   _sec_to_ts(min(_ts_to_sec(s["end"]) * k, to_total))})
    return out


def rescale_segments(segments: list, from_total: float, to_total: float) -> list:
    """把語意分段（數字秒）等比縮放"""
    if from_total <= 0 or abs(from_total - to_total) < 0.3:
        return segments
    k = to_total / from_total
    return [{**seg, "start": round(seg["start"] * k, 1),
             "end": round(min(seg["end"] * k, to_total), 1)} for seg in segments]


# ─────────────────────────────────────────────────────────────────────────────
# Step 3：字幕檔（寫 .ass，不是 .srt）
# ─────────────────────────────────────────────────────────────────────────────
# 用 .ass 而非 .srt 是刻意的：ffmpeg 的 subtitles 濾鏡把純 .srt 自動轉檔時，
# 內部假設的座標系統（PlayResX/Y）跟實際輸出畫面尺寸對不上，
# 會導致 force_style 的 MarginV 被錯誤放大，字幕整個被推出畫面外（實測驗證過）。
# .ass 檔頭明確宣告 PlayResX/PlayResY = 輸出尺寸，就不會有這個問題。
def _srt_time_to_ass(t: str) -> str:
    """'00:00:05,000' → '0:00:05.00'（ASS 時間戳只到百分秒，且不補零到兩位小時）"""
    h, m, rest = t.split(":")
    s, ms = rest.split(",")
    centis = int(ms) // 10
    return f"{int(h)}:{m}:{s}.{centis:02d}"


def write_ass(subtitles: list, path: Path) -> Path:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft JhengHei,84,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,3,1,2,20,20,280,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"""

    MAX_CHARS = 12  # 84 級字在 1080 寬的安全上限，超過自動斷行

    def _wrap(text: str) -> str:
        text = text.replace("\n", "")
        if len(text) <= MAX_CHARS:
            return text
        mid = (len(text) + 1) // 2  # 對半斷，兩行都不超過上限（字幕最長 ~24 字）
        return text[:mid] + "\\N" + text[mid:]

    lines = [header]
    for s in subtitles:
        start = _srt_time_to_ass(s["start"])
        end = _srt_time_to_ass(s["end"])
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{_wrap(s['text'])}")

    path.write_text("\n".join(lines), encoding="utf-8-sig")
    return path


# ─────────────────────────────────────────────────────────────────────────────
# ffmpeg 工具
# ─────────────────────────────────────────────────────────────────────────────
def _ffmpeg_exe() -> str:
    """找 ffmpeg，優先 PATH，退回 winget 安裝路徑"""
    import shutil
    found = shutil.which("ffmpeg")
    if found:
        return found
    winget_path = (
        r"C:\Users\kevin\AppData\Local\Microsoft\WinGet\Packages"
        r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        r"\ffmpeg-8.1.2-full_build\bin\ffmpeg.exe"
    )
    if Path(winget_path).exists():
        return winget_path
    raise FileNotFoundError("找不到 ffmpeg，請確認已安裝並在 PATH 中")


def _ffprobe_exe() -> str:
    """找 ffprobe，優先 PATH，退回跟 ffmpeg 同一個資料夾"""
    import shutil
    found = shutil.which("ffprobe")
    if found:
        return found
    return _ffmpeg_exe().replace("ffmpeg.exe", "ffprobe.exe")


def _probe_duration(path: Path) -> float:
    """讀取一支影片的總長度（秒）"""
    r = subprocess.run(
        [_ffprobe_exe(), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, encoding="utf-8"
    )
    try:
        return float(r.stdout.strip())
    except (ValueError, AttributeError):
        raise ValueError(f"無法讀取影片長度，請確認是有效的影片檔案：{path}")


def resolve_logo_path() -> Path | None:
    """找角標素材：優先標準命名 logo.png，找不到就退回設計師實際提供的檔名"""
    for candidate in LOGO_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def ff(*args, show=False):
    """執行 ffmpeg，出錯直接停"""
    cmd = [_ffmpeg_exe(), "-y"] + [str(a) for a in args]
    if not show:
        cmd += ["-loglevel", "error"]
    subprocess.run(cmd, check=True)


def esc(p: Path) -> str:
    """ffmpeg 濾鏡用路徑：反斜線→正斜線，冒號加跳脫"""
    return str(p).replace("\\", "/").replace(":", "\\:")


def font_path() -> str:
    """取得可用字型路徑（優先 templates/font.ttf，退回系統字型）"""
    if FONT_TTF.exists():
        return esc(FONT_TTF)
    system_font_path = Path(SYSTEM_FONT.replace("/", "\\"))
    if system_font_path.exists():
        return esc(system_font_path)
    return ""

def sub_style() -> str:
    """
    字幕的字級/顏色/位置已經寫進 .ass 檔頭（write_ass 的 Style 行），這裡只在
    有設計師自訂字型檔（templates/font.ttf）時才需要覆寫 FontFile；
    系統字型走 .ass 內建的 Fontname=Microsoft JhengHei 即可，不必覆寫。
    """
    if FONT_TTF.exists():
        return f"FontFile={esc(FONT_TTF)}"
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Step 4a：片頭
# ─────────────────────────────────────────────────────────────────────────────
def make_intro(script: dict) -> Path:
    out = TMP / "intro.mp4"
    title    = script["title"].replace("'", "\\'").replace(":", "\\:")
    location = script["location"].replace("'", "\\'")
    fp = font_path()
    font_arg = f":fontfile='{fp}'" if fp else ""

    text_filter = (
        f"drawtext=text='{title}'{font_arg}:fontsize=68:fontcolor=white"
        f":x=(w-text_w)/2:y=(h-text_h)/2-60:shadowcolor=black:shadowx=2:shadowy=2,"
        f"drawtext=text='{location}'{font_arg}:fontsize=42:fontcolor=white@0.85"
        f":x=(w-text_w)/2:y=(h-text_h)/2+40"
    )

    if INTRO_PNG.exists():
        ff(
            "-loop", "1", "-i", INTRO_PNG,
            "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex", f"[0:v]scale={W}:{H},{text_filter}[v]",
            "-map", "[v]", "-map", "1:a",
            "-t", INTRO_SEC, "-r", "30", "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-c:a", "aac", "-ar", "44100",
            out
        )
    else:
        # 黑底暫代
        ff(
            "-f", "lavfi", "-i", f"color=c=black:size={W}x{H}:rate=30",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex", f"[0:v]{text_filter}[v]",
            "-map", "[v]", "-map", "1:a",
            "-t", INTRO_SEC, "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-c:a", "aac", "-ar", "44100",
            out
        )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Step 4b：主畫面
# ─────────────────────────────────────────────────────────────────────────────
def _sub_filter(srt: Path) -> str:
    """字幕濾鏡字串。位置/字級/顏色寫在 .ass 檔頭（write_ass），
    這裡只在有自訂字型檔時才 force_style 覆寫 FontFile。"""
    srt_esc = esc(srt)
    style = sub_style()
    return f"subtitles='{srt_esc}':force_style='{style}'" if style else f"subtitles='{srt_esc}'"


def _mix_audio(tts: Path, audio_out: Path, main_sec: float):
    """TTS + BGM 混音（無 BGM 時只轉檔），長度切齊 main_sec"""
    if BGM_MP3.exists():
        ff(
            "-i", tts,
            "-stream_loop", "-1", "-i", BGM_MP3,
            "-filter_complex",
            "[0:a]volume=1.0[tts];[1:a]volume=0.08[bgm];[tts][bgm]amix=inputs=2:duration=first[a]",
            "-map", "[a]", "-t", main_sec,
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            audio_out
        )
    else:
        ff(
            "-i", tts, "-t", main_sec,
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            audio_out
        )


def make_main(tts: Path, srt: Path, sources: list[dict] | None = None,
              main_sec: float = MAIN_SEC) -> tuple[Path, dict]:
    """
    sources: [{"path": "...", "start": 12.0}, ...] 依序使用，不足 main_sec 秒的部分自動補黑幕。
             傳 None 或空 list = 全黑幕（測試流程）。
    main_sec: 主畫面實際長度（依旁白 TTS 實際長度動態決定，預設 MAIN_SEC）。

    回傳 (輸出路徑, 長度資訊 dict)：
      length_info = {
        "target_sec":    目標秒數,
        "used_sources":  實際用上的來源數,
        "covered_sec":   來源真正覆蓋到的秒數,
        "shortfall_sec": 不足、補黑幕的秒數,
        "insufficient":  bool，是否有補黑幕,
      }
    """
    out       = TMP / "main.mp4"
    audio_out = TMP / "audio.aac"
    sub_f = _sub_filter(srt)
    _mix_audio(tts, audio_out, main_sec)

    # ── 依序分配每支來源可用秒數，湊不滿就補黑幕（結尾）─────────────────────────
    sources = sources or []
    plan: list[dict] = []
    remaining = float(main_sec)

    for src in sources:
        if remaining <= 0.05:
            break
        path = Path(src["path"])
        start = float(src.get("start", 0) or 0)
        try:
            dur = _probe_duration(path)
        except ValueError:
            continue  # 探測失敗就跳過這支，不中斷整體流程
        avail = max(0.0, dur - start)
        if avail <= 0.05:
            continue
        alloc = min(avail, remaining)
        plan.append({"path": path, "start": start, "dur": alloc})
        remaining -= alloc

    if remaining > 0.05:
        plan.append({"path": None, "start": 0, "dur": remaining})

    length_info = _plan_length_info(plan, main_sec)
    _assemble_main(plan, audio_out, sub_f, out, main_sec)
    return out, length_info


def make_main_plan(tts: Path, srt: Path, plan: list[dict],
                   main_sec: float = MAIN_SEC) -> tuple[Path, dict]:
    """
    依智慧配對產生的時間軸組裝主畫面。
    plan: [{"path": str|None, "start": float, "dur": float}, ...]
          path=None 代表該段配不到畫面、用黑幕；黑幕可以出現在任何位置。
    """
    out       = TMP / "main.mp4"
    audio_out = TMP / "audio.aac"
    sub_f = _sub_filter(srt)
    _mix_audio(tts, audio_out, main_sec)

    norm_plan = [
        {"path": (Path(e["path"]) if e.get("path") else None),
         "start": float(e.get("start", 0) or 0),
         "dur": float(e["dur"])}
        for e in plan if float(e.get("dur", 0)) > 0.05
    ]
    length_info = _plan_length_info(norm_plan, main_sec)
    _assemble_main(norm_plan, audio_out, sub_f, out, main_sec)
    return out, length_info


def _plan_length_info(plan: list[dict], target_sec: float = MAIN_SEC) -> dict:
    covered = sum(e["dur"] for e in plan if e.get("path"))
    black   = sum(e["dur"] for e in plan if not e.get("path"))
    return {
        "target_sec":    round(target_sec, 1),
        "used_sources":  len({str(e["path"]) for e in plan if e.get("path")}),
        "covered_sec":   round(covered, 1),
        "shortfall_sec": round(black, 1),
        "insufficient":  black > 0.05,
    }


def _assemble_main(plan: list[dict], audio_out: Path, sub_f: str, out: Path,
                   main_sec: float = MAIN_SEC):
    """把 plan 的片段（影片/黑幕交錯皆可）串接 → 上字幕 → 疊角標 → 混音輸出"""
    logo_path = resolve_logo_path()
    has_logo  = logo_path is not None
    crop_scale = f"crop=ih*9/16:ih,scale={W}:{H}"

    args: list = []
    filter_parts: list[str] = []
    seg_labels: list[str] = []
    idx = 0

    for e in plan:
        if e.get("path"):
            args += ["-ss", f"{e['start']}", "-i", str(e["path"])]
            filter_parts.append(
                f"[{idx}:v]trim=duration={e['dur']:.2f},setpts=PTS-STARTPTS,{crop_scale},setsar=1[seg{idx}]"
            )
        else:
            args += ["-f", "lavfi", "-i",
                     f"color=c=black:size={W}x{H}:rate=30:duration={e['dur']:.2f}"]
            filter_parts.append(f"[{idx}:v]setsar=1[seg{idx}]")
        seg_labels.append(f"[seg{idx}]")
        idx += 1

    audio_idx = idx
    args += ["-i", str(audio_out)]
    idx += 1

    filter_parts.append("".join(seg_labels) + f"concat=n={len(seg_labels)}:v=1:a=0[vraw]")

    if has_logo:
        logo_idx = idx
        args += ["-i", str(logo_path)]
        idx += 1
        filter_parts.append(f"[vraw]{sub_f}[vs]")
        filter_parts.append(f"[vs][{logo_idx}:v]overlay=main_w-overlay_w-30:60[vout]")
    else:
        filter_parts.append(f"[vraw]{sub_f}[vout]")

    ff(
        *args,
        "-filter_complex", ";".join(filter_parts),
        "-map", "[vout]", "-map", f"{audio_idx}:a",
        "-t", main_sec, "-r", "30", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-c:a", "copy",
        out
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step 4c：片尾
# ─────────────────────────────────────────────────────────────────────────────
def make_outro() -> Path:
    out = TMP / "outro.mp4"
    if OUTRO_PNG.exists():
        ff(
            "-loop", "1", "-i", OUTRO_PNG,
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex", f"[0:v]scale={W}:{H}[v]",
            "-map", "[v]", "-map", "1:a",
            "-t", OUTRO_SEC, "-r", "30", "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-c:a", "aac", "-ar", "44100",
            out
        )
    else:
        ff(
            "-f", "lavfi", "-i", f"color=c=black:size={W}x{H}:rate=30",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-map", "0:v", "-map", "1:a",
            "-t", OUTRO_SEC, "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-c:a", "aac", "-ar", "44100",
            out
        )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Step 5：串接
# ─────────────────────────────────────────────────────────────────────────────
def concat(clips: list[Path], out: Path):
    lst = TMP / "concat.txt"
    lst.write_text(
        "\n".join(f"file '{str(c).replace(chr(92), '/')}'" for c in clips),
        encoding="utf-8"
    )
    ff("-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out)


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="社會新聞短影音產製")
    parser.add_argument("--article", required=True, help="新聞稿 .txt 路徑")
    parser.add_argument("--video",   nargs="*", default=[], help="監視器影片路徑，可指定多個依序銜接")
    parser.add_argument("--start",   nargs="*", type=float, default=[], help="對應每支影片的起始秒數，缺省補 0")
    parser.add_argument("--name",    default=None,  help="輸出檔名（預設用標題）")
    parser.add_argument("--voice",   default="hsiaochen", choices=list(TTS_VOICES),
                        help="旁白聲音（預設 hsiaochen）")
    args = parser.parse_args()

    TMP.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)

    article_path = Path(args.article)
    if not article_path.exists():
        sys.exit(f"❌ 找不到新聞稿：{article_path}")

    article = article_path.read_text(encoding="utf-8")
    sources = [
        {"path": v, "start": (args.start[i] if i < len(args.start) else 0)}
        for i, v in enumerate(args.video)
    ]

    print("① 生成播報腳本 (GPT-4o mini)...")
    script, _ = generate_script(article)
    print(f"   標題：{script['title']}")
    print(f"   地點：{script['location']}")
    print(f"   旁白字數：{len(script['narration'])} 字")

    tts_path = TMP / "narration.mp3"
    srt_path = TMP / "subtitles.ass"

    print("② 生成旁白音訊 (edge-tts)...")
    generate_tts(script["narration"], tts_path, voice=TTS_VOICES[args.voice])

    print("③ 寫字幕檔...")
    write_ass(script["subtitles"], srt_path)

    print("④ 組裝片頭 (3秒)...")
    intro = make_intro(script)

    print(f"⑤ 組裝主畫面 ({MAIN_SEC}秒)...")
    main_clip, length_info = make_main(tts_path, srt_path, sources)
    if length_info["insufficient"]:
        print(f"   ⚠️ 來源影片總長度只有 {length_info['covered_sec']} 秒，"
              f"不足 {MAIN_SEC} 秒，其餘 {length_info['shortfall_sec']} 秒為黑幕空景")

    print("⑥ 組裝片尾 (5秒)...")
    outro = make_outro()

    safe_name = re.sub(r'[\\/:*?"<>|]', "-", script["title"])[:20]
    out_path = OUTPUT / (args.name or f"{safe_name}.mp4")

    print("⑦ 串接輸出...")
    concat([intro, main_clip, outro], out_path)

    # 清暫存
    for f in TMP.iterdir():
        try:
            f.unlink()
        except Exception:
            pass

    print(f"\n✅ 完成：{out_path}")
    print(f"   總長：{INTRO_SEC + MAIN_SEC + OUTRO_SEC} 秒")


if __name__ == "__main__":
    main()
