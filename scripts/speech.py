"""
本地語音辨識（faster-whisper）：把記者素材裡的受訪原音轉成帶時間戳的逐字稿，
供「SOT 原音段」選句與字幕使用。

- 模型跑本機 CPU（int8），零 API 費用；結果進快取（同檔不重轉）。
- 只在影片音軌有實質人聲能量時才轉譯（先用 ffmpeg 快篩，省時間）。
"""
import json
import re
import subprocess
from pathlib import Path

from produce import _ffmpeg_exe
from select_clip import _cache_get, _cache_put

_MODEL = None
_MODEL_NAME = "medium"   # 中文品質夠上字幕；實測太慢再降 small


def _get_model():
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        _MODEL = WhisperModel(_MODEL_NAME, device="cpu", compute_type="int8")
    return _MODEL


def has_speech_energy(video: Path, threshold_db: float = -35.0) -> bool:
    """ffmpeg volumedetect 快篩：平均音量太低（近無聲）就不用花時間轉譯"""
    try:
        r = subprocess.run(
            [_ffmpeg_exe(), "-i", str(video), "-map", "0:a:0",
             "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120, encoding="utf-8", errors="ignore")
        m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", r.stderr or "")
        return bool(m) and float(m.group(1)) > threshold_db
    except Exception:
        return True   # 快篩失敗就保守放行給轉譯


def transcribe(video: Path) -> dict:
    """
    回傳 {"segments": [{"start","end","text","words":[{"start","end","word"},...]}],
          "full_text": str}
    無人聲（或音量太低）→ segments 為空。有快取。
    """
    video = Path(video)
    cached = _cache_get("speech1", video)
    if cached is not None:
        return cached

    result = {"segments": [], "full_text": ""}
    transcribed_ok = True   # 真的「確認無語音」才快取；轉譯失敗（OOM/磁碟滿等）不快取，下次重試
    if has_speech_energy(video):
        try:
            # 不給 initial_prompt：實測無清楚人聲時 Whisper 會把 prompt 內容
            # 幻覺回吐當成轉譯結果；簡繁問題交給後續 GPT 校正處理
            segs_iter, _info = _get_model().transcribe(
                str(video), language="zh", vad_filter=True,
                word_timestamps=True, condition_on_previous_text=False)
            segs = []
            for s in segs_iter:
                text = (s.text or "").strip()
                if not text or (s.no_speech_prob or 0) > 0.66:
                    continue
                segs.append({
                    "start": round(s.start, 2), "end": round(s.end, 2),
                    "text": text,
                    "words": [{"start": round(w.start, 2), "end": round(w.end, 2),
                               "word": w.word} for w in (s.words or [])],
                })
            full = "".join(x["text"] for x in segs)
            if len(_clean(full)) < 6:
                segs, full = [], ""   # 太短視同無人聲（噪音/幻覺）
            result = {"segments": segs, "full_text": full}
        except Exception:
            transcribed_ok = False   # 轉譯本身失敗（曾見：C 槽空間不足導致模型載入失敗）

    if transcribed_ok:
        _cache_put("speech1", video, result)
    return result


def _clean(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z一-鿿]", "", s)


def resolve_quote_span(transcript: dict, quote: str,
                       pad: float = 0.25) -> tuple[float, float, list] | None:
    """
    把 GPT 選的引句文字對回逐字稿時間軸，回傳 (start, end, 覆蓋到的 segments)。

    做法：把全部 segment 文字去標點串成一條長字串（記錄每個 segment 的字元
    起訖位置），用 find() 找引句的**實際起點**，再映射回起訖 segment——
    不能用「滑動視窗串接後 in 檢查」：引句落在視窗中段也會命中，
    會把前面不相干的句子一起圈進來（實測踩過：字幕跟原音對不上）。
    """
    q = _clean(quote)
    if not q or not transcript.get("segments"):
        return None
    segs = transcript["segments"]

    cleaned = [_clean(s["text"]) for s in segs]
    full = "".join(cleaned)
    idx = full.find(q)
    if idx < 0:
        # 引句可能被 GPT 少字/改字：退回用頭尾各 10 字定位
        head, tail = q[:10], q[-10:]
        i1 = full.find(head) if head else -1
        i2 = full.rfind(tail) if tail else -1
        if i1 < 0 or i2 < 0 or i2 + len(tail) <= i1:
            return None
        idx, end_idx = i1, i2 + len(tail)
    else:
        end_idx = idx + len(q)

    # 字元位置 → segment index
    si = ei = None
    pos = 0
    for i, c in enumerate(cleaned):
        seg_start, seg_end = pos, pos + len(c)
        if si is None and idx < seg_end:
            si = i
        if end_idx <= seg_end:
            ei = i
            break
        pos = seg_end
    if si is None:
        return None
    if ei is None:
        ei = len(segs) - 1

    return (round(max(0.0, segs[si]["start"] - pad), 2),
            round(segs[ei]["end"] + pad, 2), segs[si:ei + 1])
