"""RWD read-only streaming contracts."""

from .client import RWDClientDiagnostics, RWDRecordSource, RWDStreamClient
from .domain import (
    RWDEventRecord,
    RWDFluorescenceRecord,
    RWDFluorescenceSample,
    RWDReceivedRecord,
    RWDRecord,
    RWDSessionData,
    RWDStreamMetadata,
    RWDWavelength,
)
from .protocol import (
    EVENT_RECORD_BYTES,
    FLUORESCENCE_RECORD_BYTES,
    RWD_PROTOCOL_VERSION,
    RWDDecoderDiagnostics,
    RWDProtocolError,
    RWDStreamDecoder,
    parse_event_record,
    parse_fluorescence_record,
)
from .timing import (
    RWDClockDiscontinuity,
    RWDNormalizedEvent,
    RWDNormalizedRecord,
    RWDNormalizedSample,
    RWDNormalizedSession,
    normalize_rwd_session,
)

__all__ = [
    "EVENT_RECORD_BYTES",
    "FLUORESCENCE_RECORD_BYTES",
    "RWD_PROTOCOL_VERSION",
    "RWDClientDiagnostics",
    "RWDClockDiscontinuity",
    "RWDDecoderDiagnostics",
    "RWDEventRecord",
    "RWDFluorescenceRecord",
    "RWDFluorescenceSample",
    "RWDNormalizedEvent",
    "RWDNormalizedRecord",
    "RWDNormalizedSample",
    "RWDNormalizedSession",
    "RWDProtocolError",
    "RWDReceivedRecord",
    "RWDRecord",
    "RWDRecordSource",
    "RWDSessionData",
    "RWDStreamClient",
    "RWDStreamDecoder",
    "RWDStreamMetadata",
    "RWDWavelength",
    "normalize_rwd_session",
    "parse_event_record",
    "parse_fluorescence_record",
]
