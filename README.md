# PII 偵測與假名化系統(剪貼簿敏感資訊防護工具)

複製東西時自動偵測、遮蔽敏感資訊，貼上時可以還原；圖片/PDF/程式碼/一般文字/表格分開處理。
可以用全域快速鍵(命令列版)，也可以開圖形介面手動處理、掃描整個資料夾、管理敏感名單(敏感清單 / 黑名單 / 安全清單)。

## 檔案結構

```
core.py            共用核心：前處理接入、遮蔽/還原、重疊處理、分派(code/text/media/field/csv)、資料夾掃描、剪貼簿工具、模型預熱
entity_store.py    名單管理(敏感清單 / 黑名單 / 安全清單)、手動新增與自動判斷類型，main.py 與 ui.py 共用
hotkey_service.py  全域快速鍵服務(模式 A/B)與 hotkeys.json 設定檔，main.py 與 ui.py 共用
preprocess.py      送模型前的文字前處理（全形轉半形 / 隱形字元 / 網頁標籤 / 簡繁 / 空白），UI、快速鍵、檔案、圖片 OCR 共用，偵測結果換算回原文座標
main.py            命令列版入口：全域快速鍵 + 名單管理指令(list / add / remove / safe ...)
ui.py              圖形介面(tkinter)：文字 / 檔案 / 資料夾掃描 / 名單管理 / 快速鍵 / 對照表
handler_text.py   一般文字規則（電話、信用卡、Email、URL、檔名、引號密碼...）+ Presidio 模型 + 已知實體清單(known_entities.txt)
handler_code.py   程式碼規則（API Key、JWT、檔案路徑）+ 借用 handler_text 的個資規則
handler_csv.py    表格(csv / tsv / xlsx)：欄位內有個資則整欄遮蔽
handler_media.py  圖片 / PDF：OCR 抓文字位置 → 借用 handler_code 的規則 + Presidio 判斷 → 塗黑 → 存對照表
handler_field.py  （選用）欄位型偵測，存在時 core.py 會自動併入候選
```

所有檔案要放在**同一個資料夾**。`main.py` 與 `ui.py` 互相獨立，彼此不 import，兩者都只呼叫 `core.py` / `entity_store.py` / `hotkey_service.py`：

```bash
python main.py                  # 命令列版：全域快速鍵，執行中也能直接輸入名單管理指令
python main.py list             # 一次性名單指令(不啟動快速鍵、不用等模型載入)，見下方「名單怎麼運作」
python ui.py                    # 圖形介面版（可在「快速鍵」分頁啟動同樣的全域快速鍵）
```

兩個不要同時啟動快速鍵，會綁定兩次。

---

## 使用的技術與實作方法

