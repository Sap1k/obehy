//! Reading a serving table's columns from Parquet into [`super::serving`]'s columns.
//!
//! The serving contract's types (`serving-v5.json`): strings, int16/int32, double, bool and
//! date32. Writers differ in how they store them (Parquet.Net in releases, pyarrow in tests),
//! so each reader accepts the Arrow types those map to.

use std::fs::File;
use std::path::Path;

use arrow_array::cast::AsArray;
use arrow_array::types::{Date32Type, Float64Type, Int8Type, Int16Type, Int32Type, Int64Type};
use arrow_array::{Array, ArrayRef, RecordBatch};
use arrow_schema::DataType;
use obehy_core::index::RowsError;
use obehy_core::time::Date;
use parquet::arrow::ProjectionMask;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;

use super::serving::{
    Calendar, Calls, Exceptions, Locations, NONE, Routes, ShapePoints, SourceKeys, Strings, Trips,
};

/// The batches of `columns` of a Parquet file.
fn read(path: &Path, columns: &[&str]) -> Result<Vec<RecordBatch>, RowsError> {
    let file = File::open(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let builder = ParquetRecordBatchReaderBuilder::try_new(file)?;
    let mask = ProjectionMask::columns(builder.parquet_schema(), columns.iter().copied());
    let reader = builder
        .with_projection(mask)
        .with_batch_size(64 * 1024)
        .build()?;
    Ok(reader.collect::<Result<Vec<_>, _>>()?)
}

fn column<'a>(batch: &'a RecordBatch, name: &str) -> Result<&'a ArrayRef, RowsError> {
    batch
        .column_by_name(name)
        .ok_or_else(|| format!("no column {name}").into())
}

fn unsupported(name: &str, array: &ArrayRef) -> RowsError {
    format!("column {name}: unsupported type {:?}", array.data_type()).into()
}

fn strings(
    batch: &RecordBatch,
    name: &str,
    s: &mut Strings,
    out: &mut Vec<u32>,
) -> Result<(), RowsError> {
    let array = column(batch, name)?;
    let mut push = |value: Option<&str>| out.push(value.map_or(NONE, |v| s.intern(v)));
    match array.data_type() {
        DataType::Utf8 => array.as_string::<i32>().iter().for_each(&mut push),
        DataType::LargeUtf8 => array.as_string::<i64>().iter().for_each(&mut push),
        DataType::Utf8View => array.as_string_view().iter().for_each(&mut push),
        DataType::Dictionary(_, _) => {
            let values = arrow_cast_strings(array)?;
            values.into_iter().for_each(|v| push(v.as_deref()));
        }
        _ => return Err(unsupported(name, array)),
    }
    Ok(())
}

/// A dictionary-encoded string column, decoded.
fn arrow_cast_strings(array: &ArrayRef) -> Result<Vec<Option<String>>, RowsError> {
    let dictionary = array
        .as_any_dictionary_opt()
        .ok_or("not a dictionary column")?;
    let values = dictionary.values();
    let keys = dictionary.normalized_keys();
    let text = |i: usize| -> Option<String> {
        match values.data_type() {
            DataType::Utf8 => Some(values.as_string::<i32>().value(i).to_owned()),
            DataType::LargeUtf8 => Some(values.as_string::<i64>().value(i).to_owned()),
            _ => None,
        }
    };
    Ok((0..array.len())
        .map(|i| {
            if array.is_null(i) {
                None
            } else {
                text(keys[i])
            }
        })
        .collect())
}

/// An integer column of any width, as i64 (None for NULL).
fn ints(batch: &RecordBatch, name: &str) -> Result<Vec<Option<i64>>, RowsError> {
    let array = column(batch, name)?;
    Ok(match array.data_type() {
        DataType::Int8 => array
            .as_primitive::<Int8Type>()
            .iter()
            .map(|v| v.map(i64::from))
            .collect(),
        DataType::Int16 => array
            .as_primitive::<Int16Type>()
            .iter()
            .map(|v| v.map(i64::from))
            .collect(),
        DataType::Int32 => array
            .as_primitive::<Int32Type>()
            .iter()
            .map(|v| v.map(i64::from))
            .collect(),
        DataType::Int64 => array.as_primitive::<Int64Type>().iter().collect(),
        _ => return Err(unsupported(name, array)),
    })
}

