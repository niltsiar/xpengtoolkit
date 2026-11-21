"""When the MQTT silence watchdog should stop being polite.

The watchdog reconnects and sends a passive state query. Both are cloud-side
only and never wake the car, which is why they are free to run unconditionally.
But a reconnect does not always clear a stalled fanout, and when it does not,
the passive query is not a weaker version of the fix — it is not a fix at all.

Measured 2026-10-06: 11h31m with the socket up and ~66 reconnects, each one
authenticating, subscribing and sending a passive query, and not one byte back.
The hourly REST fallback reported the car's state normally throughout, so the
account, the tokens and the car were all fine; only the fanout was wedged. At
08:57:07 a single `service_type=19 / cmd_type:2` produced a 4492-byte full state
1.2 seconds later. The passive query sent four minutes earlier had produced
nothing.

So there is a point at which waiting is no longer the safe choice. That is what
this module decides, and it is a separate module with no Home Assistant import
so the decision can be tested: the answer wakes a physical car, and a policy
that wakes a car should not be verifiable only by watching one.

The two bounds are both required:

  * ESCALATE_AFTER_SECONDS of *unbroken* silence before a force-upload is
    allowed at all. Reconnecting gets this long to work first.
  * ESCALATE_BACKOFF_SECONDS between force-uploads, whatever happens in
    between.

The second bound is what keeps this from becoming the scheduled `cmd_type:2`
this project deliberately avoids. It has to be time-based rather than
"per stall" or "reset once data returns", because a force-upload during a
wedged fanout yields exactly *one* full state and then the silence resumes —
so treating that one message as recovery would let a stalled car be woken every
half hour. Counting from the last escalation instead means the worst case is one
wake per ESCALATE_BACKOFF, and a stall that begins after a healthy stretch still
escalates promptly, because by then the previous escalation is long past.

The first bound was two hours and is now half an hour. Nothing about the wake
*rate* changes — that is the backoff's job, and a car parked and asleep all day
is woken at most twice either way. What changes is how long a stalled
*transition* goes unnoticed. A parked car answers the passive query with a stub,
and that is why the decision is keyed on staleness rather than silence: a stub
resets the *silence* clock (so no reconnect is wasted on a car that is simply
asleep) and not this one, because it carries nothing. A car that stub-answers for
half an hour without delivering a byte therefore does escalate. In
that state a drive or a charge can begin completely unseen, because the fanout
that should have announced it is the thing that is wedged, and a passive query
does not punch through one (observed 2026-08-30, mid-drive: ~1h of zero data
until a force-upload). Two hours of that is two hours of driving that never
reaches Home Assistant or EVConduit.

Half an hour rather than something shorter because it has to stay well clear of
the reconnect cadence — RECONNECT_BACKOFF, 10 minutes, in __init__.py — so a
reconnect and its passive query get several honest attempts before we conclude
they cannot work.
"""

from __future__ import annotations

from collections.abc import Mapping

#: Unbroken silence before a force-upload is allowed. Half an hour: several
#: reconnect cycles' worth, so a connection that merely needed a nudge has had
#: its chance, and short enough that a stalled start-of-drive is noticed while
#: the drive is still happening.
ESCALATE_AFTER_SECONDS: float = 30 * 60

#: Minimum gap between force-uploads. Twelve hours bounds the cost of a fanout
#: that is wedged for days to at most two wakes a day, and the hourly REST
#: fallback keeps the car's core values current in between.
ESCALATE_BACKOFF_SECONDS: float = 12 * 3600

#: Minimum gap between two force-uploads that are justified by *motion*. The
#: number above exists to bound how often a parked car is woken; a moving car is
#: already awake, so its escalation only needs a cadence, not a budget. Five
#: minutes is the car's own upload cadence: a faster arm cannot get more out of
#: it, and a wedged fanout cannot turn every watchdog tick into a force-upload.
MOTION_ESCALATE_BACKOFF_SECONDS: float = 5 * 60

#: Oldest motion evidence that still justifies an arm. A rise seen more than
#: ten minutes ago says nothing about the car now.
MOTION_EVIDENCE_MAX_AGE_SECONDS: float = 10 * 60

#: Energy silence after which the fast widget poll asks for the widget. While
#: the stream is healthy, energy frames arrive about every 60 s, so two minutes
#: of silence means a burst ended or the car woke before the stream started; the
#: widget odometer is then the only evidence of a drive.
MOTION_ESCALATE_AFTER_SECONDS: float = 2 * 60


