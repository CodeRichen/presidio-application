"""Offline OCR inference tuning with PII ink-coverage safety gates.

Requires the existing ocr_ab_benchmark_36 directory alongside this file or
in the parent project directory. No production module is modified.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import gc
import io
import json
import statistics
import sys
import time
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent


def locate_project_and_data():
    locations = [HERE.parent, HERE, Path.cwd(), HERE.parent.parent]
    for base in locations:
        dataset = base / "ocr_ab_benchmark_36"
        if (dataset / "data" / "labels.json").is_file() and (base / "ocr_backend.py").is_file() and (base / "handler_media.py").is_file():
            return base, dataset
    raise FileNotFoundError(
        "找不到 ocr_ab_benchmark_36/data/labels.json + ocr_backend.py + handler_media.py；"
        "請將 RapidOCR_Speed_Optimization 與 ocr_ab_benchmark_36 放在專案根目錄。"
    )


def configs(profile):
    # Use EXACTLY same models, detector, recognizer, and word boxes by default.
    table = {
        "baseline": {},
        "cpu2": {"EngineConfig.onnxruntime.intra_op_num_threads": 2},
        "cpu4": {"EngineConfig.onnxruntime.intra_op_num_threads": 4},
        "cpu6": {"EngineConfig.onnxruntime.intra_op_num_threads": 6},
        "cpu8": {"EngineConfig.onnxruntime.intra_op_num_threads": 8},
        # Optional experimental mode, NOT auto-installed: may hurt PII coverage.
        "cpu4_no_cls": {"EngineConfig.onnxruntime.intra_op_num_threads": 4,
                        "Global.use_cls": False},
    }
    if profile not in table:
        raise ValueError(f"無效設定 {profile!r}; 可選 {', '.join(table)}")
    return dict(table[profile])


def median(values):
    return statistics.median(values) if values else None


def summarize(records, profiles, gate=0.98):
    base = next((row for row in records if row["profile"] == "baseline" and row["error"] is None), None)
    results = []
    for profile in profiles:
        rows = [r for r in records if r["profile"] == profile]
        ok = [r for r in rows if not r["error"]]
        total = sum(r["targets"] for r in ok)
        covered = sum(r["covered"] for r in ok)
        ocr_med = median([r["ocr_seconds"] for r in ok])
        pipeline_med = median([r["ocr_seconds"] + r["pii_seconds"] for r in ok])
        safe_ink = statistics.mean(r["safe_ink_fraction"] for r in ok) if ok else None
        results.append({"profile": profile,"images":len(ok),"attempted":len(rows),
                        "coverage":f"{covered}/{total}", "target_total":total,"covered_count":covered,
                        "ocr_median":ocr_med,"pipeline_median":pipeline_med,
                        "safe_ink_mean":safe_ink,"errors":len(rows)-len(ok)})
    reference = next((x for x in results if x["profile"]=="baseline"),None)
    baseline_cases = {r["case"]:r for r in records if r["profile"]=="baseline" and not r["error"]}
    for row in results:
        candidate_cases = {r["case"]:r for r in records if r["profile"]==row["profile"] and not r["error"]}
        no_individual_regression = bool(baseline_cases) and set(candidate_cases)==set(baseline_cases)
        if no_individual_regression:
            no_individual_regression = all(
                not passed or candidate_cases[case_id].get("target_flags",{}).get(target_id, False)
                for case_id, base_case in baseline_cases.items()
                for target_id, passed in base_case.get("target_flags",{}).items()
            )
        match = (reference is not None and row["errors"]==0
                 and no_individual_regression
                 and row["target_total"]==reference["target_total"]
                 and row["covered_count"]>=reference["covered_count"]
                 and row["safe_ink_mean"] is not None
                 and reference["safe_ink_mean"] is not None
                 # Hard gate: don't add material non-sensitive over-redaction.
                 and row["safe_ink_mean"]<=reference["safe_ink_mean"]+0.005)
        row["passes_baseline_gate"] = bool(match)
        row["speedup"] = (reference["ocr_median"]/row["ocr_median"]
                          if reference and reference["ocr_median"] and row["ocr_median"] else None)
    return results


def make_report(rows, path, count):
    out=["# RapidOCR 推論速度與 PII 覆蓋率比較", "",
         f"合成圖片 {count} 張；每張 4 個標註區域。遮蔽門檻 >=98%，Padding 取自 handler_media。", "",
         "| 設定 | 成功圖片 | PII 覆蓋 | OCR 中位秒數 | OCR 加速比 | PII+OCR 中位秒數 | 安全文字被遮蔽 | 與基準比對 |",
         "|---|---:|---:|---:|---:|---:|---:|---|" ]
    def fmt(v,n=3):return "—" if v is None else f"{v:.{n}f}"
    for r in rows:
        safe = "—" if r["safe_ink_mean"] is None else f"{r['safe_ink_mean']:.1%}"
        out.append(f"| {r['profile']} | {r['images']}/{r['attempted']} | {r['coverage']} | "
                   f"{fmt(r['ocr_median'])} | {fmt(r['speedup'],2)}× | {fmt(r['pipeline_median'])} | "
                   f"{safe} | {'PASS' if r['passes_baseline_gate'] else 'FAIL'} |")
    eligible=[r for r in rows if r["passes_baseline_gate"] and r["ocr_median"] is not None]
    if eligible:
        best=min(eligible,key=lambda x:x["ocr_median"])
        out.extend(["",f"**符合此批資料品質門檻且最快的設定：{best['profile']}**。",
                    "實際套用前，請以 `--per-condition 0` 跑完 36 張，再確認真正的 PII 覆蓋率沒有下降。"])
    else:out.extend(["", "**尚無通過品質門檻的設定；請保留原始 RapidOCR。**"])
    out.extend(["", "限制：測試集是合成資料；覆蓋率不是實際人名辨識 Recall，也不是 PDF 安全稽核。",
                "本報告的 CPU 執行緒設定不更換模型，也不提升辨識門檻；`cpu4_no_cls` 會關閉文字方向分類，必須額外驗證。",
                "本程式不讀取或上傳個人資料，也不會修改正式 OCR 設定。"])
    path.write_text("\n".join(out)+"\n",encoding="utf-8")
    return "\n".join(out)


def main():
    p=argparse.ArgumentParser(description="RapidOCR CPU threading speed vs PII-coverage benchmark (offline)")
    p.add_argument("--profiles", default="baseline,cpu4,cpu8",help="例如 baseline,cpu4,cpu8,cpu4_no_cls")
    p.add_argument("--per-condition",type=int,default=1,help="每類測幾張，0=全 36 張，預設每類 1 張")
    p.add_argument("--output",type=Path,default=HERE/"speed_results")
    args=p.parse_args()
    try: profile_names=[x.strip() for x in args.profiles.split(",") if x.strip()]
    except Exception: p.error("--profiles 格式錯誤")
    if not profile_names or len(set(profile_names))!=len(profile_names) or "baseline" not in profile_names:
        p.error("--profiles 必須包含 baseline，且不可重複")
    for profile in profile_names:
        try:configs(profile)
        except ValueError as e:p.error(str(e))
    if args.per_condition<0:p.error("--per-condition 必須 >= 0")
    project,data_dir=locate_project_and_data()
    sys.path.insert(0,str(project));sys.path.insert(0,str(data_dir))
    from benchmark_lib import score_one_case
    from rapidocr import RapidOCR
    from ocr_backend import rapid_result_to_words
    import handler_media
    from importlib.metadata import version, PackageNotFoundError
    try:rapid_version=version("rapidocr")
    except PackageNotFoundError:rapid_version="unknown"
    data=json.loads((data_dir/"data"/"labels.json").read_text(encoding="utf-8"))
    cases=data["cases"]
    if args.per_condition:
        per={};selected=[]
        for c in cases:
            k=c["condition"]
            if per.get(k,0)<args.per_condition:
                selected.append(c);per[k]=per.get(k,0)+1
        cases=selected
    args.output.mkdir(parents=True,exist_ok=True)
    print(f"RapidOCR {rapid_version}，案例數 {len(cases)}，設定 {', '.join(profile_names)}")
    print("只輸出合成圖片測試指標，不修改正式 handler_media.py 或 ocr_backend.py")
    all_rows=[]
    # Keep a profile's engine alive across all images to avoid repeated loading.
    for profile in profile_names:
        engine=None
        print(f"\n== {profile} ==")
        config=configs(profile)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                engine=RapidOCR(params=config or None)
            # Warm up all three OCR stages, word box calculation, and PII models.
            first_image=Image.open(data_dir/"data"/cases[0]["image"]).convert("RGB")
            warm_processed,_=handler_media.preprocess_for_ocr(first_image)
            with contextlib.redirect_stdout(io.StringIO()):
                warm_result=engine(warm_processed,return_word_box=True)
                warm_words=rapid_result_to_words(warm_result,warm_processed.width,warm_processed.height)
                handler_media.find_sensitive_boxes_in_words(warm_words)
            print("預熱完成（不計入時間）")
        except Exception as e:
            print(f"初始化或預熱失敗: {type(e).__name__}: {e}")
            all_rows.extend({"profile":profile,"case":c["id"],"condition":c["condition"],"error":f"{type(e).__name__}: {e}"} for c in cases)
            del engine;gc.collect();continue
        for n,case in enumerate(cases,1):
            try:
                with Image.open(data_dir/"data"/case["image"]) as im: image=im.convert("RGB")
                processed,scale=handler_media.preprocess_for_ocr(image)
                t0=time.perf_counter()
                with contextlib.redirect_stdout(io.StringIO()):
                    raw=engine(processed,return_word_box=True)
                    words=rapid_result_to_words(raw,processed.width,processed.height)
                t1=time.perf_counter()
                with contextlib.redirect_stdout(io.StringIO()):
                    boxes=handler_media.find_sensitive_boxes_in_words(words,scale=scale)
                t2=time.perf_counter()
                score=score_one_case(image,case,data_dir/"data",words,boxes,handler_media.MASK_PADDING,0.98)
                n_targets=len(score["targets"])
                n_covered=sum(bool(t["fully_covered"]) for t in score["targets"])
                row={"profile":profile,"case":case["id"],"condition":case["condition"],
                     "targets":n_targets,"covered":n_covered,
                     "target_flags":{t["id"]:bool(t["fully_covered"]) for t in score["targets"]},
                     "ocr_text_hits":sum(bool(t["text_found"]) for t in score["targets"]),
                     "safe_ink_fraction":score["safe_ink_covered_fraction"],
                     "ocr_seconds":round(t1-t0,5),"pii_seconds":round(t2-t1,5),"error":None}
                all_rows.append(row)
                print(f"{n:02d}/{len(cases)} {case['id']}: PII {n_covered}/{n_targets}, OCR {t1-t0:.3f}s")
            except Exception as e:
                all_rows.append({"profile":profile,"case":case["id"],"condition":case["condition"],
                                 "error":f"{type(e).__name__}: {e}"})
                print(f"{n:02d}/{len(cases)} {case['id']}: ERROR {type(e).__name__}: {e}")
        del engine;gc.collect()
    with (args.output/"per_case_speed.csv").open("w",encoding="utf-8-sig",newline="") as out:
        columns=["profile","case","condition","targets","covered","ocr_text_hits","safe_ink_fraction","ocr_seconds","pii_seconds","error"]
        writer=csv.DictWriter(out,fieldnames=columns);writer.writeheader()
        for row in all_rows:writer.writerow({k:row.get(k,"") for k in columns})
    stats=summarize(all_rows,profile_names)
    output={"rapidocr_version":rapid_version,"cases":len(cases),"profiles":profile_names,"summary":stats,"rows":all_rows}
    (args.output/"speed_results.json").write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding="utf-8")
    report=make_report(stats,args.output/"speed_summary.md",len(cases))
    print("\n"+report)
    print("\n報告：",args.output.resolve())
    if any(r["errors"] for r in stats):
        print("注意：至少一組有失敗，不能直接採用該組設定。")


if __name__=="__main__":main()
