"""Re-judge stored detections against the current classification.

The classification changed: an aircraft rolling out at 0 ft is ground traffic, and a
snapshot that called it a loss of separation was wrong. Deleting those rows would erase
the only evidence that the old rule behaved that way, so they are marked instead —
`invalidated_at`, a reason, and who did it — and every active view filters them out.

The re-judgement is recomputed from the stored tracks, not assumed: for each event the
aircraft's own positions around the event time are looked up and classified again. An
event survives if either participant was genuinely in the air at the time.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import phase
import store

# The invalidation records which classifier made the call. A later rule change is a new
# version, so every mark on a row says who made it and from which rules.
INVALIDATED_BY = f"reclassify:{phase.CLASSIFIER_VERSION}"
STATE_NAME = "loss_of_separation"
NIGHTLY_SECONDS = 20 * 3600
# How far either side of an event to look for the positions it was built from. The
# producer compares a single snapshot, so this only has to cover feed latency.
TRACK_WINDOW_SECONDS = 30
MIN_SAMPLES = 1


def _tracks_around(conn: Any, icao24: list[str], when: datetime) -> dict[str, list[dict[str, Any]]]:
    """Positions for these aircraft around the event, grouped by aircraft."""
    if not icao24:
        return {}
    since = when - timedelta(seconds=TRACK_WINDOW_SECONDS)
    until = when + timedelta(seconds=TRACK_WINDOW_SECONDS)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT icao24, altitude_ft, ground_speed_kt, vertical_rate_fpm, on_ground, callsign,
                   EXTRACT(EPOCH FROM observed_at)::float8
              FROM aircraft_track
             WHERE icao24 = ANY(%s) AND observed_at BETWEEN %s AND %s
             ORDER BY observed_at
            """,
            (icao24, since, until),
        )
        rows = cur.fetchall()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for icao, altitude, speed, vertical, on_ground, callsign, epoch in rows:
        grouped.setdefault(icao, []).append(
            {
                "icao24": icao,
                "callsign": callsign,
                "altitude_ft": altitude,
                "ground_speed_kt": speed,
                "vertical_rate_fpm": vertical,
                "on_ground": bool(on_ground),
                "t": float(epoch),
            }
        )
    return grouped


def _judge(event: dict[str, Any], tracks: dict[str, list[dict[str, Any]]], when: datetime) -> dict[str, Any]:
    """Decide whether an event survives the current classification.

    The minima are about airborne traffic, so a pair is only a loss of separation if both
    aircraft were flying. The aircraft's phase is taken from the position closest to the
    event time — the one the producer most likely compared — rather than from any sample
    in the window: an aircraft that was on the runway a minute before and climbing out a
    minute after has to be judged at the moment in question.

    An event with no stored positions for its aircraft is left alone. Absence of evidence
    is not evidence of a false positive, and this pass must never invalidate something it
    cannot re-derive.
    """
    participant_ids = event.get("icao24") or []
    stamped = when.timestamp()
    observed: dict[str, str] = {}
    for icao in participant_ids:
        samples = tracks.get(icao) or []
        if not samples:
            continue
        nearest = min(samples, key=lambda sample: abs(sample["t"] - stamped))
        observed[icao] = phase.classify(nearest)

    if not observed:
        return {"verdict": "unproven", "phases": {}, "reason": "no stored positions for this pair"}

    grounded = sorted(icao for icao, value in observed.items() if value == phase.GROUND)
    if grounded:
        return {
            "verdict": "invalid",
            "phases": observed,
            "reason": (
                "re-judged against the phase classifier: "
                f"{', '.join(grounded)} was ground traffic at the event time "
                "(at or below the surface band, taxiing, or rolling out), so the pair was "
                "not two aircraft inside the separation minimum"
            ),
        }
    return {"verdict": "valid", "phases": observed, "reason": ""}


# --------------------------------------------------------------------------- #
# the run record
# --------------------------------------------------------------------------- #

def ensure_state(conn: Any) -> None:
    """The pass keeps its own history: which version last judged, and when.

    Additive and idempotent, so it is safe on a store that predates this pass.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS classifier_state (
                name          TEXT PRIMARY KEY,
                version       TEXT NOT NULL,
                last_run_at   TIMESTAMPTZ NOT NULL,
                last_apply_at TIMESTAMPTZ,
                runs          INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS classifier_run (
                id         BIGSERIAL PRIMARY KEY,
                name       TEXT NOT NULL,
                version    TEXT NOT NULL,
                trigger    TEXT NOT NULL,
                checked    INTEGER NOT NULL,
                invalid    INTEGER NOT NULL,
                applied    INTEGER NOT NULL,
                ran_at     TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )


def read_state(conn: Any) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version, last_run_at, last_apply_at, runs FROM classifier_state WHERE name = %s",
            (STATE_NAME,),
        )
        row = cur.fetchone()
    if not row:
        return None
    version, last_run, last_apply, runs = row
    return {"version": version, "last_run_at": last_run, "last_apply_at": last_apply, "runs": runs}


def write_state(conn: Any, *, ran_at: datetime, applied: bool) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO classifier_state (name, version, last_run_at, last_apply_at, runs)
            VALUES (%s, %s, %s, %s, 1)
            ON CONFLICT (name) DO UPDATE
               SET version = EXCLUDED.version,
                   last_run_at = EXCLUDED.last_run_at,
                   last_apply_at = COALESCE(EXCLUDED.last_apply_at, classifier_state.last_apply_at),
                   runs = classifier_state.runs + 1
            """,
            (STATE_NAME, phase.CLASSIFIER_VERSION, ran_at, ran_at if applied else None),
        )


