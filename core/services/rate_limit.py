"""A rate limit that survives more than one worker.

The existing `core.authz.throttle` keeps its counters in a module-level dict. That
is correct for one process and wrong for the deployment: Render runs gunicorn with
two workers and no `--preload`, so each holds its own copy, every configured limit
is silently doubled, and which worker a request lands on is decided by the accept
queue. A restart erases every bucket.

Django's cache is not the fix by itself. The configured backend is `LocMemCache`,
which is also per-process — porting to `cache.get`/`cache.set` reads as a fix,
passes every single-process test, and changes nothing. And the database cache
backend has no atomic `incr`: a get followed by a set is the same read-then-write
race already removed from retry ownership, one request wide.

So the counter is a row, and it is claimed under `select_for_update()`. The lock is
held for one small UPDATE with no network call inside it, and on PostgreSQL that
serialises the workers properly. SQLite serialises writers anyway.

Buckets are keyed on the STUDENT, not the endpoint. `throttle`'s key is the view's
qualified name, so "send" and "retry" share a budget only because retry happens to
reuse the same view — split that view tomorrow and the budget silently doubles with
no test failing. Naming the bucket `advisor_generation` says the budget belongs to
generating answers, whichever door the request came through.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from core.models import RateLimitBucket

#: Logical budgets. Every endpoint that spends the same resource shares one.
GENERATION = "advisor_generation"
PLANNING = "planner_generation"
CONVERSATION = "advisor_conversation"
ESCALATION = "advisor_escalation"
FEEDBACK = "advisor_feedback"
HISTORY = "advisor_history"
TELEGRAM_COMMAND = "telegram_command"
TELEGRAM_INGRESS = "telegram_ingress"
TELEGRAM_LINK = "telegram_link"
TELEGRAM_REFUSAL_NOTICE = "telegram_refusal_notice"

#: (max_calls, window_seconds), justified where they are used.
LIMITS: dict[str, tuple[int, int]] = {
    # A turn occupies a worker for up to ninety seconds. Six across ten minutes is
    # at most one worker saturated, and the window has to be several times the call
    # duration or a patient serial sender is never limited at all — the timestamp
    # is taken before the ninety seconds are spent, not after.
    GENERATION: (6, 600),
    # Creating a conversation generates nothing, so it must NOT draw on the budget
    # above: the client creates one on its way to asking, so charging both made the
    # real ceiling three questions per ten minutes against a limit that reads as
    # six. Loose enough never to be the binding constraint, tight enough to stop a
    # script filling the sidebar with empty rows.
    # The planner's solver, which is NOT the adviser's. It shares the word
    # "generate" and almost nothing else: measured at 0.09 s per call against a
    # language-model turn's ninety, with no external call at all. Charging it to
    # GENERATION meant six timetable regenerations locked a student out of ASKING
    # THEIR ADVISER A QUESTION for the rest of the window — a cheap door closing an
    # expensive one, which is the protection running backwards.
    #
    # Twenty is roughly two seconds of solver per ten minutes at today's catalogue
    # of 50 sections. The real ceiling is `planner_builder`'s own 8 s CP-SAT limit
    # times three methods, so a full-size catalogue could make each call two orders
    # of magnitude slower; this number is sized on measurement and should be
    # re-measured, not assumed, when the catalogue is restored.
    PLANNING: (20, 600),
    CONVERSATION: (30, 600),
    # The cost here is an adviser's attention, not CPU. Kept above the number of
    # bad turns a single frustrated session produces, because this is the only
    # route to a human and locking it is worse than reading a duplicate.
    ESCALATION: (5, 3600),
    # One upsert. The client posts twice per negative rating — the verdict, then the
    # reasons — so this is roughly thirty ratings, not sixty.
    FEEDBACK: (60, 600),
    # Deliberately loose: reading your own conversation back is not an attack, and
    # the client re-reads after every send. This is a runaway-script backstop.
    HISTORY: (240, 600),
    # Cheap commands still create Telegram sends and database rows. This budget is
    # keyed by Telegram user id because an unlinked chat has no university
    # identity yet; it is intentionally separate from every student budget.
    TELEGRAM_COMMAND: (30, 600),
    # Durable messages have a separate ingress cap in addition to the generation
    # budget. Once generation is exhausted, rate-limit replies are cheap; without
    # this cap a sender could still create unlimited terminal jobs and Bot API
    # sends while no additional model calls occur.
    TELEGRAM_INGRESS: (30, 600),
    # Link/confirm mint or probe bearer credentials, so their allowance is much
    # tighter than help/privacy. Confirmation also has a per-token wrong-code cap.
    TELEGRAM_LINK: (5, 3600),
    # Once an admission budget is exhausted, replying to every refused update
    # would turn the limiter into an unlimited Bot API sender. One notice is
    # enough to explain the refusal; subsequent overload is acknowledged silently.
    TELEGRAM_REFUSAL_NOTICE: (1, 600),
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: int = 0


def consume(budget: str, student_id: int, *, now=None) -> Decision:
    """Spend one unit of `budget` for this student.

    Counted BEFORE the work, so an expensive call cannot be started and then found
    to have been over budget. The window is a fixed rolling block rather than a
    true sliding window: one row, one lock, and the error is bounded by the window
    itself.
    """
    max_calls, window_seconds = LIMITS[budget]
    now = now or timezone.now()
    key = f"{budget}:{student_id}"

    with transaction.atomic():
        bucket, created = RateLimitBucket.objects.select_for_update().get_or_create(
            key=key, defaults={"window_start": now, "count": 1}
        )
        if created:
            return Decision(allowed=True)

        age = (now - bucket.window_start).total_seconds()
        if age >= window_seconds:
            # A new window opens. The one just ended is carried forward, weighted,
            # rather than discarded: a plain reset lets a student spend the whole
            # allowance at the end of one window and the whole of it again a second
            # later, so a limit of six per ten minutes becomes eleven in two
            # seconds — and the 429 hands them the boundary to the second.
            windows_passed = age // window_seconds
            bucket.previous_count = bucket.count if windows_passed < 2 else 0
            bucket.window_start = now
            bucket.count = 1
            bucket.save(update_fields=["window_start", "count", "previous_count"])
            return Decision(allowed=True)

        # Sliding estimate: how much of the previous window still overlaps the last
        # `window_seconds`, plus everything spent in this one.
        overlap = (window_seconds - age) / window_seconds
        estimate = bucket.previous_count * overlap + bucket.count
        if estimate >= max_calls:
            return Decision(allowed=False, retry_after=_retry_after(bucket, window_seconds, age))

        bucket.count += 1
        bucket.save(update_fields=["count"])
        return Decision(allowed=True)


def _retry_after(bucket: RateLimitBucket, window_seconds: int, age: float) -> int:
    """Long enough that retrying then can actually succeed.

    Understating it invites a client to hammer a limiter that will keep refusing;
    the browser holds its Send button for exactly this long.
    """
    return max(1, int(window_seconds - age) + 1)


def release(budget: str, student_id: int) -> None:
    """Hand back one unit, for work that turned out not to happen.

    Admission control has to charge before the work, or an expensive call is
    started and only then found to be over budget. The cost of that ordering is
    that a request which does nothing — a replay served from storage, a question
    that cannot be answered because the student's record is gone — has already
    paid. Refunding those keeps the budget a measure of work done rather than of
    requests made.

    A failed GENERATION is deliberately NOT refunded: the model was called and a
    worker was occupied, which is the resource being rationed.
    """
    if budget not in LIMITS:
        return
    with transaction.atomic():
        RateLimitBucket.objects.select_for_update().filter(
            key=f"{budget}:{student_id}", count__gt=0
        ).update(count=F("count") - 1)


def _retention() -> timedelta:
    """Twice the longest configured window.

    Hard-coding a day was safe only for as long as no window exceeded one — which
    is exactly the assumption the previous sweep made and lost.
    """
    return timedelta(seconds=max(window for _calls, window in LIMITS.values()) * 2)


def purge_expired(older_than: timedelta | None = None) -> int:
    """Drop buckets nobody is counting against any more.

    A housekeeping call, not a hot path — the previous implementation swept every
    key against whichever window the CALLING endpoint happened to use, so any
    window longer than the shortest one configured anywhere was fictional.
    """
    cutoff = timezone.now() - (older_than or _retention())
    deleted, _ = RateLimitBucket.objects.filter(window_start__lt=cutoff).delete()
    return deleted
