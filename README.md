# 剪貼簿敏感資訊防護工具

複製東西時自動偵測、遮蔽敏感資訊，貼上時可以還原；圖片/PDF/程式碼/一般文字分開處理。

## 檔案結構

```
main.py           快速鍵監聽 + 分類 + 分派 + 標籤替換 + 剪貼簿讀寫（唯一需要執行的檔案）
handler_text.py   一般文字規則（電話、信用卡、Email、URL、檔名、引號密碼...）+ Presidio 模型
handler_code.py   程式碼規則（API Key、JWT、檔案路徑）+ 借用 handler_text 的個資規則
handler_media.py  圖片 / PDF：OCR 抓文字位置 → 借用 handler_code 的規則 + Presidio 判斷 → 塗黑 → 存對照表
```

四個檔案要放在**同一個資料夾**，執行 `python main.py` 即可，不用另外跑其他檔案。

---

## 使用的技術與實作方法

| 需求                             | 用的技術                                                                                                                                                                                                                                                                                                   | 在哪個檔案                                |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| 全域快速鍵監聽                   | `pynput.keyboard.GlobalHotKeys`：註冊系統層級的鍵盤鉤子，不用視窗取得焦點也能觸發                                                                                                                                                                                                                        | `main.py`                               |
| 模擬真正的 Ctrl+C / Ctrl+V       | `win32api.keybd_event`：直接送出虛擬按鍵事件給作業系統，效果等同使用者真的按下該按鍵                                                                                                                                                                                                                     | `main.py`                               |
| 讀取剪貼簿是文字還是檔案         | `win32clipboard.IsClipboardFormatAvailable(CF_HDROP)`：檢查剪貼簿格式，`CF_HDROP` 代表複製的是檔案清單                                                                                                                                                                                                 | `main.py`                               |
| 把處理完的檔案放回剪貼簿         | 手動組出 Windows 的`DROPFILES` 結構（`struct.pack` 組 header + UTF-16LE 檔名清單）塞進 `SetClipboardData(CF_HDROP, ...)`，等同在檔案總管「複製」了那個檔案                                                                                                                                           | `main.py`                               |
| 取得目前最前景視窗資訊           | `win32gui.GetForegroundWindow` + `win32process.GetWindowThreadProcessId` + `psutil.Process` 反查程式名稱                                                                                                                                                                                             | `main.py`                               |
| 正則規則比對                     | Python 內建`re`，用 `dataclass Rule(name, pattern, flags, validator)` 統一格式，方便一行一行加規則                                                                                                                                                                                                     | `handler_text.py` / `handler_code.py` |
| 降低誤判                         | 部分規則加`validator` 二次驗證，例如信用卡卡號用 **Luhn 演算法**驗證檢查碼，不是隨便 13~19 碼數字都算                                                                                                                                                                                              | `handler_text.py`                       |
| 重疊區間比對結果合併             | 自己寫的`merge_overlaps()`：依起點排序、起點相同取較長者，貪婪掃描去除重疊，避免多個規則同時命中同一段文字時，用舊座標切字串切壞                                                                                                                                                                         | `main.py` / `handler_media.py`        |
| 英文 NLP 判斷 (PERSON/EMAIL/...) | **Microsoft Presidio** (`presidio-analyzer`)，底層 NLP engine 用 spaCy 的 `en_core_web_sm`                                                                                                                                                                                                       | `handler_text.py`                       |
| 中文 NLP 判斷                    | 同一個 Presidio`AnalyzerEngine`，但 NLP engine 換成 spaCy 的 `zh_core_web_sm`，另外**額外掛載**一個自訂的 `EntityRecognizer` 子類別，內部包一個 Hugging Face `transformers.pipeline("ner")`，模型是 `ckiplab/bert-base-chinese-ner`，補強 spaCy 中文模型本身抓人名/地名/機構名不夠準的問題 | `handler_text.py`                       |
| 語言判斷、模型可替換的設計       | 依 Unicode 範圍`\u4e00`~`\u9fff` 判斷是否含中文，再從 `NLP_MODELS = {"en": ..., "zh": ...}` 這個字典挑函式呼叫（Strategy Pattern），換模型只要改字典內容                                                                                                                                             | `handler_text.py`                       |
| 判斷文字像不像程式碼             | 純規則式的特徵計數（`{`、`;`、`def`、`import`、`#include` 等出現次數 ≥ 3），一樣包成可替換的 `CLASSIFY_MODELS` 字典                                                                                                                                                                           | `main.py`                               |
| 圖片文字辨識 (OCR)               | `pytesseract`（呼叫 Tesseract OCR 執行檔），語言包用 `chi_tra+eng` 同時認中英文，`image_to_data` 拿到逐字的文字內容跟像素座標                                                                                                                                                                        | `handler_media.py`                      |
| 把同一行的字組回一句話           | 用 Tesseract 回傳的`block_num`/`par_num`/`line_num` 分組、`word_num` 排序，重建成一行文字後再拿去跑規則/模型，比對到的字元區間再換算回涵蓋那些字的像素框                                                                                                                                           | `handler_media.py`                      |
| PDF 文字直接遮蔽（非疊色塊）     | `PyMuPDF (fitz)` 的 `add_redact_annot()` + `apply_redactions()`：這是「真正的遮蔽」，會把文字內容從 PDF 資料流裡整個移除，不是只在上面疊一層黑色矩形                                                                                                                                                 | `handler_media.py`                      |
| PDF 掃描頁（沒有文字圖層）處理   | 用`page.get_text("words")` 判斷字數是否過少，過少就整頁 `get_pixmap()` 轉成圖片走跟一般圖片一樣的 OCR 流程，偵測到的像素座標再除以縮放倍率換算回 PDF 座標                                                                                                                                              | `handler_media.py`                      |
| 遮蔽內容如何還原                 | 遮蔽前先把該區域裁切下來轉成 PNG，`base64` 編碼後存進 JSON 對照表；純文字/程式碼則是原文直接存進去，還原就是查表把 `<TAG_n>` / 遮蔽像素換回原本內容                                                                                                                                                    | 全部檔案                                  |

