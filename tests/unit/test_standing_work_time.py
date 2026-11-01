"""Fixed historical time oracles for the #228 phase1 pure API.

Expected instants are literal independent examples, not values computed using
the resolver under test. No clock, native scheduler or dispatch is exercised.
"""

import pytest
from packages.contracts.standing_work import (
    DailyOccurrence,
    DailySchedule,
    StandingAuthorization,
    StandingQuietHours,
    latest_relevant,
    quiet_until,
    resolve_daily,
    validate_authorization_at,
)

ERRORS = (ValueError, TypeError)


def daily(**changes):
    return DailySchedule.model_validate(
        {
            "kind": "daily_local",
            "timezone": "UTC",
            "local_time": "09:00",
            "fold_policy": "earlier",
            "gap_policy": "skip",
            "grace_seconds": 120,
            "missed_run_policy": "latest_within_grace",
            "start_local_date": "2024-01-01",
            "valid_until_utc": "2024-01-04T00:00:00Z",
            **changes,
        }
    )


def grant(schedule):
    return StandingAuthorization.model_validate(
        {
            "label": "Synthetic daily inspection",
            "objective": "Inspect the synthetic workspace and record status.",
            "content_scope": "public_synthetic",
            "enabled": True,
            "task_session": {"session_id": "standing-session", "thread_id": "thread", "epoch": 1},
            "workspace_pin": {
                "schema_version": "1.0",
                "workspace_id": "b" * 64,
                "root_identity": "c" * 64,
                "settings_revision": 0,
                "source": "host",
                "shell_backend": "bubblewrap",
                "mode": "direct",
            },
            "schedule": schedule,
            "delivery": {
                "kind": "in_app",
                "attention": "important",
                "preview": "status_only",
                "quiet_hours": None,
            },
            "overlap_policy": "one_unresolved_per_authorization",
            "maximum_runs": 3,
            "inference_authorization": "configured_agent_profile",
        }
    )


def quiet(timezone, start, end):
    return StandingQuietHours.model_validate(
        {"timezone": timezone, "start_local_time": start, "end_local_time": end}
    )


def assert_occurrence(actual, local_date, due_utc):
    if due_utc is None:
        assert actual is None
    else:
        assert isinstance(actual, DailyOccurrence)
        assert actual.local_date == local_date
        assert actual.due_utc == due_utc


@pytest.mark.parametrize(
    ("zone", "minute", "local_date", "start", "expiry", "due"),
    [
        (
            "UTC",
            "00:00",
            "2024-01-02",
            "2024-01-01",
            "2024-01-04T00:00:00Z",
            "2024-01-02T00:00:00Z",
        ),
        (
            "America/New_York",
            "02:30",
            "2024-03-09",
            "2024-03-09",
            "2024-03-12T00:00:00Z",
            "2024-03-09T07:30:00Z",
        ),
        ("America/New_York", "02:30", "2024-03-10", "2024-03-09", "2024-03-12T00:00:00Z", None),
        (
            "America/New_York",
            "02:30",
            "2024-03-11",
            "2024-03-09",
            "2024-03-12T00:00:00Z",
            "2024-03-11T06:30:00Z",
        ),
        (
            "America/New_York",
            "01:30",
            "2024-11-03",
            "2024-11-02",
            "2024-11-05T00:00:00Z",
            "2024-11-03T05:30:00Z",
        ),
        (
            "Europe/Berlin",
            "02:30",
            "2024-10-27",
            "2024-10-26",
            "2024-10-29T00:00:00Z",
            "2024-10-27T00:30:00Z",
        ),
        (
            "Australia/Lord_Howe",
            "01:45",
            "2024-04-07",
            "2024-04-06",
            "2024-04-09T00:00:00Z",
            "2024-04-06T14:45:00Z",
        ),
        ("Pacific/Apia", "12:00", "2011-12-30", "2011-12-29", "2012-01-02T00:00:00Z", None),
        (
            "Pacific/Apia",
            "00:30",
            "2011-12-31",
            "2011-12-29",
            "2012-01-02T00:00:00Z",
            "2011-12-30T10:30:00Z",
        ),
    ],
)
def test_daily_resolution_uses_real_civil_times_and_earliest_fold(
    zone, minute, local_date, start, expiry, due
):
    schedule = daily(
        timezone=zone, local_time=minute, start_local_date=start, valid_until_utc=expiry
    )
    assert_occurrence(resolve_daily(schedule, local_date), local_date, due)


