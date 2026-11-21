//! Live polls reach the core in the order replay runs them.
//!
//! Replay runs a window's polls in reception order (`compare::merged_polls`: by `received_at`,
//! ties by `(source, channel)`, one channel's polls in archive order) and runs each tick before
//! the first poll at or after it. Live, each channel's poller fetches, archives and decodes on
//! its own, so a poll received first can be decoded last. Two pieces put them back in order:
//!
//! - [`Sequencer`]: every poll takes a ticket as its poller gets it from the fetch, before it
//!   is archived, and every timer tick takes one as it is sent. Under one lock, a poll's
//!   `received_at` is raised, if need be, to just after the last poll's and to the last tick's
//!   time, so ticket order is reception order with no ties: the archive, and so replay, sees
//!   the polls in ticket order, and every poll after a tick in ticket order is at or after it.
//!   The fetch's own stamp stands unless two polls race to their tickets (a microsecond or so).
//! - [`Reorder`]: on the core's thread, holds what arrives out of turn and releases it strictly
//!   in ticket order. Every ticket is resolved once: by its poll or tick, or by a skip (a poll
//!   with nothing for the core: an alert or archive-only channel, an error, a poller cancelled).
//!   A ticket a released item has waited on for `emit.poll_stall_s` is given up (one hung
//!   poller cannot freeze the core); if its poll comes after all, it runs at once, out of turn.

use std::collections::{BTreeMap, VecDeque};
use std::sync::Mutex;
use std::time::Duration;

use obehy_core::time::{Instant, TimeDelta};

/// Hands out tickets in reception order (one per worker; shared by the pollers and the ticks).
#[derive(Debug, Default)]
pub struct Sequencer {
    state: Mutex<Sequence>,
}

#[derive(Debug, Default)]
struct Sequence {
    next: u64,
    /// The earliest reception time the next poll may have.
    floor: Option<Instant>,
}

impl Sequencer {
    pub fn new() -> Sequencer {
        Sequencer::default()
    }

    /// A poll's ticket; `received_at` raised to keep reception times strictly increasing in
    /// ticket order and at or after every earlier tick.
    pub fn poll(&self, received_at: &mut Instant) -> u64 {
        let mut state = self.state.lock().expect("the sequence");
        if let Some(floor) = state.floor
            && *received_at < floor
        {
            *received_at = floor;
        }
        state.floor = Some(*received_at + TimeDelta::from_micros(1));
        state.take()
    }

    /// A tick's ticket: polls after it in ticket order are received at or after `at`.
    pub fn tick(&self, at: Instant) -> u64 {
        let mut state = self.state.lock().expect("the sequence");
        state.floor = Some(state.floor.map_or(at, |floor| floor.max(at)));
        state.take()
    }
}

impl Sequence {
    fn take(&mut self) -> u64 {
        let ticket = self.next;
        self.next += 1;
        ticket
    }
}

/// What [`Reorder`] releases.
#[derive(Debug, PartialEq, Eq)]
pub enum Released<T> {
    /// The next item in ticket order.
    Next(T),
    /// The stall guard gave up waiting for this ticket.
    Stalled(u64),
    /// This ticket's item came after the stall guard gave up on it: run it now.
    Late(u64, T),
}

#[derive(Debug)]
struct Held<T> {
    /// None: a skip.
    item: Option<T>,
    /// When it reached the core (monotonic).
    arrived: std::time::Instant,
}

/// The core's reorder buffer: items in, strictly in ticket order out.
#[derive(Debug)]
pub struct Reorder<T> {
    next: u64,
    held: BTreeMap<u64, Held<T>>,
    late: VecDeque<(u64, T)>,
    stall: Duration,
}

impl<T> Reorder<T> {
    pub fn new(stall: Duration) -> Reorder<T> {
        Reorder {
            next: 0,
            held: BTreeMap::new(),
            late: VecDeque::new(),
            stall,
        }
    }

    /// Resolve `ticket` with its item, or with a skip (None), as it reaches the core at `now`.
    pub fn push(&mut self, ticket: u64, item: Option<T>, now: std::time::Instant) {
        if ticket < self.next {
            // Given up on by the stall guard: a poll runs at once, a skip is moot.
            if let Some(item) = item {
                self.late.push_back((ticket, item));
            }
            return;
        }
        let held = Held { item, arrived: now };
        let previous = self.held.insert(ticket, held);
        debug_assert!(previous.is_none(), "ticket {ticket} resolved twice");
    }