def should_escalate(
    silence_seconds: float,
    last_escalation: float,
    now: float,
    escalate_after: float = ESCALATE_AFTER_SECONDS,
    escalate_backoff: float = ESCALATE_BACKOFF_SECONDS,
) -> bool:
    """Whether to send a force-upload (which wakes the car) right now.

    `last_escalation` is 0.0 when none has been sent this session, which is a
    real value rather than "unknown": it means the backoff has nothing to count
    from, so the first escalation is governed only by the silence bound.
    """
    if silence_seconds < escalate_after:
        return False
    if last_escalation and (now - last_escalation) < escalate_backoff:
        return False
    return True


def motion_refusal(
    silence_seconds: float,
    last_escalation: float,
    now: float,
    last_motion_at: float,
    escalate_after: float = ESCALATE_AFTER_SECONDS,
    motion_backoff: float = MOTION_ESCALATE_BACKOFF_SECONDS,
    motion_max_age: float = MOTION_EVIDENCE_MAX_AGE_SECONDS,
) -> str | None:
    """Why motion evidence does not justify a force-upload outside the 12 h budget.

    None means it does. Otherwise one of ``patience``, ``no_evidence``,
    ``stale_evidence``, ``backoff`` or ``spent`` (the evidence is not newer than
    the last escalation). Only ``backoff`` is a refusal of an arm that is
    otherwise due: the others are the ordinary state of a car that is parked, or
    whose stream is healthy.

    Measured on issue #3's capture (2026-10-21): the fanout stalled mid-drive at
    16:15, and from 16:46 until the cloud recovered by itself at 17:01 the
    watchdog refused 16 escalations as "one per 12h" - the budget had been spent
    on a parked stall earlier that day. Telemetry stayed frozen for 46 minutes
    while the car was being driven, where a force-upload cannot wake anything.

    Four bounds replace the sleeping-car budget, all of them cheap to satisfy
    only when they should be:

    * the caller's staleness bound still applies - motion is a reason to skip the
      12 h budget, not the patience. The motion caller measures it as the age of
      the last energy-carrying frame and passes ``MOTION_ESCALATE_AFTER_SECONDS``;
    * the evidence must be at most ``motion_max_age`` old;
    * the motion must be *newer than the last escalation*, so one episode of
      motion buys one wake rather than one per watchdog tick, and a fanout that
      stays wedged cannot keep escalating on the same old evidence;
    * `motion_backoff` since that wake, as a floor for the case where the wake
      did produce data and the car is still moving.

    `last_motion_at` is 0.0 when the last accepted state said the car was parked
    or said nothing about driving; both mean "do not spend the sleeping car's
    allowance on this".
    """
    if silence_seconds < escalate_after:
        return "patience"
    if not last_motion_at:
        return "no_evidence"
    if now - last_motion_at > motion_max_age:
        return "stale_evidence"
    if last_escalation and (now - last_escalation) < motion_backoff:
        return "backoff"
    if last_escalation and last_motion_at <= last_escalation:
        return "spent"
    return None


#: Why a force-upload is sent. The parked class can wake a sleeping car, so it
#: has a daily cap; motion arms go to a car that is already awake and have a
#: higher one; the other two are only counted.
PARKED_WAKE_REASONS: tuple[str, ...] = ("cold_start", "stall")
PARKED_WAKES_PER_DAY: int = 2
MOTION_ARMS_PER_DAY: int = 24

#: The reasons with a cap; their counts are stored, the others are not.
CAPPED_REASONS: tuple[str, ...] = (*PARKED_WAKE_REASONS, "motion")

#: ``active`` force-uploads (every scan interval while charging or driving) need
#: energy data this recent. A lost final P or unplug frame would otherwise leave
#: a one-minute force-upload loop running on a parked car.
ACTIVE_ENERGY_MAX_AGE_SECONDS: float = 15 * 60

#: A refusal is logged at WARNING at most this often per reason and cause.
REFUSAL_LOG_INTERVAL_SECONDS: float = 10 * 60


def daily_cap_allows(reason: str, counts: Mapping[str, int]) -> bool:
    """Whether today's counts leave room for another force-upload of ``reason``.

    The parked class shares one allowance, so a cold start spends one of the two
    wakes a stall could use. Motion arms have their own and never spend it.
    ``active`` and ``user`` are counted only.
    """
    if reason in PARKED_WAKE_REASONS:
        return sum(counts.get(r, 0) for r in PARKED_WAKE_REASONS) < PARKED_WAKES_PER_DAY
    if reason == "motion":
        return counts.get("motion", 0) < MOTION_ARMS_PER_DAY
    return True


def counts_for_day(
    saved_day: str | None, saved_counts: Mapping[str, int] | None, today: str
) -> dict[str, int]:
    """The stored counts if they are from ``today`` (the local day), else none.

    A store without the count keys starts the day at zero.
    """
    if saved_day != today or not isinstance(saved_counts, Mapping):
        return {}
    return {str(k): int(v) for k, v in saved_counts.items()}
