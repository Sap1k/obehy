from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, engine_from_config, pool, text

from obehy.persistence.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.environ.get("OBEHY_DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def include_object(
    object_: object, name: str | None, type_: str, reflected: bool, compare_to: object | None
) -> bool:
    del object_, reflected, compare_to
    return not (type_ == "table" and name == "spatial_ref_sys")


def online_include_object(connection: Connection):  # type: ignore[no-untyped-def]
    extension_tables = {
        (schema, name)
        for schema, name in connection.execute(
            text(
                """
                SELECT namespace.nspname, class.relname
                FROM pg_depend dependency
                JOIN pg_extension extension ON extension.oid = dependency.refobjid
                JOIN pg_class class ON class.oid = dependency.objid
                JOIN pg_namespace namespace ON namespace.oid = class.relnamespace
                WHERE dependency.refclassid = 'pg_extension'::regclass
                  AND dependency.classid = 'pg_class'::regclass
                  AND dependency.deptype = 'e'
                """
            )
        )
    }

    def filter_object(
        object_: object,
        name: str | None,
        type_: str,
        reflected: bool,
        compare_to: object | None,
    ) -> bool:
        schema = getattr(object_, "schema", None) or "public"
        if type_ == "table" and reflected and compare_to is None:
            return (schema, name) not in extension_tables
        return include_object(object_, name, type_, reflected, compare_to)

    return filter_object


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # Keep application schemas out of the implicit/default namespace. They are inspected
        # explicitly through include_schemas; including them in search_path would reflect every
        # table twice (qualified and schema=None). PostGIS extension schemas are filtered below.
        connection.execute(text("SET search_path TO public"))
        object_filter = online_include_object(connection)
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_object=object_filter,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
