"""Alarm priorities (ISA-18.2): one definition for the whole app. A lower rank is more urgent.

Critical (P0) sits above High for the rare event that means fraud is happening right now, such as a
one-time code (OTP) for a purchase you didn't make. Keep it rare so it keeps its meaning.

Rank 0 is falsy: test for `is None`, never truthiness, when a rank may be missing."""

RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
NAME = {0: "Critical", 1: "High", 2: "Medium", 3: "Low"}
RANKS = tuple(NAME)  # most urgent first
SEVERITIES = list(RANK)
OTP_SEVERITY = "critical"  # every OTP request (see pipeline.record_otp)
OTP_RANK = RANK[OTP_SEVERITY]


def rank(severity: str) -> int:
    return RANK.get(severity, 3)
