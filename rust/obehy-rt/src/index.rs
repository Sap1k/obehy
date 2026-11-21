//! The index's rows (R3): `obehy_core::index::Rows` from PostgreSQL `static.*` ([`sql`]) and from a
//! release directory's serving Parquet ([`serving`] over [`parquet`]). Both answer each question as
//! the SQL does.

pub mod parquet;
pub mod serving;
pub mod sql;

use std::path::{Path, PathBuf};

use obehy_core::index::{IndexLoader, RowsError};
use obehy_core::model::Feed;

pub use serving::ServingRows;
pub use sql::SqlRows;

/// The release ID of a release directory (`release.json`'s `run_id`).
pub fn release_run_id(release: &Path) -> Result<String, RowsError> {
    let text = std::fs::read_to_string(release.join("release.json"))
        .map_err(|e| format!("{} is not a release directory: {e}", release.display()))?;
    let document: serde_json::Value = serde_json::from_str(&text)?;
    document["run_id"]
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| "release.json has no run_id".into())
}

/// The serving directory of one feed of a release.
pub fn serving_dir(release: &Path, feed: Feed) -> PathBuf {
    release.join(feed.as_str()).join("serving")
}

/// An index loader reading a release directory's serving files (`index_sql.file_loads`).
pub fn file_loader(release: &Path, feed: Feed) -> Result<IndexLoader<ServingRows>, RowsError> {
    let run_id = release_run_id(release)?;
    let rows = ServingRows::open(&serving_dir(release, feed))?;
    Ok(IndexLoader::new(rows, run_id, feed))
}
