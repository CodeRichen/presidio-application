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

## 安裝

```bash
pip install pywin32 psutil pyperclip pynput
pip install presidio-analyzer pillow pytesseract pymupdf
python -m spacy download en_core_web_lg
```

另外要安裝 Tesseract OCR 主程式（非 pip 套件，`pytesseract` 只是呼叫它）：
- Windows：<https://github.com/UB-Mannheim/tesseract/wiki>（安裝時勾選 Chinese-Traditional）
- macOS：`brew install tesseract tesseract-lang`
- Linux：`sudo apt install tesseract-ocr tesseract-ocr-chi-tra`

Windows 上如果 `tesseract.exe` 沒加進 PATH，改 `handler_media.py` 裡的 `TESSERACT_CMD`。

沒裝 Presidio 或對應模型也沒關係，程式會自動偵測不到就退化成只用正則規則。

---

## 整體流程

### 複製 (`Ctrl+Alt+C`，可改)

```
按下快速鍵
   │
   ▼
模擬真正的 Ctrl+C（讓來源程式正常複製）
   │
   ▼
讀取剪貼簿 ── 是「檔案」？ ──是──▶ 依副檔名分類 ──▶ MEDIA_EXTENSIONS？ ──是──▶ handler_media.mask_file()
   │                                            │                              (OCR→規則+Presidio→塗黑→存對照表)
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
              └─否──▶ handler_text.detect()（個資規則 + Presidio，中文內容自動跳過 Presidio）
                        │
                        ▼
                合併重疊區間 → 換成 <ETYPE_n> 標籤 → 寫進 clipboard_map.txt
                        │
                        ▼
              文字結果 pyperclip.copy() /
              檔案結果 copy_files_to_clipboard()
                （檔案類：圖片/PDF/程式碼檔/文字檔都是「產生遮蔽後的檔案」直接放回剪貼簿）
```

### 貼上 (`Ctrl+Alt+V`，可改)

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

| 檔案 | 內容 | 誰在維護 |
|---|---|---|
| `clipboard_map.txt` | 純文字/程式碼的 `標籤 -> 原文` | `main.py` |
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

