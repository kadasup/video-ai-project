"""
數據動態字卡：把新聞裡的關鍵數字做成 2.8 秒的直式動畫插卡（1080x1920）。

做法借鏡中央社 HyperFrames：HTML＋資料化時間線（deterministic timeline）——
不即時錄影，而是逐幀 seek 動畫進度截圖再用 ffmpeg 組裝，幀幀精準、無掉幀。
瀏覽器用系統內建 Edge（playwright channel=msedge），不用另外下載 Chromium。

動畫設計：深色漸層底 → 標籤淡入 → 大數字 count-up（easeOut）→ 單位/補注淡入。
"""
import json
import shutil
import subprocess
from pathlib import Path

from select_clip import TMP, _ffmpeg_exe

FPS = 30
DUR = 2.8          # 卡片秒數（app.py 插卡時也用這個值）
W, H = 1080, 1920

_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
body{width:540px;height:960px;overflow:hidden;
  font-family:"Microsoft JhengHei","Noto Sans TC",sans-serif;
  background:linear-gradient(160deg,#0b1526 0%,#12233f 55%,#0b1526 100%);
  display:flex;align-items:center;justify-content:center}
.card{text-align:center;width:100%;padding:0 40px}
.label{color:#7dd3fc;font-size:44px;font-weight:700;letter-spacing:6px;
  opacity:0;margin-bottom:28px}
.bigrow{display:flex;align-items:baseline;justify-content:center;gap:10px}
.num{color:#ffffff;font-size:150px;font-weight:900;line-height:1;
  font-variant-numeric:tabular-nums;opacity:0;
  text-shadow:0 0 60px rgba(125,211,252,.35)}
.unit{color:#e2e8f0;font-size:52px;font-weight:700;opacity:0}
.note{color:#94a3b8;font-size:34px;margin-top:34px;opacity:0}
.bar{width:0;height:6px;background:linear-gradient(90deg,#38bdf8,#818cf8);
  margin:26px auto 0;border-radius:3px}
</style></head><body>
<div class="card">
  <div class="label" id="label"></div>
  <div class="bigrow">
    <div class="num" id="num">0</div>
    <div class="unit" id="unit"></div>
  </div>
  <div class="bar" id="bar"></div>
  <div class="note" id="note"></div>
</div>
<script>
const D = %DATA%;
document.getElementById('label').textContent = D.label;
document.getElementById('unit').textContent  = D.unit;
document.getElementById('note').textContent  = D.note || '';
const target = parseFloat(String(D.value).replace(/[^0-9.]/g, '')) || 0;
const isInt = !String(D.value).includes('.');

// 資料化時間線：p ∈ [0,1] 完全決定畫面狀態（可逐幀 seek，不靠即時播放）
const easeOut = t => 1 - Math.pow(1 - t, 3);
const seg = (p, a, b) => Math.min(1, Math.max(0, (p - a) / (b - a)));
function setProgress(p) {
  const lb = document.getElementById('label');
  lb.style.opacity = seg(p, 0.00, 0.15);
  lb.style.transform = `translateY(${(1 - easeOut(seg(p, 0, 0.18))) * 30}px)`;
  const k = easeOut(seg(p, 0.10, 0.62));
  const v = target * k;
  document.getElementById('num').textContent =
    isInt ? Math.round(v).toLocaleString('en-US') : v.toFixed(1);
  document.getElementById('num').style.opacity = seg(p, 0.08, 0.2);
  document.getElementById('unit').style.opacity = seg(p, 0.30, 0.45);
  document.getElementById('bar').style.width = (easeOut(seg(p, 0.15, 0.7)) * 320) + 'px';
  document.getElementById('note').style.opacity = seg(p, 0.5, 0.68);
}
setProgress(0);
</script></body></html>"""


def render_stat_card(stat: dict, out_path: Path, dur: float = DUR) -> bool:
    """
    stat = {"value": "760", "unit": "架次", "label": "航班取消", "note": "颱風巴威影響"}
    成功回 True；任何失敗回 False（插卡是加分項，不擋產製主流程）。
    """
    frames_dir = TMP / "_statcard_frames"
    try:
        from playwright.sync_api import sync_playwright
        shutil.rmtree(frames_dir, ignore_errors=True)
        frames_dir.mkdir(parents=True, exist_ok=True)

        html = _HTML.replace("%DATA%", json.dumps({
            "value": str(stat.get("value", "")),
            "unit": str(stat.get("unit", "")),
            "label": str(stat.get("label", "")),
            "note": str(stat.get("note", "")),
        }, ensure_ascii=False))
        page_file = frames_dir / "card.html"
        page_file.write_text(html, encoding="utf-8")

        n = int(dur * FPS)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 540, "height": 960},
                                    device_scale_factor=2)   # 截出 1080x1920
            page.goto(page_file.as_uri())
            page.wait_for_timeout(150)   # 等字型載入
            for i in range(n):
                p = min(1.0, i / (n * 0.82))   # 尾端 ~0.5 秒定格，數字停穩讓人看清
                page.evaluate(f"setProgress({p:.4f})")
                page.screenshot(path=str(frames_dir / f"f{i:04d}.png"))
            browser.close()

        r = subprocess.run(
            [_ffmpeg_exe(), "-y", "-framerate", str(FPS),
             "-i", str(frames_dir / "f%04d.png"),
             "-vf", f"scale={W}:{H}", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "20", "-pix_fmt", "yuv420p", str(out_path)],
            capture_output=True, timeout=120)
        return r.returncode == 0 and out_path.exists()
    except Exception as e:
        print(f"statcard render failed: {e}")
        return False
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)


if __name__ == "__main__":
    ok = render_stat_card(
        {"value": "760", "unit": "架次", "label": "航班取消", "note": "颱風巴威影響桃園機場"},
        Path(r"D:\VideoAI\tmp\_statcard_test.mp4"))
    print("OK" if ok else "FAILED")