| 需求                             | 用的技術                                                                                                                                                                                                                                                                                                           | 在哪個檔案                                  |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------- |
| 全域快速鍵監聽                   | `pynput.keyboard.GlobalHotKeys`：註冊系統層級的鍵盤鉤子，不用視窗取得焦點也能觸發；callback 立刻返回，實際工作丟到背景執行緒，不會卡住鍵盤鉤子                                                                                                                                                                   | `hotkey_service.py`                       |
| 快速鍵設定與執行中更換           | 設定存成`hotkeys.json`，用 `keyboard.HotKey.parse` 驗證格式；更換時停掉舊的 listener、用新設定重新綁定，不用重開程式。貼上快速鍵留空即為模式 B                                                                                                                                                                 | `hotkey_service.py` / `ui.py`           |
| 模擬真正的 Ctrl+C / Ctrl+V       | `win32api.keybd_event`：直接送出虛擬按鍵事件給作業系統，效果等同使用者真的按下該按鍵                                                                                                                                                                                                                             | `hotkey_service.py`                       |
| 等剪貼簿真的寫好                 | `win32clipboard.GetClipboardSequenceNumber`：用剪貼簿序號判斷內容是否已更新，比固定 sleep 可靠；剪貼簿被佔用時讀取會重試                                                                                                                                                                                         | `core.py`                                 |
| 讀取剪貼簿是文字還是檔案         | `win32clipboard.IsClipboardFormatAvailable(CF_HDROP)`：檢查剪貼簿格式，`CF_HDROP` 代表複製的是檔案清單                                                                                                                                                                                                         | `core.py`                                 |
| 把處理完的檔案放回剪貼簿         | 手動組出 Windows 的`DROPFILES` 結構（`struct.pack` 組 header + UTF-16LE 檔名清單）塞進 `SetClipboardData(CF_HDROP, ...)`，等同在檔案總管「複製」了那個檔案                                                                                                                                                   | `core.py`                                 |
| 取得目前最前景視窗資訊           | `win32gui.GetForegroundWindow` + `win32process.GetWindowThreadProcessId` + `psutil.Process` 反查程式名稱                                                                                                                                                                                                     | `core.py`                                 |
| 正則規則比對                     | Python 內建`re`，用 `dataclass Rule(name, pattern, flags, validator)` 統一格式，方便一行一行加規則                                                                                                                                                                                                             | `handler_text.py` / `handler_code.py`   |
| 降低誤判                         | 部分規則加`validator` 二次驗證，例如信用卡卡號用 **Luhn 演算法**驗證檢查碼，不是隨便 13~19 碼數字都算                                                                                                                                                                                                      | `handler_text.py`                         |
| 重疊區間比對結果合併             | 自己寫的`merge_overlaps()`：先依來源優先序（正則規則 > 與整段語言吻合的模型 > 另一語言的模型），同層取範圍較長者、再比信心分數，貪婪掃描去除重疊，避免多個規則同時命中同一段文字時，用舊座標切字串切壞                                                                                                           | `core.py` / `handler_media.py`          |
| 英文 NLP 判斷 (PERSON/EMAIL/...) | **Microsoft Presidio** (`presidio-analyzer`)，底層 NLP engine 用 spaCy 的 `en_core_web_sm`                                                                                                                                                                                                               | `handler_text.py`                         |
| 中文 NLP 判斷                    | 不經過 Presidio / spaCy，直接呼叫 Hugging Face 的 NER pipeline（`ZH_NER_MODEL_NAME`，目前是 `uer/roberta-base-finetuned-cluener2020-chinese`），標籤對應成 PERSON / ADDRESS / ORGANIZATION / POSITION，信心分數低於 `MIN_ZH_NER_SCORE` 的丟掉；長文字切成 350 字、重疊 50 字的區塊分別推論，再把座標換回原文 | `handler_text.py`                         |
| 送模型前的文字前處理             | 全形轉半形（NFKC）→ 隱形字元 → 網頁標籤 → 簡繁統一（逐字）→ 換行與空白；每個輸出字元記住對應的原文區間（`array`），偵測結果用 `map_matches()` 換算回原文座標，格式認不得時改用原文重新偵測，不會給出錯誤位置；圖片/PDF 則包裝 `handler_media.detect_matches`，UI 可「預覽前處理」                        | `preprocess.py` / `core.py` / `ui.py` |
| 語言判斷、模型可替換的設計       | 依 Unicode 範圍`\u4e00`~`\u9fff` 判斷是否含中文，再從 `NLP_MODELS = {"en": ..., "zh": ...}` 這個字典挑函式呼叫（Strategy Pattern），換模型只要改字典內容                                                                                                                                                     | `handler_text.py`                         |
| 表格整欄遮蔽                     | 表頭命中`HEADER_RULES`（姓名/Email/電話...）就整欄遮蔽；否則逐格偵測（同值只偵測一次），**只要一格含個資就整欄換成標籤**，類型取該欄偵測最多的。csv 自動判斷分隔符號與編碼（UTF-8 / Big5），xlsx 用 `openpyxl`                                                                                           | `handler_csv.py`                          |
| 敏感清單與自動遮蔽               | 每次遮蔽偵測到的值（值、類型、次數、來源、自動或手動）記錄成`presidio/real/sensitive_items.json`，程式結束**不刪除**；偵測時把清單編成一條「長的優先」的正則，當成最高優先序的規則（`source="regex"`）自動遮蔽，英數值前後加邊界判斷；太短的值不記錄；檔案被別的程式改過會自動重新載入                   | `entity_store.py` / `core.py`           |
| 黑名單與安全清單                 | 從敏感清單刪除的值進`blocked_items.json`，之後自動記錄一律略過；安全清單`safe_items.json`的值在重疊處理前就從偵測結果濾掉，不遮蔽也不記錄；兩份都永久保留，寫檔採「先寫暫存檔再取代」，壞掉的檔案會改名保留不被覆蓋                                                                                            | `entity_store.py` / `core.py`           |
| 手動新增與自動判斷類型           | 新增時可不指定類型：先用規則與模型（Presidio、中文 NER）判斷值本身，認不出來就放進幾句情境句再判斷（NER 需要上下文才認得出人名），取涵蓋率 ≥ 60% 的最佳候選（規則 > 吻合語言的模型），仍判斷不出來才用 `SENSITIVE`                                                                                              | `entity_store.py`                         |
| 已知實體清單                     | 手動維護的`known_entities.txt`（`LABEL<Tab>值`）：依 label 分組、值由長到短排序、用 `re.escape` 組 alternation 並一次編譯，`source` 標 `"regex"`；檔案改了會自動重載                                                                                                                                     | `handler_text.py`                         |
| 資料夾掃描與風險分級             | 先在同層建立`原名_masked`並用`shutil.copytree`完整複製，再只處理複本：檔名不變、文字檔保留原編碼與換行、圖片/PDF 從原檔讀寫到複本、表格就地換回原檔名；有偵測到才算危險並依類型關鍵字（KEY / JWT / PASSWORD / CARD / NATIONAL_ID 等）分「高」「中」風險                                                        | `core.py` / `ui.py`                     |
| 命令列名單管理                   | `main.py`的子指令（list / add / remove / safe / unsafe / unblock / import）一次性執行、不載入模型；快速鍵執行中也能在同一個終端機輸入同樣的指令（`shlex` 解析，Windows 路徑不當跳脫字元）                                                                                                                      | `main.py`                                 |
| 圖形介面不卡住                   | `tkinter` + 背景執行緒：耗時工作（OCR、模型預熱、掃描）在 worker thread 跑，`print` 經 `queue` 導到記錄區；與快速鍵共用 `WORK_LOCK`，不會同時改對照表或搶剪貼簿                                                                                                                                            | `ui.py` / `hotkey_service.py`           |
| 避免第一次複製卡住               | 啟動時先用範例文字跑一次偵測（`core.warmup()`），讓 Presidio / NER 模型提前載入；預熱不會記錄進敏感清單                                                                                                                                                                                                          | `core.py`                                 |
| 圖片文字辨識 (OCR)               | **Windows 內建 OCR**（`ocr_words`）：辨識前先對圖片做影像前處理（`preprocess_for_ocr`），取得每個字的文字內容與像素座標；不再使用 Tesseract                                                                                                                                                              | `handler_media.py`                        |
| 把同一行的字組回一句話           | 依座標把同一行的字（`ocr_words`）組成一行文字（`_group_lines`）後再送 `detect_matches`；這行文字會先經過 `preprocess` 前處理，偵測結果換算回這行文字的位置，再換算回涵蓋那些字的像素框                                                                                                                     | `handler_media.py` / `core.py`          |
| PDF 文字直接遮蔽（非疊色塊）     | `PyMuPDF (fitz)` 的 `add_redact_annot()` + `apply_redactions()`：這是「真正的遮蔽」，會把文字內容從 PDF 資料流裡整個移除，不是只在上面疊一層黑色矩形                                                                                                                                                         | `handler_media.py`                        |
| PDF 掃描頁（沒有文字圖層）處理   | 用`page.get_text("words")` 判斷字數是否過少，過少就整頁 `get_pixmap()` 轉成圖片走跟一般圖片一樣的 OCR 流程，偵測到的像素座標再除以縮放倍率換算回 PDF 座標                                                                                                                                                      | `handler_media.py`                        |
| 遮蔽內容如何還原                 | 遮蔽前先把該區域裁切下來轉成 PNG，`base64` 編碼後存進 JSON 對照表；純文字/程式碼/表格則是原文直接存進去，還原就是查表把 `<TAG_n>` / 遮蔽像素換回原本內容                                                                                                                                                       | 全部檔案                                    |

