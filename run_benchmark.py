"""Compare Windows OCR and RapidOCR through the EXISTING handler_media PII pipeline.
Runs locally. The bundled images are synthetic; do not use untrusted private files.
"""
from __future__ import annotations
import argparse
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import statistics
import sys
import time
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT))  # import project's real handler_media.py and ocr_backend.py
from benchmark_lib import (compact, ocr_joined_text, mask_from_boxes, fraction_covered,
                           union_masks, draw_redacted, score_one_case)


def run():
    parser = argparse.ArgumentParser(description="Offline synthetic OCR/PII benchmark")
    parser.add_argument("--limit", type=int, default=4, help="總共執行前幾張圖片；預設4，傳0執行全部36張")
    parser.add_argument("--engines", nargs="+", choices=["windows", "rapid"], default=["windows", "rapid"])
    parser.add_argument("--condition", choices=["all", "clear", "low_contrast", "small_text", "blur", "tilted", "jpeg_artifacts", "low_resolution", "two_column", "wrapped_email"], default="all")
    parser.add_argument("--threshold", type=float, default=0.98, help="PII 字形像素被遮罩覆蓋達此比例，視為成功，預設0.98")
    parser.add_argument("--padding", type=int, default=None, help="圖像遮罩向外擴張像素；預設採用 handler_media.MASK_PADDING")
    parser.add_argument("--previews", type=int, default=6, help="產生幾張原圖/兩引擎遮蔽對照圖")
    parser.add_argument("--no-warmup", action="store_true", help="不做模型預熱，首次呼叫的載入耗時會計入 OCR 時間")
    parser.add_argument("--output", type=Path, default=HERE / "results")
    args = parser.parse_args()
    if args.limit < 0 or args.padding is not None and args.padding < 0 or not 0 < args.threshold <= 1:
        parser.error("limit/padding/threshold 不合法")
    data_dir = HERE / "data"
    with (data_dir / "labels.json").open(encoding="utf-8") as fh:
        data = json.load(fh)
    cases = [c for c in data["cases"] if args.condition == "all" or c["condition"] == args.condition]
    # 0 = all, otherwise use first N per requested conditions. Default 4 for a quick trial.
    if args.limit:
        cases = cases[:args.limit]
    if not cases:
        parser.error("沒有案例")
    args.output.mkdir(parents=True, exist_ok=True)
    print("正在載入專案的 handler_media（可能載入 NER 模型）…")
    import handler_media
    functions = {}
    if "windows" in args.engines:
        if hasattr(handler_media, "_windows_ocr_words"):
            functions["windows"] = handler_media._windows_ocr_words
        elif "ocr_words_with_backend" in getattr(getattr(handler_media.ocr_words, "__code__", None), "co_names", ()):
            raise RuntimeError("handler_media 的 OCR 已被切換器包裝，但找不到 _windows_ocr_words，無法確保 Windows 測試正確")
        else:
            functions["windows"] = handler_media.ocr_words
    if "rapid" in args.engines:
        from ocr_backend import rapid_ocr_words
        functions["rapid"] = rapid_ocr_words
    padding = handler_media.MASK_PADDING if args.padding is None else args.padding
    result_rows = []
    time_samples = {e:[] for e in args.engines}
    problems = []
    log_path = args.output / "model_debug.log"
    # The log can contain original strings in other datasets. Keep private.
    with log_path.open("w", encoding="utf-8") as log:
        if not args.no_warmup:
            with Image.open(data_dir / cases[0]["image"]) as img0:
                warm_image = img0.convert("RGB")
            warm_processed, warm_scale = handler_media.preprocess_for_ocr(warm_image)
            print("正在預熱 OCR 與 PII 模型；預熱耗時不計入測試數字…")
            for engine_name in args.engines:
                try:
                    with contextlib.redirect_stdout(log):
                        warm_words = functions[engine_name](warm_processed)
                        handler_media.find_sensitive_boxes_in_words(warm_words, scale=warm_scale)
                except Exception as exc:
                    print(f"[預熱提示] {engine_name}：{type(exc).__name__}: {exc}；正式測試將再次記錄錯誤")
        for index, case in enumerate(cases, 1):
            img_path = data_dir / case["image"]
            with Image.open(img_path) as im:
                image = im.convert("RGB")
            prepped, scale = handler_media.preprocess_for_ocr(image)
            record = {"case":case["id"],"condition":case["condition"],"engines":{}}
            masks = {}
            for engine_name in args.engines:
                fn = functions[engine_name]
                try:
                    t0 = time.perf_counter()
                    with contextlib.redirect_stdout(log):
                        words = fn(prepped)
                    ocr_seconds = time.perf_counter()-t0
                    t1 = time.perf_counter()
                    with contextlib.redirect_stdout(log):
                        boxes = handler_media.find_sensitive_boxes_in_words(words,scale=scale)
                    pii_seconds = time.perf_counter()-t1
                    scored = score_one_case(image,case,data_dir,words,boxes,padding,args.threshold)
                    masks[engine_name] = scored.pop("redaction_mask")
                    text = compact(ocr_joined_text(words))
                    record["engines"][engine_name] = {"status":"ok", "ocr_seconds":round(ocr_seconds,3),
                        "pii_seconds":round(pii_seconds,3),"redaction_count":len(boxes),
                        "redactions":[{"bbox": [round(float(v),2) for v in b[:4]],"label":str(b[4])} for b in boxes],
                        **scored}
                    time_samples[engine_name].append(ocr_seconds)
                except Exception as exc:
                    record["engines"][engine_name]={"status":"error","error":f"{type(exc).__name__}: {exc}"}
                    problems.append(f"{case['id']} / {engine_name}: {type(exc).__name__}: {exc}")
            if all(e in masks for e in ("windows", "rapid")):
                merged = union_masks(masks["windows"],masks["rapid"])
                targets=[]
                for target in case["targets"]:
                    with Image.open(data_dir / target["mask_path"]) as m:
                        coverage = fraction_covered(m.convert("L"),merged)
                    targets.append({"id":target["id"],"label":target["type"],
                                    "ink_coverage":round(coverage,6),"fully_covered":coverage>=args.threshold})
                record["union_potential"]={"status":"ok","targets":targets}
            if index <= args.previews:
                preview_panel(image, masks, case["id"], args.output)
            result_rows.append(record)
            summaries=[]
            for e in args.engines:
                item=record["engines"][e]
                if item["status"] == "ok":
                    success=sum(t["fully_covered"] for t in item["targets"])
                    summaries.append(f"{e}: {success}/{len(item['targets'])} covered")
                else:
                    summaries.append(f"{e}: ERROR")
            print(f"{index:02d}/{len(cases)} {case['id']:<25} {'  |  '.join(summaries)}")
    result_data={"version":1,"synthetic_only":True,"threshold":args.threshold,"padding":padding, "warmed_up":not args.no_warmup,
                 "source":"PII bboxes produced by handler_media.find_sensitive_boxes_in_words",
                 "note":"ink coverage is a visual proxy, NOT a real-world privacy/security guarantee",
                 "results":result_rows,"errors":problems}
    (args.output/"benchmark_results.json").write_text(json.dumps(result_data,ensure_ascii=False,indent=2),encoding="utf-8")
    write_csv_results(result_rows,args.output,args.engines)
    summary=make_summary(result_data,args.engines,time_samples)
    (args.output/"summary.md").write_text(summary,encoding="utf-8")
    print("\n"+summary)
    print("結果已儲存：",args.output.resolve())
    if problems:
        print(f"注意：有 {len(problems)} 次引擎錯誤，不可把此結果當作完整模型比較。")


