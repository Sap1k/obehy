//! What the worker does with one poll (`worker.PollHandler`): archive it (filtered if the
//! channel says so), decode the stored body as replay decodes it (`replay::decode_payload`),
//! and hand the observations of its live feeds to the core thread; an alert channel's alerts
//! are written to `rt.alert` on a connection of their own, never through the core.
//!
//! Archiving and decoding run on the blocking pool, off the core's thread. Until warm replay is
//! done the poll is only queued for the core (the backlog: the poller goes on polling); after
//! it the poller waits for the core to have run it, as Python's poller waits on its lock.
//!
//! Each poll takes its ticket ([`super::order::Sequencer`]) before anything else, which may
//! raise its `received_at` so ticket order is reception order; the core runs polls in ticket
//! order. The ticket is resolved exactly once: by the poll sent to the core, or, when it is
//! dropped unused (an alert or archive-only poll, an error, a cancelled poller), by a skip.

use std::sync::{Arc, Mutex};

use indexmap::IndexMap;
use obehy_core::alerts::Alert;
use obehy_core::config::Channel;
use obehy_core::model::Observation;
use obehy_core::time::{Instant, TimeDelta};
use obehy_sources::Decoders;
use obehy_sources::reader::json::{Json, Object};
use tokio_postgres::Client;

use super::core_loop::{Message, Reply};
use super::order::Sequencer;
use crate::archive::{ArchiveWriter, Poll};
use crate::logs::Log;
use crate::phases::Phases;
use crate::record::{ChannelStats, Filters, archive_poll};
use crate::replay::{Payload, decode_payload};
use crate::scheduler::{HandlerError, OnPoll, Stop};
use crate::store::alerts::write_alerts;

/// A poll after archiving and decoding.
enum Decoded {
    /// A facts channel's poll: its observations (none when failed or undecodable).
    Facts(Vec<Arc<Observation>>),
    /// An alert channel's whole current set.
    Alerts(Vec<Alert>),
    /// Archived only.
    Nothing,
}

pub struct PollHandler {
    pub archive: ArchiveWriter,
    pub decoders: Arc<Decoders>,
    pub filters: Filters,
    pub skew: TimeDelta,
    pub stats: Mutex<IndexMap<String, ChannelStats>>,
    /// Archiving and decoding, merged into the core's phases in the summary.
    pub phases: Mutex<Phases>,
    pub core: std::sync::mpsc::Sender<Message>,
    /// The worker's tickets, shared with the ticks timer.
    pub sequencer: Arc<Sequencer>,
    /// Set once warm replay is done and the backlog queued.
    pub ready: Stop,
    /// The alerts' own connection (warm replay and the writer use theirs meanwhile).
    pub alerts: Option<Client>,
    pub log: Log,
}

impl std::fmt::Debug for PollHandler {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PollHandler")
            .field("archive", &self.archive)
            .finish_non_exhaustive()
    }
}

/// A poll's place in the core's order: resolved by [`PollHandler::to_core`], or by a skip when
/// dropped unused.
#[derive(Debug)]
pub struct Ticket {
    number: u64,
    core: Option<std::sync::mpsc::Sender<Message>>,
}

impl Ticket {
    fn into_number(mut self) -> u64 {
        self.core = None;
        self.number
    }
}

impl Drop for Ticket {
    fn drop(&mut self) {
        if let Some(core) = self.core.take() {
            let _ = core.send(Message::Skip {
                ticket: self.number,
            });
        }
    }
}

/// The archived request as a decoder reads it (None when the poll had none, as replay reads an
/// index entry without one).
fn request_json(poll: &Poll) -> Option<Json> {
    if poll.request.is_empty() {
        return None;
    }
    let mut object = Object::default();
    for (key, value) in &poll.request {
        object.insert(key.clone(), Json::Str(value.clone()));
    }
    Some(Json::Object(object))
}

impl PollHandler {
    /// The poll's ticket, taken as the poller gets it from the fetch (`received_at` raised if
    /// need be: [`Sequencer::poll`]).
    pub fn ticket(&self, poll: &mut Poll) -> Ticket {
        Ticket {
            number: self.sequencer.poll(&mut poll.received_at),
            core: Some(self.core.clone()),
        }
    }

