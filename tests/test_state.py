import pytest

from driftless_photometry.state import AcquisitionState, AcquisitionStateMachine


def test_happy_path_state_transitions() -> None:
    state = AcquisitionStateMachine()
    for target in (
        AcquisitionState.READY,
        AcquisitionState.CALIBRATING,
        AcquisitionState.ARMED,
        AcquisitionState.RECORDING,
        AcquisitionState.DRAINING,
        AcquisitionState.READY,
    ):
        state.transition(target)
    assert state.state is AcquisitionState.READY


def test_invalid_transition_is_rejected() -> None:
    state = AcquisitionStateMachine()
    with pytest.raises(RuntimeError, match="invalid acquisition transition"):
        state.transition(AcquisitionState.RECORDING)
