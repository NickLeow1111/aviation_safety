"""Tests for the question-to-query reader.

The bug these protect against was quiet: the assistant answered "no go-arounds at WSSS"
because it never filtered by place, type or time at all — it handed the model the newest
fifteen rows of anything. Nothing errored; the answer was simply about different events.
"""
from datetime import datetime, timedelta, timezone

import event_query

NOW = datetime(2026, 11, 2, 2, 10, tzinfo=timezone.utc)


def test_go_around_last_week_at_wsss():
    query = event_query.parse("help me extract the go-around events last week in wsss", now=NOW)
    assert query.types == ["go_around"]
    assert query.place_icao == "WSSS"
    assert query.since == NOW - timedelta(days=7)
    assert query.until is None


def test_hyphen_and_space_spellings_agree():
    hyphen = event_query.parse("go-around events", now=NOW)
    spaced = event_query.parse("go around events", now=NOW)
    joined = event_query.parse("goaround events", now=NOW)
    assert hyphen.types == spaced.types == joined.types == ["go_around"]


def test_airport_name_is_resolved_to_a_code():
    query = event_query.parse("unstable approaches at changi yesterday", now=NOW)
    assert query.place_icao == "WSSS"
    assert query.types == ["unstable_approach"]
    assert query.since == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert query.until == datetime(2026, 11, 2, tzinfo=timezone.utc)


def test_kuala_lumpur_means_wmkk():
    assert event_query.parse("any incidents near KLIA", now=NOW).place_icao == "WMKK"


def test_los_is_loss_of_separation():
    assert event_query.parse("any LOS events today", now=NOW).types == ["loss_of_separation"]


def test_relative_hours():
    query = event_query.parse("loss of separation near KLIA in the last 24 hours", now=NOW)
    assert query.types == ["loss_of_separation"]
    assert query.place_icao == "WMKK"
    assert query.since == NOW - timedelta(hours=24)


def test_this_month_starts_on_the_first():
    query = event_query.parse("any bird strikes this month", now=NOW)
    assert query.types == ["bird_strike"]
    assert query.since == datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_a_word_is_not_an_airport_code():
    # "week" is four letters. Reading it as an aerodrome is how the empty answer happened.
    assert event_query.parse("go-arounds in the last week", now=NOW).place_icao is None


def test_known_place_allows_lowercase():
    query = event_query.parse("anything at wsss", now=NOW, known_places={"WSSS"})
    assert query.place_icao == "WSSS"


def test_uppercase_unknown_code_is_still_read():
    assert event_query.parse("events at ZZZZ", now=NOW).place_icao == "ZZZZ"


def test_a_plain_question_filters_nothing():
    query = event_query.parse("what happened recently", now=NOW)
    assert query.is_empty()


def test_describe_names_the_filters_for_the_answer():
    query = event_query.parse("go-arounds last week in wsss", now=NOW)
    text = query.describe()
    assert "go around" in text
    assert "WSSS" in text
    assert "since" in text