---

## 安裝

```bash
pip install pywin32 psutil pyperclip pynput
pip install presidio-analyzer pillow pymupdf
pip install openpyxl                    # 處理 xlsx 表格用
pip install opencc-python-reimplemented  # 前處理的簡繁統一用
pip install transformers torch          # 中文 NER 用
python -m spacy download en_core_web_sm
```

圖形介面用的 `tkinter` 隨 Python 安裝，不用另外裝（Windows 安裝 Python 時請保留 tcl/tk 選項）。

圖片 / PDF 的文字辨識使用 **Windows 內建 OCR**，不再使用 Tesseract，也不用另外安裝 OCR 主程式。需要在 Windows 的「設定 → 時間與語言 → 語言與區域」
安裝要辨識的語言（繁體中文、英文）並確認有勾選「光學字元辨識」。呼叫 Windows OCR 所需的 Python 套件以 `handler_media.py` 檔頭的 import 為準。

第一次執行時，中文 NER 模型（`ZH_NER_MODEL_NAME`，目前是 `uer/roberta-base-finetuned-cluener2020-chinese`）會自動從 Hugging Face 下載（幾百 MB），需要網路；之後會用本地快取。沒裝 `transformers`、沒網路下載模型、或完全沒裝 Presidio，程式都會自動偵測失敗並退化，只用正則規則。

