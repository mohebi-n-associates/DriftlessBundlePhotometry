import math

import numpy as np
import pytest

from driftless_photometry.snr import live_snr, spectral_snr, welch_psd


def test_welch_psd_integrates_to_the_signal_variance() -> None:
    rng = np.random.default_rng(1)
    noise = rng.normal(0.0, 2.0, 40 * 120)
    freqs, psd = welch_psd(noise, 40.0, len(noise) // 8)
    assert freqs[-1] == pytest.approx(20.0)
    assert float(np.sum(psd) * (freqs[1] - freqs[0])) == pytest.approx(4.0, rel=0.05)


def test_white_noise_has_no_signal_and_a_slow_signal_scores_high() -> None:
    rng = np.random.default_rng(2)
    fs = 40.0
    t = np.arange(0, 120, 1 / fs)
    noise = rng.normal(0.0, 1.0, t.size)
    assert abs(spectral_snr(noise, fs)) < 0.3
    # A 1 Hz sine of amplitude 10 carries 50 power units over noise of 14/20.
    snr = spectral_snr(10 * np.sin(2 * math.pi * 1.0 * t) + noise, fs)
    assert 50 < snr < 90


def test_live_snr_needs_a_minute_of_data_and_28_hz_per_wavelength() -> None:
    rng = np.random.default_rng(3)
    t = np.arange(0, 50, 1 / 40)
    snr, fs = live_snr(t, 1000 + rng.normal(0, 1, t.size))
    assert math.isnan(snr) and fs == pytest.approx(40.0)
    slow = np.arange(0, 200, 1 / 6.67)
    snr, fs = live_snr(slow, 1000 + rng.normal(0, 1, slow.size))
    assert math.isnan(snr) and fs == pytest.approx(6.67, rel=1e-3)


def test_live_snr_uses_the_last_two_minutes_and_ignores_bleaching_drift() -> None:
    rng = np.random.default_rng(4)
    t = np.arange(0, 300, 1 / 40)
    bleach = 1500 * np.exp(-t / 400)  # slow decay only: removed by the detrend
    signal = 10 * np.sin(2 * math.pi * 1.0 * t)
    snr, _fs = live_snr(t, bleach + signal + rng.normal(0, 1, t.size))
    assert 50 < snr < 90