fn i32s(batch: &RecordBatch, name: &str, out: &mut Vec<Option<i32>>) -> Result<(), RowsError> {
    for value in ints(batch, name)? {
        out.push(value.map(i32::try_from).transpose()?);
    }
    Ok(())
}

fn i16s(batch: &RecordBatch, name: &str, out: &mut Vec<Option<i16>>) -> Result<(), RowsError> {
    for value in ints(batch, name)? {
        out.push(value.map(i16::try_from).transpose()?);
    }
    Ok(())
}

fn required<T>(values: Vec<Option<T>>, name: &str) -> Result<Vec<T>, RowsError> {
    values
        .into_iter()
        .map(|v| v.ok_or_else(|| format!("NULL in required column {name}").into()))
        .collect()
}

fn floats(batch: &RecordBatch, name: &str, out: &mut Vec<Option<f64>>) -> Result<(), RowsError> {
    let array = column(batch, name)?;
    match array.data_type() {
        DataType::Float64 => out.extend(array.as_primitive::<Float64Type>().iter()),
        _ => return Err(unsupported(name, array)),
    }
    Ok(())
}

fn bools(batch: &RecordBatch, name: &str, out: &mut Vec<bool>) -> Result<(), RowsError> {
    let array = column(batch, name)?;
    match array.data_type() {
        DataType::Boolean => {
            for value in array.as_boolean().iter() {
                out.push(value.ok_or_else(|| format!("NULL in required column {name}"))?);
            }
        }
        _ => return Err(unsupported(name, array)),
    }
    Ok(())
}

fn dates(batch: &RecordBatch, name: &str, out: &mut Vec<Date>) -> Result<(), RowsError> {
    let array = column(batch, name)?;
    match array.data_type() {
        DataType::Date32 => {
            for value in array.as_primitive::<Date32Type>().iter() {
                let days = value.ok_or_else(|| format!("NULL in required column {name}"))?;
                out.push(Date::from_days(days));
            }
        }
        _ => return Err(unsupported(name, array)),
    }
    Ok(())
}

pub fn source_keys(path: &Path, s: &mut Strings) -> Result<SourceKeys, RowsError> {
    let mut t = SourceKeys::default();
    let names = [
        "entity_kind",
        "namespace",
        "identifier",
        "public_id",
        "valid_from",
        "valid_to",
    ];
    for batch in read(path, &names)? {
        strings(&batch, "entity_kind", s, &mut t.entity_kind)?;
        strings(&batch, "namespace", s, &mut t.namespace)?;
        strings(&batch, "identifier", s, &mut t.identifier)?;
        strings(&batch, "public_id", s, &mut t.public_id)?;
        dates(&batch, "valid_from", &mut t.valid_from)?;
        dates(&batch, "valid_to", &mut t.valid_to)?;
    }
    Ok(t)
}

pub fn trips(path: &Path, s: &mut Strings) -> Result<Trips, RowsError> {
    let mut t = Trips::default();
    let names = [
        "trip_id",
        "route_id",
        "service_id",
        "headsign",
        "shape_id",
        "run_key",
        "run_part",
    ];
    for batch in read(path, &names)? {
        strings(&batch, "trip_id", s, &mut t.trip_id)?;
        strings(&batch, "route_id", s, &mut t.route_id)?;
        strings(&batch, "service_id", s, &mut t.service_id)?;
        strings(&batch, "headsign", s, &mut t.headsign)?;
        strings(&batch, "shape_id", s, &mut t.shape_id)?;
        strings(&batch, "run_key", s, &mut t.run_key)?;
        i16s(&batch, "run_part", &mut t.run_part)?;
    }
    Ok(t)
}

pub fn routes(path: &Path, s: &mut Strings) -> Result<Routes, RowsError> {
    let mut t = Routes::default();
    for batch in read(path, &["route_id", "short_name", "mode", "agency_id"])? {
        strings(&batch, "route_id", s, &mut t.route_id)?;
        strings(&batch, "short_name", s, &mut t.short_name)?;
        strings(&batch, "mode", s, &mut t.mode)?;
        strings(&batch, "agency_id", s, &mut t.agency_id)?;
    }
    Ok(t)
}

