//! Machine-local settings, the same ones Python's `runtime_config.py` reads: the database URL, the realtime secrets and the egress proxy list, each from the environment
//! before the gitignored `config/obehy.local.toml`; and where the shared data files are (the
//! manifests, the policy, the SR70 catalogue), found from the repository layout.
//!
//! Values are never logged; only setting names are.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use obehy_core::config::Policy;
use obehy_core::config::policy::POLICY_FILE;
use toml::{Table, Value};

pub const DATABASE_URL_ENV: &str = "OBEHY_DATABASE_URL";
pub const LIST_ENV: &str = "OBEHY_EGRESS_PROXY_LIST_FILE";
pub const LIST_SETTING: &str = "[realtime.egress] proxy_list_file";

/// The machine-local configuration is missing or invalid (`ConfigurationError`).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct ConfigurationError(pub String);

/// The process environment, or a fixed one in tests (setting variables is unsafe in Rust 2024
/// and races between test threads).
pub trait Env: Send + Sync {
    fn var(&self, name: &str) -> Option<String>;
    /// Every variable's name.
    fn names(&self) -> Vec<String>;
}

#[derive(Debug, Clone, Copy, Default)]
pub struct ProcessEnv;

impl Env for ProcessEnv {
    fn var(&self, name: &str) -> Option<String> {
        std::env::var_os(name).map(|v| v.to_string_lossy().into_owned())
    }

    fn names(&self) -> Vec<String> {
        std::env::vars_os()
            .map(|(k, _)| k.to_string_lossy().into_owned())
            .collect()
    }
}

#[derive(Debug, Clone, Default)]
pub struct MapEnv(pub BTreeMap<String, String>);

impl MapEnv {
    pub fn of(pairs: &[(&str, &str)]) -> MapEnv {
        MapEnv(
            pairs
                .iter()
                .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
                .collect(),
        )
    }
}

impl Env for MapEnv {
    fn var(&self, name: &str) -> Option<String> {
        self.0.get(name).cloned()
    }

    fn names(&self) -> Vec<String> {
        self.0.keys().cloned().collect()
    }
}

/// An environment variable, trimmed; None when empty.
fn env_value(env: &dyn Env, name: &str) -> Option<String> {
    env.var(name)
        .map(|v| v.trim().to_owned())
        .filter(|v| !v.is_empty())
}

/// Where the shared files are: the repository the Python package lives in.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Paths {
    pub root: PathBuf,
}

impl Paths {
    /// The repository (or the image's `/app`) holding `src/obehy/data/realtime/sources`: the
    /// current directory or one above it, else the executable's directory or one above it.
    pub fn discover() -> Result<Paths, ConfigurationError> {
        let mut starts = Vec::new();
        if let Ok(cwd) = std::env::current_dir() {
            starts.push(cwd);
        }
        if let Some(dir) = std::env::current_exe()
            .ok()
            .and_then(|e| e.parent().map(Path::to_path_buf))
        {
            starts.push(dir);
        }
        for start in &starts {
            for candidate in start.ancestors() {
                let paths = Paths {
                    root: candidate.to_path_buf(),
                };
                if paths.sources().is_dir() {
                    return Ok(paths);
                }
            }
        }
        Err(ConfigurationError(
            "cannot find the Oběhy repository (src/obehy/data/realtime/sources) above the current \
             directory or the executable"
                .to_owned(),
        ))
    }

    /// The connector manifests.
    pub fn sources(&self) -> PathBuf {
        self.root.join("src/obehy/data/realtime/sources")
    }

    pub fn data(&self) -> PathBuf {
        self.root.join("src/obehy/data/realtime")
    }

    pub fn policy(&self) -> PathBuf {
        self.data().join(POLICY_FILE)
    }

    /// The SR70 station catalogue (`demand.CATALOGUE`).
    pub fn catalogue(&self) -> PathBuf {
        self.data().join("sr70-name20.csv")
    }

    /// `default_config_path()`.
    pub fn config(&self) -> PathBuf {
        self.root.join("config/obehy.local.toml")
    }

    pub fn load_policy(&self) -> Result<Policy, ConfigurationError> {
        Policy::load(&self.policy()).map_err(|e| ConfigurationError(e.to_string()))
    }
}

/// The parsed config file, None when there is no such file.
pub fn read_config(path: &Path) -> Result<Option<Table>, ConfigurationError> {
    let source = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    if !source.is_file() {
        return Ok(None);
    }
    let text = std::fs::read_to_string(&source)
        .map_err(|e| ConfigurationError(format!("{}: {e}", source.display())))?;
    text.parse::<Table>()
        .map(Some)
        .map_err(|e| ConfigurationError(format!("Invalid TOML in {}: {e}", source.display())))
}