    /// The next thing to run at `now`, if any: late items first, then the next ticket's item;
    /// skips pass silently; a ticket some held item has waited on for the stall time is given
    /// up.
    pub fn pop(&mut self, now: std::time::Instant) -> Option<Released<T>> {
        if let Some((ticket, item)) = self.late.pop_front() {
            return Some(Released::Late(ticket, item));
        }
        loop {
            match self.held.remove(&self.next) {
                Some(Held { item, .. }) => {
                    self.next += 1;
                    if let Some(item) = item {
                        return Some(Released::Next(item));
                    }
                }
                None => {
                    let deadline = self.deadline()?;
                    if now < deadline {
                        return None;
                    }
                    let ticket = self.next;
                    self.next += 1;
                    return Some(Released::Stalled(ticket));
                }
            }
        }
    }

    /// When the stall guard gives up on the missing next ticket: the stall time after the
    /// earliest held item (not skip) reached the core. None when nothing waits on it.
    pub fn deadline(&self) -> Option<std::time::Instant> {
        if self.held.contains_key(&self.next) || !self.late.is_empty() {
            return None;
        }
        self.held
            .values()
            .filter(|h| h.item.is_some())
            .map(|h| h.arrived)
            .min()
            .map(|arrived| arrived + self.stall)
    }

    /// At shutdown: everything held, in ticket order, missing tickets given up.
    pub fn drain(&mut self) -> Vec<Released<T>> {
        let mut out: Vec<Released<T>> = self
            .late
            .drain(..)
            .map(|(ticket, item)| Released::Late(ticket, item))
            .collect();
        let held = std::mem::take(&mut self.held);
        for (ticket, Held { item, .. }) in held {
            while self.next < ticket {
                out.push(Released::Stalled(self.next));
                self.next += 1;
            }
            self.next = ticket + 1;
            if let Some(item) = item {
                out.push(Released::Next(item));
            }
        }
        out
    }

    /// Items and skips held.
    pub fn len(&self) -> usize {
        self.held.len() + self.late.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const STALL: Duration = Duration::from_secs(30);

    fn all(
        order: &mut Reorder<&'static str>,
        now: std::time::Instant,
    ) -> Vec<Released<&'static str>> {
        std::iter::from_fn(|| order.pop(now)).collect()
    }

    #[test]
    fn tickets_follow_reception_and_never_tie() {
        let sequencer = Sequencer::new();
        let t = |s: i64| Instant::from_micros(s * 1_000_000);
        let mut first = t(100);
        assert_eq!(sequencer.poll(&mut first), 0);
        assert_eq!(first, t(100));
        // Same microsecond, or stamped earlier but ticketed later: just after the last.
        let mut tie = t(100);
        assert_eq!(sequencer.poll(&mut tie), 1);
        assert_eq!(tie, t(100) + TimeDelta::from_micros(1));
        let mut earlier = t(99);
        assert_eq!(sequencer.poll(&mut earlier), 2);
        assert_eq!(earlier, t(100) + TimeDelta::from_micros(2));
        let mut later = t(105);
        assert_eq!(sequencer.poll(&mut later), 3);
        assert_eq!(later, t(105));
        // A tick at 110: a poll stamped before it but ticketed after runs after it, at 110.
        assert_eq!(sequencer.tick(t(110)), 4);
        let mut slow = t(108);
        assert_eq!(sequencer.poll(&mut slow), 5);
        assert_eq!(slow, t(110));
        // A tick behind the last poll moves nothing.
        assert_eq!(sequencer.tick(t(100)), 6);
        let mut next = t(111);
        assert_eq!(sequencer.poll(&mut next), 7);
        assert_eq!(next, t(111));
    }

    #[test]
    fn out_of_order_arrivals_come_out_in_ticket_order() {
        let now = std::time::Instant::now();
        let mut order = Reorder::new(STALL);
        order.push(2, Some("c"), now);
        order.push(1, Some("b"), now);
        assert!(all(&mut order, now).is_empty());
        order.push(0, Some("a"), now);
        assert_eq!(
            all(&mut order, now),
            [
                Released::Next("a"),
                Released::Next("b"),
                Released::Next("c")
            ]
        );
        assert!(order.is_empty());
    }

