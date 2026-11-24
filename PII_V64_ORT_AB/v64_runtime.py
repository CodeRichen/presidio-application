"""An opt-in, process-local ONNX Runtime thread-count experiment.

This is not an application patch. Import *before* handler_media/ocr_backend.
Never used for production unless a separate validation/rollout is approved.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PatchStats:
    attempted: int = 0
    sessions_tuned: int = 0
    existing_options: int = 0


def install_ort_thread_trial(threads: int | None, ort_module=None):
    """Intercept Python ORT Session creation in this subprocess.

    - None = baseline, untouched
    - positive integer = explicit intra_op_num_threads setting
    Return (stats, undo). Does not rely on undocumented ORT environment vars.
    """
    if threads is not None and (not isinstance(threads, int) or isinstance(threads, bool) or not 1 <= threads <= 64):
        raise ValueError('threads must be an integer from 1 to 64')
    if ort_module is None:
        import onnxruntime as ort_module
    original = ort_module.InferenceSession
    stats = PatchStats()
    if threads is None:
        return stats, lambda: None

    def session_with_trial_threads(path_or_bytes, sess_options=None, *args, **kwargs):
        stats.attempted += 1
        # Preserve the caller's already configured options other than intra-op threads.
        if sess_options is not None:
            stats.existing_options += 1
        else:
            sess_options = ort_module.SessionOptions()
        # Adjust before the inference session is constructed.
        sess_options.intra_op_num_threads = threads
        result = original(path_or_bytes, sess_options=sess_options, *args, **kwargs)
        stats.sessions_tuned += 1
        return result

    ort_module.InferenceSession = session_with_trial_threads
    def undo():
        if ort_module.InferenceSession is session_with_trial_threads:
            ort_module.InferenceSession = original
    return stats, undo
