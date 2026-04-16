"""
app/modules/m6_relance/scheduler.py — Relance timing and quiet hours logic.

Implements:
  1. Quiet hours guard (22:00–07:00 Africa/Kinshasa)
  2. Cadence calculation (relance #1: +24h, #2: +48-72h, #3: +7-10d)
  3. Timezone-aware scheduling
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import structlog

log = structlog.get_logger(__name__)

KINSHASA_TZ = ZoneInfo("Africa/Kinshasa")
QUIET_HOUR_START = 22  # 22:00
QUIET_HOUR_END = 7  # 07:00


def is_quiet_hours(dt: datetime | None = None) -> bool:
    """
    Check if given datetime falls within quiet hours (22:00–07:00 Kinshasa time).

    Args:
        dt: Datetime to check (defaults to now in UTC)

    Returns:
        True if in quiet hours, False otherwise
    """
    if dt is None:
        dt = datetime.now(timezone.utc)

    # Convert to Kinshasa timezone
    dt_kin = dt.astimezone(KINSHASA_TZ)
    hour = dt_kin.hour

    # Quiet hours: 22:00–07:00 (includes midnight wrap)
    in_quiet = hour >= QUIET_HOUR_START or hour < QUIET_HOUR_END

    if in_quiet:
        log.debug("m6.scheduler.quiet_hours", hour=hour, dt_kin=str(dt_kin))

    return in_quiet


def get_next_allowed_time(dt: datetime | None = None) -> datetime:
    """
    If given datetime is in quiet hours, return next 07:00 Kinshasa time.
    Otherwise, return the datetime as-is.

    Args:
        dt: Datetime to check (defaults to now in UTC)

    Returns:
        Next allowed send time (UTC timezone)
    """
    if dt is None:
        dt = datetime.now(timezone.utc)

    if not is_quiet_hours(dt):
        return dt

    # Convert to Kinshasa, find next 07:00
    dt_kin = dt.astimezone(KINSHASA_TZ)
    next_morning = dt_kin.replace(hour=7, minute=0, second=0, microsecond=0)

    # If we're past 22:00, next morning is tomorrow
    if dt_kin.hour >= QUIET_HOUR_START:
        next_morning += timedelta(days=1)

    # Convert back to UTC
    next_allowed_utc = next_morning.astimezone(timezone.utc)

    log.info(
        "m6.scheduler.rescheduled_quiet",
        original_hour=dt_kin.hour,
        next_send=str(next_allowed_utc),
    )

    return next_allowed_utc


def calculate_next_relance_time(
    *,
    last_customer_message_time: datetime,
    attempt_number: int,
) -> datetime:
    """
    Calculate when next relance should be sent based on attempt number.

    Cadence (from Phase 1.B spec):
      - Relance #1: +24 hours from last customer message
      - Relance #2: +48–72 hours from last customer message (use 60h midpoint)
      - Relance #3: +7–10 days from last customer message (use 8.5d midpoint)

    Args:
        last_customer_message_time: Timestamp of last inbound message
        attempt_number: 1, 2, or 3

    Returns:
        Scheduled send time (UTC, adjusted for quiet hours if needed)
    """
    if attempt_number == 1:
        delay = timedelta(hours=24)
    elif attempt_number == 2:
        delay = timedelta(hours=60)  # 60h = 2.5 days (midpoint of 48-72h)
    elif attempt_number == 3:
        delay = timedelta(days=8, hours=12)  # 8.5 days (midpoint of 7-10d)
    else:
        log.error("m6.scheduler.invalid_attempt", attempt_number=attempt_number)
        raise ValueError(f"Invalid attempt_number: {attempt_number} (must be 1, 2, or 3)")

    scheduled_time = last_customer_message_time + delay

    # Adjust for quiet hours
    final_time = get_next_allowed_time(scheduled_time)

    log.info(
        "m6.scheduler.calculated",
        attempt_number=attempt_number,
        delay_hours=delay.total_seconds() / 3600,
        scheduled_time=str(final_time),
    )

    return final_time