    #[test]
    fn skips_resolve_their_tickets_silently() {
        let now = std::time::Instant::now();
        let mut order = Reorder::new(STALL);
        order.push(3, Some("d"), now);
        order.push(1, None, now);
        order.push(0, Some("a"), now);
        assert_eq!(all(&mut order, now), [Released::Next("a")]);
        order.push(2, None, now);
        assert_eq!(all(&mut order, now), [Released::Next("d")]);
        assert!(order.is_empty());
    }

    #[test]
    fn a_tick_waits_for_the_polls_ticketed_before_it() {
        // Sequencer and buffer together: polls 0 and 1, the tick, poll 3; poll 1 decodes last.
        let sequencer = Sequencer::new();
        let at = Instant::from_micros(0);
        let (mut p0, mut p1, mut p3) = (at, at, at);
        let tickets = [
            sequencer.poll(&mut p0),
            sequencer.poll(&mut p1),
            sequencer.tick(at),
            sequencer.poll(&mut p3),
        ];
        assert_eq!(tickets, [0, 1, 2, 3]);
        let now = std::time::Instant::now();
        let mut order = Reorder::new(STALL);
        order.push(3, Some("poll 3"), now);
        order.push(2, Some("tick"), now);
        order.push(0, Some("poll 0"), now);
        assert_eq!(all(&mut order, now), [Released::Next("poll 0")]);
        order.push(1, Some("poll 1"), now);
        assert_eq!(
            all(&mut order, now),
            [
                Released::Next("poll 1"),
                Released::Next("tick"),
                Released::Next("poll 3")
            ]
        );
    }

    #[test]
    fn the_stall_guard_gives_up_a_missing_ticket_and_runs_it_late() {
        let start = std::time::Instant::now();
        let mut order = Reorder::new(STALL);
        // Nothing waits on a missing ticket: no deadline.
        order.push(1, None, start);
        assert_eq!(order.deadline(), None);
        order.push(2, Some("c"), start + Duration::from_secs(5));
        assert_eq!(order.deadline(), Some(start + Duration::from_secs(35)));
        assert!(all(&mut order, start + Duration::from_secs(34)).is_empty());
        assert_eq!(
            all(&mut order, start + Duration::from_secs(35)),
            [Released::Stalled(0), Released::Next("c")]
        );
        // Ticket 0 comes after all: run at once; a late skip is moot.
        order.push(0, Some("a"), start + Duration::from_secs(40));
        order.push(0, None, start + Duration::from_secs(40));
        assert_eq!(order.deadline(), None);
        assert_eq!(
            all(&mut order, start + Duration::from_secs(40)),
            [Released::Late(0, "a")]
        );
        assert!(order.is_empty());
    }

    #[test]
    fn the_stall_guard_gives_up_every_ticket_an_old_item_waits_on() {
        let start = std::time::Instant::now();
        let mut order = Reorder::new(STALL);
        // 0 and 2 hang; 3 has waited since the start, 1 only came now.
        order.push(3, Some("d"), start);
        order.push(1, Some("b"), start + Duration::from_secs(30));
        assert_eq!(
            all(&mut order, start + Duration::from_secs(30)),
            [
                Released::Stalled(0),
                Released::Next("b"),
                Released::Stalled(2),
                Released::Next("d")
            ]
        );
    }

    #[test]
    fn drain_at_shutdown_releases_everything_in_order() {
        let now = std::time::Instant::now();
        let mut order = Reorder::new(STALL);
        order.push(4, Some("e"), now);
        order.push(2, Some("c"), now);
        order.push(3, None, now);
        order.push(0, Some("a"), now);
        assert_eq!(all(&mut order, now), [Released::Next("a")]);
        assert_eq!(
            order.drain(),
            [
                Released::Stalled(1),
                Released::Next("c"),
                Released::Next("e")
            ]
        );
        assert!(order.is_empty());
        // Tickets after the drain go on from where it ended.
        order.push(5, Some("f"), now);
        assert_eq!(all(&mut order, now), [Released::Next("f")]);
    }
}
