import pytest

from scripts.acquire_ashare_research import read_result


def test_silent_full_page_network_failure_is_not_successful_end_of_data():
    class TruncatedResult:
        error_code = "0"
        fields = ["date"]
        data = [None] * 2000
        cur_row_num = 2000

        def next(self):
            return False

    with pytest.raises(RuntimeError, match="terminal response unverified"):
        read_result(TruncatedResult())
