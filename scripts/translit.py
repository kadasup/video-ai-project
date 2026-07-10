"""
譯名核實：把旁白裡的外國人名/地名/組織譯名，比對報社的音譯總表（Google Sheet 原表）。

跟 transliteration-lookup 專案讀同一張原表、同一種讀法（公開 CSV 匯出連結，GET 唯讀）。
⚠️ 硬規則：原表永遠唯讀，這裡只 fetch CSV，沒有任何寫入能力（連權限都沒有）。

欄位（無表頭列）：[類別, 英文名, 縮寫, 中文譯名(常含括號描述), 國家, 日期, 編輯, 旗標]
"""
import csv
import io
import json
import re
import time
import urllib.request
from pathlib import Path

from produce import BASE

SHEET_CSV_URL = ("https://docs.google.com/spreadsheets/d/"
                 "1jJZuYj7gsiy2YNuGF1rWWIUpboQzfN_1Q45sd4HFr_Y/export?format=csv&gid=93759874")
CACHE_FILE = BASE / "logs" / "translit_table.json"
CACHE_TTL = 12 * 3600   # 12 小時；同事白天可能會加新條目，別快取太久

_mem: dict | None = None


def _strip_descriptor(name: str) -> str:
    """「韓森(摩根大通經濟學家)」→「韓森」；全形半形括號都處理"""
    return re.sub(r"[（(].*?[）)]", "", name or "").strip()


def _fetch_rows() -> list[list[str]]:
    req = urllib.request.Request(SHEET_CSV_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def load_table(force: bool = False) -> list[dict]:
    """
    回傳 [{category, english, abbr, chinese_core, chinese_full, country}, ...]
    磁碟快取 12 小時；抓失敗時退回舊快取（寧可用舊表也不要讓產製流程掛掉）。
    """
    global _mem
    if _mem is not None and not force:
        return _mem

    cached = None
    if CACHE_FILE.exists():
        try:
            cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            cached = None

    if cached and not force and time.time() - cached.get("fetched_at", 0) < CACHE_TTL:
        _mem = cached["entries"]
        return _mem

    try:
        rows = _fetch_rows()
        entries = []
        for r in rows:
            if len(r) < 4:
                continue
            chinese_full = (r[3] or "").strip()
            core = _strip_descriptor(chinese_full)
            if not core:
                continue
            entries.append({
                "category": (r[0] or "").strip(),
                "english": (r[1] or "").strip(),
                "abbr": (r[2] or "").strip(),
                "chinese_core": core,
                "chinese_full": chinese_full,
                "country": (r[4] or "").strip() if len(r) > 4 else "",
            })
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps({"fetched_at": time.time(), "entries": entries},
                                         ensure_ascii=False), encoding="utf-8")
        _mem = entries
    except Exception:
        _mem = cached["entries"] if cached else []   # 抓失敗退回舊快取
    return _mem


def _norm_en(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def check_names(names: list[dict]) -> list[dict]:
    """
    names: [{"chinese": "旁白用的譯名", "english_guess": "推測的英文原名"}]（GPT 抽取）
    回傳逐名結果：
      status = standard  旁白譯名跟表上一致 ✓
             = mismatch  英文原名在表上、但表上的標準譯名跟旁白不同 ⚠️（最重要）
             = unknown   表上查無此人/地/組織 ℹ️（提示未收錄，照原稿用字即可）
    """
    table = load_table()
    if not table:
        return []
    by_core = {}
    for e in table:
        by_core.setdefault(e["chinese_core"], e)

    results = []
    for n in names:
        zh = (n.get("chinese") or "").strip()
        en = _norm_en(n.get("english_guess", ""))
        if not zh:
            continue

        if zh in by_core:
            results.append({"chinese": zh, "status": "standard",
                            "english": by_core[zh]["english"]})
            continue

        # 英文原名比對：雙向包含（表上存全名、GPT 可能只給姓）
        hit = None
        if len(en) >= 3:
            for e in table:
                te = _norm_en(e["english"])
                ta = _norm_en(e["abbr"])
                if (te and (en in te or te in en)) or (ta and en == ta):
                    hit = e
                    break
        if hit:
            results.append({"chinese": zh, "status": "mismatch",
                            "expected": hit["chinese_core"],
                            "expected_full": hit["chinese_full"],
                            "english": hit["english"]})
        else:
            results.append({"chinese": zh, "status": "unknown",
                            "english": n.get("english_guess", "")})
    return results
