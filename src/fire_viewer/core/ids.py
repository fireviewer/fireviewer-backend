import re
from uuid import uuid4

FIRE_ID_RE = re.compile(r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")
SOURCE_KEY_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{1,126}[a-zA-Z0-9]$")
TRACE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{7,127}$")
TERRITORY_CODE_RE = re.compile(r"^[0-9A-Z]{2,3}$")


def new_prefixed_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def new_observation_id() -> str:
    return new_prefixed_id("O")


def new_event_id() -> str:
    return new_prefixed_id("EV")


def new_trace_id() -> str:
    return new_prefixed_id("tr")


def new_asset_id() -> str:
    return new_prefixed_id("A")


def new_job_id() -> str:
    return new_prefixed_id("J")


def format_fire_id(territory_code: str, sequence: int) -> str:
    return f"FR-{territory_code}-{sequence:05d}"


def format_episode_id(ordinal: int) -> str:
    return f"E{ordinal:02d}"
