//! A row's cells: Python's tuple items, typed for PostgreSQL's binary format and for the
//! compare harness's records.

use std::cmp::Ordering;
use std::error::Error;
use std::sync::Arc;

use bytes::{BufMut, BytesMut};
use obehy_core::py::json::{DumpOptions, PyJson, dumps};
use obehy_core::time::{Date, Instant};
use tokio_postgres::types::{FromSql, IsNull, ToSql, Type, to_sql_checked};

use crate::canonical::{C, utc_text};

/// Microseconds from 1970-01-01 to PostgreSQL's epoch, 2000-01-01.
pub const PG_EPOCH_MICROS: i64 = 946_684_800_000_000;
/// Days from 1970-01-01 to 2000-01-01.
pub const PG_EPOCH_DAYS: i32 = 10_957;

/// One value of a row.
#[derive(Debug, Clone)]
pub enum Cell {
    Null,
    Text(String),
    Int(i64),
    Bool(bool),
    Float(f64),
    Date(Date),
    Time(Instant),
    /// A jsonb value (written as Python's `json.dumps` text; recorded as the object). Shared:
    /// a journey's calls are one tree for its row and the writer's cache, and an unchanged
    /// row compares by pointer.
    Json(Arc<PyJson>),
}

impl PartialEq for Cell {
    fn eq(&self, other: &Cell) -> bool {
        match (self, other) {
            (Cell::Null, Cell::Null) => true,
            (Cell::Text(a), Cell::Text(b)) => a == b,
            (Cell::Int(a), Cell::Int(b)) => a == b,
            (Cell::Bool(a), Cell::Bool(b)) => a == b,
            (Cell::Float(a), Cell::Float(b)) => a == b,
            (Cell::Date(a), Cell::Date(b)) => a == b,
            (Cell::Time(a), Cell::Time(b)) => a == b,
            (Cell::Json(a), Cell::Json(b)) => Arc::ptr_eq(a, b) || a == b,
            _ => false,
        }
    }
}

/// A row: one cell per column.
pub type Row = Vec<Cell>;

impl Cell {
    pub fn text(value: impl Into<String>) -> Cell {
        Cell::Text(value.into())
    }

    pub fn json(value: PyJson) -> Cell {
        Cell::Json(Arc::new(value))
    }

    pub fn opt_text(value: Option<impl Into<String>>) -> Cell {
        value.map_or(Cell::Null, |v| Cell::Text(v.into()))
    }

    pub fn opt_time(value: Option<Instant>) -> Cell {
        value.map_or(Cell::Null, Cell::Time)
    }

    pub fn opt_date(value: Option<Date>) -> Cell {
        value.map_or(Cell::Null, Cell::Date)
    }

    pub fn opt_int(value: Option<i64>) -> Cell {
        value.map_or(Cell::Null, Cell::Int)
    }

    pub fn opt_float(value: Option<f64>) -> Cell {
        value.map_or(Cell::Null, Cell::Float)
    }

    pub fn as_text(&self) -> Option<&str> {
        match self {
            Cell::Text(text) => Some(text),
            _ => None,
        }
    }

    pub fn as_time(&self) -> Option<Instant> {
        match self {
            Cell::Time(at) => Some(*at),
            _ => None,
        }
    }

    pub fn as_date(&self) -> Option<Date> {
        match self {
            Cell::Date(day) => Some(*day),
            _ => None,
        }
    }

    /// The record value (`canonical.plain` of the Python cell).
    pub fn record(&self) -> C {
        match self {
            Cell::Null => C::Null,
            Cell::Text(text) => C::Str(text.clone()),
            Cell::Int(n) => C::Int(i128::from(*n)),
            Cell::Bool(b) => C::Bool(*b),
            Cell::Float(x) => C::Float(*x),
            Cell::Date(day) => C::Str(day.isoformat()),
            Cell::Time(at) => C::Str(utc_text(*at)),
            Cell::Json(value) => C::from_py(value),
        }
    }

    fn rank(&self) -> u8 {
        match self {
            Cell::Null => 0,
            Cell::Bool(_) => 1,
            Cell::Int(_) => 2,
            Cell::Float(_) => 3,
            Cell::Text(_) => 4,
            Cell::Date(_) => 5,
            Cell::Time(_) => 6,
            Cell::Json(_) => 7,
        }
    }

    /// A total order for sorting keys (text, dates and times, as Python orders key tuples).
    pub fn order(&self, other: &Cell) -> Ordering {
        match (self, other) {
            (Cell::Text(a), Cell::Text(b)) => a.cmp(b),
            (Cell::Date(a), Cell::Date(b)) => a.cmp(b),
            (Cell::Time(a), Cell::Time(b)) => a.cmp(b),
            (Cell::Int(a), Cell::Int(b)) => a.cmp(b),
            (Cell::Bool(a), Cell::Bool(b)) => a.cmp(b),
            (Cell::Float(a), Cell::Float(b)) => a.total_cmp(b),
            _ => self.rank().cmp(&other.rank()),
        }
    }
}

/// Rows compared as tuples of [`Cell::order`].
pub fn order_rows(a: &Row, b: &Row) -> Ordering {
    for (x, y) in a.iter().zip(b) {
        let found = x.order(y);
        if found != Ordering::Equal {
            return found;
        }
    }
    a.len().cmp(&b.len())
}