@pytest.mark.parametrize(
    ("date", "due"),
    [
        ("2024-01-01", None),
        ("2024-01-02", "2024-01-02T09:00:00Z"),
        ("2024-01-03", None),
        ("2024-01-04", None),
    ],
)
def test_daily_nominal_range_and_expiry_are_not_extra_catchup_permission(date, due):
    schedule = daily(start_local_date="2024-01-02", valid_until_utc="2024-01-03T09:00:00Z")
    assert_occurrence(resolve_daily(schedule, date), date, due)


def test_resolved_due_instant_is_frozen_and_not_recomputed_from_another_schedule():
    schedule = daily()
    occurrence = resolve_daily(schedule, "2024-01-02")
    assert_occurrence(occurrence, "2024-01-02", "2024-01-02T09:00:00Z")
    replacement = daily(timezone="America/New_York")
    assert_occurrence(
        resolve_daily(replacement, "2024-01-02"), "2024-01-02", "2024-01-02T14:00:00Z"
    )
    assert occurrence.due_utc == "2024-01-02T09:00:00Z"
    with pytest.raises(ERRORS):
        occurrence.due_utc = "2024-01-02T14:00:00Z"


@pytest.mark.parametrize(
    "value", ["2024-1-02", "2024-02-30", "2023-02-29", "2024-01-02 ", True, None, 20240102]
)
def test_resolver_revalidates_explicit_civil_date(value):
    with pytest.raises(ERRORS):
        resolve_daily(daily(), value)


@pytest.mark.parametrize("field", ["timezone", "local_time", "grace_seconds"])
def test_resolver_refuses_copied_invalid_schedule(field):
    value = {"timezone": "Not/A_Zone", "local_time": "24:00", "grace_seconds": True}[field]
    forged = daily().model_copy(update={field: value})
    with pytest.raises(ERRORS):
        resolve_daily(forged, "2024-01-02")


def test_constructor_is_clock_free_but_save_validation_enforces_the_exact_30_day_window():
    authorization = grant(daily(valid_until_utc="2024-01-31T00:00:00Z"))
    assert authorization.schedule.valid_until_utc == "2024-01-31T00:00:00Z"
    assert validate_authorization_at(authorization, "2024-01-01T00:00:00Z") is None
    with pytest.raises(ERRORS):
        validate_authorization_at(authorization, "2023-12-31T23:59:59Z")
    with pytest.raises(ERRORS):
        validate_authorization_at(authorization, "2024-01-31T00:00:00Z")
    with pytest.raises(ERRORS):
        validate_authorization_at(authorization, "2024-01-31T00:00:01Z")


def test_32_civil_dates_can_be_valid_when_saved_later_without_preauthorization_catchup():
    authorization = grant(daily(valid_until_utc="2024-02-01T00:00:00Z"))
    assert validate_authorization_at(authorization, "2024-01-02T00:00:00Z") is None
    with pytest.raises(ERRORS):
        validate_authorization_at(authorization, "2024-01-01T00:00:00Z")
    assert (
        latest_relevant(authorization.schedule, "2024-01-02T10:00:00Z", "2024-01-02T10:00:00Z")
        is None
    )


@pytest.mark.parametrize(
    "clock",
    [
        "2024-01-01T00:00:00+00:00",
        "2024-01-01T00:00:00.000Z",
        "2024-01-01 00:00:00Z",
        "2024-01-01T00:00:00z",
        "2024-02-30T00:00:00Z",
        "2024-01-01T23:59:60Z",
        True,
        None,
        1704067200,
    ],
)
def test_save_validation_requires_exact_explicit_utc_seconds(clock):
    with pytest.raises(ERRORS):
        validate_authorization_at(grant(daily()), clock)


def test_save_validation_revalidates_typed_authorization_instead_of_trusting_a_copy():
    forged = grant(daily()).model_copy(update={"enabled": 1})
    with pytest.raises(ERRORS):
        validate_authorization_at(forged, "2024-01-01T00:00:00Z")


@pytest.mark.parametrize(
    ("saved", "now", "grace", "date", "due"),
    [
        (
            "2024-01-01T00:00:00Z",
            "2024-01-03T10:00:00Z",
            86400,
            "2024-01-03",
            "2024-01-03T09:00:00Z",
        ),
        ("2024-01-02T09:00:00Z", "2024-01-02T09:00:00Z", 60, "2024-01-02", "2024-01-02T09:00:00Z"),
        ("2024-01-02T09:00:01Z", "2024-01-02T09:00:30Z", 60, "2024-01-02", None),
        ("2024-01-02T00:00:00Z", "2024-01-02T08:59:59Z", 60, "2024-01-02", None),
        ("2024-01-02T00:00:00Z", "2024-01-02T09:01:00Z", 60, "2024-01-02", "2024-01-02T09:00:00Z"),
        ("2024-01-02T00:00:00Z", "2024-01-02T09:01:01Z", 60, "2024-01-02", None),
        ("2024-01-02T10:00:00Z", "2024-01-02T09:00:00Z", 86400, "2024-01-02", None),
    ],
)
def test_latest_selector_obeys_saved_at_due_order_and_inclusive_grace(saved, now, grace, date, due):
    assert_occurrence(latest_relevant(daily(grace_seconds=grace), saved, now), date, due)


