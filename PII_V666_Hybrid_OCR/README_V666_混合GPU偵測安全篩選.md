# V6.6.6：RapidOCR 混合 GPU／CPU 辨識效能 A/B（只用合成 PDF）

## 目的

V6.6.5 在 **14 組（七頁 × 2×／3×）** OCR 上，觀察到全 GPU 相比全 CPU **加快 6.44%**。詳細數據顯示 GPU 的文字偵測（det）顯著較快，但方向分類（cls）與文字辨識（rec）較慢；2× GPU 也比 CPU 慢。因此本版測試：

1. **cpu**：det／cls／rec 全 CPU。
2. **cuda**：det／cls／rec 優先 CUDA（ORT 可對不支援的節點回退 CPU）。
3. **hybrid**：**det 優先 CUDA**，**cls 與 rec 強制 CPU**。

全部使用 RapidOCR 3.10.0、ONNX Runtime GPU 1.31.1、同一模型、同一前處理、同一份 SHA-256 固定合成 PDF、完整 **2× + 3×** OCR，不減少任何辨識流程。

**重要**：RapidOCR 3.10 的 `EngineConfig.onnxruntime.use_cuda` 是引擎層級選項，並非三個模型各有的官方 CUDA 開關。本研究用**獨立測試程序**攔截 `onnxruntime.InferenceSession`，依明確辨識出的 ONNX 模型檔案類型指定執行提供者。若無法明確識別模型類型，立即失敗，不會當作成功或自動繼續。這是實驗性方法，**不可直接搬進正式程式**。

## 執行前提

- Windows，使用**現有獨立 `.venv_ocr_gpu`**（V6.6.4 和 V6.6.5 已建立），不改動正式 `.venv`。
- 已安裝 RapidOCR 3.10.0、onnxruntime-gpu 1.31.1、PyMuPDF、Pillow、numpy。
- `PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf` 存在且 SHA-256 與工具內固定值完全相同。
- 本工具資料夾 `PII_V666_Hybrid_OCR` 解壓於 `presidio_app_allen` 專案根目錄。
- 不需安裝新套件；不要同時跑 GUI 或其他 GPU 工作。

## Windows PowerShell 指令

在 `presidio_app_allen` 根目錄執行：

**1. 工具單元測試（不載入正式模型，不跑 PDF）**

```powershell
.\.venv_ocr_gpu\Scripts\python.exe .\PII_V666_Hybrid_OCR\test_v666.py
```

**2. 檢查環境**

```powershell
.\.venv_ocr_gpu\Scripts\python.exe .\PII_V666_Hybrid_OCR\v666_hybrid_trial.py --check-only
```

**3. 第一次三組 A/B（每種模式 14 次 OCR）**

```powershell
.\.venv_ocr_gpu\Scripts\python.exe .\PII_V666_Hybrid_OCR\v666_hybrid_trial.py `
  --repeats 1 `
  --results-dir .\v666_hybrid_results
```

由於每個輸入都要執行三種模式，測試次數是 42 次 OCR，運算時間會高於 V6.6.5；不保證 GPU/混合模式加速。

如果一組驗證通過，再考慮使用 `--repeats 2` 擴大樣本（請使用新的 `--results-dir`）。不要把一次試驗說成穩定加速。

## 驗收與資料保護

- 確認三個模式各建立 det／cls／rec ONNX Session。
- 確認混合模式的 **det 有 CUDA Node events**、**cls／rec 有 CPU Node events** 且 cls／rec 無 CUDA Node events。CUDA det 的少量 CPU fallback 屬 ORT 允許行為。
- CPU／全 GPU 與 CPU／混合模式各自對 **14 組 OCR** 比較：**文字完全相同**、字框偏差 ≤0.5 px、信心分數誤差 ≤0.002；若任一配對失敗，不會標記等價。
- 報告只輸出模式耗時、預熱時間、模型分類後的 Provider 事件計數，以及等價配對數。**不寫入 OCR 原文、PII、文字雜湊、行框座標、原始圖像或原始 ORT profiling。**
- ORT profiling 檔只存在 `TemporaryDirectory` 中，正常結束自動清除。若強制終止，檢查 `%TEMP%` 中 `pii_v666_` 前綴目錄。
- 只允許固定合成 PDF，不能拿真實文件測試此工具。
- **本版沒有**執行完整 27/27 文字 PII、17/17 已記錄人臉框、V5 PDF 安全重建或正式 GUI 評估。即使混合模式很快且 OCR 輸出相同，仍須另做整體安全 A/B 才能考慮部署。

## 產出與上傳

```text
v666_hybrid_results/
├── v666_hybrid_ocr_summary.json
└── v666_hybrid_ocr_summary.md
```

只上傳上述兩個摘要檔；**不要上傳模型、PDF、中間圖片或私人還原 map**。

狀態說明：

- `HYBRID_OCR_EQUIVALENT_SYNTHETIC_ONLY`：三種模式的 Provider 路由驗證通過，且與 CPU 相比 14 組 OCR 輸出等價。**不代表加速**，需查看實測時間。
- `OCR_OUTPUT_DIFFERENCE_NEEDS_REVIEW`：任何文字、框、分數相容性差異，不得直接替換正式引擎。
- `SESSION_MODEL_ROUTE_UNVERIFIED`／`REQUIRED_PROVIDER_KERNEL_NOT_VERIFIED`／`CPU_ONLY_MODEL_EXECUTED_CUDA`：沒有足夠證據證明預期 GPU/CPU 路由生效，拒絕通過。
- `RUNTIME_ERROR`：模型、硬體或內部呼叫問題；摘要只記錄例外類型，不含可能敏感的錯誤內容。

## 後續決策

若混合模式的 OCR 輸出全部等價，且相比全 CPU 有**至少 10% 的可重複純 OCR 加速**，再規劃完整 PDF 遮蔽流程的安全 A/B；仍須另計冷啟動、IPC/整合開銷、GUI 回應和多份合成情境，不可只看 OCR-only。若改善很小則停止 GPU 優化研究，保留正式 CPU 方案。

參考文件：

- RapidOCR ONNX Runtime 設定：https://github.com/RapidAI/RapidOCR/blob/main/python/rapidocr/config.yaml
- RapidOCR 推論引擎使用：https://rapidai.github.io/RapidOCRDocs/main/install_usage/rapidocr/how_to_use_infer_engine/