fn subtable<'a>(table: &'a Table, path: &[&str]) -> Option<&'a Table> {
    let mut current = table;
    for key in path {
        current = current.get(*key)?.as_table()?;
    }
    Some(current)
}

/// `os.path.expandvars`: `$name` and `${name}` (and on Windows `%name%`, `$$`, `%%` and single
/// quotes, as `ntpath`), an unknown variable left as written.
pub fn expandvars(text: &str, env: &dyn Env) -> String {
    if cfg!(windows) {
        expandvars_nt(text, env)
    } else {
        expandvars_posix(text, env)
    }
}

/// `posixpath.expandvars`: `\$(\w+|\{[^}]*\})` with ASCII `\w`; a substituted value is not
/// scanned again.
pub fn expandvars_posix(text: &str, env: &dyn Env) -> String {
    if !text.contains('$') {
        return text.to_owned();
    }
    let chars: Vec<char> = text.chars().collect();
    let word = |c: &char| c.is_ascii_alphanumeric() || *c == '_';
    let mut out = String::new();
    let mut i = 0;
    while i < chars.len() {
        if chars[i] != '$' {
            out.push(chars[i]);
            i += 1;
            continue;
        }
        let matched = if chars.get(i + 1) == Some(&'{') {
            chars[i + 2..].iter().position(|&c| c == '}').map(|close| {
                (
                    chars[i + 2..i + 2 + close].iter().collect::<String>(),
                    i + 3 + close,
                )
            })
        } else {
            let len = chars[i + 1..].iter().take_while(|c| word(c)).count();
            (len > 0).then(|| {
                (
                    chars[i + 1..i + 1 + len].iter().collect::<String>(),
                    i + 1 + len,
                )
            })
        };
        match matched {
            None => {
                out.push('$');
                i += 1;
            }
            Some((name, end)) => {
                match env.var(&name) {
                    Some(value) => out.push_str(&value),
                    None => out.extend(&chars[i..end]),
                }
                i = end;
            }
        }
    }
    out
}

/// `ntpath.expandvars`, step for step.
pub fn expandvars_nt(text: &str, env: &dyn Env) -> String {
    if !text.contains('$') && !text.contains('%') {
        return text.to_owned();
    }
    let varchars = |c: char| c.is_ascii_alphanumeric() || c == '_' || c == '-';
    let mut path: Vec<char> = text.chars().collect();
    let mut res = String::new();
    let mut index = 0usize;
    while index < path.len() {
        let c = path[index];
        if c == '\'' {
            path = path[index + 1..].to_vec();
            match path.iter().position(|&x| x == '\'') {
                Some(found) => {
                    res.push('\'');
                    res.extend(&path[..=found]);
                    index = found;
                }
                None => {
                    res.push('\'');
                    res.extend(&path);
                    index = path.len().wrapping_sub(1);
                }
            }
        } else if c == '%' {
            if path.get(index + 1) == Some(&'%') {
                res.push('%');
                index += 1;
            } else {
                path = path[index + 1..].to_vec();
                match path.iter().position(|&x| x == '%') {
                    None => {
                        res.push('%');
                        res.extend(&path);
                        index = path.len().wrapping_sub(1);
                    }
                    Some(found) => {
                        let var: String = path[..found].iter().collect();
                        match env.var(&var) {
                            Some(value) => res.push_str(&value),
                            None => res.push_str(&format!("%{var}%")),
                        }
                        index = found;
                    }
                }
            }
        } else if c == '$' {
            if path.get(index + 1) == Some(&'$') {
                res.push('$');
                index += 1;
            } else if path.get(index + 1) == Some(&'{') {
                path = path[index + 2..].to_vec();
                match path.iter().position(|&x| x == '}') {
                    None => {
                        res.push_str("${");
                        res.extend(&path);
                        index = path.len().wrapping_sub(1);
                    }
                    Some(found) => {
                        let var: String = path[..found].iter().collect();
                        match env.var(&var) {
                            Some(value) => res.push_str(&value),
                            None => res.push_str(&format!("${{{var}}}")),
                        }
                        index = found;
                    }
                }
            } else {
                let mut var = String::new();
                index += 1;
                while index < path.len() && varchars(path[index]) {
                    var.push(path[index]);
                    index += 1;
                }
                match env.var(&var) {
                    Some(value) => res.push_str(&value),
                    None => {
                        res.push('$');
                        res.push_str(&var);
                    }
                }
                if index < path.len() {
                    index -= 1;
                }
            }
        } else {
            res.push(c);
        }
        index = index.wrapping_add(1);
    }
    res
}

