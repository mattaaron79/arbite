"""The ticket lifecycle: claims, attempts, and the guards every acquisition path shares.

Three modules own coordination policy, and the split is the whole design:

- `store.py` records attempts, claims, receipts and events, and answers questions
  about them (what is active, what a receipt staged).
- `app.py` holds operations that touch the coordination store *alone* -- the
  workspace binding, the event stream.
- this module holds the operations that touch **both** stores, because they are
  decisions about a *ticket* and about the *attempt* doing the work: claiming,
  adopting a legacy ticket, ending an attempt when work stops, and the guards that
  keep a generic setter or a batch claim from routing around those transitions.

The rules that live here rather than in argparse, and why:

- **Readiness is a claim guard, not just a discovery filter.** `arbite list next`
  filters on it, and the claim re-checks it, so naming a ticket directly cannot
  bypass a prerequisite. The plan says a hard rule belongs in the operation; a
  filter in `list next` would be advice.
- **One active attempt per ticket.** The ticket sink's compare-and-swap is what
  makes a *claim race* serial -- both sinks enforce `Expect(status, assignee)`
  inside their own atomic write -- and the attempt is created afterwards, in one
  coordination transaction that refuses to double-book. Two racing claims therefore
  produce one winner, and the loser may not have written anything.
- **A generation is revoked, never inferred.** A takeover requires an explicit
  reason, ends the old attempt as `interrupted` and appends a revocation event; the
  guard that later file operations present (`require_attempt`) refuses a generation
  that is no longer current. Nothing here decides that a worker is *gone* -- that
  judgement is the caller's, which is why `--force` demands the reason.
- **Timestamps are recorded, never interpreted.** Attempts keep created, activity
  and end times so a later stale policy has data; nothing in this build expires an
  attempt, heartbeats one, or reassigns work because a clock moved.

Two storage domains, one written order. A ticket lives in a sink and an attempt
lives in the coordination store, and no transaction spans both (the durability
section of the plan says a SQL transaction cannot commit both). So an acquisition
is ordered: guard -> ticket compare-and-swap -> attempt in one coordination
transaction. The consequences are stated honestly rather than hidden:

- A lost claim writes **nothing** anywhere: the refusal is the existing
  compare-and-swap text plus the holder's attempt, which is the fact a caller needs
  to pick other work.
- A claim whose *attempt* write fails after the ticket write leaves a ticket that
  is `in_progress` with no attempt. That is exactly the legacy state this module
  adopts (`arbite attempt adopt`), so the recovery path already exists and no
  history is invented for it.
- A lifecycle command that ends an attempt and then loses the ticket's
  compare-and-swap says so: the refusal states that the attempt was already ended
  and tells the caller to re-read.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional, Sequence

from .. import schema
from ..errors import (
    Busy,
    Conflict,
    CoordinationError,
    LifecycleRequired,
    LostRace,
    NotReady,
    StaleGeneration,
    TicketError,
    TicketNotFound,
)
from ..sinks import Expect
from .app import CoordinationApp
from .records import (
    ATTEMPT_ACTIVE,
    WorkAttempt,
    new_id,
    parse_utc,
    utc_now,
)
from .results import REFUSAL_INDENT, register_next_actions, succeeded

# Event kinds this module appends. The convention is `<subject>.<verb>`, and the
# category names the stream the event belongs to (see `records.EVENT_CATEGORIES`):
# a lifecycle event is about a *ticket* changing state, an attempt event is about
# the work's own history. Reads of the stream are the events slice's surface; these
# are the rows it prints.
ATTEMPT_STARTED = "attempt.started"
ATTEMPT_ADOPTED = "attempt.adopted"
ATTEMPT_ENDED = "attempt.ended"
ATTEMPT_REVOKED = "attempt.revoked"
ATTEMPT_INVALIDATED = "attempt.invalidated"
TICKET_CLAIMED = "ticket.claimed"
TICKET_REOPENED = "ticket.reopened"

#: The attempt states a lifecycle command ends an attempt with, and the outcome it
#: records beside it. Deliberately three different stories rather than one: a
#: yielding worker *released* the work, a blocker or an administrator *interrupted*
#: it, and only the future slice that finishes work marks an attempt `finished`.
STATE_RELEASED = "released"
STATE_INTERRUPTED = "interrupted"

#: The statuses a generic setter may not reach while an attempt is active, each with
#: the words the refusal uses and the command that owns the transition. The frozen
#: LC5 transcript pins the `closed` entry word for word.
SETTER_STATUS_OWNERS = {
    "closed": ("close", "arbite close {id}"),
    "open": ("re-open", "arbite release {id} --agent {agent}"),
    "blocked": ("block", 'arbite block {id} --reason "<why>"'),
    "shelved": ("shelve", 'arbite shelve {id} --reason "<why>"'),
}

# The indent a wrapped refusal's continuation line uses lives in `results`, beside the
# label it comes from, so this module and the path validation both wrap the same way.

# Hints for the three refusals this module raises, registered so a bare raise still
# carries a `next:` line. Every operation here attaches its own, computed from the
# state at failure time (the ticket that lost the race, the filters that would find
# workable work); these are only the fallbacks that are true whatever caused it.
register_next_actions(
    "not_ready",
    ["'arbite list next' to see workable tickets, or 'arbite deps <id>' for the chain"],
)
register_next_actions(
    "lost_race",
    ["'arbite show <id>' to re-read the ticket and who holds it"],
)
register_next_actions(
    "lifecycle_required",
    ["'arbite show <id>' to read the ticket, then run the lifecycle command the refusal names"],
)


def attempt_payload(attempt: WorkAttempt) -> dict:
    """One attempt as every `--json` payload in this surface reports it.

    The field names are the frozen CL1 payload's: `workspace` rather than
    `workspace_id` (it is the workspace the attempt belongs to, not a lookup key)
    and `started` in UTC, exactly as stored."""
    return {
        "id": attempt.id,
        "generation": attempt.generation,
        "state": attempt.state,
        "workspace": attempt.workspace_id,
        "started": attempt.started,
    }


def local_time(timestamp: str) -> str:
    """A stored UTC timestamp as the `HH:MM:SS` a report prints beside a record."""
    return parse_utc(timestamp).astimezone().strftime("%H:%M:%S")


class TicketLifecycle:
    """The ticket-and-attempt operations, for one sink and one coordination store."""

    def __init__(self, sink, app: CoordinationApp):
        self.sink = sink
        self.app = app
        self.store = app.store

    # ------------------------------------------------------------------
    # Facts later slices ask about
    # ------------------------------------------------------------------

    def workspace_id(self) -> str:
        """The recorded workspace an attempt must name.

        A store with no binding cannot be the home of an attempt: the workspace is
        what makes "these attempts belong to this checkout" answerable, and
        `arbite init` is what records it."""
        workspace = self.store.get_workspace()
        if workspace is None:
            raise CoordinationError(
                "this coordination store has no workspace binding, so an attempt cannot "
                "name one (run 'arbite init' in this project)"
            )
        return workspace.id

    def active_attempt(self, ticket_id: str) -> Optional[WorkAttempt]:
        """The active attempt for a ticket, or None.

        Two active attempts is the state the one-active-attempt rule forbids, and it
        is reported rather than silently resolved: picking one of them would be
        inventing an ownership decision."""
        active = self.store.active_attempts(ticket_id)
        if len(active) > 1:
            names = ", ".join(sorted(attempt.id for attempt in active))
            raise CoordinationError(
                f"ticket {ticket_id} has {len(active)} active attempts ({names}); one "
                "active attempt per ticket is the rule this store is supposed to hold -- "
                "inspect them with 'arbite events' before claiming anything"
            )
        return active[0] if active else None

    def require_attempt(
        self, ticket_id: str, attempt_id: str, generation: Optional[int] = None
    ) -> WorkAttempt:
        """The attempt a file operation names, verified to still be current.

        The public guard for every later command that carries an attempt id (file
        claims and mutations, tic-9b57 and tic-60c7): the attempt must exist, belong
        to this ticket, be active, and -- when the caller presents one -- still be at
        the generation it read. `StaleGeneration` is outcome 5, because the caller's
        correct response is to re-read rather than to retry the same token.
        """
        attempt = self.store.get_attempt(attempt_id)
        if attempt is None:
            raise StaleGeneration(
                f"attempt {attempt_id} does not exist in this store",
                [
                    f"'arbite show {ticket_id}' to read the ticket, then claim it to get a "
                    "current attempt id"
                ],
            )
        if attempt.ticket_id != ticket_id:
            raise StaleGeneration(
                f"attempt {attempt_id} belongs to {attempt.ticket_id}, not {ticket_id}",
                [f"'arbite show {attempt.ticket_id}' to work the ticket that attempt owns"],
            )
        if generation is not None and attempt.generation != generation:
            raise StaleGeneration(
                f"attempt {attempt_id} is at generation {attempt.generation}, not "
                f"{generation}: it was revoked and restarted since you read it",
                [f"'arbite show {ticket_id}' to re-read, then claim it again"],
            )
        if not attempt.is_active:
            raise StaleGeneration(
                f"attempt {attempt_id} generation {attempt.generation} is no longer "
                f"current ({attempt.ticket_id} is {attempt.state})",
                [
                    f"'arbite show {ticket_id}' to read the ticket, then claim it again for "
                    "a current attempt"
                ],
            )
        return attempt

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    def claim(
        self,
        ticket,
        agent: str,
        *,
        force: bool = False,
        reason: Optional[str] = None,
    ):
        """Claim a ticket for `agent`, creating the attempt that owns the work.

        The guards run in the order a caller can act on them: something to go on
        with (readiness), something to claim (the state), and nothing already doing
        it (the one-active-attempt rule). Then the ticket write, which is the
        acquisition's serialisation point, and finally the attempt and its events in
        one coordination transaction -- see this module's docstring for why that
        order and what each failure leaves behind.

        Claiming a ticket this worker already holds is idempotent and returns the
        attempt it already has: a re-claim after a context switch is not a second
        attempt, and the generation stays what it was."""
        agent = (agent or "").strip()
        if not agent:
            raise TicketError("--agent must name the worker claiming the ticket")
        if force and not (reason or "").strip():
            raise LifecycleRequired(
                f"--force takes {ticket.id} over from whoever has it, so it needs a "
                "--reason: an administrative override is recorded, and the reason is the "
                "only record of why the previous worker lost the ticket",
                [f"'arbite claim {ticket.id} --agent {agent} --force --reason \"<why>\"'"],
            )

        holder = self.active_attempt(ticket.id)
        if (
            holder is not None
            and holder.worker_id == agent
            and not force
            and ticket.status == "in_progress"
            and ticket.assignee == agent
        ):
            # Already mine: a re-claim after a context switch is the same attempt, not
            # a second one, so nothing is written and the generation is unchanged.
            return self._claimed_result(ticket, holder, agent, revoked=[])

        if not force:
            self._guard_ready(ticket)
            self._guard_classified(ticket)

        violations = self._claim_violations(ticket, agent)
        if violations and not force:
            raise self._lost_race(ticket, violations, agent, holder)
        if holder is not None and not force:
            raise self._attempt_in_the_way(ticket, holder, agent)

        # The ticket write is the serialisation point: whichever process wins this
        # exchange owns the ticket, and the loser is told who beat it.
        expect = Expect(status=ticket.status, assignee=ticket.assignee)
        taken_from = ticket.assignee if force and ticket.assignee != agent else None
        if taken_from:
            schema.append_note(ticket, agent, f"Claim taken over from {taken_from} (--force).")
        ticket.status = "in_progress"
        ticket.assignee = agent
        ticket.updated = schema.now()
        try:
            self.sink.update(ticket, expect=expect)
        except Conflict:
            fresh = self.sink.get(ticket.id, unique=True)
            raise self._lost_race(
                fresh,
                self._claim_violations(fresh, agent),
                agent,
                self.active_attempt(fresh.id),
            )

        attempt, revoked = self._start_attempt(
            ticket,
            agent,
            event_kind=ATTEMPT_STARTED,
            ticket_event=TICKET_CLAIMED,
            revoke_reason=reason if force else None,
        )
        flagged = self._flag_if_prerequisite_reopened(ticket, attempt)
        return self._claimed_result(
            ticket,
            attempt,
            agent,
            revoked=revoked,
            taken_from=taken_from,
            reopened=flagged,
        )

    def adopt(self, ticket, agent: str):
        """Record the attempt for work that started before arbite tracked attempts.

        Deliberately explicit, and deliberately quiet about the past: the attempt
        starts *now*, and the receipt says that no prior activity is implied by it.
        A ticket already held by a live attempt is refused as busy rather than
        adopted a second time, because that state has no honest history to write."""
        agent = (agent or "").strip()
        if not agent:
            raise TicketError("--agent must name the worker doing the work")
        if ticket.status != "in_progress":
            raise TicketError(
                f"ticket {ticket.id} is not in_progress (status: {ticket.status}); adoption "
                "records an attempt for work that is already underway, so it only applies "
                "to a ticket that was left in_progress before attempts existed"
            )
        holder = self.active_attempt(ticket.id)
        if holder is not None:
            raise Busy(
                f"{ticket.id} already has an active attempt {holder.id} "
                f"({holder.worker_id}, generation {holder.generation}), so there is "
                "nothing to adopt",
                reason="attempt_held",
            )
        if ticket.assignee and ticket.assignee != agent:
            failure = LostRace(
                f"ticket {ticket.id} is assigned to {ticket.assignee}, not {agent}: an "
                "adopted attempt has to name the worker the ticket already names",
                [f"'arbite claim {ticket.id} --agent {agent} --force --reason \"<why>\"'"],
            )
            raise failure

        attempt, _ = self._start_attempt(
            ticket, agent, event_kind=ATTEMPT_ADOPTED, ticket_event=None
        )
        return succeeded(
            lines=[
                f"adopted {ticket.id} for {agent}: attempt {attempt.id} created "
                f"(generation {attempt.generation})",
                "no prior activity is implied by this record; the ticket was already "
                "in_progress when arbite began tracking attempts",
            ],
            data={
                "id": ticket.id,
                "status": ticket.status,
                "assignee": ticket.assignee,
                "path": self._location(ticket.id),
                "attempt": attempt_payload(attempt),
            },
            next_actions=[
                f"arbite file claim <path> --ticket {ticket.id} --attempt {attempt.id}"
            ],
            text_hint=(
                "next: claim its files before changing them -- 'arbite file list' then "
                f"'arbite file claim <path> --ticket {ticket.id} --attempt {attempt.id}'"
            ),
        )

    def begin_attempt(
        self,
        ticket,
        agent: str,
        *,
        event_kind: str = ATTEMPT_STARTED,
        ticket_event: Optional[str] = TICKET_CLAIMED,
        result: Optional[str] = None,
    ) -> WorkAttempt:
        """Record a fresh attempt for a ticket a command has just put into play.

        The shared half of every acquisition path that is not `claim` itself --
        `promote --agent` classifying and claiming in one write, `unblock` resuming
        blocked work -- so those paths create the same durable attempt, with the same
        one-active-attempt check, as a direct claim."""
        attempt, _ = self._start_attempt(
            ticket, agent, event_kind=event_kind, ticket_event=ticket_event, result=result
        )
        return attempt

    def file_hint(self, ticket_id: str, attempt_id: str) -> str:
        """The next action after acquiring work, as the bare command.

        Public because the commands that acquire work (claim, adopt, promote) all hand
        the same instruction to the caller and the attempt id in it is the token later
        file commands need. This is what JSON publishes as `next_actions`; the *text*
        sentence around it is `file_hint_text`."""
        return (
            f"arbite file claim <path>... --ticket {ticket_id} --attempt {attempt_id}"
        )

    def file_hint_text(self, ticket_id: str, attempt_id: str) -> str:
        """The `next:` line a fresh acquisition prints: the command, and why it matters."""
        return (
            "next: claim the files you will change -- "
            f"'{self.file_hint(ticket_id, attempt_id)}'"
        )

    # ------------------------------------------------------------------
    # Ending an attempt: the lifecycle commands
    # ------------------------------------------------------------------

    def release(self, ticket, agent: str, reason: str = ""):
        """Give a ticket back: end the attempt and return it to the open pool."""
        ended = self.end_attempt(
            ticket, state=STATE_RELEASED, outcome="released", handoff=reason or None, actor=agent
        )
        previous = ticket.assignee
        expect = Expect(status=ticket.status, assignee=ticket.assignee)
        message = "Released." if not reason else f"Released: {reason}"
        schema.append_note(ticket, agent, message)
        ticket.assignee = None
        ticket.blocked_by = None
        ticket.status = "open"
        ticket.updated = schema.now()
        self._write(ticket, expect, action="release", attempt_ended=ended is not None)
        owner = f" (was {previous})" if previous else ""
        lines = [f"released {ticket.id}{owner} -> {self._location(ticket.id)}"]
        lines.extend(self._ending_lines(ended))
        return succeeded(lines=lines, data=self._lifecycle_data(ticket, ended))

    def block(self, ticket, reason: str):
        """Block a ticket: the attempt is interrupted and nothing is undone."""
        ended = self.end_attempt(
            ticket, state=STATE_INTERRUPTED, outcome="blocked", handoff=reason
        )
        expect = Expect(status=ticket.status, assignee=ticket.assignee)
        ticket.blocked_by = reason
        ticket.status = "blocked"
        ticket.updated = schema.now()
        self._write(ticket, expect, action="block", attempt_ended=ended is not None)
        lines = [f"blocked {ticket.id} ({reason}) -> {self._location(ticket.id)}"]
        lines.extend(self._ending_lines(ended))
        return succeeded(lines=lines, data=self._lifecycle_data(ticket, ended))

    def shelve(self, ticket, reason: str = ""):
        """Shelve a ticket: parked, and the attempt ends with it."""
        ended = self.end_attempt(
            ticket, state=STATE_RELEASED, outcome="shelved", handoff=reason or None
        )
        expect = Expect(status=ticket.status, assignee=ticket.assignee)
        ticket.status = "shelved"
        ticket.updated = schema.now()
        message = "Shelved." if not reason else f"Shelved: {reason}"
        schema.append_note(ticket, "system", message)
        self._write(ticket, expect, action="shelve", attempt_ended=ended is not None)
        lines = [f"shelved {ticket.id} -> {self._location(ticket.id)}"]
        lines.extend(self._ending_lines(ended))
        return succeeded(lines=lines, data=self._lifecycle_data(ticket, ended))

    def reopen(self, ticket, agent: str, reason: str, dependents: Sequence = ()):
        """Reopen a ticket, leaving the past in the past.

        Old attempts stay historical and nothing is re-acquired. A dependency that
        gets reopened is the interesting case: any *running* work that depends on it
        is told -- an invalidation event naming the attempt -- but is not silently
        undone, because the bytes on disk are real and only the caller can decide what
        to do about them.

        The dependent scan runs **twice**: once before this ticket's own state change
        and once after it, because a claim that is committing at the same moment can
        appear in the window between them. The claim verifies after its own commit in
        return (`_flag_if_prerequisite_reopened`), so whichever of the two commits
        landed second always records the other's effect -- which is the serial order
        this race has, given that neither store can be locked together."""
        ended = self.end_attempt(
            ticket, state=STATE_INTERRUPTED, outcome="reopened", handoff=reason, actor=agent
        )
        self._record_ticket_state(ticket, TICKET_REOPENED, agent, reason)
        invalidated = self._invalidate_dependents(ticket, dependents, agent)
        expect = Expect(status=ticket.status, assignee=ticket.assignee)
        ticket.closed = None
        ticket.blocked_by = None
        ticket.status = "open"
        ticket.updated = schema.now()
        schema.append_note(ticket, agent, f"Reopened: {reason}.")
        self._write(ticket, expect, action="reopen", attempt_ended=ended is not None)
        late = self._invalidate_dependents(
            ticket, dependents, agent, flagged={attempt.id for _, attempt in invalidated}
        )
        lines = [f"reopened {ticket.id} -> {self._location(ticket.id)}"]
        lines.extend(self._ending_lines(ended))
        for dependent, attempt in [*invalidated, *late]:
            lines.append(
                f"invalidated: attempt {attempt.id} on {dependent.id} depends on "
                f"{ticket.id} (running work was flagged, not undone)"
            )
        return succeeded(
            lines=lines,
            data={
                **self._lifecycle_data(ticket, ended),
                "invalidated": len(invalidated) + len(late),
            },
        )

    def end_attempt(
        self,
        ticket,
        *,
        state: str,
        outcome: str,
        handoff: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> Optional[WorkAttempt]:
        """End the ticket's active attempt, if it has one, and record the event.

        The optimistic write is what makes two commands that end the same attempt
        serial: one wins, the other is told (outcome 5) that the record moved, rather
        than both writing a state the history then contradicts."""
        active = self.active_attempt(ticket.id)
        if active is None:
            return None
        ended = replace(
            active,
            state=state,
            outcome=outcome,
            handoff=handoff if handoff is not None else active.handoff,
            ended=utc_now(),
            last_activity=utc_now(),
        )
        revision = self.store.revision("attempt", active.id)
        with self.store.transaction() as txn:
            txn.replace_record(ended, expect_revision=revision)
            txn.append_event(
                ATTEMPT_ENDED,
                "attempt",
                subject=ended.id,
                result=state,
                ticket_id=ticket.id,
                attempt_id=ended.id,
                actor=actor,
                payload={"outcome": outcome, "handoff": handoff or ""},
            )
        return ended

    # ------------------------------------------------------------------
    # Guards for the generic setters
    # ------------------------------------------------------------------

    def guard_status_change(self, ticket, new_status: str) -> None:
        """Refuse a status change that would strand an active attempt.

        Only the statuses whose lifecycle command exists are refused, and each
        refusal names that command. A status the attempt survives (`in_progress`
        kept, `review` awaiting the cascade slice) is left alone rather than blocked
        behind a command that does not exist yet."""
        active = self.active_attempt(ticket.id)
        if active is None or new_status == ticket.status:
            return
        owner = SETTER_STATUS_OWNERS.get(new_status)
        if owner is None:
            return
        verb, template = owner
        command = template.format(id=ticket.id, agent=active.worker_id)
        raise LifecycleRequired(
            f"'set status' cannot {verb} a ticket that has an active attempt and file "
            f"claims;\n{REFUSAL_INDENT}use '{command}' so the attempt ends and its claims "
            "are released",
            [f"'{command}'"],
        )

    def guard_assignee_change(self, ticket, new_assignee: Optional[str]) -> None:
        """Refuse moving a ticket to a different worker while an attempt is running.

        An assignee and an attempt's worker disagreeing is how attribution rots: the
        takeover has to be the explicit, reasoned one."""
        active = self.active_attempt(ticket.id)
        if active is None or (new_assignee or None) == (ticket.assignee or None):
            return
        wanted = new_assignee or "nobody"
        hand_back = f"'arbite release {ticket.id} --agent {active.worker_id}'"
        if new_assignee:
            takeover = (
                f"'arbite claim {ticket.id} --agent {new_assignee} --force --reason \"<why>\"' "
                "to take the work over, or "
            )
        else:
            # Clearing the assignee while somebody is working is only ever a hand-back:
            # there is no new worker to name.
            takeover = ""
        raise LifecycleRequired(
            f"'set assignee' cannot move {ticket.id} from {active.worker_id} to {wanted} "
            f"while attempt {active.id} is active;\n"
            f"{REFUSAL_INDENT}use {takeover}{hand_back} to hand it back",
            [f"{takeover}{hand_back}"],
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _unmet_now(self, ticket) -> list:
        """The dependencies of `ticket` that are not closed *right now*.

        Read from the ticket store at the moment of asking rather than from a snapshot
        the caller took, because "is this claimable" is exactly the question a
        dependency edit races with. A dependency that does not resolve is skipped, the
        same way the queue's readiness filter skips it: a dangling id is `doctor`'s
        finding, not a permanent block."""
        unmet = []
        for dependency_id in ticket.depends_on:
            try:
                dependency = self.sink.get(dependency_id)
            except TicketNotFound:
                continue
            if dependency.status != "closed":
                unmet.append(dependency)
        return unmet

    def _guard_ready(self, ticket) -> None:
        """Refuse a claim whose prerequisites are not closed (CL2).

        Readiness is a property of every known ticket, including the ones a filter
        would exclude, so it is answered from the store rather than from a filtered
        set -- and it is asked *again* immediately before the attempt is written (see
        `_start_attempt`), which is what makes a dependency edit and a claim two
        operations that observe each other instead of neither."""
        unmet = self._unmet_now(ticket)
        if not unmet:
            return
        reasons = ", ".join(
            f"depends_on {dependency.id} is {dependency.status} (not closed)"
            for dependency in unmet
        )
        raise NotReady(
            f"{ticket.id} is not ready: {reasons}",
            [self._workable_hint(ticket), f"'arbite deps {ticket.id}' to see the chain"],
        )

    def _workable_hint(self, ticket) -> str:
        """`arbite list next` narrowed the way this ticket is classified (CL2).

        The frozen hint names the tier and the epic, which are the two filters that
        select this epic's workable queue."""
        filters = [f"--{name} {value}" for name, value in (("tier", ticket.tier), ("epic", ticket.epic)) if value]
        return (
            f"'arbite list next {' '.join(filters)}' for workable tickets"
            if filters
            else "'arbite list next' for workable tickets"
        )

    def _guard_classified(self, ticket) -> None:
        """Refuse a claim on work that is still a placeholder.

        `arbite list next` never offers such a ticket -- a raw capture has status
        `raw`, and `promote` refuses to keep its placeholders -- but a status change
        can put a half-classified ticket into the open queue, and a claim is where
        that stops being harmless: it would create an attempt on work nobody has
        described yet."""
        placeholders = [
            name
            for name, value in (
                ("title", ticket.title if schema.is_raw_title_placeholder(ticket.title) else None),
                ("tier", ticket.tier if schema.is_placeholder(ticket.tier) else None),
                ("domain", ticket.domain if schema.is_placeholder(ticket.domain) else None),
            )
            if value is not None
        ]
        if not placeholders:
            return
        if schema.is_raw_title_placeholder(ticket.title):
            hint = (
                f"'arbite promote {ticket.id} --title \"<title>\" --tier <tier> "
                "--domain <domain>' to classify it"
            )
        else:
            hint = (
                f"'arbite set {ticket.id} tier <tier> domain <domain>' to replace the "
                "placeholder text"
            )
        raise LifecycleRequired(
            f"{ticket.id} is not classified yet: {', '.join(placeholders)} still hold "
            "placeholder text, so it cannot be claimed as work",
            [hint],
        )

    def _claim_violations(self, ticket, agent: Optional[str] = None) -> list:
        """Why the ticket may not be claimed, in the compare-and-swap vocabulary.

        An assignee that is the *claiming* worker is not a violation: `reopen` leaves
        the assignee in place, and re-claiming work you already own is not a race. Any
        other assignee is, and the wording is the frozen CL3 refusal's."""
        violations = []
        if ticket.status != "open":
            violations.append(f"status is '{ticket.status}', expected 'open'")
        if ticket.assignee and ticket.assignee != agent:
            violations.append(f"assignee is {ticket.assignee}, expected unassigned")
        return violations

    def _lost_race(self, ticket, violations, agent: str, holder: Optional[WorkAttempt]):
        """The claim refusal: the ticket's state, then who is already on it (CL3).

        Built from the state at failure time, whether the loss was noticed before the
        write or by the compare-and-swap itself, so a caller that lost a race reads
        the same thing as one that arrived second."""
        if not violations:
            # The compare-and-swap lost because the state changed and changed back
            # (claimed and released while this claim was in flight). Nothing of ours
            # was written, so the caller simply runs it again.
            return LostRace(
                f"{ticket.id} changed while this claim was being written and is "
                f"'{ticket.status}' again; this claim wrote nothing",
                [f"'arbite claim {ticket.id} --agent {agent}' to try again"],
            )
        reason = f";\n{REFUSAL_INDENT}".join(violations)
        lines = [
            f"{ticket.id} is not in the expected state ({reason}); re-read it with "
            f"'arbite show {ticket.id}'"
        ]
        if holder is not None:
            lines.append(
                f"attempt held by: {holder.id} ({holder.worker_id}), started "
                f"{local_time(holder.started)}, generation {holder.generation}"
            )
        return LostRace(
            "\n".join(lines),
            [f"'arbite list next --claim {agent}' to take the next workable ticket instead"],
        )

    def _attempt_in_the_way(self, ticket, holder: WorkAttempt, agent: str):
        """The ticket looks claimable but an attempt already owns it.

        Outcome 4 rather than an error: the caller's correct response is to pick
        other work, and nothing changed."""
        failure = Busy(
            f"{ticket.id} already has an active attempt {holder.id} ({holder.worker_id}, "
            "generation {holder.generation}); one active attempt per ticket, so it cannot "
            "be claimed again",
            reason="attempt_held",
        )
        failure.next_actions = (
            f"'arbite claim {ticket.id} --agent {agent} --force --reason \"<why>\"' to revoke "
            f"{holder.id} and start a fresh attempt",
            f"'arbite list next --claim {agent}' for other work instead",
        )
        return failure

    def _start_attempt(
        self,
        ticket,
        agent: str,
        *,
        event_kind: str,
        ticket_event: Optional[str],
        result: Optional[str] = None,
        revoke_reason: Optional[str] = None,
        payload: Optional[dict] = None,
    ):
        """Create the attempt and its events in **one** commit.

        The attempt read happens here, inside the operation that is about to write,
        which is as close to "checked in the same transaction as the acquisition" as
        two storage domains allow: the coordination store's own commit is the
        serialisation this rule needs, because the attempt is what a second claimant
        would collide with.

        With `revoke_reason`, whatever is active is revoked as `interrupted` first:
        that is a takeover, and the reason is kept with the attempt it ended as well
        as in the revocation event."""
        stored = self.store.active_attempts(ticket.id)
        revoked = []
        with self.store.transaction() as txn:
            if revoke_reason is None:
                # Asked again here, as late as the acquisition can: the answer the
                # guard acted on is already old, and a prerequisite reopened in
                # between must not pass unnoticed (the reverse order -- the claim
                # landing first -- is what `reopen`'s invalidation event is for).
                self._guard_ready(ticket)
            now = utc_now()
            for other in stored:
                if revoke_reason is None:
                    raise self._attempt_in_the_way(ticket, other, agent)
                ended = replace(
                    other,
                    state=STATE_INTERRUPTED,
                    outcome="taken_over",
                    handoff=revoke_reason,
                    ended=now,
                    last_activity=now,
                )
                txn.replace_record(ended, expect_revision=self.store.revision("attempt", other.id))
                txn.append_event(
                    ATTEMPT_REVOKED,
                    "attempt",
                    subject=ended.id,
                    result=f"gen {ended.generation}",
                    ticket_id=ticket.id,
                    attempt_id=ended.id,
                    actor=agent,
                    payload={"generation": ended.generation, "reason": revoke_reason},
                )
                revoked.append(ended)
            attempt = WorkAttempt(
                id=self._new_attempt_id(),
                ticket_id=ticket.id,
                worker_id=agent,
                workspace_id=self.workspace_id(),
                generation=1,
                state=ATTEMPT_ACTIVE,
                started=now,
                last_activity=now,
            )
            txn.put_record(attempt)
            txn.append_event(
                event_kind,
                "attempt",
                subject=attempt.id,
                result=f"gen {attempt.generation}",
                ticket_id=ticket.id,
                attempt_id=attempt.id,
                actor=agent,
                payload=payload,
            )
            if ticket_event is not None:
                txn.append_event(
                    ticket_event,
                    "lifecycle",
                    subject=ticket.id,
                    result=result or agent,
                    ticket_id=ticket.id,
                    attempt_id=attempt.id,
                    actor=agent,
                )
        return attempt, revoked

    def _invalidate_dependents(self, reopened, dependents, actor: Optional[str], flagged=()):
        """Flag the running work that depends on a reopened ticket.

        One event per active attempt, in one commit, and nothing else: the attempt
        stays active and its bytes stay where they are, because a reopened
        prerequisite says the *work* may now be wrong, not that the worker did
        anything wrong.

        `flagged` are the attempt ids an earlier pass (or the claim's own
        post-commit check) already named, and any attempt that already carries an
        invalidation for this dependency is skipped too, so one race cannot produce two
        identical events."""
        already = set(flagged) | self._invalidated_attempts(reopened.id)
        affected = []
        for dependent in dependents:
            attempt = self.active_attempt(dependent.id)
            if attempt is not None and attempt.id not in already:
                affected.append((dependent, attempt))
        if not affected:
            return []
        with self.store.transaction() as txn:
            for dependent, attempt in affected:
                txn.append_event(
                    ATTEMPT_INVALIDATED,
                    "lifecycle",
                    subject=dependent.id,
                    result=f"depends on {reopened.id}",
                    ticket_id=dependent.id,
                    attempt_id=attempt.id,
                    actor=actor,
                    payload={"dependency": reopened.id, "generation": attempt.generation},
                )
        return affected

    def _invalidated_attempts(self, reopened_id: str) -> set:
        """The attempts already flagged as depending on `reopened_id`."""
        return {
            event.attempt_id
            for event in self.store.events()
            if event.kind == ATTEMPT_INVALIDATED
            and event.attempt_id is not None
            and event.payload.get("dependency") == reopened_id
        }

    def _flag_if_prerequisite_reopened(self, ticket, attempt) -> list:
        """The claim's half of the claim-versus-reopen race, verified *after* the fact.

        Two storage domains cannot be locked together, so each side checks after it has
        committed instead: this one asks whether a prerequisite is still closed now, and
        if it is not, the attempt it just recorded is flagged and the receipt says so.
        Whichever of the two commits landed second therefore records the other's effect,
        which is what turns "a dependency edit racing a claim" into one documented order
        rather than a silent window."""
        reopened = self._unmet_now(ticket)
        if not reopened:
            return []
        with self.store.transaction() as txn:
            for dependency in reopened:
                txn.append_event(
                    ATTEMPT_INVALIDATED,
                    "lifecycle",
                    subject=ticket.id,
                    result=f"depends on {dependency.id}",
                    ticket_id=ticket.id,
                    attempt_id=attempt.id,
                    actor=attempt.worker_id,
                    payload={"dependency": dependency.id, "generation": attempt.generation},
                )
        return reopened

    def _record_ticket_state(self, ticket, kind: str, actor: Optional[str], result: str) -> None:
        """Append one lifecycle event about a ticket, on its own.

        The stream is where "this ticket was reopened at 09:14" stays answerable
        without reading the ticket's notes, and it is the durable effect the other side
        of a race can see."""
        with self.store.transaction() as txn:
            txn.append_event(
                kind,
                "lifecycle",
                subject=ticket.id,
                result=result,
                ticket_id=ticket.id,
                actor=actor,
            )

    def _new_attempt_id(self) -> str:
        return new_id("attempt", {attempt.id for attempt in self.store.records("attempt")})

    def _claimed_result(
        self,
        ticket,
        attempt: WorkAttempt,
        agent: str,
        *,
        revoked: Sequence = (),
        taken_from: Optional[str] = None,
        reopened: Sequence = (),
    ):
        """What `claim` reports: the ticket, the attempt, and what the caller does next."""
        takeover = f" (taken over from {taken_from})" if taken_from else ""
        lines = [
            f"claimed {ticket.id} for {agent} -> {self._location(ticket.id)}{takeover}"
        ]
        for ended in revoked:
            lines.append(
                f"revoked: attempt {ended.id} generation {ended.generation} (reason recorded)"
            )
        for dependency in reopened:
            lines.append(
                f"note: {dependency.id} was reopened while this claim was being "
                "recorded, so the attempt is flagged as depending on it "
                "(see 'arbite events'); nothing was undone"
            )
        label = "new attempt" if revoked else "attempt"
        lines.append(
            f"{label}: {attempt.id} (generation {attempt.generation}, ticket {ticket.id}, "
            f"workspace {attempt.workspace_id})"
        )
        return succeeded(
            lines=lines,
            data={
                **ticket.to_dict(self._location(ticket.id)),
                "attempt": attempt_payload(attempt),
                **(
                    {"revoked": [attempt_payload(ended) for ended in revoked]}
                    if revoked
                    else {}
                ),
            },
            next_actions=[self.file_hint(ticket.id, attempt.id)],
            text_hint=self.file_hint_text(ticket.id, attempt.id),
        )

    def _ending_lines(self, ended: Optional[WorkAttempt]) -> list:
        """The lines an ended attempt adds: what stopped, and that bytes remain.

        The claim-and-receipt cascade (tic-e9ed) extends the first line with the
        file claims it releases; the bytes-remain line is already true here, because
        an attempt ends without anyone undoing what it wrote."""
        if ended is None:
            return []
        return [
            f"ended attempt {ended.id} ({ended.state})",
            "partial work is left on disk and visible; the next worker must re-read it",
        ]

    def _lifecycle_data(self, ticket, ended: Optional[WorkAttempt]) -> dict:
        return {
            "id": ticket.id,
            "status": ticket.status,
            "assignee": ticket.assignee,
            "path": self._location(ticket.id),
            "attempt": attempt_payload(ended) if ended is not None else None,
        }

    def _write(self, ticket, expect, *, action: str, attempt_ended: bool) -> None:
        """Write the ticket, and say honestly when the write lost its race.

        A lifecycle command ends the attempt *before* it writes the ticket, so a lost
        compare-and-swap means the ticket moved on while the attempt was already
        ended -- which the refusal states, instead of claiming that nothing changed."""
        try:
            self.sink.update(ticket, expect=expect)
        except Conflict as e:
            detail = (
                "; that attempt was already ended, so this command is only half applied"
                if attempt_ended
                else ""
            )
            failure = Conflict(f"{action} {ticket.id} lost its race -- {e}{detail}")
            failure.next_actions = (
                f"'arbite show {ticket.id}' to read the state the other command left, then "
                "run the lifecycle command that fits",
            )
            raise failure

    def _location(self, ticket_id: str) -> str:
        return self.sink.location(ticket_id)

