from fabri.benchmarks.stats import wilson_interval


def test_wilson_seven_of_ten():
    low, high = wilson_interval(7, 10)
    assert abs(low - 0.397) < 0.01
    assert abs(high - 0.892) < 0.01


def test_wilson_nine_of_ten():
    low, high = wilson_interval(9, 10)
    assert abs(low - 0.596) < 0.01
    assert abs(high - 0.982) < 0.01


def test_wilson_zero_of_ten():
    low, high = wilson_interval(0, 10)
    assert low == 0.0
    assert high < 0.31


def test_wilson_ten_of_ten():
    _low, high = wilson_interval(10, 10)
    assert high == 1.0


def test_wilson_zero_trials():
    assert wilson_interval(0, 0) == (0.0, 0.0)


def test_wilson_monotonic_in_k():
    n = 10
    points = []
    for k in range(0, n + 1):
        low, high = wilson_interval(k, n)
        points.append((low + high) / 2)
    assert all(a < b for a, b in zip(points, points[1:]))