/// The PostgreSQL URL: `OBEHY_DATABASE_URL` or `[database] url` (`load_database_url`). Only
/// `schema_version` and `[database]` are read.
pub fn load_database_url(config: &Path, env: &dyn Env) -> Result<String, ConfigurationError> {
    if let Some(url) = env_value(env, DATABASE_URL_ENV) {
        return Ok(url);
    }
    let shown = std::fs::canonicalize(config).unwrap_or_else(|_| config.to_path_buf());
    let Some(document) = read_config(config)? else {
        return Err(ConfigurationError(format!(
            "No database configured: set {DATABASE_URL_ENV} or [database] url in {}",
            shown.display()
        )));
    };
    if !matches!(document.get("schema_version"), Some(Value::Integer(1))) {
        return Err(ConfigurationError(format!(
            "{} must contain schema_version = 1",
            shown.display()
        )));
    }
    let Some(database) = document.get("database").and_then(Value::as_table) else {
        return Err(ConfigurationError("Missing [database] table".to_owned()));
    };
    match database.get("url").and_then(Value::as_str).map(str::trim) {
        Some(url) if !url.is_empty() => Ok(expandvars(url, env)),
        _ => Err(ConfigurationError(format!(
            "Missing non-empty 'url' in [database] of {}",
            shown.display()
        ))),
    }
}

/// The DB-test admin URL: `OBEHY_TEST_DATABASE_URL` or `[database] test_url`, if any
/// (`load_test_database_url`).
pub fn load_test_database_url(config: &Path, env: &dyn Env) -> Option<String> {
    if let Some(url) = env_value(env, "OBEHY_TEST_DATABASE_URL") {
        return Some(url);
    }
    let document = read_config(config).ok()??;
    let url = document
        .get("database")?
        .as_table()?
        .get("test_url")?
        .as_str()
        .map(str::trim)?;
    (!url.is_empty()).then(|| expandvars(url, env))
}

/// `OBEHY_SECRET_<NAME>`.
pub fn secret_env(name: &str) -> String {
    format!("OBEHY_SECRET_{}", name.to_uppercase().replace('-', "_"))
}

/// The configured secrets among `names` (API keys of realtime sources): `OBEHY_SECRET_<NAME>` or
/// `[realtime.secrets] <name>` (`load_secrets`).
pub fn load_secrets<'a>(
    names: impl IntoIterator<Item = &'a str>,
    config: &Path,
    env: &dyn Env,
) -> Result<BTreeMap<String, String>, ConfigurationError> {
    let mut wanted: Vec<&str> = names.into_iter().collect();
    wanted.sort_unstable();
    wanted.dedup();
    let mut found: BTreeMap<String, String> = wanted
        .iter()
        .filter_map(|name| env_value(env, &secret_env(name)).map(|v| ((*name).to_owned(), v)))
        .collect();
    if found.len() == wanted.len() {
        return Ok(found);
    }
    let Some(document) = read_config(config)? else {
        return Ok(found);
    };
    if let Some(table) = subtable(&document, &["realtime", "secrets"]) {
        for name in wanted {
            if found.contains_key(name) {
                continue;
            }
            if let Some(value) = table.get(name).and_then(Value::as_str).map(str::trim)
                && !value.is_empty()
            {
                found.insert(name.to_owned(), value.to_owned());
            }
        }
    }
    Ok(found)
}

