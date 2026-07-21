"""
本地語音辨識（faster-whisper）：把記者素材裡的受訪原音轉成帶時間戳的逐字稿，
供「SOT 原音段」選句與字幕使用。

- 模型跑本機 CPU（int8），零 API 費用；結果進快取（同檔不重轉）。
- 只在影片音軌有實質人聲能量時才轉譯（先用 ffmpeg 快篩，省時間）。
"""
import json
import re
import subprocess
import threading
from pathlib import Path

from produce import _ffmpeg_exe, _probe_duration
from select_clip import _cache_get, _cache_put

_MODEL = None
_MODEL_NAME = "medium"   # 中文品質夠上字幕；實測太慢再降 small

# 逐支轉譯逾時（防卡死安全網，非用來加速）：正常長片在 CPU 上 medium+逐字時間戳
# 本來就慢，門檻要放寬到只攔「真的卡死」而不誤殺仍在跑的正常轉譯。
_TIMEOUT_FLOOR_SEC  = 300.0    # 至少給這麼久
_TIMEOUT_FACTOR     = 12.0     # 依影片長度放大（medium+逐字 CPU 約數倍實時，×12 留足餘裕）
_TIMEOUT_CAP_SEC    = 1500.0   # 上限 25 分鐘，再久視為卡死放棄這支


def _load_model():
    from faster_whisper import WhisperModel
    return WhisperModel(_MODEL_NAME, device="cpu", compute_type="int8")


def _get_model():
    global _MODEL
    if _MODEL is None:
        _MODEL = _load_model()
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


def _do_transcribe(model, video: Path, out: dict):
    """實際跑 Whisper（在 worker thread 內執行，結果寫回 out）。"""
    try:
        # 不給 initial_prompt：實測無清楚人聲時 Whisper 會把 prompt 內容
        # 幻覺回吐當成轉譯結果；簡繁問題交給後續 GPT 校正處理
        segs_iter, _info = model.transcribe(
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
        out["result"] = {"segments": segs, "full_text": full}
    except Exception as e:
        out["error"] = e   # 轉譯本身失敗（曾見：C 槽空間不足導致模型載入失敗）


def transcribe(video: Path) -> dict:
    """
    回傳 {"segments": [{"start","end","text","words":[{"start","end","word"},...]}],
          "full_text": str}
    無人聲（或音量太低）→ segments 為空。有快取。

    逐支逾時保護：一支影片若轉太久（疑似卡死），放棄該支（回空稿、不快取，
    下次可重試），讓整個產製工作不會被單一影片無限卡住。
    """
    global _MODEL
    video = Path(video)
    cached = _cache_get("speech1", video)
    if cached is not None:
        return cached

    result = {"segments": [], "full_text": ""}
    transcribed_ok = True   # 真的「確認無語音」才快取；轉譯失敗（OOM/磁碟滿等）不快取，下次重試
    if has_speech_energy(video):
        try:
            dur = _probe_duration(video)
        except Exception:
            dur = 0.0
        budget = min(_TIMEOUT_CAP_SEC, max(_TIMEOUT_FLOOR_SEC, dur * _TIMEOUT_FACTOR))

        # 在 worker thread 跑轉譯，主執行緒 join(逾時)。逾時就放棄這支，
        # 並「丟掉共用模型物件」——被放棄的執行緒仍抓著舊模型獨自跑完，
        # 下一支改用全新模型實例，避免兩支同時操作同一個模型（CTranslate2 非執行緒安全）。
        model = _get_model()
        out: dict = {}
        t = threading.Thread(target=_do_transcribe, args=(model, video, out), daemon=True)
        t.start()
        t.join(budget)

        if t.is_alive():
            transcribed_ok = False
            _MODEL = None   # 這個模型物件還被卡住的執行緒占著，下一支載入全新的
            print(f"[transcribe] {video.name} 轉譯逾時（>{int(budget)}s，影片長 {dur:.0f}s），"
                  f"放棄該支原音、不快取（可日後重試）")
        elif out.get("error") is not None:
            transcribed_ok = False
            print(f"[transcribe] {video.name} 轉譯失敗：{out['error']}")
        else:
            result = out.get("result", result)

    if transcribed_ok:
        _cache_put("speech1", video, result)
    return result


def _clean(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z一-鿿]", "", s)


_SENT_END = "。！？!?…"


def resolve_quote_span(transcript: dict, quote: str,
                       pad: float = 0.25,
                       snap_sentence_sec: float = 0.0
                       ) -> tuple[float, float, list, str] | None:
    """
    把 GPT 選的引句文字對回逐字稿時間軸，回傳 (start, end, 覆蓋到的 segments, tail)。
    tail 是為了講完句子（snap）而多納入的段落原文，沒延伸時為空字串。

    做法：把全部 segment 文字去標點串成一條長字串（記錄每個 segment 的字元
    起訖位置），用 find() 找引句的**實際起點**，再映射回起訖 segment——
    不能用「滑動視窗串接後 in 檢查」：引句落在視窗中段也會命中，
    會把前面不相干的句子一起圈進來（實測踩過：字幕跟原音對不上）。

    snap_sentence_sec > 0：GPT 給的 quote 常是半句，結束段落落在話沒講完處
    （回饋：「議員講話也沒讓他講完」）。開這個選項後，若結束段的原文不是以句尾
    標點（。！？…）收尾，就往後多納入幾段直到湊成完整句子，最多多延伸這麼多秒，
    寧可讓受訪者把話講完也不要硬切在半句。
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

    # 收尾對齊句尾：結束段原文沒以句尾標點收，往後多納段落把整句講完（限秒數預算內）。
    # tail = 為了講完句子而「多納入的段落原文」，回給呼叫端補進字幕，音畫才同步。
    tail = ""
    if snap_sentence_sec > 0:
        base_end = segs[ei]["end"]
        ei0 = ei
        while (ei < len(segs) - 1
               and (segs[ei]["text"].rstrip() or "x")[-1] not in _SENT_END
               and segs[ei + 1]["end"] - base_end <= snap_sentence_sec):
            ei += 1
        if ei > ei0:
            tail = "".join(segs[k]["text"] for k in range(ei0 + 1, ei + 1)).strip()

    return (round(max(0.0, segs[si]["start"] - pad), 2),
            round(segs[ei]["end"] + pad, 2), segs[si:ei + 1], tail)
