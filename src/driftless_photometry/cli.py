"""Command-line entry points for headless acquisition and GUI demo."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from driftless_photometry import __version__
from driftless_photometry.acquisition import AcquisitionEngine
from driftless_photometry.config import RWDSourceConfig, demo_config
from driftless_photometry.hardware import SimulatedRig
from driftless_photometry.rwd import (
    RWDStreamClient,
    RWDWireCaptureInspection,
    RWDWireCaptureWriter,
    RWDWireReplaySource,
    inspect_rwd_wire_capture,
)
from driftless_photometry.rwd.acquisition import RWDAcquisitionEngine
from driftless_photometry.settings import import_settings
from driftless_photometry.storage import (
    discover_rwd_session_spools,
    discover_session_spools,
    recover_rwd_session_spool,
    recover_session_spool,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Driftless Bundle Photometry")
    parser.add_argument("--version", action="version", version=f"DBF {__version__}")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--headless", action="store_true", help="run a simulated recording")
    mode.add_argument(
        "--rwd-settings",
        type=Path,
        metavar="PATH",
        help="run a headless read-only RWD recording from a settings-v2 JSON file",
    )
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
        "--inspect-rwd-capture",
        type=Path,
        metavar="PATH",
        help="checksum and summarize a completed exact-wire RWD bench capture",
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
    wire_mode = parser.add_mutually_exclusive_group()
    wire_mode.add_argument(
        "--rwd-wire-capture",
        type=Path,
        metavar="PATH",
        help="retain checksummed exact TCP chunks while running --rwd-settings",
    )
    wire_mode.add_argument(
        "--rwd-replay",
        type=Path,
        metavar="PATH",
        help="replay a completed exact-wire capture instead of opening the RWD socket",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.rwd_wire_capture is not None or args.rwd_replay is not None) and (
        args.rwd_settings is None
    ):
        parser.error("--rwd-wire-capture and --rwd-replay require --rwd-settings")
    if args.inspect_rwd_capture is not None:
        inspection = inspect_rwd_wire_capture(args.inspect_rwd_capture)
        print(json.dumps(_rwd_capture_json(inspection), indent=2))
        return 0
    if args.inspect_spools is not None:
        native_inspections = discover_session_spools(args.inspect_spools)
        rwd_inspections = discover_rwd_session_spools(args.inspect_spools)
        print(
            json.dumps(
                [
                    {
                        "source_kind": "native",
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
                    for item in native_inspections
                ]
                + [
                    {
                        "source_kind": "rwd",
                        "path": str(item.path),
                        "session_id": item.session_id,
                        "schema_version": item.schema_version,
                        "acquisition_complete": item.acquisition_complete,
                        "stream_records": item.stream_record_count,
                        "fluorescence_records": item.fluorescence_record_count,
                        "trace_samples": item.trace_sample_count,
                        "named_events": item.named_event_count,
                        "invalid_times": item.invalid_time_count,
                        "system_events": item.system_event_count,
                        "fiber_files": item.fiber_file_count,
                    }
                    for item in rwd_inspections
                ],
                indent=2,
            )
        )
        return 0
    if args.recover_spool is not None:
        if args.recover_spool.name.endswith(".rwd-spool"):
            recovered_rwd = recover_rwd_session_spool(
                args.recover_spool,
                keep_spool=args.keep_spool,
            )
            print(
                json.dumps(
                    {
                        "source_kind": "rwd",
                        "nwb_paths": [str(report.path) for report in recovered_rwd.nwbs],
                        "fiber_files": len(recovered_rwd.nwbs),
                        "stream_records": recovered_rwd.inspection.stream_record_count,
                        "trace_samples": recovered_rwd.inspection.trace_sample_count,
                        "named_events": recovered_rwd.inspection.named_event_count,
                        "acquisition_was_complete": recovered_rwd.acquisition_was_complete,
                        "spool_removed": recovered_rwd.spool_removed,
                        "reused_existing_outputs": recovered_rwd.reused_existing_outputs,
                    },
                    indent=2,
                )
            )
            return 0
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
    if args.rwd_settings is not None:
        config = import_settings(args.rwd_settings)
        if not isinstance(config.source, RWDSourceConfig):
            raise ValueError("--rwd-settings requires settings whose source.kind is 'rwd'")
        source = None
        capture_writer = None
        capture_inspection = None
        run_duration_s = args.duration
        if args.rwd_replay is not None:
            source = RWDWireReplaySource(config.source, args.rwd_replay)
            run_duration_s = max(run_duration_s, source.inspection.duration_s)
        elif args.rwd_wire_capture is not None:
            capture_writer = RWDWireCaptureWriter(args.rwd_wire_capture, config.source)
            source = RWDStreamClient(config.source, on_wire_chunk=capture_writer.append)
        try:
            result = RWDAcquisitionEngine().run(
                config,
                duration_s=run_duration_s,
                source=source,
            )
        except BaseException as error:
            if capture_writer is not None:
                if capture_writer.chunk_count:
                    capture_inspection = capture_writer.close(
                        terminal_outcome=f"acquisition_error:{type(error).__name__}",
                        stream_metadata=None if source is None else source.stream_metadata,
                    )
                else:
                    capture_writer.abort()
            raise
        if capture_writer is not None:
            capture_inspection = capture_writer.close(
                terminal_outcome=(
                    "stopped_by_request" if result.stopped_by_request else "completed_duration"
                ),
                stream_metadata=None if source is None else source.stream_metadata,
            )
        replay_inspection = (
            None if not isinstance(source, RWDWireReplaySource) else source.inspection
        )
        print(
            json.dumps(
                {
                    "source_kind": "rwd",
                    "nwb_paths": [str(report.path) for report in result.reports],
                    "fiber_files": len(result.reports),
                    "stream_records": result.reports[0].stream_record_count,
                    "trace_samples": result.reports[0].trace_sample_count,
                    "named_events": result.reports[0].named_event_count,
                    "stopped_by_request": result.stopped_by_request,
                    "wire_capture": (
                        None
                        if capture_inspection is None
                        else _rwd_capture_json(capture_inspection)
                    ),
                    "wire_replay": (
                        None if replay_inspection is None else _rwd_capture_json(replay_inspection)
                    ),
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


def _rwd_capture_json(inspection: RWDWireCaptureInspection) -> dict[str, object]:
    return {
        "path": str(inspection.path),
        "schema_version": inspection.schema_version,
        "protocol_version": inspection.protocol_version,
        "chunks": inspection.chunk_count,
        "bytes": inspection.byte_count,
        "duration_s": inspection.duration_s,
        "sha256": inspection.sha256,
        "terminal_outcome": inspection.terminal_outcome,
        "application_version": inspection.runtime_provenance["application_version"],
        "resolved_preamble_mode": (
            None
            if inspection.stream_metadata is None
            else inspection.stream_metadata.resolved_preamble_mode.value
        ),
        "machine_name": (
            None
            if inspection.stream_metadata is None
            else inspection.stream_metadata.machine_name.decode("ascii", errors="backslashreplace")
        ),
        "machine_name_hex": (
            None
            if inspection.stream_metadata is None
            else inspection.stream_metadata.machine_name.hex()
        ),
    }
