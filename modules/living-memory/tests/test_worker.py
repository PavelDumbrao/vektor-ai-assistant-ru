from __future__ import annotations

import sys
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
import worker


def test_contract_retry_only_for_model_contract_errors():
    assert worker._should_contract_retry([], [{"reason": "invalid_enum_or_confidence"}])
    assert worker._should_contract_retry([], [{"reason": "no_valid_user_evidence"}])
    assert not worker._should_contract_retry([], [{"reason": "sensitive_auto_memory_blocked"}])
    assert not worker._should_contract_retry([], [{"reason": "unsafe_or_empty_text"}])
    assert not worker._should_contract_retry([{"action": "add"}], [{"reason": "invalid_enum_or_confidence"}])
