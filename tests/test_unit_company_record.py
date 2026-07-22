import pytest

from fabri.benchmarks.company_record import _sign_test_p

pytestmark = pytest.mark.unit


def test_sign_test_is_one_when_evenly_split() -> None:
    # 2 cheaper / 2 pricier -> no evidence of a direction.
    assert _sign_test_p([-1.0, -1.0, 1.0, 1.0]) == 1.0


def test_sign_test_drops_ties() -> None:
    # Zero deltas carry no sign information and must not inflate n.
    assert _sign_test_p([0.0, 0.0, -1.0, -1.0]) == _sign_test_p([-1.0, -1.0])


def test_sign_test_all_one_direction_is_small_but_not_significant_at_n4() -> None:
    # 0/4 in one direction is p=0.125 two-sided -- suggestive, NOT significant.
    assert _sign_test_p([1.0, 1.0, 1.0, 1.0]) == pytest.approx(0.125)


def test_sign_test_needs_enough_pairs_to_ever_be_significant() -> None:
    # With 5 pairs all one way the two-sided p is 0.0625 -- still above 0.05.
    # This guards against ever reporting "significant" off a tiny run.
    assert _sign_test_p([1.0] * 5) == pytest.approx(0.0625)
    assert _sign_test_p([1.0] * 6) < 0.05


def test_sign_test_empty_is_one() -> None:
    assert _sign_test_p([]) == 1.0