改成你要的組合，語法照 [pynput 的 HotKey 格式](https://pynput.readthedocs.io/en/latest/keyboard.html#global-hotkeys)，例如：

```python
HOTKEY_COPY = '<ctrl>+<shift>+m'      # 換成 Ctrl+Shift+M
HOTKEY_PASTE = '<ctrl>+<alt>+<shift>+v'  # 三鍵組合也可以
```

支援的修飾鍵名稱：`<ctrl>`、`<alt>`、`<shift>`、`<cmd>`（Mac）；一般按鍵直接寫小寫字母/數字。

### 2. 新增偵測規則（電話/信用卡/API Key 這類）

一般文字的規則在 `handler_text.py` 的 `RULES`，程式碼專屬規則在 `handler_code.py` 的 `RULES`。格式都一樣：

```python
Rule(name, pattern, flags=0, validator=None)
```

- `name`：遮蔽後標籤裡會用到的名稱，例如 `<PASSPORT_1>`。
- `pattern`：正則表達式字串。
- `flags`：選填，例如 `re.IGNORECASE`。
- `validator`：選填的二次驗證函式 `func(matched_text) -> bool`，比對到之後再驗證一次降低誤判（`CREDIT_CARD` 用 Luhn 演算法驗證就是這樣做的）。

範例：新增台灣護照號碼規則（9 碼數字）：

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

加完存檔即可，`_COMPILED` 那行會自動重新編譯，不用改其他地方。

### 3. 換掉或加入「判斷文字是不是程式碼」的模型

`main.py` 裡的 `CLASSIFY_MODELS` 是一個註冊表：

```python
def heuristic_is_code(text: str) -> bool:
    ...

CLASSIFY_MODELS = {
    "heuristic": heuristic_is_code,
}
TEXT_CLASSIFY_MODE = "heuristic"
```

要加一個新的判斷方式（不管是規則式、呼叫本地模型、還是打 API），步驟固定三步：

```python
# 1. 寫一個函式，輸入文字、輸出 True(是程式碼)/False(不是)
def qwen_is_code(text: str) -> bool:
    # 這裡放你真正的判斷邏輯，例如呼叫本地跑起來的 LLM
    result = my_qwen_model.classify(text)
    return result == "code"

# 2. 註冊進字典
CLASSIFY_MODELS = {
    "heuristic": heuristic_is_code,
    "qwen": qwen_is_code,
}

# 3. 切換要用哪一個
TEXT_CLASSIFY_MODE = "qwen"
```

改完 `TEXT_CLASSIFY_MODE` 這一行，其他呼叫的地方（`anonymize_text()` 裡的 `is_code_text()`）完全不用動。

### 4. 新增/排除 Presidio 判斷的實體類型

`handler_text.py`：

```python
EXCLUDED_PRESIDIO_LABELS = {"US_DRIVER_LICENSE"}
```

不想遮蔽某個 Presidio 判斷出來的類型，就把它的名稱加進這個 `set`，例如額外排除信用卡（因為你已經有自己的 `CREDIT_CARD` 正則規則）：

```python
EXCLUDED_PRESIDIO_LABELS = {"US_DRIVER_LICENSE", "CREDIT_CARD"}
```

### 5. 調整「複製檔案」時的分類規則

`main.py` 設定區：

```python
CODE_EXTENSIONS = {".py", ".js", ".ts", ...}
MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf", ...}
TEXT_FILE_EXTENSIONS = {".txt", ".md", ".csv", ".log"}
```

複製到的檔案副檔名如果不在這三個集合裡，會直接跳過不處理。想支援更多類型，把副檔名加進對應集合即可，例如想讓 `.docx` 也走純文字流程（注意：`.docx` 其實是壓縮格式，直接用文字模式讀取會亂碼，這裡只是示範怎麼加，真的要支援 Word 檔建議另外寫一個 handler 用 `python-docx` 讀取內容）：

```python
TEXT_FILE_EXTENSIONS = {".txt", ".md", ".csv", ".log", ".rst"}
```

### 6. 調整圖片/PDF 遮蔽後的檔案要存哪裡

`handler_media.py`：

```python
MEDIA_OUTPUT_DIR = None
```

- `None`（預設）：存到系統暫存資料夾，不會弄髒原始檔案所在的位置（反正結果都會放回剪貼簿）。
- `"SAME_FOLDER"`：跟原始檔案同一個資料夾，檔名加 `_masked`/`_restored`。
- 自訂字串，例如 `r"C:\DeidOutput"`：統一集中存到你指定的資料夾，不存在會自動建立。

### 7. 其他常用小設定

| 想改什麼 | 在哪個檔案 | 變數 |
|---|---|---|
| OCR 語言包 | `handler_media.py` | `OCR_LANG`（預設 `"chi_tra+eng"`） |
| 塗黑框留白像素 | `handler_media.py` | `MASK_PADDING` |
| tesseract 執行檔路徑 | `handler_media.py` | `TESSERACT_CMD` |
| 對照表檔名 | `main.py` / `handler_media.py` | `MAP_FILE` / `DEFAULT_MAP_FILE` |
| 程式碼判斷門檻 | `main.py` | `heuristic_is_code()` 裡的 `>= 3` |

---

## 已知限制

- `Ctrl+Alt+C`/`V` 是**模擬**按鍵：實際上是先幫你按一次真正的 Ctrl+C/V，再做後續處理，所以來源程式看到的仍是正常的複製/貼上操作。
- 這是全域鍵盤監聽 + 讀寫剪貼簿的工具，設計給個人本機使用；不建議打包成公開發佈的產品直接讓不熟悉的使用者安裝執行。
- 圖片/PDF 的偵測精準度取決於 OCR 辨識品質，掃描歪斜、字太小、手寫字都可能漏偵測，不是百分之百保證。
- 終端機按 `Ctrl+C` 才能正常結束程式；直接關掉終端機視窗也可以，但不會有「正在關閉監聽...」的訊息。