def write_csv_results(rows, output, engines):
    """Spreadsheet-readable records; no Excel dependency needed."""
    with (output / "per_case.csv").open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.writer(fp)
        writer.writerow(["case", "condition", "engine", "status", "target_total", "ocr_text_hits",
                         "pii_ink_covered", "safe_ink_covered_fraction", "ocr_seconds", "pii_seconds", "error"])
        for row in rows:
            for engine in engines:
                item=row["engines"][engine]
                targets=item.get("targets",[])
                writer.writerow([row["case"],row["condition"],engine,item["status"],len(targets),
                                 sum(t["text_found"] for t in targets), sum(t["fully_covered"] for t in targets),
                                 item.get("safe_ink_covered_fraction",""),item.get("ocr_seconds",""),
                                 item.get("pii_seconds",""),item.get("error","")])
    with (output / "per_target.csv").open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.writer(fp)
        writer.writerow(["case", "condition", "target_id", "type", "expected_text",
                         "windows_covered", "rapid_covered", "union_covered",
                         "windows_ocr_text", "rapid_ocr_text"])
        for row in rows:
            by={}
            for engine in engines:
                item=row["engines"].get(engine,{})
                if item.get("status") == "ok":
                    by[engine]={t["id"]:t for t in item["targets"]}
            union={t["id"]:t for t in row.get("union_potential",{}).get("targets",[])}
            targets=next((row["engines"][e]["targets"] for e in engines
                          if row["engines"][e]["status"] == "ok"), [])
            for t in targets:
                def value(e, field):
                    return by[e][t["id"]][field] if e in by and t["id"] in by[e] else ""
                writer.writerow([row["case"],row["condition"],t["id"],t["label"],t["expected"],
                                 value("windows","fully_covered"),value("rapid","fully_covered"),
                                 union[t["id"]]["fully_covered"] if t["id"] in union else "",
                                 value("windows","text_found"),value("rapid","text_found")])