---

## 整體流程

### 文字前處理（送模型前）

文字、程式碼、表格儲存格、圖片與 PDF 的 OCR 文字，在送進規則與模型偵測**之前**，統一先過 `preprocess.py`（流程參考 text-preprocessing notebook）。UI 文字分頁、全域快速鍵、檔案、資料夾掃描、圖片/PDF 走的是同一套：

| 步驟             | 做什麼                                                                   | 備註                                                                           |
| ---------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------ |
| 1 全形轉半形     | NFKC：全形英數、標點、空白，以及相容字元（㈱→(株)）統一                 | 全形手機 / Email 轉成半形後，規則才抓得到                                      |
| 2 隱形字元清理   | 移除零寬字元、軟連字號、控制字元                                         | NFKC 不會處理這些                                                              |
| 3 網頁標籤刪除   | 移除 HTML 註解與 script / style、標籤換成換行、`&amp;` 等實體還原      | 只對一般文字；程式碼與圖片 OCR 文字不做；`<PERSON_1>` 這類遮蔽標籤不會被誤刪 |
| 4 簡繁統一       | 逐字簡轉繁（OpenCC `s2t`）                                             | 長度不變；沒裝 `opencc` 會略過這步                                           |
| 5 換行與空白整理 | `\r\n` 統一成 `\n`、水平空白壓成單一空格、連續換行合併、去除首尾空白 |                                                                                |

（編碼讀取 utf-8-sig → utf-8 → cp950 → big5 在 `core._read_text_file`；notebook 的「敏感資訊遮蔽」就是本系統本身。）

**關鍵設計：偵測用前處理後的文字，遮蔽卻改回原文。** 前處理會改變字數，所以每個輸出字元都記住它對應原文的區間，偵測結果再換算回原文座標。結果是：

- 使用者的原文只有被偵測到的那一段會變，全形、HTML 標籤、空白、簡體字其餘部分原樣保留。
- 對照表存的是原文，還原後與原文逐字相同（含 `\r\n` 與隱形字元）。
- 簡繁只做逐字轉換，不用 notebook 的 `s2twp`：詞彙層級轉換（如 内存→記憶體）會改變字數，無法對回原文位置。

