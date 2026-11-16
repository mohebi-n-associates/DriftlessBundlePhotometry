from driftless_photometry.power import KeepAwake


def test_keep_awake_requests_once_and_releases_once() -> None:
    calls: list[int] = []
    awake = KeepAwake(set_state=lambda flags: calls.append(flags) or 1)

    awake.acquire()
    awake.acquire()  # a second request changes nothing
    assert awake.held and calls == [0x80000001]
    awake.release()
    awake.release()
    assert not awake.held and calls == [0x80000001, 0x80000000]

    with KeepAwake(set_state=lambda flags: calls.append(flags) or 1) as held:
        assert held.held
    assert calls[-2:] == [0x80000001, 0x80000000]


def test_keep_awake_is_harmless_where_unsupported() -> None:
    awake = KeepAwake(set_state=None)
    awake._set_state = None  # as on macOS
    with awake:
        assert awake.held
    assert not awake.held
