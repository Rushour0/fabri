"""Tests that pin cart_total's intended behaviour. Self-locating so it runs
from any cwd (`python3 test_store.py`) — the tester agent and fabri's repair
verify_command both invoke it directly."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import cart_total


def test_applies_discount():
    # $10 x2 + $5 x1 = $25 subtotal; a 10% discount should leave $22.50.
    got = cart_total([(10.0, 2), (5.0, 1)], 0.1)
    assert got == 22.5, f"expected 22.5 after 10% discount, got {got}"


def test_no_discount_is_subtotal():
    assert cart_total([(3.0, 4)], 0.0) == 12.0


if __name__ == "__main__":
    test_applies_discount()
    test_no_discount_is_subtotal()
    print("PASS: cart_total handles discounts correctly")