在 UI 文字分頁按「預覽前處理」，可以看到每一步改了哪些地方與最後送模型的文字；勾選框可整個關閉（`preprocess.ENABLED`）。

> 注意：`handler_text.py` 目前的中文 NER 模型（`uer/roberta-base-finetuned-cluener2020-chinese`）是用**簡體**語料訓練的，簡轉繁雖然讓繁體的規則關鍵字（統一編號、健保投保序號…）抓得到，卻可能讓人名 / 地址的召回率下降。建議用測試程式比較 `CHINESE_CONVERT` 設成 `"s2t"` / `"t2s"` / `None` 的 F1，再決定預設值。

### 快速鍵的兩種模式

| 模式                   | 條件                  | 行為                                                                                                                                                         |
| ---------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| A・專屬快速鍵          | `paste` 有設定值    | 複製鍵只算出覆蓋結果並暫存，不動剪貼簿；按貼上鍵才暫時寫入剪貼簿、模擬 Ctrl+V、貼完立刻還原。原生 Ctrl+C / Ctrl+V 不受影響                                   |
| B・Ctrl+C 直接當快速鍵 | `paste` 留空 (null) | 原生 Ctrl+C 寫完剪貼簿後，程式讀出來處理並**直接覆寫剪貼簿**，原生 Ctrl+V 自然貼出處理後版本。處理完成前就貼上會貼出原始內容；終端機按 Ctrl+C 也會觸發 |

### 對照表與清單檔案

| 檔案                     | 內容                                                                | 誰在維護              | 結束時                 |
| ------------------------ | ------------------------------------------------------------------- | --------------------- | ---------------------- |
| `clipboard_map.txt`    | 純文字/程式碼/表格的`標籤 -> 原文`                                | `core.py`           | 刪除                   |
| `media_deid_map.json`  | 圖片/PDF 每個遮蔽區塊的座標與原始畫面截圖 (base64)                  | `handler_media.py`  | 刪除                   |
| `sensitive_items.json` | 敏感清單：值、類型、次數、最近出現、來源、類型判斷方式、自動/手動   | `entity_store.py`   | **保留，不刪除** |
| `blocked_items.json`   | 黑名單：從敏感清單刪除的值（永遠不會再被自動加入）                  | `entity_store.py`   | **保留，不刪除** |
| `safe_items.json`      | 安全清單：確認不是敏感資料的值（偵測到也不遮蔽）                    | `entity_store.py`   | **保留，不刪除** |
| `known_entities.txt`   | 手動維護的已知實體（`LABEL<Tab>值`），由 `handler_text.py` 讀取 | 使用者                | 保留                   |
| `hotkeys.json`         | 快速鍵設定（`copy` / `paste`）                                  | `hotkey_service.py` | 保留                   |

除了 `hotkeys.json`（放在程式旁邊），其餘都在 `presidio/real/` 底下，存的是**明碼敏感資訊**，注意存放位置的權限，不要外流；也不要手動亂改格式。

### 名單怎麼運作

三份名單由 `entity_store.py` 管理，`ui.py` 與 `main.py` 共用同一份；任何一邊改了，另一邊會自動重新載入（UI 每秒檢查一次）。

| 名單     | 作用                                                                                                                                                 |
| -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| 敏感清單 | 遮蔽時自動記錄 + 手動新增。清單裡的值之後會被當成正則規則（`source="regex"`，優先序最高）自動遮蔽，不需要模型再判斷                                |
| 黑名單   | 從敏感清單刪除的值。之後**永遠不會再被自動加進**敏感清單；模型仍可能在文字裡偵測到並遮蔽它，只是不再被記錄。手動新增同一個值時會自動移出黑名單 |
| 安全清單 | 確認「不是敏感資料」的值（例如公開的公司名稱、範例帳號）。偵測結果剛好等於清單裡的值時直接略過，不遮蔽，也不會被記進敏感清單                         |