def record_run(conn: Any, *, trigger: str, result: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO classifier_run (name, version, trigger, checked, invalid, applied)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                STATE_NAME,
                phase.CLASSIFIER_VERSION,
                trigger,
                result["checked"],
                len(result["invalid"]),
                result["invalidated"],
            ),
        )


def reassess(conn: Any, apply: bool = False, trigger: str = "manual") -> dict[str, Any]:
    """Run the pass. `apply=False` reports what would change and writes nothing."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, event_time, event_type, callsigns, icao24, place_icao,
                   metrics->>'separation_nm', metrics->>'vertical_ft', detector
              FROM safety_event
             WHERE invalidated_at IS NULL AND event_type = 'loss_of_separation'
             ORDER BY event_time
            """
        )
        rows = cur.fetchall()

    checked = 0
    invalid: list[dict[str, Any]] = []
    unproven = 0
    valid = 0

    for event_id, when, event_type, callsigns, icao24, place_icao, separation, vertical, detector in rows:
        checked += 1
        tracks = _tracks_around(conn, list(icao24 or []), when)
        result = _judge({"icao24": list(icao24 or [])}, tracks, when)
        if result["verdict"] == "invalid":
            invalid.append(
                {
                    "id": event_id,
                    "event_time": when.isoformat(),
                    "callsigns": list(callsigns or []),
                    "place_icao": place_icao,
                    "separation_nm": separation,
                    "vertical_ft": vertical,
                    "detector": detector,
                    "phases": result["phases"],
                    "reason": result["reason"],
                }
            )
        elif result["verdict"] == "unproven":
            unproven += 1
        else:
            valid += 1

    written = 0
    if apply and invalid:
        written = _invalidate(conn, invalid)

    if apply:
        record_run(
            conn,
            trigger=trigger,
            result={"checked": checked, "invalid": invalid, "invalidated": written},
        )

    return {
        "checked": checked,
        "valid": valid,
        "unproven": unproven,
        "invalid": invalid,
        "invalidated": written,
        "applied": apply,
    }


def _invalidate(conn: Any, findings: list[dict[str, Any]]) -> int:
    """Mark the rows. Everything else about them stays exactly as it was."""
    import json  # noqa: PLC0415

    written = 0
    with conn.cursor() as cur:
        for finding in findings:
            cur.execute(
                """
                UPDATE safety_event
                   SET invalidated_at = now(),
                       invalidation_reason = %s,
                       invalidated_by = %s,
                       metrics = COALESCE(metrics, '{}'::jsonb) || %s::jsonb
                 WHERE id = %s AND invalidated_at IS NULL
                """,
                (
                    finding["reason"],
                    INVALIDATED_BY,
                    json.dumps(
                        {
                            "revalidation": {
                                "by": INVALIDATED_BY,
                                "at": datetime.now(timezone.utc).isoformat(),
                                "phases": finding["phases"],
                            }
                        }
                    ),
                    finding["id"],
                ),
            )
            written += cur.rowcount
    return written


def decide(state: dict[str, Any] | None, now: datetime) -> str | None:
    """Whether an automatic pass should run, and why.

    Two triggers, and no others:

    * the classifier version changed — the stored judgements were made by other rules;
    * a day has passed — positions arrive late (a feed backfills a gap, an upload lands),
      so an event that could not be re-derived yesterday can be re-derivable today.

    Returns the trigger name, or None to leave the store alone.
    """
    if state is None:
        return "first_run"
    if state["version"] != phase.CLASSIFIER_VERSION:
        return "classifier_changed"
    last = state["last_run_at"]
    if last is None:
        return "first_run"
    if (now - last).total_seconds() >= NIGHTLY_SECONDS:
        return "nightly_catch_up"
    return None


def auto(conn: Any, now: datetime | None = None) -> dict[str, Any]:
    """The scheduled entry point: judge only when there is something to judge."""
    moment = now or datetime.now(timezone.utc)
    ensure_state(conn)
    state = read_state(conn)
    trigger = decide(state, moment)

    if trigger is None:
        return {"skipped": True, "reason": "rules unchanged and the last pass is under a day old", "state": state}

    result = reassess(conn, apply=True, trigger=trigger)
    write_state(conn, ran_at=moment, applied=True)
    result.update({"skipped": False, "trigger": trigger})
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-judge stored loss-of-separation events.")
    parser.add_argument("--apply", action="store_true", help="write the invalidations (default: dry run)")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="scheduled mode: apply only when the classifier version changed or a day has passed",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    with store.connect() as conn:
        if args.auto:
            result = auto(conn)
            if result.get("skipped"):
                print(f"reclassify: nothing to do — {result['reason']}")
                return 0
            trigger = result["trigger"]
            print(
                f"reclassify[{trigger}]: checked {result['checked']}, invalidated "
                f"{result['invalidated']} of {len(result['invalid'])} findings "
                f"(classifier {phase.CLASSIFIER_VERSION})"
            )
            return 0

        result = reassess(conn, apply=args.apply)
        if args.apply:
            ensure_state(conn)
            write_state(conn, ran_at=datetime.now(timezone.utc), applied=True)

    if args.json:
        import json  # noqa: PLC0415

        print(json.dumps(result, indent=2, default=str))
        return 0

    print(
        f"checked {result['checked']} loss-of-separation events: "
        f"{result['valid']} stand, {len(result['invalid'])} are false positives, "
        f"{result['unproven']} could not be re-derived and were left alone"
    )
    for item in result["invalid"]:
        print(
            f"  id {item['id']}  {item['event_time'][:16]}  {' / '.join(item['callsigns'])}  "
            f"{item['place_icao'] or '—'}  {item['separation_nm']} NM  phases {item['phases']}"
        )
    if args.apply:
        print(f"invalidated {result['invalidated']} rows (records kept, audit trail written)")
    else:
        print("dry run: nothing written. Re-run with --apply to mark them.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
