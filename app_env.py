"""
app_env.py - 執行環境設定 (用 python 直接跑 / 打包成 EXE 都共用)
====================================================
【必須在 import core / handler_* 之前 import】ui.py、main.py 的第一個專案 import 就是它。

做的事：
1. 決定「程式所在資料夾」APP_DIR：
     - 打包成 EXE (PyInstaller) -> EXE 檔案所在的資料夾 (不是執行時解壓縮的暫存資料夾)
     - 直接用 python 跑      -> 這個 .py 檔所在的資料夾
2. 打包成 EXE 後，把工作目錄切到 APP_DIR，讓程式裡所有 "./presidio/..." 這種相對路徑
   (對照表、敏感清單、hotkeys.json ...) 都落在 EXE 旁邊，不管使用者是雙擊、捷徑還是開機自動啟動。
3. 沒有主控台 (視窗模式) 時 sys.stdout / sys.stderr 會是 None，有些套件直接寫入會當掉，這裡補上假的。
4. 模型的尋找順序：① 先查「環境」(pip 裝好的 spaCy 模型 / 本機 Hugging Face 快取)，
   ② 環境沒有，再看 APP_DIR/models/ 資料夾有沒有模型檔；兩邊都沒有，對應功能自動停用。
   一律不連網下載 (HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE)；
   Presidio 的 Email 辨識器用到的 tldextract 也改成離線快照，不連網抓網域清單。
5. 把用不到的 torchvision 標成不可用，避免它與 torch 版本不合時，連帶害 transformers / Presidio 無法載入。
   找不到模型資料夾時，對應功能自動停用，其餘功能照常。

models 資料夾 (給「環境裡沒有模型」的電腦用，可用 prepare_models.py 一次產生)：
    models/roberta-base-finetuned-cluener2020-chinese/   中文 NER (人名/地址/機構)，沒有就停用
    models/en_core_web_sm/                               英文 NER (Presidio)，沒有就停用
圖片 / PDF 的 OCR 用的是 Windows 內建 OCR，不需要任何模型檔。
"""
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
MODELS_DIR = os.path.join(APP_DIR, "models")


def find_model_dir(name):
    """回傳 models/<name> 的完整路徑；資料夾不存在或是空的就回傳 None (呼叫端據此停用該功能)"""
    path = os.path.join(MODELS_DIR, name)
    if os.path.isdir(path) and os.listdir(path):
        return path
    return None


def has_spacy_package(name):
    """環境裡 (pip 裝好的) 有沒有這個 spaCy 模型套件，例如 en_core_web_sm"""
    try:
        import spacy
        return bool(spacy.util.is_package(name))
    except Exception:
        return False


def has_hf_cached(repo_id):
    """環境裡 (本機 Hugging Face 快取) 有沒有這個模型，例如 uer/roberta-base-finetuned-cluener2020-chinese。
    只查本機快取，不連網。"""
    try:
        from huggingface_hub import try_to_load_from_cache
        return isinstance(try_to_load_from_cache(repo_id, "config.json"), str)
    except Exception:
        return False


def _block_torchvision():
    """這個程式只用 transformers 的文字 (NER) 功能，用不到 torchvision (影像模型)。
    環境裡 torch 與 torchvision 版本不合時 (典型錯誤：operator torchvision::nms does not exist)，
    transformers 一 import 就會當掉，連帶 Presidio 與中文 NER 都被停用。
    所以直接把 torchvision 標成「不可用」，transformers 就會把它當成沒安裝、略過影像相關功能。"""
    sys.modules.setdefault("torchvision", None)


def _make_tldextract_offline():
    """Presidio 的 Email 辨識器會用 tldextract，它預設第一次使用時會連網抓 publicsuffix.org 的網域清單；
    沒有網路 / 被防火牆擋住時會卡住或噴一大串錯誤。改成只用套件內建的離線快照，不連網。"""
    try:
        import tldextract
        tldextract.tldextract.TLD_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)
    except Exception:
        pass   # 沒裝 tldextract (沒用到 Presidio) 或版本不同：不影響其他功能


def _setup():
    if FROZEN:
        os.chdir(APP_DIR)
    for stream in ("stdout", "stderr"):
        if getattr(sys, stream) is None:
            setattr(sys, stream, open(os.devnull, "w", encoding="utf-8"))
    # 只用本機模型，不連網 (沒有網路或在公司內網時，也不會卡在嘗試下載)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    _make_tldextract_offline()
    _block_torchvision()


_setup()
