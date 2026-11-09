import pytest

from evals.scenarios import SCENARIOS


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_scenario(scenario):
    actual, notes = scenario.run()
    assert actual, f"{scenario.name} produced no result"
