# PII 偵測與假名化系統(剪貼簿敏感資訊防護工具)

複製東西時自動偵測、遮蔽敏感資訊，貼上時可以還原；圖片/PDF/程式碼/一般文字/表格分開處理。
可以用全域快速鍵(命令列版)，也可以開圖形介面手動處理、掃描整個資料夾、檢視敏感清單。

## 檔案結構

```
core.py            共用核心：遮蔽/還原、重疊處理、分派(code/text/media/field/csv)、敏感清單、資料夾掃描、剪貼簿工具、模型預熱
hotkey_service.py  全域快速鍵服務(模式 A/B)與 hotkeys.json 設定檔，main.py 與 ui.py 共用
main.py            命令列版入口：載入對照表 → 預熱 → 啟動快速鍵服務 → 結束時清理對照表
ui.py              圖形介面(tkinter)：文字 / 檔案 / 資料夾掃描 / 快速鍵 / 敏感清單 / 對照表
handler_text.py   一般文字規則（電話、信用卡、Email、URL、檔名、引號密碼...）+ Presidio 模型
handler_code.py   程式碼規則（API Key、JWT、檔案路徑）+ 借用 handler_text 的個資規則
handler_csv.py    表格(csv / tsv / xlsx)：欄位內有個資則整欄遮蔽
handler_media.py  圖片 / PDF：OCR 抓文字位置 → 借用 handler_code 的規則 + Presidio 判斷 → 塗黑 → 存對照表
handler_field.py  （選用）欄位型偵測，存在時 core.py 會自動併入候選
```

所有檔案要放在**同一個資料夾**。`main.py` 與 `ui.py` 互相獨立，彼此不 import，兩者都只呼叫 `core.py` / `hotkey_service.py`：

```bash
python main.py   # 命令列版：只有全域快速鍵
python ui.py     # 圖形介面版（可在「快速鍵」分頁啟動同樣的全域快速鍵）
```

兩個不要同時啟動快速鍵，會綁定兩次。

---

## 使用的技術與實作方法

