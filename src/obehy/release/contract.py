"""The JrUtil serving contract (schema 5) as the loader and DDL generator see it."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "data" / "serving" / "serving-v5.json"

SQL_TYPES = {
    "string": "text",
    "int16": "smallint",
    "int32": "integer",
    "double": "double precision",
    "bool": "boolean",
    "date32": "date",
}


class ContractError(RuntimeError):
    """The serving contract or a package's declared schema is unusable."""


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    nullable: bool
    enum: str | None = None

    @property
    def sql_type(self) -> str:
        return SQL_TYPES[self.type]


@dataclass(frozen=True)
class ForeignKey:
    fields: tuple[str, ...]
    relation: str
    target_fields: tuple[str, ...]


@dataclass(frozen=True)
class Relation:
    name: str
    fields: tuple[Field, ...]
    primary_key: tuple[str, ...]
    foreign_keys: tuple[ForeignKey, ...]

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields)


@dataclass(frozen=True)
class Contract:
    version: str
    relations: tuple[Relation, ...]
    enumerations: dict[str, frozenset[str]]

    def relation(self, name: str) -> Relation:
        for relation in self.relations:
            if relation.name == name:
                return relation
        raise KeyError(name)


def _field(value: dict[str, Any]) -> Field:
    field_type = value["type"]
    if field_type not in SQL_TYPES:
        raise ContractError(f"Unsupported contract type {field_type!r} for {value['name']}")
    return Field(
        name=value["name"],
        type=field_type,
        nullable=bool(value["nullable"]),
        enum=value.get("enum"),
    )


def parse_contract(document: dict[str, Any]) -> Contract:
    relations = tuple(
        Relation(
            name=relation["name"],
            fields=tuple(_field(field) for field in relation["fields"]),
            primary_key=tuple(relation["primary_key"]),
            foreign_keys=tuple(
                ForeignKey(
                    fields=tuple(key["fields"]),
                    relation=key["relation"],
                    target_fields=tuple(key["target_fields"]),
                )
                for key in relation["foreign_keys"]
            ),
        )
        for relation in cast(list[dict[str, Any]], document["relations"])
    )
    enumerations = {
        name: frozenset(cast(dict[str, Any], value["values"]))
        for name, value in cast(dict[str, dict[str, Any]], document["enumerations"]).items()
    }
    return Contract(
        version=document["serving_schema_version"],
        relations=relations,
        enumerations=enumerations,
    )


def load_contract(path: Path = CONTRACT_PATH) -> Contract:
    return parse_contract(json.loads(path.read_text(encoding="utf-8")))


def check_declared_schema(contract: Contract, declared: list[dict[str, Any]]) -> None:
    """Require every contract relation and field in a package's declared schema.

    Later minors may add relations and nullable fields; those are ignored here and never
    copied. Anything missing or retyped is a different major and is rejected.
    """

    by_name = {relation["name"]: relation for relation in declared}
    for relation in contract.relations:
        package_relation = by_name.get(relation.name)
        if package_relation is None:
            raise ContractError(f"Package lacks relation {relation.name}")
        fields = {
            field["name"]: field for field in cast(list[dict[str, Any]], package_relation["schema"])
        }
        for field in relation.fields:
            package_field = fields.get(field.name)
            if package_field is None:
                raise ContractError(f"Package relation {relation.name} lacks {field.name}")
            if package_field["type"] != field.type or bool(package_field["nullable"]) != (
                field.nullable
            ):
                raise ContractError(
                    f"Package field {relation.name}.{field.name} is "
                    f"{package_field['type']} nullable={package_field['nullable']}, "
                    f"contract says {field.type} nullable={field.nullable}"
                )
        if tuple(package_relation["primary_key"]) != relation.primary_key:
            raise ContractError(f"Package relation {relation.name} has another primary key")