pub fn calls(path: &Path, s: &mut Strings) -> Result<Calls, RowsError> {
    let mut t = Calls::default();
    let names = [
        "trip_id",
        "sequence",
        "location_id",
        "passenger_service",
        "boarding_point_id",
        "scheduled_arrival",
        "scheduled_departure",
        "pickup_type",
        "dropoff_type",
    ];
    let (mut sequence, mut pickup, mut dropoff) = (Vec::new(), Vec::new(), Vec::new());
    for batch in read(path, &names)? {
        strings(&batch, "trip_id", s, &mut t.trip_id)?;
        i32s(&batch, "sequence", &mut sequence)?;
        strings(&batch, "location_id", s, &mut t.location_id)?;
        bools(&batch, "passenger_service", &mut t.passenger_service)?;
        strings(&batch, "boarding_point_id", s, &mut t.boarding_point_id)?;
        i32s(&batch, "scheduled_arrival", &mut t.scheduled_arrival)?;
        i32s(&batch, "scheduled_departure", &mut t.scheduled_departure)?;
        i16s(&batch, "pickup_type", &mut pickup)?;
        i16s(&batch, "dropoff_type", &mut dropoff)?;
    }
    t.sequence = required(sequence, "sequence")?;
    t.pickup_type = required(pickup, "pickup_type")?;
    t.dropoff_type = required(dropoff, "dropoff_type")?;
    Ok(t)
}

pub fn locations(path: &Path, s: &mut Strings) -> Result<Locations, RowsError> {
    let mut t = Locations::default();
    let names = [
        "location_id",
        "name",
        "longitude",
        "latitude",
        "coordinate_precision",
    ];
    for batch in read(path, &names)? {
        strings(&batch, "location_id", s, &mut t.location_id)?;
        strings(&batch, "name", s, &mut t.name)?;
        floats(&batch, "longitude", &mut t.longitude)?;
        floats(&batch, "latitude", &mut t.latitude)?;
        strings(
            &batch,
            "coordinate_precision",
            s,
            &mut t.coordinate_precision,
        )?;
    }
    Ok(t)
}

pub fn shape_points(path: &Path, s: &mut Strings) -> Result<ShapePoints, RowsError> {
    let mut t = ShapePoints::default();
    let (mut sequence, mut lon, mut lat) = (Vec::new(), Vec::new(), Vec::new());
    for batch in read(path, &["shape_id", "sequence", "longitude", "latitude"])? {
        strings(&batch, "shape_id", s, &mut t.shape_id)?;
        i32s(&batch, "sequence", &mut sequence)?;
        floats(&batch, "longitude", &mut lon)?;
        floats(&batch, "latitude", &mut lat)?;
    }
    t.sequence = required(sequence, "sequence")?;
    t.longitude = required(lon, "longitude")?;
    t.latitude = required(lat, "latitude")?;
    Ok(t)
}

pub fn calendar(path: &Path, s: &mut Strings) -> Result<Calendar, RowsError> {
    let mut t = Calendar::default();
    let mut mask = Vec::new();
    for batch in read(
        path,
        &["service_id", "valid_from", "valid_to", "weekday_mask"],
    )? {
        strings(&batch, "service_id", s, &mut t.service_id)?;
        dates(&batch, "valid_from", &mut t.valid_from)?;
        dates(&batch, "valid_to", &mut t.valid_to)?;
        i16s(&batch, "weekday_mask", &mut mask)?;
    }
    t.weekday_mask = required(mask, "weekday_mask")?;
    Ok(t)
}

pub fn exceptions(path: &Path, s: &mut Strings) -> Result<Exceptions, RowsError> {
    let mut t = Exceptions::default();
    for batch in read(path, &["service_id", "service_date", "added"])? {
        strings(&batch, "service_id", s, &mut t.service_id)?;
        dates(&batch, "service_date", &mut t.service_date)?;
        bools(&batch, "added", &mut t.added)?;
    }
    Ok(t)
}