/// The saved proxy-list file's path: the environment before the config file
/// (`load_proxy_list`). A per-source list of an older config (`OBEHY_EGRESS_SZ_PROXY_LIST_FILE`,
/// `[realtime.egress.sz]`) is not read: it is named in the log, to be renamed.
pub fn load_proxy_list(
    config: &Path,
    env: &dyn Env,
    log: &dyn Fn(&str),
) -> Result<Option<String>, ConfigurationError> {
    let document = read_config(config)?.unwrap_or_default();
    let empty = Table::new();
    let table = subtable(&document, &["realtime", "egress"]).unwrap_or(&empty);
    let mut old: Vec<String> = env
        .names()
        .into_iter()
        .filter(|k| k.starts_with("OBEHY_EGRESS_") && k != LIST_ENV)
        .chain(
            table
                .iter()
                .filter(|(_, v)| v.is_table())
                .map(|(k, _)| format!("[realtime.egress.{k}]")),
        )
        .collect();
    old.sort();
    if !old.is_empty() {
        log(&format!(
            "egress: {} not read; the one list is {LIST_ENV} or {LIST_SETTING}",
            old.join(", ")
        ));
    }
    let saved = env_value(env, LIST_ENV).or_else(|| {
        table
            .get("proxy_list_file")
            .and_then(Value::as_str)
            .map(|s| s.trim().to_owned())
            .filter(|s| !s.is_empty())
    });
    Ok(saved.map(|s| expandvars(&s, env)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config(dir: &Path, text: &str) -> PathBuf {
        let path = dir.join("obehy.local.toml");
        std::fs::write(&path, text).unwrap();
        path
    }

    fn scratch(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("obehy-rt-settings-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn the_list_file_comes_from_the_environment_before_the_config() {
        let dir = scratch("list");
        let path = config(
            &dir,
            "schema_version = 1\n[realtime.egress]\nproxy_list_file = \"D:/secrets/a.txt\"\n",
        );
        let none = MapEnv::default();
        assert_eq!(
            load_proxy_list(&path, &none, &|_| {}).unwrap().as_deref(),
            Some("D:/secrets/a.txt")
        );
        let env = MapEnv::of(&[(LIST_ENV, "/app/secrets/b.txt")]);
        assert_eq!(
            load_proxy_list(&path, &env, &|_| {}).unwrap().as_deref(),
            Some("/app/secrets/b.txt")
        );
    }

    #[test]
    fn a_per_source_list_of_an_older_config_is_named_not_read() {
        let dir = scratch("old");
        let path = config(
            &dir,
            "schema_version = 1\n[realtime.egress.sz]\nproxy_list_file = \"D:/secrets/a.txt\"\n",
        );
        let env = MapEnv::of(&[("OBEHY_EGRESS_SZ_PROXY_LIST_FILE", "/app/secrets/b.txt")]);
        let logs = std::sync::Mutex::new(Vec::<String>::new());
        let found = load_proxy_list(&path, &env, &|m| logs.lock().unwrap().push(m.to_owned()));
        assert_eq!(found.unwrap(), None);
        let logs = logs.into_inner().unwrap();
        assert!(logs[0].contains("OBEHY_EGRESS_SZ_PROXY_LIST_FILE"));
        assert!(logs[0].contains("[realtime.egress.sz]"));
        assert!(!logs[0].contains("b.txt"));
    }

    #[test]
    fn secrets_come_from_the_environment_before_the_config() {
        let dir = scratch("secrets");
        let path = config(
            &dir,
            "schema_version = 1\n[realtime.secrets]\ngolemio = \"from-file\"\n",
        );
        let found = load_secrets(["golemio", "other"], &path, &MapEnv::default()).unwrap();
        assert_eq!(
            found,
            BTreeMap::from([("golemio".to_owned(), "from-file".to_owned())])
        );
        let env = MapEnv::of(&[("OBEHY_SECRET_GOLEMIO", "from-env")]);
        let found = load_secrets(["golemio"], &path, &env).unwrap();
        assert_eq!(found["golemio"], "from-env");
        assert_eq!(secret_env("sz-key"), "OBEHY_SECRET_SZ_KEY");
    }

    #[test]
    fn the_database_url_needs_schema_version_one() {
        let dir = scratch("db");
        let path = config(
            &dir,
            "schema_version = 1\n[database]\nurl = \" postgresql://${HOST}/x \"\n",
        );
        let env = MapEnv::of(&[("HOST", "db")]);
        assert_eq!(load_database_url(&path, &env).unwrap(), "postgresql://db/x");
        let env = MapEnv::of(&[(DATABASE_URL_ENV, "postgresql://env/y")]);
        assert_eq!(
            load_database_url(&path, &env).unwrap(),
            "postgresql://env/y"
        );
        let path = config(&dir, "[database]\nurl = \"x\"\n");
        let error = load_database_url(&path, &MapEnv::default()).unwrap_err();
        assert!(error.0.contains("schema_version = 1"));
        let missing = load_database_url(&dir.join("missing.toml"), &MapEnv::default());
        assert!(missing.unwrap_err().0.contains("No database configured"));
    }

    #[test]
    fn expandvars_follows_posixpath_and_ntpath() {
        let env = MapEnv::of(&[("A", "1"), ("B_2", "two")]);
        assert_eq!(
            expandvars_posix("$A/${B_2}/$C/${}/$/x$", &env),
            "1/two/$C/${}/$/x$"
        );
        assert_eq!(expandvars_posix("${A", &env), "${A");
        assert_eq!(
            expandvars_nt("%A%/$A/${B_2}/'$A'/%%/$$/%C%/$C", &env),
            "1/1/two/'$A'/%/$/%C%/$C"
        );
        assert_eq!(expandvars_nt("50%", &env), "50%");
        assert_eq!(expandvars_nt("a-$A-b", &env), "a-$A-b");
    }
}
