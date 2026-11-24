import core

samples = [
    "Email: demo@example.com",
    "Phone: 0912345678",
    "姓名：王小明",
    "王小明的 Email 是 demo@example.com",
    "Email marketing is important."
]

for s in samples:
    print("\n" + "=" * 55)
    print("原始文字:", s)
    print("Unicode 檢查:", repr(s))

    _, matches = core.detect_entities(s, is_code=False)

    print("偵測結果:")
    if not matches:
        print("  無")

    for start, end, label, score, source in matches:
        print(f"  {s[start:end]!r} -> {label} ({source})")
