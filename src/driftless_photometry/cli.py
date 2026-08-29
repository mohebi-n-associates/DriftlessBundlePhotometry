"""Command-line entry points for headless acquisition and GUI demo."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from driftless_photometry import __version__
from driftless_photometry.acquisition import AcquisitionEngine
from driftless_photometry.config import demo_config
from driftless_photometry.hardware import SimulatedRig
from driftless_photometry.storage import recover_session_spool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Driftless Bundle Photometry")
    parser.add_argument("--version", action="version", version=f"DBF {__version__}")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--headless", action="store_true", help="run a simulated recording")
    mode.add_argument("--demo", action="store_true", help="open the desktop GUI demo")
    mode.add_argument(
        "--recover-spool",
        type=Path,
        metavar="PATH",
        help="finalize committed records from an interrupted session spool",
    )
    parser.add_argument("--duration", type=float, default=5.0, help="recording duration in seconds")
    parser.add_argument("--output", type=Path, default=Path("demo-output"))
    parser.add_argument("--fibers", type=int, default=3, choices=range(1, 10))
    parser.add_argument("--raw", action="store_true", help="embed original uint16 frames")
    parser.add_argument(
        "--realtime", action="store_true", help="pace headless simulation in real time"
    )
    parser.add_argument(
        "--keep-spool",
        action="store_true",
        help="retain a successfully recovered spool for forensic inspection",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.recover_spool is not None:
        recovered = recover_session_spool(args.recover_spool, keep_spool=args.keep_spool)
        print(
            json.dumps(
                {
                    "nwb_paths": [str(report.path) for report in recovered.nwbs],
                    "roi_files": len(recovered.nwbs),
                    "frames": recovered.nwb.frame_count,
                    "trace_samples": recovered.nwb.trace_sample_count,
                    "ttl_edges": recovered.nwb.ttl_edge_count,
                    "acquisition_was_complete": recovered.acquisition_was_complete,
                    "spool_removed": recovered.spool_removed,
                },
                indent=2,
            )
        )
        return 0
    if args.demo:
        from driftless_photometry.gui.app import run_gui

        return run_gui(
            output_directory=args.output,
            default_duration_s=args.duration,
            default_fibers=args.fibers,
            default_raw=args.raw,
        )

    config = demo_config(
        args.output,
        fiber_count=args.fibers,
        raw_capture=args.raw,
        recording_duration_s=args.duration,
    )
    engine = AcquisitionEngine()
    result = engine.run(
        config,
        SimulatedRig(config, realtime=args.realtime),
        duration_s=args.duration,
    )
    print(
        json.dumps(
            {
                "nwb_paths": [str(report.path) for report in result.reports],
                "roi_files": len(result.reports),
                "frames": result.report.frame_count,
                "trace_samples": result.report.trace_sample_count,
                "ttl_edges": result.report.ttl_edge_count,
                "stopped_by_request": result.stopped_by_request,
            },
            indent=2,
        )
    )
    return 0
