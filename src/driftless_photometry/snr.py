"""Spectral signal-to-noise ratio of a photometry trace, as in the lab's analysis code.

Same method as ``compute_snr`` in ``dataAnalysis/fiberphotometry/
preprocess_rwd_photometry.py``: white noise has a flat spectrum, and the sensor signal
lies below 10 Hz. With a Welch PSD (Hann window, segments of an eighth of the trace,
half overlap, as ``scipy.signal.welch`` with ``nperseg=len // 8``):

- noise power = mean PSD in 10-14 Hz x the 14 Hz signal bandwidth;
- signal power = total power in 0-14 Hz - noise power;
- SNR = signal power / noise power (linear).

The analysis code removes the photobleaching fit first; live, DBF removes a quadratic
trend over the window instead, which takes out the same slow drift. The 10-14 Hz noise
band needs each wavelength sampled at 28 Hz or faster.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

NOISE_BAND_HZ = (10.0, 14.0)
SIGNAL_BAND_HZ = (0.0, 14.0)
MIN_SAMPLING_HZ = 2 * NOISE_BAND_HZ[1]
WINDOW_S = 120.0
"""Live SNR uses the last two minutes of each trace..."""
UPDATE_S = 60.0
"""...and is recomputed once a minute, so it stays steady."""
MIN_WINDOW_S = 60.0


def _trapezoid(y: NDArray[np.float64], x: NDArray[np.float64]) -> float:
    if len(x) < 2:
        return 0.0
    return float(np.sum((y[1:] + y[:-1]) * np.diff(x)) / 2.0)


def welch_psd(
    signal: NDArray[np.float64], fs: float, nperseg: int
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """One-sided PSD density, matching ``scipy.signal.welch`` defaults."""

    nperseg = max(1, min(nperseg, len(signal)))
    step = nperseg - nperseg // 2
    # scipy's default Hann window is periodic.
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(nperseg) / nperseg)
    scale = 1.0 / (fs * float(np.sum(window**2)))
    starts = range(0, len(signal) - nperseg + 1, step)
    spectra = []
    for start in starts:
        segment = signal[start : start + nperseg]
        segment = segment - segment.mean()
        spectra.append(np.abs(np.fft.rfft(segment * window)) ** 2 * scale)
    psd = np.mean(spectra, axis=0)
    if nperseg % 2:
        psd[1:] *= 2
    else:
        psd[1:-1] *= 2
    return np.fft.rfftfreq(nperseg, 1.0 / fs), psd


def spectral_snr(signal: Sequence[float] | NDArray[np.float64], fs: float) -> float:
    """SNR of an already detrended trace sampled at ``fs`` Hz (NaN if not computable)."""

    values = np.asarray(signal, dtype=np.float64)
    if len(values) < 10 or fs < MIN_SAMPLING_HZ:
        return float("nan")
    freqs, psd = welch_psd(values, fs, max(len(values) // 8, 1))
    low, high = NOISE_BAND_HZ
    noise = (freqs >= low) & (freqs <= high)
    noise_power = _trapezoid(psd[noise], freqs[noise]) / (high - low)
    noise_power *= SIGNAL_BAND_HZ[1] - SIGNAL_BAND_HZ[0]
    band = (freqs >= SIGNAL_BAND_HZ[0]) & (freqs <= SIGNAL_BAND_HZ[1])
    total_power = _trapezoid(psd[band], freqs[band])
    return (total_power - noise_power) / noise_power if noise_power > 0 else float("nan")


def live_snr(times: Sequence[float], values: Sequence[float]) -> tuple[float, float]:
    """(SNR, sampling rate) over the last ``WINDOW_S`` of a trace, detrended.

    The rate comes from the samples' median spacing; SNR is NaN when the window is
    shorter than ``MIN_WINDOW_S`` or the rate is below ``MIN_SAMPLING_HZ``.
    """

    t = np.asarray(times, dtype=np.float64)
    y = np.asarray(values, dtype=np.float64)
    if len(t) < 10:
        return float("nan"), float("nan")
    keep = t > t[-1] - WINDOW_S
    t, y = t[keep], y[keep]
    fs = 1.0 / float(np.median(np.diff(t))) if len(t) > 1 else float("nan")
    if t[-1] - t[0] < MIN_WINDOW_S - 1.0 or not fs >= MIN_SAMPLING_HZ:
        return float("nan"), fs
    centered = t - t.mean()
    detrended = y - np.polyval(np.polyfit(centered, y, 2), centered)
    return spectral_snr(detrended, fs), fs