@pytest.mark.parametrize(
    ("now", "due"),
    [
        ("2024-01-02T00:00:30Z", "2024-01-01T23:59:00Z"),
        ("2024-01-02T00:01:00Z", "2024-01-01T23:59:00Z"),
        ("2024-01-02T00:01:01Z", None),
    ],
)
def test_grace_crosses_midnight_without_relabeling_the_occurrence(now, due):
    schedule = daily(local_time="23:59", grace_seconds=120)
    assert_occurrence(latest_relevant(schedule, "2024-01-01T00:00:00Z", now), "2024-01-01", due)


@pytest.mark.parametrize(
    ("now", "due"),
    [
        ("2024-01-03T09:00:59Z", "2024-01-03T09:00:00Z"),
        ("2024-01-03T09:01:00Z", None),
    ],
)
def test_expiry_prevents_dispatch_even_while_the_last_occurrence_remains_in_grace(now, due):
    schedule = daily(valid_until_utc="2024-01-03T09:01:00Z")
    assert_occurrence(latest_relevant(schedule, "2024-01-01T00:00:00Z", now), "2024-01-03", due)


@pytest.mark.parametrize(
    ("now", "due"),
    [
        ("2024-11-03T06:30:00Z", "2024-11-03T05:30:00Z"),
        ("2024-11-03T06:30:01Z", None),
    ],
)
def test_repeated_fold_does_not_create_a_second_fresh_daily_occurrence(now, due):
    schedule = daily(
        timezone="America/New_York",
        local_time="01:30",
        grace_seconds=3600,
        start_local_date="2024-11-02",
        valid_until_utc="2024-11-05T00:00:00Z",
    )
    assert_occurrence(latest_relevant(schedule, "2024-11-02T00:00:00Z", now), "2024-11-03", due)


@pytest.mark.parametrize(
    ("now", "due"),
    [
        ("2024-03-10T07:15:00Z", "2024-03-09T07:30:00Z"),
        ("2024-03-10T08:00:00Z", None),
    ],
)
def test_gap_skips_today_without_fabricating_a_new_due_or_ignoring_previous_grace(now, due):
    schedule = daily(
        timezone="America/New_York",
        local_time="02:30",
        grace_seconds=86400,
        start_local_date="2024-03-09",
        valid_until_utc="2024-03-12T00:00:00Z",
    )
    assert_occurrence(latest_relevant(schedule, "2024-03-09T00:00:00Z", now), "2024-03-09", due)


def test_rollback_selector_is_stateless_and_does_not_claim_native_occurrence_deduplication():
    schedule = daily(local_time="00:00", grace_seconds=86400)
    saved = "2024-01-01T00:00:00Z"
    assert_occurrence(
        latest_relevant(schedule, saved, "2024-01-02T01:00:00Z"),
        "2024-01-02",
        "2024-01-02T00:00:00Z",
    )
    assert_occurrence(
        latest_relevant(schedule, saved, "2024-01-01T23:00:00Z"),
        "2024-01-01",
        "2024-01-01T00:00:00Z",
    )
    assert_occurrence(
        latest_relevant(schedule, saved, "2024-01-02T01:00:00Z"),
        "2024-01-02",
        "2024-01-02T00:00:00Z",
    )
    assert_occurrence(
        latest_relevant(schedule, saved, "2024-01-02T01:00:00Z"),
        "2024-01-02",
        "2024-01-02T00:00:00Z",
    )


@pytest.mark.parametrize(
    "clock", ["2024-01-02T09:00:00+00:00", "2024-01-02T09:00:00.1Z", "2024-01-02", True, None]
)
@pytest.mark.parametrize("argument", ["saved_at_utc", "now_utc"])
def test_selector_requires_both_explicit_valid_clocks(argument, clock):
    clocks = {
        "saved_at_utc": "2024-01-01T00:00:00Z",
        "now_utc": "2024-01-02T09:00:00Z",
        argument: clock,
    }
    with pytest.raises(ERRORS):
        latest_relevant(daily(), **clocks)


def test_selector_revalidates_copied_schedule():
    forged = daily().model_copy(update={"grace_seconds": True})
    with pytest.raises(ERRORS):
        latest_relevant(forged, "2024-01-01T00:00:00Z", "2024-01-02T09:00:00Z")


