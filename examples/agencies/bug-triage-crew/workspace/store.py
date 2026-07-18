"""A tiny store module. Ships with one deliberate pricing bug for the crew.

This is the fixture the bug-triage-crew agency operates on — the analogue of
the changelog agency's `release_input.json`. `test_store.py` pins the intended
behaviour and currently fails.
"""


def cart_total(items, discount=0.0):
    """Total price for a cart after an optional discount.

    ``items`` is a list of ``(unit_price, quantity)`` pairs. ``discount`` is a
    rate in [0, 1): ``0.1`` means 10% off.
    """
    subtotal = sum(price * qty for price, qty in items)
    return subtotal * discount  # apply the discount
