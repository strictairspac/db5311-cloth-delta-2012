"""减容保留方案的纯逻辑测试：能留下最多批次，同数时先约先留。"""
from src.services.trips import plan_retention


def batches(*specs):
    return [{'weight_g': weight, 'pieces': pieces} for weight, pieces in specs]


def test_all_fit_keeps_everything():
    kept, squeezed = plan_retention(batches((100, 1), (200, 2), (300, 3)), 600, 6)
    assert kept == [0, 1, 2]
    assert squeezed == []


def test_nothing_fits_squeezes_everything():
    kept, squeezed = plan_retention(batches((100, 1), (200, 2)), 50, 0)
    assert kept == []
    assert squeezed == [0, 1]


def test_prefers_more_batches_over_heavier_ones():
    # 先约的 50 克批次占额后只能再留一批；放弃它能留下更小的三批。
    kept, squeezed = plan_retention(batches((50, 1), (40, 1), (30, 1), (20, 1)), 90, 10)
    assert kept == [1, 2, 3]
    assert squeezed == [0]


def test_equal_count_keeps_earliest_bookings():
    kept, squeezed = plan_retention(batches((10, 1), (10, 1), (10, 1)), 20, 2)
    assert kept == [0, 1]
    assert squeezed == [2]


def test_pieces_limit_is_enforced_alongside_weight():
    # 件数上限 2：只有两件小件批能一起留下，5 件的大批被挤出。
    kept, squeezed = plan_retention(batches((10, 5), (10, 1), (10, 1)), 100, 2)
    assert kept == [1, 2]
    assert squeezed == [0]


def test_weight_and_pieces_can_bind_different_batches():
    # 重量够但件数不够时，件数同样卡住所留批次。
    kept, squeezed = plan_retention(batches((10, 5), (10, 1), (10, 1)), 100, 6)
    assert kept == [0, 1]
    assert squeezed == [2]


def test_tiebreak_prefers_earlier_when_swap_is_free():
    # 三批等重等件，容量只够两批：留下先预约的两批。
    kept, squeezed = plan_retention(batches((7, 2), (7, 2), (7, 2)), 14, 4)
    assert kept == [0, 1]
    assert squeezed == [2]


def test_empty_input():
    assert plan_retention([], 100, 10) == ([], [])


def test_deterministic_for_same_input():
    data = batches((50, 3), (40, 2), (30, 2), (20, 1), (10, 1))
    first = plan_retention(data, 90, 4)
    assert first == plan_retention(list(data), 90, 4)
    assert len(first[0]) == 3  # 40+30+20 或 40+30+10 等，最多 3 批
