from scripts.run_copa_research import PERIODS


def test_copa_research_periods_are_ordered_and_non_overlapping():
    train = PERIODS["train"]
    validation = PERIODS["validation"]
    sealed = PERIODS["sealed_oos"]
    assert train[0] <= train[1] < validation[0] <= validation[1] < sealed[0] <= sealed[1]
