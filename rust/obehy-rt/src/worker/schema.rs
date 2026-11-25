//! The database schema the worker writes to. Migrations are Python's
//! (`src/obehy/release/migrations/NNNN_name.sql`, applied by `obehy db migrate` and recorded in
//! `control.schema_migration` as version, name and checksum); the worker refuses to start on a
//! schema it does not know: older (a table or column it writes may be missing) or newer (a
//! migration may have changed what it writes). A new migration therefore means a new
//! [`SCHEMA_VERSION`] here, which the test below demands.

use tokio_postgres::Client;

use super::WorkerError;

/// The last migration this worker was written against: version and name.
pub const SCHEMA_VERSION: i32 = 29;
pub const SCHEMA_NAME: &str = "shape_point_emptied";

/// Why `applied` (the recorded migrations, by version) is not the schema this worker knows.
pub fn verdict(applied: &[(i32, String)]) -> Result<(), String> {
    let migrate = "run `obehy db migrate`";
    let Some((last, name)) = applied.iter().max_by_key(|(version, _)| *version) else {
        return Err(format!("the database has no schema migrations; {migrate}"));
    };
    if *last > SCHEMA_VERSION {
        return Err(format!(
            "the database schema is at migration {last:04}_{name}, newer than this worker knows \
             ({SCHEMA_VERSION:04}_{SCHEMA_NAME}); update the worker"
        ));
    }
    if *last < SCHEMA_VERSION {
        return Err(format!(
            "the database schema is at migration {last:04}_{name}, this worker needs \
             {SCHEMA_VERSION:04}_{SCHEMA_NAME}; {migrate}"
        ));
    }
    if name != SCHEMA_NAME {
        return Err(format!(
            "the database's migration {last:04} is {name}, this worker knows it as {SCHEMA_NAME}"
        ));
    }
    let versions: Vec<i32> = applied.iter().map(|(v, _)| *v).collect();
    if versions.len() != SCHEMA_VERSION as usize
        || (1..=SCHEMA_VERSION).any(|v| !versions.contains(&v))
    {
        return Err(format!(
            "the database's migrations are not 1..{SCHEMA_VERSION} without gaps: {versions:?}"
        ));
    }
    Ok(())
}

/// Refuse a database whose schema this worker does not know.
pub async fn check_schema(client: &Client) -> Result<(), WorkerError> {
    let table = client
        .query_one(
            "SELECT to_regclass('control.schema_migration') IS NOT NULL",
            &[],
        )
        .await?;
    let applied: Vec<(i32, String)> = if table.get::<_, bool>(0) {
        client
            .query(
                "SELECT version, name FROM control.schema_migration ORDER BY version",
                &[],
            )
            .await?
            .iter()
            .map(|row| (row.get(0), row.get(1)))
            .collect()
    } else {
        Vec::new()
    };
    verdict(&applied).map_err(WorkerError::Schema)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn applied(last: i32, name: &str) -> Vec<(i32, String)> {
        (1..last)
            .map(|v| (v, format!("m{v}")))
            .chain([(last, name.to_owned())])
            .collect()
    }

    #[test]
    fn the_known_schema_is_the_last_migration_file() {
        let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../src/obehy/release/migrations");
        let mut names: Vec<String> = std::fs::read_dir(&dir)
            .unwrap()
            .filter_map(|e| e.ok())
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .filter(|n| n.ends_with(".sql"))
            .collect();
        names.sort();
        assert_eq!(
            names.len(),
            SCHEMA_VERSION as usize,
            "a migration was added"
        );
        assert_eq!(
            names.last().map(String::as_str),
            Some(format!("{SCHEMA_VERSION:04}_{SCHEMA_NAME}.sql").as_str())
        );
    }

    #[test]
    fn only_the_known_schema_passes() {
        assert_eq!(verdict(&applied(SCHEMA_VERSION, SCHEMA_NAME)), Ok(()));
        assert!(verdict(&[]).unwrap_err().contains("no schema migrations"));
        let older = verdict(&applied(SCHEMA_VERSION - 1, "x")).unwrap_err();
        assert!(older.contains("obehy db migrate"), "{older}");
        let newer = verdict(&applied(SCHEMA_VERSION + 1, "x")).unwrap_err();
        assert!(newer.contains("newer than this worker"), "{newer}");
        assert!(verdict(&applied(SCHEMA_VERSION, "other")).is_err());
        let mut gap = applied(SCHEMA_VERSION, SCHEMA_NAME);
        gap.remove(3);
        assert!(verdict(&gap).unwrap_err().contains("without gaps"));
    }
}
