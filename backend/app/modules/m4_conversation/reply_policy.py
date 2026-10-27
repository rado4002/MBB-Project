"""Shared eligibility policy for Human-authored conversation replies."""

REPLY_ELIGIBLE_STATUSES = frozenset(
    {"active", "qualifying", "nurturing", "escalated"}
)
