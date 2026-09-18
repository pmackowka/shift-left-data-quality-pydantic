"""Test dymny etapu 0: workspace sklada sie i oba pakiety sa importowalne."""

import dq_contracts
import dq_datagen


def test_packages_are_importable() -> None:
    assert dq_contracts.__version__ == "0.1.0"
    assert dq_datagen.__version__ == "0.1.0"
