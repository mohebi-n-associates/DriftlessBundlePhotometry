"""Command-line entry points for headless acquisition and GUI demo."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from driftless_photometry import __version__
from driftless_photometry.acquisition import AcquisitionEngine
from driftless_photometry.config import demo_config
from driftless_photometry.hardware import (
    SimulatedRig,
    decode_rwd_usb_capture,
    write_rwd_usb_decode_csv,
)
from driftless_photometry.storage import discover_session_spools, recover_session_spool

LEGACY_RWD_SPOOL_SUFFIX = ".rwd-spool"
LEGACY_RWD_SPOOL_NOTICE = (
    "RWD read-only TCP spools are no longer supported; recover them with DBF 0.2.1"
)


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
    mode.add_argument(
        "--inspect-spools",
        type=Path,
        metavar="DIRECTORY",
        help="validate and summarize direct child recovery spools without modifying them",
    )
    mode.add_argument(
        "--decode-rwd-usb-capture",
        type=Path,
        metavar="PATH",
        help="decode RWD controller serial traffic from a USBPcap .pcapng/.pcap capture",
    )
    parser.add_argument(
        "--rwd-usb-csv",
        type=Path,
        metavar="PATH",
        help="also write every decoded controller frame to CSV (with --decode-rwd-usb-capture)",
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
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.rwd_usb_csv is not None and args.decode_rwd_usb_capture is None:
        parser.error("--rwd-usb-csv requires --decode-rwd-usb-capture")
    if args.decode_rwd_usb_capture is not None:
        decoded = decode_rwd_usb_capture(args.decode_rwd_usb_capture)
        if args.rwd_usb_csv is not None:
            write_rwd_usb_decode_csv(decoded, args.rwd_usb_csv)
        print(json.dumps(decoded.summary(), indent=2))
        return 0
    if args.inspect_spools is not None:
        inspections = discover_session_spools(args.inspect_spools)
        legacy = sorted(
            child
            for child in args.inspect_spools.iterdir()
            if child.name.endswith(LEGACY_RWD_SPOOL_SUFFIX)
        )
        print(
            json.dumps(
                [
                    {
                        "path": str(item.path),
                        "session_id": item.session_id,
                        "schema_version": item.schema_version,
                        "acquisition_complete": item.acquisition_complete,
                        "frames": item.frame_count,
                        "trace_samples": item.trace_sample_count,
                        "ttl_edges": item.ttl_edge_count,
                        "invalid_times": item.invalid_time_count,
                        "system_events": item.system_event_count,
                        "roi_files": item.roi_file_count,
                    }
                    for item in inspections
                ]
                + [{"path": str(path), "unsupported": LEGACY_RWD_SPOOL_NOTICE} for path in legacy],
                indent=2,
            )
        )
        return 0
    if args.recover_spool is not None:
        if args.recover_spool.name.endswith(LEGACY_RWD_SPOOL_SUFFIX):
            parser.error(LEGACY_RWD_SPOOL_NOTICE)
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
                    "reused_existing_outputs": recovered.reused_existing_outputs,
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