| 需求                             | 用的技術                                                                                                                                                                                                                                                                                                   | 在哪個檔案                                |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| 全域快速鍵監聽                   | `pynput.keyboard.GlobalHotKeys`：註冊系統層級的鍵盤鉤子，不用視窗取得焦點也能觸發；callback 立刻返回，實際工作丟到背景執行緒，不會卡住鍵盤鉤子                                                                                                                                                           | `hotkey_service.py`                     |
| 快速鍵設定與執行中更換           | 設定存成`hotkeys.json`，用 `keyboard.HotKey.parse` 驗證格式；更換時停掉舊的 listener、用新設定重新綁定，不用重開程式。貼上快速鍵留空即為模式 B                                                                                                                                                         | `hotkey_service.py` / `ui.py`         |
| 模擬真正的 Ctrl+C / Ctrl+V       | `win32api.keybd_event`：直接送出虛擬按鍵事件給作業系統，效果等同使用者真的按下該按鍵                                                                                                                                                                                                                     | `hotkey_service.py`                     |
| 等剪貼簿真的寫好                 | `win32clipboard.GetClipboardSequenceNumber`：用剪貼簿序號判斷內容是否已更新，比固定 sleep 可靠；剪貼簿被佔用時讀取會重試                                                                                                                                                                                 | `core.py`                               |
| 讀取剪貼簿是文字還是檔案         | `win32clipboard.IsClipboardFormatAvailable(CF_HDROP)`：檢查剪貼簿格式，`CF_HDROP` 代表複製的是檔案清單                                                                                                                                                                                                 | `core.py`                               |
| 把處理完的檔案放回剪貼簿         | 手動組出 Windows 的`DROPFILES` 結構（`struct.pack` 組 header + UTF-16LE 檔名清單）塞進 `SetClipboardData(CF_HDROP, ...)`，等同在檔案總管「複製」了那個檔案                                                                                                                                           | `core.py`                               |
| 取得目前最前景視窗資訊           | `win32gui.GetForegroundWindow` + `win32process.GetWindowThreadProcessId` + `psutil.Process` 反查程式名稱                                                                                                                                                                                             | `core.py`                               |
| 正則規則比對                     | Python 內建`re`，用 `dataclass Rule(name, pattern, flags, validator)` 統一格式，方便一行一行加規則                                                                                                                                                                                                     | `handler_text.py` / `handler_code.py` |
| 降低誤判                         | 部分規則加`validator` 二次驗證，例如信用卡卡號用 **Luhn 演算法**驗證檢查碼，不是隨便 13~19 碼數字都算                                                                                                                                                                                              | `handler_text.py`                       |
| 重疊區間比對結果合併             | 自己寫的`merge_overlaps()`：先依來源優先序（正則規則 > 與整段語言吻合的模型 > 另一語言的模型），同層取範圍較長者、再比信心分數，貪婪掃描去除重疊，避免多個規則同時命中同一段文字時，用舊座標切字串切壞                                                                                                   | `core.py` / `handler_media.py`        |
| 英文 NLP 判斷 (PERSON/EMAIL/...) | **Microsoft Presidio** (`presidio-analyzer`)，底層 NLP engine 用 spaCy 的 `en_core_web_sm`                                                                                                                                                                                                       | `handler_text.py`                       |
| 中文 NLP 判斷                    | 同一個 Presidio`AnalyzerEngine`，但 NLP engine 換成 spaCy 的 `zh_core_web_sm`，另外**額外掛載**一個自訂的 `EntityRecognizer` 子類別，內部包一個 Hugging Face `transformers.pipeline("ner")`，模型是 `ckiplab/bert-base-chinese-ner`，補強 spaCy 中文模型本身抓人名/地名/機構名不夠準的問題 | `handler_text.py`                       |
| 語言判斷、模型可替換的設計       | 依 Unicode 範圍`\u4e00`~`\u9fff` 判斷是否含中文，再從 `NLP_MODELS = {"en": ..., "zh": ...}` 這個字典挑函式呼叫（Strategy Pattern），換模型只要改字典內容                                                                                                                                             | `handler_text.py`                       |
| 表格整欄遮蔽                     | 表頭命中`HEADER_RULES`（姓名/Email/電話...）就整欄遮蔽；否則逐格偵測（同值只偵測一次），**只要一格含個資就整欄換成標籤**，類型取該欄偵測最多的。csv 自動判斷分隔符號與編碼（UTF-8 / Big5），xlsx 用 `openpyxl`                                                                                   | `handler_csv.py`                        |
| 敏感清單與自動遮蔽               | 每次遮蔽偵測到的敏感值記錄成`presidio/real/sensitive_items.json`（值、類型、次數、來源檔案），程式結束**不刪除**；之後偵測時把清單編成一條「長的優先」的正則，當成最高優先序的規則自動遮蔽（英數值前後加邊界判斷，避免命中字的一部分）                                                             | `core.py`                               |
| 資料夾掃描與風險分級             | `os.walk` 走過資料夾（略過 `.git` / `__pycache__` 等、略過已產生的 `*_masked`），逐檔依副檔名分派；有偵測到才產生 `*_masked` 去識別化檔並標為危險，偵測類型含 KEY / JWT / PASSWORD / CARD / NATIONAL_ID 等關鍵字為「高」風險，其餘為「中」                                                       | `core.py` / `ui.py`                   |
| 圖形介面不卡住                   | `tkinter` + 背景執行緒：耗時工作（OCR、模型預熱、掃描）在 worker thread 跑，`print` 經 `queue` 導到記錄區；與快速鍵共用 `WORK_LOCK`，不會同時改對照表或搶剪貼簿                                                                                                                                    | `ui.py` / `hotkey_service.py`         |
| 避免第一次複製卡住               | 啟動時先用範例文字跑一次偵測（`core.warmup()`），讓 Presidio / NER 模型提前載入；預熱不會記錄進敏感清單                                                                                                                                                                                                  | `core.py`                               |
| 圖片文字辨識 (OCR)               | `pytesseract`（呼叫 Tesseract OCR 執行檔），語言包用 `chi_tra+eng` 同時認中英文，`image_to_data` 拿到逐字的文字內容跟像素座標                                                                                                                                                                        | `handler_media.py`                      |
| 把同一行的字組回一句話           | 用 Tesseract 回傳的`block_num`/`par_num`/`line_num` 分組、`word_num` 排序，重建成一行文字後再拿去跑規則/模型，比對到的字元區間再換算回涵蓋那些字的像素框                                                                                                                                           | `handler_media.py`                      |
| PDF 文字直接遮蔽（非疊色塊）     | `PyMuPDF (fitz)` 的 `add_redact_annot()` + `apply_redactions()`：這是「真正的遮蔽」，會把文字內容從 PDF 資料流裡整個移除，不是只在上面疊一層黑色矩形                                                                                                                                                 | `handler_media.py`                      |
| PDF 掃描頁（沒有文字圖層）處理   | 用`page.get_text("words")` 判斷字數是否過少，過少就整頁 `get_pixmap()` 轉成圖片走跟一般圖片一樣的 OCR 流程，偵測到的像素座標再除以縮放倍率換算回 PDF 座標                                                                                                                                              | `handler_media.py`                      |
| 遮蔽內容如何還原                 | 遮蔽前先把該區域裁切下來轉成 PNG，`base64` 編碼後存進 JSON 對照表；純文字/程式碼/表格則是原文直接存進去，還原就是查表把 `<TAG_n>` / 遮蔽像素換回原本內容                                                                                                                                               | 全部檔案                                  |