---

## 安裝

```bash
pip install pywin32 psutil pyperclip pynput
pip install presidio-analyzer pillow pytesseract pymupdf
pip install transformers torch          # 中文 NER 用
python -m spacy download en_core_web_sm
python -m spacy download zh_core_web_sm
```

另外要安裝 Tesseract OCR 主程式（非 pip 套件，`pytesseract` 只是呼叫它）：

- Windows：[https://github.com/UB-Mannheim/tesseract/wiki](https://github.com/UB-Mannheim/tesseract/wiki)（安裝時勾選 Chinese-Traditional）
- macOS：`brew install tesseract tesseract-lang`
- Linux：`sudo apt install tesseract-ocr tesseract-ocr-chi-tra`

Windows 上如果 `tesseract.exe` 沒加進 PATH，改 `handler_media.py` 裡的 `TESSERACT_CMD`。

第一次執行時，`ckiplab/bert-base-chinese-ner` 會自動從 Hugging Face 下載（幾百 MB），需要網路；之後會用本地快取。沒裝 `transformers`、沒網路下載模型、或完全沒裝 Presidio，程式都會自動偵測失敗並退化（分別是：中文只剩 spaCy 內建結果 / 中英文都只剩 spaCy 內建結果 / 全部只剩正則規則），不會讓整支程式掛掉。

---

## 整體流程

### 複製 (`Ctrl+C`，可改)

```
按下快速鍵
   │
   ▼
模擬真正的 Ctrl+C（讓來源程式正常複製）
   │
   ▼
讀取剪貼簿 ── 是「檔案」？ ──是──▶ 依副檔名分類 ──▶ MEDIA_EXTENSIONS？ ──是──▶ handler_media.mask_file()
   │                                            │                              (OCR→正則+Presidio→塗黑→存對照表)
   否                                           否
   │                                            ▼
   │                                     CODE_EXTENSIONS 或
   │                                     TEXT_FILE_EXTENSIONS？──是──▶ 讀檔內容 ──▶ 走下面「文字」流程
   │                                            │
   │                                            否 ──▶ 跳過(不在設定內的副檔名)
   ▼
內容含 <TAG_n> 標籤？
   │
   ├─是──▶ 查 clipboard_map.txt，還原成原文
   │
   └─否──▶ is_code_text(text) 判斷像不像程式碼
              │
              ├─是──▶ handler_code.detect()（API Key/JWT/路徑 + 個資規則，不跑 Presidio）
              │
              └─否──▶ handler_text.detect()
                        │
                        ├─ 正則規則 (RULES)
                        │
                        └─ contains_chinese(text)？
                             ├─是──▶ presidio_zh()：spaCy 中文模型 + CKIP Hugging Face NER
                             └─否──▶ presidio_en()：spaCy 英文模型
                        │
                        ▼
                合併重疊區間 (merge_overlaps) → 換成 <ETYPE_n> 標籤 → 寫進 clipboard_map.txt
                        │
                        ▼
              文字結果 pyperclip.copy() /
              檔案結果 copy_files_to_clipboard()
                （檔案類：圖片/PDF/程式碼檔/文字檔都是「產生遮蔽後的檔案」直接放回剪貼簿）
```

### 貼上 (`Ctrl+V`，可改)

```
按下快速鍵
   │
   ▼
剪貼簿是純文字，且含 <TAG_n> 標籤？
   │
   ├─是──▶ 查表還原成真實內容 → 模擬 Ctrl+V 貼上 → 立刻把剪貼簿改回標籤版本(避免真實資料留在剪貼簿)
   │
   └─否──▶ 直接模擬 Ctrl+V 貼上（一般內容或檔案不受影響）
```

### 對照表檔案

| 檔案                    | 內容                                               | 誰在維護             |
| ----------------------- | -------------------------------------------------- | -------------------- |
| `clipboard_map.txt`   | 純文字/程式碼的`標籤 -> 原文`                    | `main.py`          |
| `media_deid_map.json` | 圖片/PDF 每個遮蔽區塊的座標與原始畫面截圖 (base64) | `handler_media.py` |

這兩個檔案存的是**明碼敏感資訊**，注意存放位置的權限，不要外流；也不要手動亂改格式。

---

## 客製化教學

以下每一項都只要改對應檔案「設定區」的一小段，不用動偵測/替換的核心邏輯。

### 1. 改快速鍵

在 `main.py` 最上面：

```python
HOTKEY_COPY = '<ctrl>+<alt>+c'
HOTKEY_PASTE = '<ctrl>+<alt>+v'
```

改成你要的組合，語法照 [pynput 的 HotKey 格式](https://pynput.readthedocs.io/en/latest/keyboard.html#global-hotkeys)：

```python
HOTKEY_COPY = '<ctrl>+<shift>+m'
HOTKEY_PASTE = '<ctrl>+<alt>+<shift>+v'   # 三鍵組合也可以
```

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

### 4. 換掉「判斷文字是不是程式碼」的方式

`main.py` 裡的 `CLASSIFY_MODELS`：

```python
CLASSIFY_MODELS = {
    "heuristic": heuristic_is_code,
}
TEXT_CLASSIFY_MODE = "heuristic"
```

要加一個新的判斷方式，一樣是「寫函式 → 註冊進字典 → 切換 key」三步：

```python
def qwen_is_code(text: str) -> bool:
    return my_qwen_model.classify(text) == "code"

CLASSIFY_MODELS["qwen"] = qwen_is_code
TEXT_CLASSIFY_MODE = "qwen"
```

### 5. 新增/排除 Presidio 判斷的實體類型

`handler_text.py`：

```python
EXCLUDED_PRESIDIO_LABELS = {"US_DRIVER_LICENSE"}
```

不想遮蔽某個 Presidio 判斷出來的類型，就把它的名稱加進這個 `set`。

### 6. 調整「複製檔案」時的分類規則

`main.py` 設定區：

```python
CODE_EXTENSIONS = {".py", ".js", ".ts", ...}
MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf", ...}
TEXT_FILE_EXTENSIONS = {".txt", ".md", ".csv", ".log"}
```

複製到的檔案副檔名如果不在這三個集合裡，會直接跳過不處理。想支援更多類型，把副檔名加進對應集合即可。

### 7. 調整圖片/PDF 遮蔽後的檔案要存哪裡

`handler_media.py`：

```python
MEDIA_OUTPUT_DIR = None
```

- `None`（預設）：存到系統暫存資料夾，不會弄髒原始檔案所在的位置。
- `"SAME_FOLDER"`：跟原始檔案同一個資料夾，檔名加 `_masked`/`_restored`。
- 自訂字串，例如 `r"C:\DeidOutput"`：統一集中存到你指定的資料夾。

### 8. 其他常用小設定

| 想改什麼                              | 在哪個檔案                         | 變數                                                                     |
| ------------------------------------- | ---------------------------------- | ------------------------------------------------------------------------ |
| OCR 語言包                            | `handler_media.py`               | `OCR_LANG`（預設 `"chi_tra+eng"`）                                   |
| 塗黑框留白像素                        | `handler_media.py`               | `MASK_PADDING`                                                         |
| tesseract 執行檔路徑                  | `handler_media.py`               | `TESSERACT_CMD`                                                        |
| 對照表檔名                            | `main.py` / `handler_media.py` | `MAP_FILE` / `DEFAULT_MAP_FILE`                                      |
| 程式碼判斷門檻                        | `main.py`                        | `heuristic_is_code()` 裡的 `>= 3`                                    |
| 中文 NER 模型換其他 Hugging Face 模型 | `handler_text.py`                | `CustomHfChineseRecognizer.__init__` 裡 `pipeline(..., model="...")` |

---

## 已知限制

- `Ctrl+Alt+C`/`V` 是**模擬**按鍵：實際上是先幫你按一次真正的 Ctrl+C/V，再做後續處理，所以來源程式看到的仍是正常的複製/貼上操作。
- 這是全域鍵盤監聽 + 讀寫剪貼簿的工具，設計給個人本機使用；不建議打包成公開發佈的產品直接讓不熟悉的使用者安裝執行。
- 圖片/PDF 的偵測精準度取決於 OCR 辨識品質，掃描歪斜、字太小、手寫字都可能漏偵測，不是百分之百保證。
- CKIP 中文 NER 第一次執行需要網路下載模型；離線環境會自動退化成只用 spaCy 內建結果。
- 終端機按 `Ctrl+C` 才能正常結束程式；直接關掉終端機視窗也可以，但不會有「正在關閉監聽...」的訊息。
