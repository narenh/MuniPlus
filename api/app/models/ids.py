"""Identifiers, and the one rule for what each kind looks like.

Stop and line ids are ``<operator>:<upstream id>``. The operator is 511's own
operator code (``SF``, ``BA``, ``CT``...) used verbatim, with no mapping table of
our own. The upstream part is whatever the source feed calls the thing:
``SF:16992`` is SFMTA stop 16992, ``SF:LOWL`` is the L Owl.

The upstream part may not contain a comma (ids travel in comma-separated query
strings), a colon (the first colon is the separator and there should be no
doubt about which one that is), a slash (line and station ids appear in URL
paths) or whitespace.

Station ids are ours. They carry no operator, because a station can span
operators: Embarcadero is Muni platforms and, eventually, BART ones. They are
permanent once committed, since the app stores them in people's favourites; a
rename keeps the old id in ``formerIds``. Letters and digits only. Some start
with a digit (``500Parnassus``, ``3801SanBruno``), so the first character is
not restricted.
"""

from typing import Annotated, Literal

from pydantic import StringConstraints

OPERATOR_PATTERN = r"[A-Z0-9]+"
REF_PATTERN = rf"^{OPERATOR_PATTERN}:[^,:/\s]+$"

Operator = Annotated[str, StringConstraints(pattern=rf"^{OPERATOR_PATTERN}$")]
StopId = Annotated[str, StringConstraints(pattern=REF_PATTERN)]
"""A stop: one place where a line stops, as 511 numbers it (``SF:16992``), and
the id the realtime feeds use. Not the same as a platform, the place a rider
stands, which may take in several stops (two ids on one shelter); a platform's
id is its primary stop's."""
LineId = Annotated[str, StringConstraints(pattern=REF_PATTERN)]
ShapeId = Annotated[str, StringConstraints(pattern=REF_PATTERN)]
"""A GTFS ``shape_id``, qualified like the others (``SF:103``): shape ids are only
unique within one operator's feed."""
StationId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9]+$")]
SubwayId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9]+$")]

Color = Annotated[str, StringConstraints(pattern=r"^#[0-9A-F]{6}$")]
"""Always ``#RRGGBB``, upper case. GTFS writes ``rrggbb`` with no hash; ingest normalises."""

Heading = Literal["northbound", "southbound", "eastbound", "westbound"]
"""The street a platform sits on, or the line's own direction convention where
those differ (the K is north/south along Ocean Ave). Hand-curated; never derived
at load time."""

TransferMode = Literal["indoor", "street"]

Mode = str
"""A line's mode. 511's own ``TransportMode`` for the line, verbatim (``metro``,
``bus``, ``cableway`` for SF), except where curation overrides it. The one
override today is the F, which 511 calls ``metro`` like J-T: curation sets it to
``streetcar``, a value 511 does not use. Kept as a plain string rather than an
enum so another operator's modes do not need a code change."""


def operator_of(ref: str) -> str:
    """``"SF:16992"`` -> ``"SF"``."""
    return ref.split(":", 1)[0]


def upstream_of(ref: str) -> str:
    """``"SF:16992"`` -> ``"16992"``, the id the upstream feed uses."""
    return ref.split(":", 1)[1]


def ref(operator: str, upstream_id: str) -> str:
    return f"{operator}:{upstream_id}"