1. **自動記錄**：文字 / 程式碼 / 表格 / 圖片 / PDF 每次遮蔽，偵測到的值會記錄進敏感清單；少於 3 個字的值不記（`KNOWN_MIN_LEN`），預熱、測試、表格逐格判斷用的暫時偵測不會記錄。
2. **一鍵刪除**：在 UI「名單管理 → 敏感清單」點最左邊的「✕」就立刻刪除，**不會跳出確認**（選取後按 Delete 或「刪除選取」也一樣）。刪掉的值進黑名單；反悔到「黑名單」分頁點「↩」移出即可。
3. **標為安全**：在敏感清單選取後按「標為安全」，或到「安全清單」分頁直接輸入，值會進安全清單並從敏感清單移除。
4. **手動新增**：UI「新增敏感資料」分頁一行一筆，可只填值，也可寫成 `類型<Tab>值`，也能匯入文字檔；命令列用 `main.py add`。**類型可以不填**，由模型判斷（見上方技術表），判斷結果與判斷方式會顯示在結果表。至少 2 個字。明確新增的值會自動移出黑名單 / 安全清單。
5. **關閉自動遮蔽**：`core.USE_KNOWN_ITEMS = False`（敏感清單仍會記錄，只是不拿來自動遮蔽）。
6. **圖片 / PDF**：OCR 文字送進 `handler_media.detect_matches` 時，`core.py` 會包一層，同樣套用前處理、敏感清單、`known_entities.txt` 與安全清單（偵測結果是 `(start, end, label, score, source)` 的 tuple 時才補清單；格式不同就只做前處理）。

**命令列指令**（一次性用 `python main.py 指令`，快速鍵執行中直接輸入即可，`help` 看說明、`exit` 結束）：

| 指令                          | 說明                                                     |
| ----------------------------- | -------------------------------------------------------- |
| `list [safe\|blocked]`       | 看敏感清單（預設）/ 安全清單 / 黑名單                    |
| `add 值 [值 ...] [-l 類型]` | 新增敏感資料；不指定類型就由模型判斷（值有空格請加引號） |
| `remove 值 [值 ...]`        | 從敏感清單刪除並加入黑名單                               |
| `safe 值 [值 ...]`          | 加入安全清單                                             |
| `unsafe 值 [值 ...]`        | 移出安全清單                                             |
| `unblock 值 [值 ...]`       | 移出黑名單                                               |
| `import 檔案 [-l 類型]`     | 匯入文字檔（每行「值」或「類型<Tab></tab>值」）          |

### 資料夾掃描

在介面的「資料夾掃描」分頁選一個資料夾（可選是否包含子資料夾）→「開始掃描並遮蔽」：

1. 在選擇的資料夾**同層**建立 `<原名>_masked`（已存在就 `_masked_2`、`_masked_3`…），**先把原資料夾裡所有東西複製過去**（含子資料夾、空資料夾、不支援的檔案）。
2. 只處理複製出來的檔案：**檔名不變**，有敏感資訊的檔案內容會被遮蔽，其他檔案原樣保留；原資料夾完全不動。
   - 文字 / 程式碼：保留原編碼（UTF-8 / BOM / Big5）與換行符號。
   - 圖片 / PDF：從原檔讀、寫到複本；沒偵測到內容時複製回原檔，不會留下被重寫過的版本。
   - 表格：欄位內有個資就整欄遮蔽；輸出會變成 UTF-8（含 BOM）。
3. 結果表格依風險排序：**高**（含 API Key、JWT、密碼、信用卡、身分證號等）→ **中**（其他個資）→ 安全。掃描完會跳出對話框列出危險檔案與輸出資料夾位置。
4. 選到 `*_masked` 輸出資料夾會被拒絕；掃描上層資料夾時也會自動略過先前產生的輸出資料夾。`.git`、`node_modules` 等（`SCAN_SKIP_DIRS`）會複製但不掃描。

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
    "zh": zh_ner,        # 目前是 Hugging Face 的中文 NER (ZH_NER_MODEL_NAME)，不經過 Presidio / spaCy
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

