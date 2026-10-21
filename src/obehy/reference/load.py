"""Mirror a dataset into `ref.*`: its previous rows go and the new ones come in one
transaction, so readers see one version or the other."""

from __future__ import annotations

import psycopg

from obehy.reference.dataset import Dataset
from obehy.reference.vocabulary import load_vocabulary


def load_dataset(connection: psycopg.Connection, dataset: Dataset) -> bool:
    """Load `dataset` unless this version is already loaded; True when it was loaded."""

    dataset.check(load_vocabulary())
    with connection.transaction():
        row = connection.execute(
            "SELECT version FROM ref.dataset WHERE dataset = %s FOR UPDATE", (dataset.name,)
        ).fetchone()
        if row is not None and row[0] == dataset.version:
            return False
        connection.execute("DELETE FROM ref.dataset WHERE dataset = %s", (dataset.name,))
        connection.execute(
            "INSERT INTO ref.dataset (dataset, version, title, private, priority, rows)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (
                dataset.name,
                dataset.version,
                dataset.title,
                dataset.private,
                dataset.priority,
                dataset.rows,
            ),
        )
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO ref.vehicle_model (vehicle_model_id, dataset, manufacturer, model,"
                " mode) VALUES (%s, %s, %s, %s, %s)",
                [
                    (m.vehicle_model_id, dataset.name, m.manufacturer, m.model, m.mode)
                    for m in dataset.models
                ],
            )
            cursor.executemany(
                "INSERT INTO ref.vehicle (vehicle_id, dataset, vehicle_model_id, public_label)"
                " VALUES (%s, %s, %s, %s)",
                [
                    (v.vehicle_id, dataset.name, v.vehicle_model_id, v.public_label)
                    for v in dataset.vehicles
                ],
            )
            cursor.executemany(
                "INSERT INTO ref.vehicle_key (dataset, vehicle_id, source, source_vehicle_id,"
                " scheme, identifier, valid_from, valid_to)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        dataset.name,
                        k.vehicle_id,
                        k.source,
                        k.source_vehicle_id,
                        k.scheme,
                        k.identifier,
                        k.valid_from,
                        k.valid_to,
                    )
                    for k in dataset.keys
                ],
            )
            cursor.executemany(
                "INSERT INTO ref.vehicle_attribute (vehicle_id, attribute, value, dataset)"
                " VALUES (%s, %s, %s, %s)",
                [(*a, dataset.name) for a in dataset.attributes],
            )
            cursor.executemany(
                "INSERT INTO ref.model_attribute (vehicle_model_id, attribute, value, dataset)"
                " VALUES (%s, %s, %s, %s)",
                [(*a, dataset.name) for a in dataset.model_attributes],
            )
    return True