---

## 安裝

```bash
pip install pywin32 psutil pyperclip pynput
pip install presidio-analyzer pillow pytesseract pymupdf
pip install openpyxl                    # 處理 xlsx 表格用
pip install transformers torch          # 中文 NER 用
python -m spacy download en_core_web_sm
python -m spacy download zh_core_web_sm
```

圖形介面用的 `tkinter` 隨 Python 安裝，不用另外裝（Windows 安裝 Python 時請保留 tcl/tk 選項）。

另外要安裝 Tesseract OCR 主程式（非 pip 套件，`pytesseract` 只是呼叫它）：

- Windows：[https://github.com/UB-Mannheim/tesseract/wiki](https://github.com/UB-Mannheim/tesseract/wiki)（安裝時勾選 Chinese-Traditional）
- macOS：`brew install tesseract tesseract-lang`
- Linux：`sudo apt install tesseract-ocr tesseract-ocr-chi-tra`

Windows 上如果 `tesseract.exe` 沒加進 PATH，改 `handler_media.py` 裡的 `TESSERACT_CMD`。

第一次執行時，`ckiplab/bert-base-chinese-ner` 會自動從 Hugging Face 下載（幾百 MB），需要網路；之後會用本地快取。沒裝 `transformers`、沒網路下載模型、或完全沒裝 Presidio，程式都會自動偵測失敗並退化

---

## 整體流程

### 快速鍵的兩種模式

| 模式                   | 條件                  | 行為                                                                                                                                                         |
| ---------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| A・專屬快速鍵          | `paste` 有設定值    | 複製鍵只算出覆蓋結果並暫存，不動剪貼簿；按貼上鍵才暫時寫入剪貼簿、模擬 Ctrl+V、貼完立刻還原。原生 Ctrl+C / Ctrl+V 不受影響                                   |
| B・Ctrl+C 直接當快速鍵 | `paste` 留空 (null) | 原生 Ctrl+C 寫完剪貼簿後，程式讀出來處理並**直接覆寫剪貼簿**，原生 Ctrl+V 自然貼出處理後版本。處理完成前就貼上會貼出原始內容；終端機按 Ctrl+C 也會觸發 |

### 對照表與清單檔案

| 檔案                     | 內容                                                         | 誰在維護              | 結束時                 |
| ------------------------ | ------------------------------------------------------------ | --------------------- | ---------------------- |
| `clipboard_map.txt`    | 純文字/程式碼/表格的`標籤 -> 原文`                         | `core.py`           | 刪除                   |
| `media_deid_map.json`  | 圖片/PDF 每個遮蔽區塊的座標與原始畫面截圖 (base64)           | `handler_media.py`  | 刪除                   |
| `sensitive_items.json` | 敏感清單：每次偵測到的敏感值、類型、次數、最近出現、來源檔案 | `core.py`           | **保留，不刪除** |
| `hotkeys.json`         | 快速鍵設定（`copy` / `paste`）                           | `hotkey_service.py` | 保留                   |

`clipboard_map.txt`、`media_deid_map.json`、`sensitive_items.json` 都存在 `presidio/real/` 底下，存的是**明碼敏感資訊**，注意存放位置的權限，不要外流；也不要手動亂改格式。

### 敏感清單怎麼運作

1. 文字 / 程式碼 / 表格 / 圖片 / PDF 每次遮蔽，偵測到的值會記錄進 `sensitive_items.json`（太短的值不記，見 `KNOWN_MIN_LEN`）。預熱、測試用的暫時偵測不會記錄。
2. 之後偵測文字、程式碼、表格儲存格時，清單裡的值會被當成正則規則（`source="regex"`，優先序最高）自動遮蔽，不需要模型再判斷一次。
3. 如果某個詞被誤判成敏感資訊，到介面的「敏感清單」分頁選取後按「移除」即可；`core.USE_KNOWN_ITEMS = False` 可整個關閉自動遮蔽。
4. 圖片 / PDF 的偵測值會寫進清單，但 OCR 文字目前**不會**自動套用清單；要套用，需要在 `handler_media.py` 偵測文字時一併呼叫清單規則。

### 資料夾掃描

在介面的「資料夾掃描」分頁選一個資料夾（可選是否包含子資料夾）→「開始掃描並遮蔽」：

- 圖片 / PDF / 文字 / 程式碼 / 表格都會處理；偵測到敏感資訊的檔案旁邊會產生 `*_masked` 去識別化檔，原檔不動。
- 結果表格依風險排序：**高**（含 API Key、JWT、密碼、信用卡、身分證號等）→ **中**（其他個資）→ 安全。掃描完會跳出對話框列出危險檔案。
- 可以把選取（或全部）的去識別化檔複製到剪貼簿，或在檔案總管中定位原檔。
- 已經是 `*_masked` 的檔案會自動略過，不會重複處理。

---

## 客製化教學

以下每一項都只要改對應檔案「設定區」的一小段，不用動偵測/替換的核心邏輯。

### 1. 改快速鍵

**方法 A（建議）**：開 `ui.py` →「快速鍵」分頁 → 按「錄製」後直接按下想用的組合鍵 →「套用並儲存」。服務執行中會立刻用新設定重新綁定。設定存在 `hotkeys.json`，`main.py` 啟動時也會讀同一份。

**方法 B**：直接改 `hotkeys.json`（沒有檔案時用 `hotkey_service.py` 裡的 `DEFAULT_CONFIG`）：

```json
{
  "copy": "<ctrl>+<alt>+c",
  "paste": "<ctrl>+<alt>+v"
}
```

語法照 [pynput 的 HotKey 格式](https://pynput.readthedocs.io/en/latest/keyboard.html#global-hotkeys)：

```json
{ "copy": "<ctrl>+<shift>+m", "paste": "<ctrl>+<alt>+<shift>+v" }
```

想切成模式 B：`"copy": "<ctrl>+c"`，`"paste": null`。

支援的修飾鍵名稱：`<ctrl>`、`<alt>`、`<shift>`、`<cmd>`（Mac）；一般按鍵直接寫小寫字母/數字。

### 2. 新增偵測規則（電話/信用卡/API Key 這類）

一般文字的規則在 `handler_text.py` 的 `RULES`，程式碼專屬規則在 `handler_code.py` 的 `RULES`。格式都一樣：

```python
Rule(name, pattern, flags=0, validator=None)
```

範例：新增台灣護照號碼規則：

```python
# handler_text.py
RULES = [
    ...
    Rule("TW_PASSPORT", r"\b\d{9}\b"),
]
```

範例：新增資料庫連線字串規則（程式碼專用）：

```python
# handler_code.py
RULES = [
    ...
    Rule("DB_CONN_STRING", r"(?:postgres|mysql|mongodb)://[^\s\"']+"),
]
```

不想寫規則、只是有幾個固定要遮的值（某個專案代號、客戶名稱）：直接讓它被遮蔽過一次，就會進敏感清單，之後自動遮蔽。

### 3. 換掉某個語言用的 NLP 模型

`handler_text.py` 裡的 `NLP_MODELS` 是語言對應模型的註冊表：

```python
NLP_MODELS = {
    "en": presidio_en,
    "zh": presidio_zh,   # 目前是 spaCy 中文模型 + ckiplab 中文 NER
}
```

想整個換掉中文那層（例如換別的中文 NER 模型、接雲端 API），步驟固定：

```python
# 1. 寫一個函式，輸入文字、輸出格式一致的比對結果
def my_chinese_model(text: str):
    results = my_model.analyze(text)
    return [(r.start, r.end, r.type) for r in results]

# 2. 蓋掉字典裡的值
NLP_MODELS["zh"] = my_chinese_model
```

改完這一行，`detect()`、`handler_code.py`、`handler_media.py` 完全不用動，因為它們都是呼叫 `presidio_matches()` / `detect()` 這個統一入口。

### 4. 調整「複製檔案 / 掃描資料夾」時的分類規則

`core.py` 設定區：

```python
CODE_EXTENSIONS = {".py", ".js", ".ts", ...}
MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf", ...}
TEXT_FILE_EXTENSIONS = {".txt", ".md", ".log"}
TABLE_EXTENSIONS = handler_csv.TABLE_EXTENSIONS   # .csv .tsv .xlsx .xlsm，交給 handler_csv
```

複製到的檔案副檔名如果不在這幾個集合裡，會直接跳過不處理。想支援更多類型，把副檔名加進對應集合即可。注意 `.csv` 已經從純文字檔移到表格類。

### 5. 調整表格（csv / xlsx）的欄位規則

`handler_csv.py` 的 `HEADER_RULES`：一行一條 `(表頭 regex, 標籤類型)`，命中的欄位不看內容、整欄遮蔽：

```python
HEADER_RULES = [
    (re.compile(r"姓名|名字|聯絡人", re.I), "PERSON"),
    (re.compile(r"e-?mail|電子郵件|信箱", re.I), "EMAIL_ADDRESS"),
    ...
]
```

其他欄位只要有一格偵測到個資就整欄遮蔽。如果不想因為某些類型（例如日期、網址）就整欄遮蔽：

```python
IGNORE_TYPES_FOR_COLUMN = {"DATE_TIME", "URL"}
```

### 6. 其他常用小設定

| 想改什麼                              | 在哪個檔案                         | 變數                                                                     |
| ------------------------------------- | ---------------------------------- | ------------------------------------------------------------------------ |
| OCR 語言包                            | `handler_media.py`               | `OCR_LANG`（預設 `"chi_tra+eng"`）                                   |
| 塗黑框留白像素                        | `handler_media.py`               | `MASK_PADDING`                                                         |
| tesseract 執行檔路徑                  | `handler_media.py`               | `TESSERACT_CMD`                                                        |
| 對照表檔名                            | `core.py` / `handler_media.py` | `MAP_FILE` / `DEFAULT_MAP_FILE`                                      |
| 敏感清單檔名 / 是否自動遮蔽           | `core.py`                        | `SENSITIVE_FILE` / `USE_KNOWN_ITEMS`                                 |
| 清單最短記錄長度                      | `core.py`                        | `KNOWN_MIN_LEN`（預設 3）                                              |
| 資料夾掃描略過的資料夾                | `core.py`                        | `SCAN_SKIP_DIRS`                                                       |
| 判定為「高風險」的類型關鍵字          | `core.py`                        | `HIGH_RISK_KEYWORDS`                                                   |
| 程式碼判斷門檻                        | `core.py`                        | `heuristic_is_code()` 裡的 `>= 3`                                    |
| 預設快速鍵                            | `hotkey_service.py`              | `DEFAULT_CONFIG`                                                       |
| 中文 NER 模型換其他 Hugging Face 模型 | `handler_text.py`                | `CustomHfChineseRecognizer.__init__` 裡 `pipeline(..., model="...")` |
