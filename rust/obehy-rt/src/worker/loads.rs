//! The active release's loads in PostgreSQL (`index_sql.ReleaseLoads`, `active_loads`): the
//! run ID and each feed's load ID, and the index's rows read from `static.*` of those loads.

use obehy_core::index::RowsError;
use obehy_core::model::Feed;
use obehy_core::runner::Loads;
use tokio_postgres::{Client, Config, NoTls};

use super::WorkerError;
use crate::index::sql::{Driver, SqlRows};

/// What `control.publication` names: the active run and its jdf and czptt loads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReleaseLoads {
    pub run_id: String,
    pub jdf: i32,
    pub czptt: i32,
}

impl ReleaseLoads {
    pub fn load_id(&self, feed: Feed) -> i32 {
        match feed {
            Feed::Jdf => self.jdf,
            Feed::Czptt => self.czptt,
        }
    }
}

/// The active publication's loads; an error when no release is active (the worker then exits,
/// and restarts until the first release update has run).
pub async fn active_loads(client: &Client) -> Result<ReleaseLoads, WorkerError> {
    let (run_id, jdf, czptt) = crate::demand::active_loads(client)
        .await
        .map_err(|e| WorkerError::Other(e.to_string()))?;
    Ok(ReleaseLoads { run_id, jdf, czptt })
}

/// The index's rows of a release in PostgreSQL: each feed's runtime opens a connection of its
/// own on the core's thread (a fresh one at each compaction and release switch).
#[derive(Debug, Clone)]
pub struct PgLoads {
    pub config: Config,
    pub loads: ReleaseLoads,
}

impl Loads for PgLoads {
    type Rows = SqlRows;

    fn release_id(&self) -> &str {
        &self.loads.run_id
    }

    fn rows(&self, feed: Feed) -> Result<SqlRows, RowsError> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        let client = runtime.block_on(async {
            let (client, connection) = self.config.connect(NoTls).await?;
            tokio::spawn(async move {
                if let Err(error) = connection.await {
                    crate::logs::log(&format!("index database connection: {error}"));
                }
            });
            Ok::<_, tokio_postgres::Error>(client)
        })?;
        Ok(SqlRows::new(
            client,
            Driver::Own(runtime),
            self.loads.load_id(feed),
        ))
    }
}