/// `json.dumps(value)` (default separators, ASCII), the text a jsonb column is sent as.
pub fn json_text(value: &PyJson) -> String {
    dumps(value, DumpOptions::DEFAULT)
}

fn mismatch(cell: &Cell, ty: &Type) -> Box<dyn Error + Sync + Send> {
    format!("cannot send {cell:?} as {ty}").into()
}

impl ToSql for Cell {
    fn to_sql(
        &self,
        ty: &Type,
        out: &mut BytesMut,
    ) -> Result<IsNull, Box<dyn Error + Sync + Send>> {
        match (self, ty) {
            (Cell::Null, _) => return Ok(IsNull::Yes),
            (Cell::Text(text), _) if is_text(ty) => out.put_slice(text.as_bytes()),
            (Cell::Int(n), &Type::INT2) => out.put_i16(i16::try_from(*n)?),
            (Cell::Int(n), &Type::INT4) => out.put_i32(i32::try_from(*n)?),
            (Cell::Int(n), &Type::INT8) => out.put_i64(*n),
            (Cell::Bool(b), &Type::BOOL) => out.put_u8(u8::from(*b)),
            (Cell::Float(x), &Type::FLOAT8) => out.put_f64(*x),
            (Cell::Date(day), &Type::DATE) => out.put_i32(day.days_since_epoch() - PG_EPOCH_DAYS),
            (Cell::Time(at), &Type::TIMESTAMPTZ) => out.put_i64(at.micros() - PG_EPOCH_MICROS),
            (Cell::Json(value), &Type::JSONB) => {
                out.put_u8(1);
                out.put_slice(json_text(value).as_bytes());
            }
            (Cell::Json(value), &Type::JSON) => out.put_slice(json_text(value).as_bytes()),
            (cell, ty) => return Err(mismatch(cell, ty)),
        }
        Ok(IsNull::No)
    }

    fn accepts(_: &Type) -> bool {
        true
    }

    to_sql_checked!();
}

fn is_text(ty: &Type) -> bool {
    matches!(*ty, Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::NAME)
        || ty.name() == "event_kind"
}

/// A `timestamptz` read in the binary format.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PgTime(pub Instant);

impl<'a> FromSql<'a> for PgTime {
    fn from_sql(_: &Type, raw: &'a [u8]) -> Result<Self, Box<dyn Error + Sync + Send>> {
        let micros = i64::from_be_bytes(raw.try_into()?);
        Ok(PgTime(Instant::from_micros(micros + PG_EPOCH_MICROS)))
    }

    fn accepts(ty: &Type) -> bool {
        *ty == Type::TIMESTAMPTZ
    }
}

impl ToSql for PgTime {
    fn to_sql(&self, _: &Type, out: &mut BytesMut) -> Result<IsNull, Box<dyn Error + Sync + Send>> {
        out.put_i64(self.0.micros() - PG_EPOCH_MICROS);
        Ok(IsNull::No)
    }

    fn accepts(ty: &Type) -> bool {
        *ty == Type::TIMESTAMPTZ
    }

    to_sql_checked!();
}

/// A `jsonb` (or `json`) value read as its text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JsonText(pub String);

impl<'a> FromSql<'a> for JsonText {
    fn from_sql(ty: &Type, raw: &'a [u8]) -> Result<Self, Box<dyn Error + Sync + Send>> {
        let body = if *ty == Type::JSONB {
            match raw.split_first() {
                Some((1, rest)) => rest,
                _ => return Err("unknown jsonb version".into()),
            }
        } else {
            raw
        };
        Ok(JsonText(std::str::from_utf8(body)?.to_owned()))
    }

    fn accepts(ty: &Type) -> bool {
        *ty == Type::JSONB || *ty == Type::JSON
    }
}

/// Text sent as a `jsonb` parameter.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Jsonb(pub String);

impl ToSql for Jsonb {
    fn to_sql(
        &self,
        ty: &Type,
        out: &mut BytesMut,
    ) -> Result<IsNull, Box<dyn Error + Sync + Send>> {
        if *ty == Type::JSONB {
            out.put_u8(1);
        }
        out.put_slice(self.0.as_bytes());
        Ok(IsNull::No)
    }

    fn accepts(ty: &Type) -> bool {
        *ty == Type::JSONB || *ty == Type::JSON
    }

    to_sql_checked!();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys_sort_as_python_tuples_and_cells_record_canonically() {
        let a = vec![Cell::text("jdf"), Cell::Date(Date::ymd(2026, 10, 8))];
        let b = vec![Cell::text("jdf"), Cell::Date(Date::ymd(2026, 10, 9))];
        assert_eq!(order_rows(&a, &b), Ordering::Less);
        assert_eq!(order_rows(&b, &b), Ordering::Equal);
        assert_eq!(
            Cell::Date(Date::ymd(2026, 10, 8)).record(),
            C::str("2026-10-08")
        );
        assert_eq!(
            Cell::Time(Instant::from_micros(1_500_000)).record(),
            C::str("1970-01-01T00:00:01.500000Z")
        );
        let json = PyJson::object([("a", PyJson::Int(1)), ("b", PyJson::str("ž"))]);
        assert_eq!(json_text(&json), "{\"a\": 1, \"b\": \"\\u017e\"}");
    }
}
