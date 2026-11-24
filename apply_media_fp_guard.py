"""Safely apply the opt-in synthetic-card false-positive guard to handler_media.py."""
from pathlib import Path
from datetime import datetime
import argparse

PATTERN = '''    boxes = _dedupe_sensitive_boxes(boxes)
    return boxes
'''
REPLACEMENT = '''    boxes = _dedupe_sensitive_boxes(boxes)
    # BEGIN SYNTHETIC_FACE_CARD_FP_GUARD: OFF unless explicitly enabled.
    from media_fp_guard import filter_synthetic_test_false_positives
    boxes, suppressed = filter_synthetic_test_false_positives(boxes, words, scale=scale)
    if os.environ.get("PII_MEDIA_FP_REPORT", "0") == "1":
        # Privacy-safe debug output: does not print detected PII text.
        for item in suppressed:
            print(f"[FP-SUPPRESSED] {item[4]} source={item[7]} bbox={item[:4]}")
    # END SYNTHETIC_FACE_CARD_FP_GUARD
    return boxes
'''


def apply(source):
    path = Path(source)
    old = path.read_text(encoding='utf-8')
    if 'BEGIN SYNTHETIC_FACE_CARD_FP_GUARD' in old:
        return ('already-applied', None)
    if old.count(PATTERN) != 1:
        raise RuntimeError('找不到唯一的 find_sensitive_boxes_in_words() 最後去重區段；未修改檔案。')
    a = old.index(PATTERN)
    find_start = old.rfind('def find_sensitive_boxes_in_words(', 0, a)
    find_end = old.find('\ndef ', find_start)
    if find_start == -1 or (find_end != -1 and a > find_end):
        raise RuntimeError('匹配到的不是 find_sensitive_boxes_in_words；未修改檔案。')
    new = old.replace(PATTERN, REPLACEMENT)
    compile(new, str(path), 'exec')
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup = path.with_name(f'{path.name}.before_fp_{stamp}.bak')
    if backup.exists():
        raise RuntimeError(f'備份檔已存在: {backup}')
    backup.write_text(old, encoding='utf-8')
    try:
        path.write_text(new, encoding='utf-8')
    except Exception:
        path.write_text(old, encoding='utf-8')
        raise
    return ('modified', str(backup))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--handler', default='handler_media.py')
    args = parser.parse_args()
    target = Path(args.handler).resolve()
    helper = target.parent / 'media_fp_guard.py'
    if not helper.exists():
        raise SystemExit(f'找不到 {helper}，請先把 media_fp_guard.py 放到 handler_media.py 同一層。')
    result, backup = apply(target)
    print('結果:', result)
    if backup:
        print('已建立備份:', backup)