def test_no_quiet_hours_is_an_explicit_unrestricted_delivery_interval():
    assert quiet_until(None, "2024-01-02T22:00:00Z") is None


@pytest.mark.parametrize(
    ("now", "end"),
    [
        ("2024-01-01T21:59:59Z", None),
        ("2024-01-01T22:00:00Z", "2024-01-01T23:00:00Z"),
        ("2024-01-01T22:30:00Z", "2024-01-01T23:00:00Z"),
        ("2024-01-01T23:00:00Z", None),
    ],
)
def test_quiet_start_is_inclusive_and_end_is_exclusive(now, end):
    assert quiet_until(quiet("UTC", "22:00", "23:00"), now) == end


@pytest.mark.parametrize(
    ("now", "end"),
    [
        ("2024-01-01T22:00:00Z", "2024-01-02T07:00:00Z"),
        ("2024-01-02T06:59:59Z", "2024-01-02T07:00:00Z"),
        ("2024-01-02T07:00:00Z", None),
        ("2024-01-02T21:00:00Z", None),
    ],
)
def test_quiet_interval_can_wrap_midnight(now, end):
    assert quiet_until(quiet("UTC", "22:00", "07:00"), now) == end


@pytest.mark.parametrize(
    ("now", "end"),
    [
        ("2024-11-03T04:29:59Z", None),
        ("2024-11-03T04:30:00Z", "2024-11-03T06:30:00Z"),
        ("2024-11-03T05:30:00Z", "2024-11-03T06:30:00Z"),
        ("2024-11-03T05:50:00Z", "2024-11-03T06:30:00Z"),
        ("2024-11-03T06:00:00Z", "2024-11-03T06:30:00Z"),
        ("2024-11-03T06:29:59Z", "2024-11-03T06:30:00Z"),
        ("2024-11-03T06:30:00Z", None),
    ],
)
def test_quiet_repeated_fold_end_covers_both_real_intervals(now, end):
    assert quiet_until(quiet("America/New_York", "00:30", "01:30"), now) == end


def test_quiet_repeated_start_uses_first_fold_even_when_second_clock_is_before_start():
    assert (
        quiet_until(quiet("America/New_York", "01:30", "02:00"), "2024-11-03T06:15:00Z")
        == "2024-11-03T07:00:00Z"
    )


@pytest.mark.parametrize(
    ("start", "end", "now", "until"),
    [
        ("02:15", "04:00", "2024-03-10T06:59:59Z", None),
        ("02:15", "04:00", "2024-03-10T07:00:00Z", "2024-03-10T08:00:00Z"),
        ("02:15", "04:00", "2024-03-10T08:00:00Z", None),
        ("01:00", "02:30", "2024-03-10T06:30:00Z", "2024-03-10T07:00:00Z"),
        ("01:00", "02:30", "2024-03-10T07:00:00Z", None),
    ],
)
def test_nonexistent_quiet_boundaries_advance_to_first_valid_minute(start, end, now, until):
    assert quiet_until(quiet("America/New_York", start, end), now) == until


@pytest.mark.parametrize(
    ("now", "until"),
    [("2024-10-05T15:20:00Z", "2024-10-05T15:30:00Z"), ("2024-10-05T15:30:00Z", None)],
)
def test_non_hour_timezone_gap_does_not_assume_a_60_minute_jump(now, until):
    assert quiet_until(quiet("Australia/Lord_Howe", "01:45", "02:15"), now) == until


@pytest.mark.parametrize(
    ("now", "until"),
    [
        ("2011-12-30T10:30:00Z", None),
        ("2011-12-30T11:00:00Z", "2011-12-30T13:00:00Z"),
        ("2011-12-30T13:00:00Z", None),
    ],
)
def test_skipped_civil_day_is_bounded_and_does_not_invent_an_active_interval(now, until):
    assert quiet_until(quiet("Pacific/Apia", "01:00", "03:00"), now) == until


@pytest.mark.parametrize(
    "clock",
    ["2024-01-01T22:00:00+00:00", "2024-01-01T22:00:00.1Z", "2024-02-30T22:00:00Z", True, None],
)
def test_quiet_requires_an_explicit_valid_utc_clock(clock):
    with pytest.raises(ERRORS):
        quiet_until(quiet("UTC", "22:00", "07:00"), clock)


def test_quiet_revalidates_copied_and_constructed_boundaries():
    invalid = quiet("UTC", "22:00", "07:00").model_copy(update={"end_local_time": "24:00"})
    constructed = StandingQuietHours.model_construct(
        timezone="UTC", start_local_time="22:00", end_local_time="22:00"
    )
    for candidate in [invalid, constructed]:
        with pytest.raises(ERRORS):
            quiet_until(candidate, "2024-01-01T22:00:00Z")
