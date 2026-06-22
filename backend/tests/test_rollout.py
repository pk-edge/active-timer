from app.core.rollout import in_rollout


def test_rollout_bounds_and_determinism():
    dev = "11111111-1111-1111-1111-111111111111"
    assert in_rollout(dev, "1.2.0", 100) is True       # everyone
    assert in_rollout(dev, "1.2.0", 0) is False         # no one
    # Deterministic / sticky for a (device, version) pair.
    assert in_rollout(dev, "1.2.0", 50) == in_rollout(dev, "1.2.0", 50)


def test_rollout_is_monotonic_as_percent_increases():
    # A device included at P% stays included at any higher percent.
    dev = "22222222-2222-2222-2222-222222222222"
    first_in = next(p for p in range(0, 101) if in_rollout(dev, "1.2.0", p))
    assert all(in_rollout(dev, "1.2.0", p) for p in range(first_in, 101))


def test_rollout_distribution_is_reasonable():
    included = sum(in_rollout(f"dev-{i}", "1.2.0", 25) for i in range(2000))
    assert 400 < included < 600   # ~25% of 2000, allow noise