### 6. 管理敏感名單與已知實體清單

**方法 A（建議）**：開 `ui.py` →「名單管理」分頁：敏感清單（點 ✕ 刪除）、新增敏感資料（類型可不填）、黑名單、安全清單。
**方法 B**：命令列 `python main.py add 邢立承`、`python main.py list`（指令見上方「名單怎麼運作」）。
**方法 C**：直接手改 `known_entities.txt`，每行 `LABEL<Tab>值`（欄位間必須是 Tab，`#` 開頭與空行會被忽略，LABEL 只能是大寫英文與底線）：

```
PERSON	邢立承
ORGANIZATION	某某公司
```

`known_entities.txt` 是純手動維護的檔案，由 `handler_text.py` 當成正則規則讀取，改了會自動重載；敏感清單 `sensitive_items.json` 則由程式自動記錄、可在 UI 管理。兩者可以並存，同一段文字重複命中時會在重疊處理中合併成一筆。

### 7. 其他常用小設定

| 想改什麼                           | 在哪個檔案                         | 變數                                                                     |
| ---------------------------------- | ---------------------------------- | ------------------------------------------------------------------------ |
| OCR 辨識設定（語言等）             | `handler_media.py`               | 以該檔設定區為準（已不是 Tesseract 的 `OCR_LANG` / `TESSERACT_CMD`） |
| 塗黑框留白像素                     | `handler_media.py`               | `MASK_PADDING`                                                         |
| 對照表檔名                         | `core.py` / `handler_media.py` | `MAP_FILE` / `DEFAULT_MAP_FILE`                                      |
| 名單存放資料夾                     | `entity_store.py`                | `REAL_DIR`（預設 `./presidio/real`）                                 |
| 敏感清單是否自動遮蔽               | `core.py`                        | `USE_KNOWN_ITEMS`                                                      |
| 自動記錄 / 手動新增的最短長度      | `entity_store.py`                | `KNOWN_MIN_LEN`（預設 3）/ `MANUAL_MIN_LEN`（預設 2）                |
| 已知實體清單檔位置                 | `handler_text.py`                | `KNOWN_ENTITIES_FILE`                                                  |
| 掃描輸出資料夾的後綴               | `core.py`                        | `SCAN_OUTPUT_SUFFIX`（預設 `_masked`）                               |
| 資料夾掃描略過的資料夾（仍會複製） | `core.py`                        | `SCAN_SKIP_DIRS`                                                       |
| 判定為「高風險」的類型關鍵字       | `core.py`                        | `HIGH_RISK_KEYWORDS`                                                   |
| 程式碼判斷門檻                     | `core.py`                        | `heuristic_is_code()` 裡的 `>= 3`                                    |
| 預設快速鍵                         | `hotkey_service.py`              | `DEFAULT_CONFIG`                                                       |
| 中文 NER 模型 / 信心分數門檻       | `handler_text.py`                | `ZH_NER_MODEL_NAME` / `MIN_ZH_NER_SCORE`（預設 0.7）                 |
| 前處理總開關                       | `preprocess.py`                  | `ENABLED`（UI 文字分頁的「送模型前先前處理」勾選框就是它）             |
| 前處理各步驟開關                   | `preprocess.py`                  | `OPT_NFKC` / `OPT_INVISIBLE` / `OPT_HTML` / `OPT_WHITESPACE`     |
| 簡繁統一方向                       | `preprocess.py`                  | `CHINESE_CONVERT`（`"s2t"` 簡轉繁 / `"t2s"` 繁轉簡 / `None`）    |
| 圖片 / PDF 的 OCR 文字是否前處理   | `preprocess.py`                  | `APPLY_TO_MEDIA`                                                       |
| 超長文字略過前處理的門檻           | `preprocess.py`                  | `MAX_CHARS`（預設 2,000,000 字元）                                     |