def preview_panel(image, masks, case_id, output):
    width,height=image.size
    panel=Image.new("RGB",(width*3,height+32),"white")
    variants=[("Original",image), ("Windows OCR",draw_redacted(image,masks["windows"]) if "windows" in masks else None),
              ("RapidOCR",draw_redacted(image,masks["rapid"]) if "rapid" in masks else None)]
    draw=ImageDraw.Draw(panel)
    for i,(label,im) in enumerate(variants):
        draw.text((i*width+8,6),label,fill=(0,0,0))
        if im is not None:
            panel.paste(im,(i*width,32))
    folder=output/"previews"
    folder.mkdir(exist_ok=True)
    panel.save(folder/f"{case_id}_comparison.png")


def make_summary(data, engines, timings):
    rows=data["results"]
    out=["# 雙 OCR Benchmark：合成資料", "",f"測試案例：{len(rows)} 張；預設每張 4 個個資標註；完整資料集共 144 處",
         f"PII 字形遮蔽成功門檻：{data['threshold']:.0%}；遮罩 Padding：{data['padding']} px",
         f"模型預熱：{'是（不計首次模型載入時間）' if data.get('warmed_up') else '否（首輪可能包含載入時間）'}", "",
         "| 引擎 | 成功處理圖片 | OCR 文字命中 | PII 字形覆蓋 | 非敏感文字遮蔽率平均 | OCR 中位耗時（秒） |", 
         "|---|---:|---:|---:|---:|---:|"]
    for e in engines:
        valid=[r['engines'][e] for r in rows if r['engines'][e]['status']=="ok"]
        total=sum(len(x['targets']) for x in valid)
        text_hits=sum(t['text_found'] for x in valid for t in x['targets'])
        hits=sum(t['fully_covered'] for x in valid for t in x['targets'])
        loss=statistics.mean(x['safe_ink_covered_fraction'] for x in valid) if valid else 0
        speed=statistics.median(timings[e]) if timings[e] else None
        out.append(f"| {e} | {len(valid)}/{len(rows)} | {text_hits}/{total} | {hits}/{total} | {loss:.1%} | {speed:.3f} |" if speed is not None else f"| {e} | 0/{len(rows)} | N/A | N/A | N/A | N/A |")
    # Break down each difficulty condition; avoid treating engine errors as failures or zeros.
    out.extend(["", "## 各難度的字形遮蔽成功率（已執行成功的案例）", "",
                "| 難度 | Windows | RapidOCR | 可比較圖片數 |",
                "|---|---:|---:|---:|"])
    for condition in dict.fromkeys(r["condition"] for r in rows):
        subset=[r for r in rows if r["condition"]==condition]
        columns=[]
        for e in ("windows", "rapid"):
            if e not in engines:
                columns.append("未測")
                continue
            valid=[r["engines"][e] for r in subset if r["engines"][e]["status"]=="ok"]
            ok=sum(t["fully_covered"] for v in valid for t in v["targets"])
            total=sum(len(v["targets"]) for v in valid)
            columns.append(f"{ok}/{total}" if total else "ERROR")
        comparable=sum(1 for r in subset if all(r["engines"].get(e,{}).get("status")=="ok" for e in ("windows","rapid")))
        out.append(f"| {condition} | {columns[0]} | {columns[1]} | {comparable}/{len(subset)} |")
    unions=[r['union_potential'] for r in rows if 'union_potential' in r]
    if unions:
        all_n=sum(len(u['targets']) for u in unions)
        successful=sum(t['fully_covered'] for u in unions for t in u['targets'])
        out.extend(["",f"兩引擎遮蔽框聯集（僅理論上界）：{successful}/{all_n}（{len(unions)} 張成功執行兩引擎）"])
        if len(unions)!=len(rows):
            out.append("**注意：並非所有測試都成功執行兩引擎，聯集分數不能與完整集直接比較。**")
        only_windows=[];only_rapid=[];neither=[]
        for r in rows:
            a=r['engines'].get('windows');b=r['engines'].get('rapid')
            if not a or not b or a['status']!='ok' or b['status']!='ok':
                continue
            for x,y in zip(a['targets'],b['targets']):
                item=f"{r['case']} / {x['label']} / {x['id']}"
                if x['fully_covered'] and not y['fully_covered']: only_windows.append(item)
                if y['fully_covered'] and not x['fully_covered']: only_rapid.append(item)
                if not x['fully_covered'] and not y['fully_covered']: neither.append(item)
        out.extend(["", "## 互補案例（PII 字形覆蓋）",
                    f"- 只有 Windows 成功：{len(only_windows)} 筆",
                    f"- 只有 Rapid 成功：{len(only_rapid)} 筆",
                    f"- 兩者都失敗：{len(neither)} 筆", ""])
        for title,items in [("Windows 專屬",only_windows),("Rapid 專屬",only_rapid),("共同失敗",neither)]:
            if items:
                out.append(f"**{title}（最多列出 15 筆）：** " + "；".join(items[:15]))
    errors=data['errors']
    if errors:
        out.extend(["", "## 引擎執行錯誤", "", *[f"- {err}" for err in errors[:10]]])
    out.extend(["", "## 指標解讀", "",
                "- OCR 文字命中：整張圖的 OCR 文字是否包含 Ground Truth；不測座標。",
                "- PII 字形覆蓋：把 `handler_media` 的遮蔽框畫到影像上，對合成標註的文字像素計算覆蓋比例。",
                "- 非敏感文字遮蔽率：安全文字像素被遮罩蓋住的平均比例，只是誤遮蔽指標之一，不等於 Precision。",
                "- 雙引擎聯集只合併各自的遮蔽框，未整合 OCR 字串，也未重新跑偵測；不是完整 Ensemble 實作。",
                "- 預設先預熱兩個引擎與 PII 模型，OCR 耗時不計入首次載入；若指定 --no-warmup 則會計入。",
                "- 本資料全為合成文字，不能宣稱真實世界 PII Recall，也不能證明 PDF 內部物件已安全清除。",
                "- 用戶若處理真實資料，請勿把 model_debug.log、benchmark_results.json 或遮蔽對照表上傳公開儲存庫。"])
    return "\n".join(out)+"\n"

if __name__=="__main__":
    run()