    /// Archive the poll and decode it (blocking: compression and decoding are CPU work).
    fn archive_and_decode(&self, channel: &Channel, poll: &Poll) -> std::io::Result<Decoded> {
        let mut phases = Phases::new();
        let (sha256, body) = phases.time("archive", || {
            let mut stats = self.stats.lock().expect("stats");
            let entry = stats.entry(channel.name()).or_default();
            archive_poll(
                &self.archive,
                channel,
                poll,
                entry,
                &self.filters,
                &*self.log,
            )
        })?;
        let decoded = phases.time("decode", || self.decode(channel, poll, sha256, body));
        let mut total = self.phases.lock().expect("phases");
        for (name, cpu) in &phases.cpu_s {
            total.add(name, *cpu, phases.wall_s[name]);
        }
        Ok(decoded)
    }

    fn decode(
        &self,
        channel: &Channel,
        poll: &Poll,
        sha256: Option<String>,
        body: Option<Vec<u8>>,
    ) -> Decoded {
        let (source, name) = (&channel.source, &channel.channel);
        let undecodable = |message: &str| {
            (self.log)(&format!(
                "{}: undecodable payload: {message}",
                channel.name()
            ));
        };
        if self.decoders.is_alert_channel(source, name) {
            let Some(body) = body.filter(|_| poll.ok()) else {
                return Decoded::Nothing;
            };
            return match self
                .decoders
                .decode_alerts(source, name, &body, poll.received_at)
            {
                Ok(alerts) => Decoded::Alerts(alerts),
                Err(error) => {
                    undecodable(&error.message);
                    Decoded::Nothing
                }
            };
        }
        if !self.decoders.decodes_facts(source, name) {
            return Decoded::Nothing;
        }
        let (Some(body), Some(sha256), true) = (body, sha256, poll.ok()) else {
            return Decoded::Facts(Vec::new());
        };
        let request = request_json(poll);
        let payload = Payload {
            body: &body,
            sha256: &sha256,
            received_at: poll.received_at,
            request: request.as_ref(),
        };
        match decode_payload(&self.decoders, channel, &payload, self.skew, false) {
            Ok(observations) => Decoded::Facts(observations),
            Err(error) => {
                undecodable(&error.message);
                Decoded::Facts(Vec::new())
            }
        }
    }

    /// The poll's observations to the core; waits for them to have run once the worker is
    /// ready.
    async fn to_core(
        &self,
        ticket: Ticket,
        at: Instant,
        observations: Vec<Arc<Observation>>,
    ) -> Result<(), HandlerError> {
        let (done, wait) = tokio::sync::oneshot::channel::<Reply>();
        let waiting = self.ready.is_set();
        self.core
            .send(Message::Poll {
                ticket: ticket.into_number(),
                at,
                observations,
                done: waiting.then_some(done),
            })
            .map_err(|_| "the core has stopped")?;
        if waiting {
            wait.await.map_err(|_| "the core has stopped")??;
        }
        Ok(())
    }
}

/// The handler shared by every poller.
#[derive(Debug, Clone)]
pub struct Shared(pub Arc<PollHandler>);

impl OnPoll for Shared {
    async fn on_poll(&self, channel: &Channel, mut poll: Poll) -> Result<(), HandlerError> {
        // First, before any await: the ticket fixes the poll's place among all channels'.
        let ticket = self.0.ticket(&mut poll);
        let handler = Arc::clone(&self.0);
        let owned = channel.clone();
        let at = poll.received_at;
        let decoded = tokio::task::spawn_blocking(move || {
            let decoded = handler.archive_and_decode(&owned, &poll);
            (decoded, poll)
        })
        .await?
        .0?;
        let handler = &self.0;
        match decoded {
            Decoded::Nothing => Ok(()),
            Decoded::Facts(observations) => handler.to_core(ticket, at, observations).await,
            Decoded::Alerts(alerts) => {
                drop(ticket); // a skip: the alerts' write holds back no poll
                if let Some(client) = &handler.alerts {
                    write_alerts(client, &channel.source, &channel.channel, &alerts, at).await?;
                    // Republished from the next emitting tick on.
                    let _ = handler.core.send(Message::AlertsChanged);
                }
                Ok(())
            }
        }
    }
}
