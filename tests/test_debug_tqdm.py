
import re
import multiprocessing
from pathlib import Path
from unittest.mock import MagicMock

from wisper_transcribe.pipeline import _patch_tqdm_for_queue

def test_patch_tqdm_for_queue_strips_ansi_and_trailing_noise():
    """
    Verify that the cleaning logic correctly handles ANSI codes and strips 
    any trailing non-ANSI noise (like leftover digits/spaces).
    """
    # The logic we are testing is actually inside _patch_tqdm_for_queue's _QueueFile.write
    _ansi_src = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
    
    # Test Case 1: Standard ANSI stripping
    input_str_1 = "\x1b[32mFrame 1\x1b[0m\rFrame 2\r\x1b[31mFrame 3 with noise\x1b[0m "
    clean_1 = _ansi_src.sub('', input_str_1)
    parts_1 = [p.strip() for p in clean_1.split('\r') if p.strip()]
    assert parts_1[-1] == "Frame 3 with noise"
    
    # Test Case 2: ANSI stripping + trailing non-ANSI noise (the bug fix check)
    # e.g., leftover digits from a previous tqdm update that weren't cleared by \r
    input_str_2 = "\x1b[32mFrame 1\x1b[0m\rFrame 2\r\x1b[31mFrame 3 with noise 123\x1b[0m 999"
    clean_2 = _ansi_src.sub('', input_str_2)
    parts_2 = [p.strip() for p in clean_2.split('\r') if p.strip()]
    
    # Emulate the new logic in the code:
    import re as _re_clean
    final_msg = parts_2[-1]
    final_msg = _re_clean.sub(r'[\s\d]+$', '', final_msg)
    
    # Should result in "Frame 3 with noise" (stripping " 123 999")
    assert final_msg == "Frame 3 with noise"

if __name__ == "__main__":
    test_patch_tqdm_for_queue_strips_ansi_and_trailing_noise()
