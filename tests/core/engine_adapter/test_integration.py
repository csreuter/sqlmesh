# type: ignore
from __future__ import annotations

import os
import pathlib
import sys
import typing as t
from datetime import timedelta

import pandas as pd
import pytest
from pandas._libs import NaTType
from sqlglot import exp, parse_one
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from sqlmesh import Config, Context, EngineAdapter
from sqlmesh.core.config import load_config_from_paths
from sqlmesh.core.dialect import normalize_model_name
from sqlmesh.core.engine_adapter.shared import DataObject, DataObjectType
from sqlmesh.utils.date import now, to_date, to_ds, yesterday
from sqlmesh.utils.errors import UnsupportedCatalogOperationError
from sqlmesh.utils.pydantic import PydanticModel
from tests.conftest import SushiDataValidator

if t.TYPE_CHECKING:
    from sqlmesh.core.engine_adapter._typing import Query


TEST_SCHEMA = "test_schema"


class TestContext:
    def __init__(
        self,
        test_type: str,
        engine_adapter: EngineAdapter,
        columns_to_types: t.Optional[t.Dict[str, t.Union[str, exp.DataType]]] = None,
    ):
        self.test_type = test_type
        self.engine_adapter = engine_adapter
        self._columns_to_types = columns_to_types

    @property
    def columns_to_types(self):
        if self._columns_to_types is None:
            self._columns_to_types = {
                "id": exp.DataType.build("int"),
                "ds": exp.DataType.build("string"),
            }
        return self._columns_to_types

    @columns_to_types.setter
    def columns_to_types(self, value: t.Dict[str, t.Union[str, exp.DataType]]):
        self._columns_to_types = {
            k: exp.DataType.build(v, dialect=self.dialect) for k, v in value.items()
        }

    @property
    def time_columns(self) -> t.List[str]:
        return [
            k
            for k, v in self.columns_to_types.items()
            if v.sql().lower().startswith("timestamp")
            or v.sql().lower().startswith("date")
            or k.lower() == "ds"
        ]

    @property
    def timestamp_columns(self) -> t.List[str]:
        return [
            k
            for k, v in self.columns_to_types.items()
            if v.sql().lower().startswith("timestamp")
            or (v.sql().lower() == "datetime" and self.dialect == "bigquery")
        ]

    @property
    def time_column(self) -> str:
        return self.time_columns[0]

    @property
    def time_formatter(self) -> t.Callable:
        return lambda x, _: exp.Literal.string(to_ds(x))

    @property
    def partitioned_by(self) -> t.List[exp.Expression]:
        return [parse_one(self.time_column)]

    @property
    def dialect(self) -> str:
        return self.engine_adapter.dialect

    @classmethod
    def _compare_dfs(
        cls, actual: t.Union[pd.DataFrame, exp.Expression, str], expected: pd.DataFrame
    ) -> None:
        def replace_nat_with_none(df):
            return [
                col if type(col) != NaTType else None
                for row in sorted(list(df.itertuples(index=False, name=None)))
                for col in row
            ]

        assert replace_nat_with_none(actual) == replace_nat_with_none(expected)

    def get_metadata_results(self, schema: str = TEST_SCHEMA) -> MetadataResults:
        return MetadataResults.from_data_objects(
            self.engine_adapter._get_data_objects(self.schema(schema))
        )

    def _init_engine_adapter(self) -> None:
        schema = self.schema(TEST_SCHEMA)
        self.engine_adapter.drop_schema(schema, ignore_if_not_exists=True, cascade=True)
        self.engine_adapter.create_schema(schema)

    def _format_df(self, data: pd.DataFrame, to_datetime: bool = True) -> pd.DataFrame:
        for timestamp_column in self.timestamp_columns:
            if timestamp_column in data.columns and to_datetime:
                data[timestamp_column] = pd.to_datetime(data[timestamp_column])
        return data

    def init(self):
        if self.test_type == "pyspark" and not hasattr(self.engine_adapter, "is_pyspark_df"):
            pytest.skip(f"Engine adapter {self.engine_adapter} doesn't support pyspark")
        self._init_engine_adapter()

    def input_data(
        self,
        data: pd.DataFrame,
        columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
    ) -> t.Union[Query, pd.DataFrame]:
        columns_to_types = columns_to_types or self.columns_to_types
        if self.test_type == "query":
            return self.engine_adapter._values_to_sql(
                list(data.itertuples(index=False, name=None)),
                batch_start=0,
                batch_end=sys.maxsize,
                columns_to_types=columns_to_types,
            )
        elif self.test_type == "pyspark":
            return self.engine_adapter.spark.createDataFrame(data)  # type: ignore
        return self._format_df(data, to_datetime=self.dialect != "trino")

    def output_data(self, data: pd.DataFrame) -> pd.DataFrame:
        return self._format_df(data)

    def table(self, table_name: str, schema: str = TEST_SCHEMA) -> exp.Table:
        return exp.to_table(
            normalize_model_name(
                ".".join([schema, table_name]),
                default_catalog=self.engine_adapter.default_catalog,
                dialect=self.dialect,
            )
        )

    def schema(self, schema_name: str, catalog_name: t.Optional[str] = None) -> str:

        return exp.table_name(
            normalize_model_name(
                ".".join(
                    p
                    for p in (catalog_name or self.engine_adapter.default_catalog, schema_name)
                    if p
                )
                if "." not in schema_name
                else schema_name,
                default_catalog=None,
                dialect=self.dialect,
            )
        )

    def get_current_data(self, table: exp.Table) -> pd.DataFrame:
        return self.engine_adapter.fetchdf(exp.select("*").from_(table), quote_identifiers=True)

    def compare_with_current(self, table: exp.Table, expected: pd.DataFrame) -> None:
        self._compare_dfs(self.get_current_data(table), self.output_data(expected))

    def get_table_comment(
        self,
        schema_name: str,
        table_name: str,
        table_kind: str = "BASE TABLE",
    ) -> str:
        if self.dialect in ["postgres", "redshift"]:
            query = f"""
                SELECT
                    pgc.relname,
                    pg_catalog.obj_description(pgc.oid, 'pg_class')
                FROM pg_catalog.pg_class pgc
                INNER JOIN pg_catalog.pg_namespace n
                ON pgc.relnamespace = n.oid
                WHERE
                    n.nspname = '{schema_name}'
                    AND pgc.relname = '{table_name}'
                    AND pgc.relkind = '{'v' if table_kind == "VIEW" else 'r'}'
                ;
            """
        elif self.dialect in ["mysql", "snowflake"]:
            schema_name = schema_name.upper() if self.dialect == "snowflake" else schema_name
            table_name = table_name.upper() if self.dialect == "snowflake" else table_name

            comment_field_name = {
                "mysql": "table_comment",
                "snowflake": "comment",
            }

            query = f"""
                SELECT
                    table_name,
                    {comment_field_name[self.dialect]}
                FROM INFORMATION_SCHEMA.TABLES
                WHERE
                    table_schema='{schema_name}'
                    AND table_name='{table_name}'
                    AND table_type='{table_kind}'
            """
        elif self.dialect == "bigquery":
            query = f"""
                SELECT
                    table_name,
                    option_value
                FROM `region-us.INFORMATION_SCHEMA.TABLE_OPTIONS`
                WHERE
                    table_schema='{schema_name}'
                    AND table_name='{table_name}'
                    AND option_name = 'description'
            """
        elif self.dialect in ["spark", "databricks"]:
            query = f"DESCRIBE TABLE EXTENDED {schema_name}.{table_name}"
        elif self.dialect == "trino":
            query = f"""
                SELECT
                    table_name,
                    comment
                FROM system.metadata.table_comments
                WHERE
                    schema_name = '{schema_name}'
                    AND table_name = '{table_name}'
            """

        result = self.engine_adapter.fetchall(query)

        if result:
            if self.dialect == "bigquery":
                comment = result[0][1].replace('"', "")
            elif self.dialect in ["spark", "databricks"]:
                comment = [x for x in result if x[0] == "Comment"][0][1]
            else:
                comment = result[0][1]

            return comment

        return None

    def get_column_comments(
        self, schema_name: str, table_name: str, table_kind: str = "BASE TABLE"
    ) -> t.Dict[str, str]:
        comment_index = 1
        if self.dialect in ["postgres", "redshift"]:
            query = f"""
                SELECT
                    cols.column_name,
                    pg_catalog.col_description(pgc.oid, cols.ordinal_position::int) AS column_comment
                FROM pg_catalog.pg_class pgc
                INNER JOIN pg_catalog.pg_namespace n
                ON
                    pgc.relnamespace = n.oid
                INNER JOIN information_schema.columns cols
                ON
                    pgc.relname = cols.table_name
                    AND n.nspname = cols.table_schema
                WHERE
                    n.nspname = '{schema_name}'
                    AND pgc.relname = '{table_name}'
                    AND pgc.relkind = '{'v' if table_kind == "VIEW" else 'r'}'
                ;
            """
        elif self.dialect in ["mysql", "snowflake"]:
            schema_name = schema_name.upper() if self.dialect == "snowflake" else schema_name
            table_name = table_name.upper() if self.dialect == "snowflake" else table_name

            comment_field_name = {
                "mysql": "column_comment",
                "snowflake": "comment",
            }

            query = f"""
                SELECT
                    column_name,
                    {comment_field_name[self.dialect]}
                FROM
                    information_schema.columns
                WHERE
                    table_schema = '{schema_name}'
                    AND table_name = '{table_name}'
                ;
            """
        elif self.dialect == "bigquery":
            query = f"""
                SELECT
                    column_name,
                    description
                FROM
                    `region-us.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS`
                WHERE
                    table_schema = '{schema_name}'
                    AND table_name = '{table_name}'
                ;
            """
        elif self.dialect in ["spark", "databricks"]:
            query = f"DESCRIBE TABLE {schema_name}.{table_name}"
            comment_index = 2
        elif self.dialect == "trino":
            query = f"SHOW COLUMNS FROM {schema_name}.{table_name}"
            comment_index = 3

        result = self.engine_adapter.fetchall(query)

        if result:
            if self.dialect in ["spark", "databricks"]:
                result = list(set([x for x in result if not x[0].startswith("#")]))

            comments = {
                x[0]: x[comment_index]
                for x in result
                if x[comment_index] is not None and x[comment_index].strip() != ""
            }

            return comments if comments else None

        return None


class MetadataResults(PydanticModel):
    tables: t.List[str] = []
    views: t.List[str] = []
    materialized_views: t.List[str] = []

    @classmethod
    def from_data_objects(cls, data_objects: t.List[DataObject]) -> MetadataResults:
        tables = []
        views = []
        materialized_views = []
        for obj in data_objects:
            if obj.type.is_table:
                tables.append(obj.name)
            elif obj.type.is_view:
                views.append(obj.name)
            elif obj.type.is_materialized_view:
                materialized_views.append(obj.name)
            else:
                raise ValueError(f"Unexpected object type: {obj.type}")
        return MetadataResults(tables=tables, views=views, materialized_views=materialized_views)

    @property
    def non_temp_tables(self) -> t.List[str]:
        return [x for x in self.tables if not x.startswith("__temp") and not x.startswith("temp")]


@pytest.fixture(params=["df", "query", "pyspark"])
def test_type(request):
    return request.param


@pytest.fixture(scope="session")
def config() -> Config:
    return load_config_from_paths(
        project_paths=[
            pathlib.Path("examples/wursthall/config.yaml"),
            pathlib.Path(os.path.join(os.path.dirname(__file__), "config.yaml")),
        ],
        personal_paths=[pathlib.Path("~/.sqlmesh/config.yaml").expanduser()],
    )


@pytest.fixture(
    params=[
        "duckdb",
        pytest.param(
            "postgres",
            marks=[
                pytest.mark.integration,
                pytest.mark.engine_integration,
                pytest.mark.engine_integration_local,
            ],
        ),
        pytest.param(
            "mysql",
            marks=[
                pytest.mark.integration,
                pytest.mark.engine_integration,
                pytest.mark.engine_integration_local,
            ],
        ),
        pytest.param(
            "mssql",
            marks=[
                pytest.mark.integration,
                pytest.mark.engine_integration,
                pytest.mark.engine_integration_local,
            ],
        ),
        pytest.param(
            "trino",
            marks=[
                pytest.mark.integration,
                pytest.mark.engine_integration,
                pytest.mark.engine_integration_local,
            ],
        ),
        pytest.param(
            "spark",
            marks=[
                pytest.mark.integration,
                pytest.mark.engine_integration,
                pytest.mark.engine_integration_local,
            ],
        ),
        pytest.param("bigquery", marks=[pytest.mark.integration, pytest.mark.engine_integration]),
        pytest.param("databricks", marks=[pytest.mark.integration, pytest.mark.engine_integration]),
        pytest.param("redshift", marks=[pytest.mark.integration, pytest.mark.engine_integration]),
        pytest.param("snowflake", marks=[pytest.mark.integration, pytest.mark.engine_integration]),
    ]
)
def engine_adapter(request, config) -> EngineAdapter:
    gateway = f"inttest_{request.param}"
    if gateway not in config.gateways:
        # TODO: Once everything is fully setup we want to error if a gateway is not configured that we expect
        pytest.skip(f"Gateway {gateway} not configured")
    connection_config = config.gateways[gateway].connection
    engine_adapter = connection_config.create_engine_adapter()
    # Trino: If we batch up the requests then when running locally we get a table not found error after creating the
    # table and then immediately after trying to insert rows into it. There seems to be a delay between when the
    # metastore is made aware of the table and when it responds that it exists. I'm hoping this is not an issue
    # in practice on production machines.
    if request.param != "trino":
        engine_adapter.DEFAULT_BATCH_SIZE = 1
    # Clear our any local db files that may have been left over from previous runs
    if request.param == "duckdb":
        for raw_path in (connection_config.catalogs or {}).values():
            # Once 3.7 support is dropped this can be changed to `pathlib.Path(path).unlink(missing_ok=True)`
            path = pathlib.Path(raw_path)
            if path.is_file():
                path.unlink()
    return engine_adapter


@pytest.fixture
def default_columns_to_types():
    return {"id": exp.DataType.build("int"), "ds": exp.DataType.build("string")}


@pytest.fixture
def ctx(engine_adapter, test_type):
    return TestContext(test_type, engine_adapter)


def test_catalog_operations(ctx: TestContext):
    if (
        ctx.engine_adapter.CATALOG_SUPPORT.is_unsupported
        or ctx.engine_adapter.CATALOG_SUPPORT.is_single_catalog_only
    ):
        pytest.skip(
            f"Engine adapter {ctx.engine_adapter.dialect} doesn't support catalog operations"
        )
    if ctx.test_type != "query":
        pytest.skip("Catalog operation tests only need to run once so we skip anything not query")
    catalog_name = "testing"
    if ctx.dialect == "databricks":
        catalog_name = "catalogtest"
        ctx.engine_adapter.execute(f"CREATE CATALOG IF NOT EXISTS {catalog_name}")
    elif ctx.dialect == "tsql":
        ctx.engine_adapter.cursor.connection.autocommit(True)
        try:
            ctx.engine_adapter.cursor.execute(f"CREATE DATABASE {catalog_name}")
        except Exception:
            pass
        ctx.engine_adapter.cursor.connection.autocommit(False)
    elif ctx.dialect == "snowflake":
        ctx.engine_adapter.execute(f'CREATE DATABASE IF NOT EXISTS "{catalog_name}"')
    current_catalog = ctx.engine_adapter.get_current_catalog()
    ctx.engine_adapter.set_current_catalog(catalog_name)
    assert ctx.engine_adapter.get_current_catalog() == catalog_name
    ctx.engine_adapter.set_current_catalog(current_catalog)
    assert ctx.engine_adapter.get_current_catalog() == current_catalog


def test_drop_schema_catalog(ctx: TestContext):
    def drop_schema_and_validate(schema_name: str):
        ctx.engine_adapter.drop_schema(schema_name, cascade=True)
        results = ctx.get_metadata_results(schema_name)
        assert (
            len(results.tables)
            == len(results.views)
            == len(results.materialized_views)
            == len(results.non_temp_tables)
            == 0
        )

    def create_objects_and_validate(schema_name: str):
        ctx.engine_adapter.create_schema(schema_name)
        ctx.engine_adapter.create_view(f"{schema_name}.test_view", parse_one("SELECT 1 as col"))
        ctx.engine_adapter.create_table(
            f"{schema_name}.test_table", {"col": exp.DataType.build("int")}
        )
        ctx.engine_adapter.create_table(
            f"{schema_name}.replace_table", {"col": exp.DataType.build("int")}
        )
        ctx.engine_adapter.replace_query(
            f"{schema_name}.replace_table",
            parse_one("SELECT 1 as col"),
            {"col": exp.DataType.build("int")},
        )
        results = ctx.get_metadata_results(schema_name)
        assert len(results.tables) == 2
        assert len(results.views) == 1
        assert len(results.materialized_views) == 0
        assert len(results.non_temp_tables) == 2

    if ctx.engine_adapter.CATALOG_SUPPORT.is_unsupported:
        pytest.skip(
            f"Engine adapter {ctx.engine_adapter.dialect} doesn't support catalog operations"
        )
    if ctx.dialect == "spark":
        pytest.skip(
            "Currently local spark is configured to have iceberg be the testing catalog and drop cascade doesn't work on iceberg. Skipping until we have time to fix."
        )
    if ctx.test_type != "query":
        pytest.skip("Drop Schema Catalog tests only need to run once so we skip anything not query")
    catalog_name = "testing"
    if ctx.dialect == "databricks":
        catalog_name = "catalogtest"
        ctx.engine_adapter.execute(f"CREATE CATALOG IF NOT EXISTS {catalog_name}")
    elif ctx.dialect == "tsql":
        ctx.engine_adapter.cursor.connection.autocommit(True)
        try:
            ctx.engine_adapter.cursor.execute(f"CREATE DATABASE {catalog_name}")
        except Exception:
            pass
        ctx.engine_adapter.cursor.connection.autocommit(False)
    elif ctx.dialect == "snowflake":
        ctx.engine_adapter.execute(f'CREATE DATABASE IF NOT EXISTS "{catalog_name}"')
    elif ctx.dialect == "bigquery":
        catalog_name = "tobiko-test"

    schema = ctx.schema("drop_schema_catalog_test", catalog_name)
    if ctx.engine_adapter.CATALOG_SUPPORT.is_single_catalog_only:
        with pytest.raises(
            UnsupportedCatalogOperationError,
            match=".*requires that all catalog operations be against a single catalog.*",
        ):
            drop_schema_and_validate(schema)
        return
    drop_schema_and_validate(schema)
    create_objects_and_validate(schema)


def test_temp_table(ctx: TestContext):
    ctx.init()
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    table = ctx.table("example")
    with ctx.engine_adapter.temp_table(ctx.input_data(input_data), table.sql()) as table_name:
        results = ctx.get_metadata_results()
        assert len(results.views) == 0
        assert len(results.tables) == 1
        assert len(results.non_temp_tables) == 0
        assert len(results.materialized_views) == 0
        ctx.compare_with_current(table_name, input_data)
    results = ctx.get_metadata_results()
    assert len(results.views) == len(results.tables) == len(results.non_temp_tables) == 0


def test_create_table(ctx: TestContext):
    table = ctx.table("test_table")
    ctx.init()
    ctx.engine_adapter.create_table(
        table,
        {"id": exp.DataType.build("int")},
        table_description="test table description",
        column_descriptions={"id": "test id column description"},
    )
    results = ctx.get_metadata_results()
    assert len(results.tables) == 1
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert results.tables[0] == table.name

    if not ctx.engine_adapter.COMMENT_CREATION.is_unsupported:
        table_description = ctx.get_table_comment(TEST_SCHEMA, "test_table")
        column_comments = ctx.get_column_comments(TEST_SCHEMA, "test_table")
        assert table_description == "test table description"
        assert column_comments == {"id": "test id column description"}


def test_ctas(ctx: TestContext):
    ctx.init()
    table = ctx.table("test_table")
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    ctx.engine_adapter.ctas(
        table,
        ctx.input_data(input_data),
        table_description="test table description",
        column_descriptions={"id": "test id column description"},
    )
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(table, input_data)

    if not ctx.engine_adapter.COMMENT_CREATION.is_unsupported:
        table_description = ctx.get_table_comment(TEST_SCHEMA, "test_table")
        column_comments = ctx.get_column_comments(TEST_SCHEMA, "test_table")

        # Trino on Hive COMMENT permissions are separate from standard SQL object permissions.
        # Trino has a bug where CREATE SQL permissions are not passed to COMMENT permissions,
        # which generates permissions errors when COMMENT commands are issued.
        #
        # The errors are thrown for both table and comments, but apparently the
        # table comments are actually registered with the engine. Column comments are not.
        assert table_description == "test table description"
        assert column_comments == (
            None if ctx.dialect == "trino" else {"id": "test id column description"}
        )


def test_create_view(ctx: TestContext):
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    view = ctx.table("test_view")
    ctx.init()
    ctx.engine_adapter.create_view(
        view, ctx.input_data(input_data), table_description="test view description"
    )
    results = ctx.get_metadata_results()
    assert len(results.tables) == 0
    assert len(results.views) == 1
    assert len(results.materialized_views) == 0
    assert results.views[0] == view.name
    ctx.compare_with_current(view, input_data)

    if (
        not ctx.engine_adapter.COMMENT_CREATION.is_unsupported
        and ctx.engine_adapter.SUPPORTS_VIEW_COMMENT
    ):
        table_description = ctx.get_table_comment(TEST_SCHEMA, "test_view", table_kind="VIEW")
        assert table_description == "test view description"


def test_materialized_view(ctx: TestContext):
    if not ctx.engine_adapter.SUPPORTS_MATERIALIZED_VIEWS:
        pytest.skip(f"Engine adapter {ctx.engine_adapter} doesn't support materialized views")
    if ctx.engine_adapter.dialect == "databricks":
        pytest.skip(
            "Databricks requires DBSQL Serverless or Pro warehouse to test materialized views which we do not have setup"
        )
    if ctx.engine_adapter.dialect == "snowflake":
        pytest.skip("Snowflake requires enterprise edition which we do not have setup")
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    ctx.init()
    source_table = ctx.table("source_table")
    ctx.engine_adapter.ctas(source_table, ctx.input_data(input_data), ctx.columns_to_types)
    view = ctx.table("test_view")
    view_query = exp.select(*ctx.columns_to_types).from_(source_table)
    ctx.engine_adapter.create_view(view, view_query, materialized=True)
    results = ctx.get_metadata_results()
    # Redshift considers the underlying dataset supporting materialized views as a table therefore we get 2
    # tables in the result
    if ctx.engine_adapter.dialect == "redshift":
        assert len(results.tables) == 2
    else:
        assert len(results.tables) == 1
    assert len(results.views) == 0
    assert len(results.materialized_views) == 1
    assert results.materialized_views[0] == view.name
    ctx.compare_with_current(view, input_data)
    # Make sure that dropping a materialized view also works
    ctx.engine_adapter.drop_view(view, materialized=True)
    results = ctx.get_metadata_results()
    assert len(results.materialized_views) == 0


def test_drop_schema(ctx: TestContext):
    if ctx.test_type != "query":
        pytest.skip("Drop Schema tests only need to run once so we skip anything not query")
    ctx.columns_to_types = {"one": "int"}
    schema = normalize_identifiers(
        exp.to_identifier(TEST_SCHEMA), dialect=ctx.engine_adapter.dialect
    ).sql(dialect=ctx.engine_adapter.dialect)
    ctx.engine_adapter.drop_schema(schema, cascade=True)
    results = ctx.get_metadata_results()
    assert len(results.tables) == 0
    assert len(results.views) == 0

    ctx.engine_adapter.create_schema(schema)
    view = ctx.table("test_view")
    view_query = exp.Select().select(exp.Literal.number(1).as_("one"))
    ctx.engine_adapter.create_view(view, view_query, ctx.columns_to_types)
    results = ctx.get_metadata_results(schema)
    assert len(results.tables) == 0
    assert len(results.views) == 1

    ctx.engine_adapter.drop_schema(schema, cascade=True)
    results = ctx.get_metadata_results()
    assert len(results.tables) == 0
    assert len(results.views) == 0


def test_replace_query(ctx: TestContext):
    ctx.engine_adapter.DEFAULT_BATCH_SIZE = sys.maxsize
    ctx.init()
    table = ctx.table("test_table")
    # Initial Load
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    ctx.engine_adapter.create_table(table, ctx.columns_to_types)
    ctx.engine_adapter.replace_query(
        table,
        ctx.input_data(input_data),
        # Spark based engines do a create table -> insert overwrite instead of replace. If columns to types aren't
        # provided then it checks the table itself for types. This is fine within SQLMesh since we always know the tables
        # exist prior to evaluation but when running these tests that isn't the case. As a result we just pass in
        # columns_to_types for these two engines so we can still test inference on the other ones
        columns_to_types=ctx.columns_to_types if ctx.dialect in ["spark", "databricks"] else None,
    )
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(table, input_data)

    # Replace that we only need to run once
    if type == "df":
        replace_data = pd.DataFrame(
            [
                {"id": 4, "ds": "2022-01-04"},
                {"id": 5, "ds": "2022-01-05"},
                {"id": 6, "ds": "2022-01-06"},
            ]
        )
        ctx.engine_adapter.replace_query(
            table,
            ctx.input_data(replace_data),
            columns_to_types=ctx.columns_to_types
            if ctx.dialect in ["spark", "databricks"]
            else None,
        )
        results = ctx.get_metadata_results()
        assert len(results.views) == 0
        assert len(results.materialized_views) == 0
        assert len(results.tables) == len(results.non_temp_tables) == 1
        assert results.non_temp_tables[0] == table.name
        ctx.compare_with_current(table, replace_data)


def test_insert_append(ctx: TestContext):
    ctx.init()
    table = ctx.table("test_table")
    ctx.engine_adapter.create_table(table, ctx.columns_to_types)
    # Initial Load
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    ctx.engine_adapter.insert_append(table, ctx.input_data(input_data))
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(table, input_data)

    # Replace that we only need to run once
    if type == "df":
        append_data = pd.DataFrame(
            [
                {"id": 4, "ds": "2022-01-04"},
                {"id": 5, "ds": "2022-01-05"},
                {"id": 6, "ds": "2022-01-06"},
            ]
        )
        ctx.engine_adapter.insert_append(table, ctx.input_data(append_data))
        results = ctx.get_metadata_results()
        assert len(results.views) == 0
        assert len(results.materialized_views) == 0
        assert len(results.tables) in [1, 2, 3]
        assert len(results.non_temp_tables) == 1
        assert results.non_temp_tables[0] == table.name
        ctx.compare_with_current(table, pd.concat([input_data, append_data]))


def test_insert_overwrite_by_time_partition(ctx: TestContext):
    ds_type = "string"
    if ctx.dialect == "bigquery":
        ds_type = "datetime"
    if ctx.dialect == "tsql":
        ds_type = "varchar(max)"

    ctx.columns_to_types = {"id": "int", "ds": ds_type}
    ctx.init()
    table = ctx.table("test_table")
    if ctx.dialect == "bigquery":
        partitioned_by = ["DATE(ds)"]
    else:
        partitioned_by = ctx.partitioned_by  # type: ignore
    ctx.engine_adapter.create_table(
        table,
        ctx.columns_to_types,
        partitioned_by=partitioned_by,
        partition_interval_unit="DAY",
    )
    input_data = pd.DataFrame(
        [
            {"id": 1, ctx.time_column: "2022-01-01"},
            {"id": 2, ctx.time_column: "2022-01-02"},
            {"id": 3, ctx.time_column: "2022-01-03"},
        ]
    )
    ctx.engine_adapter.insert_overwrite_by_time_partition(
        table,
        ctx.input_data(input_data),
        start="2022-01-02",
        end="2022-01-03",
        time_formatter=ctx.time_formatter,
        time_column=ctx.time_column,
        columns_to_types=ctx.columns_to_types,
    )
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(table, input_data.iloc[1:])

    if test_type == "df":
        overwrite_data = pd.DataFrame(
            [
                {"id": 10, ctx.time_column: "2022-01-03"},
                {"id": 4, ctx.time_column: "2022-01-04"},
                {"id": 5, ctx.time_column: "2022-01-05"},
            ]
        )
        ctx.engine_adapter.insert_overwrite_by_time_partition(
            table,
            ctx.input_data(overwrite_data),
            start="2022-01-03",
            end="2022-01-05",
            time_formatter=ctx.time_formatter,
            time_column=ctx.time_column,
            columns_to_types=ctx.columns_to_types,
        )
        results = ctx.get_metadata_results()
        assert len(results.views) == 0
        assert len(results.materialized_views) == 0
        assert len(results.tables) == len(results.non_temp_tables) == 1
        assert results.non_temp_tables[0] == table.name
        ctx.compare_with_current(
            table,
            pd.DataFrame(
                [
                    {"id": 2, ctx.time_column: "2022-01-02"},
                    {"id": 10, ctx.time_column: "2022-01-03"},
                    {"id": 4, ctx.time_column: "2022-01-04"},
                    {"id": 5, ctx.time_column: "2022-01-05"},
                ]
            ),
        )


def test_merge(ctx: TestContext):
    if ctx.dialect in ("trino", "spark"):
        pytest.skip(f"{ctx.dialect} doesn't support merge")

    ctx.init()
    table = ctx.table("test_table")
    ctx.engine_adapter.create_table(table, ctx.columns_to_types)
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    ctx.engine_adapter.merge(
        table,
        ctx.input_data(input_data),
        columns_to_types=None,
        unique_key=[exp.to_identifier("id")],
    )
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(table, input_data)

    if test_type == "df":
        merge_data = pd.DataFrame(
            [
                {"id": 2, "ds": "2022-01-10"},
                {"id": 4, "ds": "2022-01-04"},
                {"id": 5, "ds": "2022-01-05"},
            ]
        )
        ctx.engine_adapter.merge(
            table,
            ctx.input_data(merge_data),
            columns_to_types=None,
            unique_key=[exp.to_identifier("id")],
        )
        results = ctx.get_metadata_results()
        assert len(results.views) == 0
        assert len(results.materialized_views) == 0
        assert len(results.tables) == len(results.non_temp_tables) == 1
        assert results.non_temp_tables[0] == table.name
        ctx.compare_with_current(
            table,
            pd.DataFrame(
                [
                    {"id": 1, "ds": "2022-01-01"},
                    {"id": 2, "ds": "2022-01-10"},
                    {"id": 3, "ds": "2022-01-03"},
                    {"id": 4, "ds": "2022-01-04"},
                    {"id": 5, "ds": "2022-01-05"},
                ]
            ),
        )


def test_scd_type_2(ctx: TestContext):
    time_type = "datetime" if ctx.dialect == "bigquery" else "timestamp"

    ctx.columns_to_types = {
        "id": "int",
        "name": "string",
        "updated_at": time_type,
        "valid_from": time_type,
        "valid_to": time_type,
    }
    ctx.init()
    table = ctx.table("test_table")
    input_schema = {
        k: v for k, v in ctx.columns_to_types.items() if k not in ("valid_from", "valid_to")
    }
    ctx.engine_adapter.create_table(table, ctx.columns_to_types)
    input_data = pd.DataFrame(
        [
            {"id": 1, "name": "a", "updated_at": "2022-01-01 00:00:00"},
            {"id": 2, "name": "b", "updated_at": "2022-01-02 00:00:00"},
            {"id": 3, "name": "c", "updated_at": "2022-01-03 00:00:00"},
        ]
    )
    ctx.engine_adapter.scd_type_2(
        table,
        ctx.input_data(input_data, input_schema),
        unique_key=[exp.to_identifier("id")],
        valid_from_name="valid_from",
        valid_to_name="valid_to",
        updated_at_name="updated_at",
        execution_time="2023-01-01",
        columns_to_types=input_schema,
    )
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(
        table,
        pd.DataFrame(
            [
                {
                    "id": 1,
                    "name": "a",
                    "updated_at": "2022-01-01 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                },
                {
                    "id": 2,
                    "name": "b",
                    "updated_at": "2022-01-02 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                },
                {
                    "id": 3,
                    "name": "c",
                    "updated_at": "2022-01-03 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                },
            ]
        ),
    )

    if ctx.test_type == "query":
        return
    current_data = pd.DataFrame(
        [
            # Change `a` to `x`
            {"id": 1, "name": "x", "updated_at": "2022-01-04 00:00:00"},
            # Delete
            # {"id": 2, "name": "b", "updated_at": "2022-01-02 00:00:00"},
            # No change
            {"id": 3, "name": "c", "updated_at": "2022-01-03 00:00:00"},
            # Add
            {"id": 4, "name": "d", "updated_at": "2022-01-04 00:00:00"},
        ]
    )
    ctx.engine_adapter.scd_type_2(
        table,
        ctx.input_data(current_data, input_schema),
        unique_key=[exp.to_identifier("id")],
        valid_from_name="valid_from",
        valid_to_name="valid_to",
        updated_at_name="updated_at",
        execution_time="2023-01-05",
        columns_to_types=input_schema,
    )
    results = ctx.get_metadata_results()
    assert len(results.views) == 0
    assert len(results.materialized_views) == 0
    assert len(results.tables) == len(results.non_temp_tables) == 1
    assert results.non_temp_tables[0] == table.name
    ctx.compare_with_current(
        table,
        pd.DataFrame(
            [
                {
                    "id": 1,
                    "name": "a",
                    "updated_at": "2022-01-01 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": "2022-01-04 00:00:00",
                },
                {
                    "id": 1,
                    "name": "x",
                    "updated_at": "2022-01-04 00:00:00",
                    "valid_from": "2022-01-04 00:00:00",
                    "valid_to": pd.NaT,
                },
                {
                    "id": 2,
                    "name": "b",
                    "updated_at": "2022-01-02 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": "2023-01-05 00:00:00",
                },
                {
                    "id": 3,
                    "name": "c",
                    "updated_at": "2022-01-03 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                },
                {
                    "id": 4,
                    "name": "d",
                    "updated_at": "2022-01-04 00:00:00",
                    "valid_from": "1970-01-01 00:00:00",
                    "valid_to": pd.NaT,
                },
            ]
        ),
    )


def test_truncate_table(ctx: TestContext):
    if ctx.test_type != "query":
        pytest.skip("Truncate table test does not change based on input data type")

    ctx.init()
    table = ctx.table("test_table")
    ctx.engine_adapter.create_table(table, ctx.columns_to_types)
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    ctx.engine_adapter.insert_append(table, ctx.input_data(input_data))
    ctx.compare_with_current(table, input_data)
    ctx.engine_adapter._truncate_table(table)
    assert ctx.engine_adapter.fetchone(exp.select("count(*)").from_(table))[0] == 0


def test_transaction(ctx: TestContext):
    if ctx.engine_adapter.SUPPORTS_TRANSACTIONS is False:
        pytest.skip(f"Engine adapter {ctx.engine_adapter.dialect} doesn't support transactions")
    if ctx.test_type != "query":
        pytest.skip("Transaction test can just run for query")

    ctx.init()
    table = ctx.table("test_table")
    input_data = pd.DataFrame(
        [
            {"id": 1, "ds": "2022-01-01"},
            {"id": 2, "ds": "2022-01-02"},
            {"id": 3, "ds": "2022-01-03"},
        ]
    )
    with ctx.engine_adapter.transaction():
        ctx.engine_adapter.create_table(table, ctx.columns_to_types)
        ctx.engine_adapter.insert_append(
            table, ctx.input_data(input_data, ctx.columns_to_types), ctx.columns_to_types
        )
    ctx.compare_with_current(table, input_data)
    with ctx.engine_adapter.transaction():
        ctx.engine_adapter._truncate_table(table)
        ctx.engine_adapter._connection_pool.rollback()
    ctx.compare_with_current(table, input_data)


def test_sushi(ctx: TestContext):
    if ctx.test_type != "query":
        pytest.skip("Sushi end-to-end tests only need to run for query")

    config = load_config_from_paths(
        project_paths=[
            pathlib.Path(os.path.join(os.path.dirname(__file__), "config.yaml")),
        ],
        personal_paths=[pathlib.Path("~/.sqlmesh/config.yaml").expanduser()],
    )
    gateway = "inttest_mssql" if ctx.dialect == "tsql" else f"inttest_{ctx.dialect}"
    context = Context(paths="./examples/sushi", config=config, gateway=gateway)

    # clean up any leftover schemas from previous runs (requires context)
    for schema in [
        "sushi__test_prod",
        "sushi__test_dev",
        "sushi",
        "sqlmesh__sushi",
        "sqlmesh",
        "raw",
    ]:
        context.engine_adapter.drop_schema(schema, ignore_if_not_exists=True, cascade=True)

    start = to_date(now() - timedelta(days=7))
    end = now()

    context.plan(
        environment="test_prod",
        start=start,
        end=end,
        skip_tests=True,
        no_prompts=True,
        auto_apply=True,
    )

    data_validator = SushiDataValidator.from_context(context)
    data_validator.validate(
        "sushi.customer_revenue_lifetime",
        start,
        yesterday(),
        env_name="test_prod",
        dialect=ctx.dialect,
    )

    # Ensure table and column comments were registered with engine
    if not ctx.engine_adapter.COMMENT_CREATION.is_unsupported:
        comments = {
            "customer_revenue_by_day": {
                "column": {
                    "customer_id": "Customer id",
                    "revenue": "Revenue from orders made by this customer",
                    "event_date": "Date",
                },
            },
            "customer_revenue_lifetime": {
                "column": {
                    "customer_id": "Customer id",
                    "revenue": "Lifetime revenue from this customer",
                    "event_date": "End date of the lifetime calculation",
                },
            },
            "orders": {
                "table": "Table of sushi orders.",
            },
            "raw_marketing": {
                "table": "Table of marketing status.",
            },
            "waiter_revenue_by_day": {
                "table": "Table of revenue generated by waiters by day.",
                "column": {
                    "waiter_id": "Waiter id",
                    "revenue": "Revenue from orders taken by this waiter",
                    "event_date": "Date",
                },
            },
        }

        physical_layer_objects = context.engine_adapter._get_data_objects("sqlmesh__sushi")
        physical_layer_models = {
            x.name.split("__")[1]: {
                "table_name": x.name,
                "is_view": x.type == DataObjectType.VIEW,
            }
            for x in physical_layer_objects
            if not x.name.endswith("__temp")
        }

        for model_name, comment in comments.items():
            physical_table_name = physical_layer_models[model_name]["table_name"]

            if not (
                physical_layer_models[model_name]["is_view"]
                and not ctx.engine_adapter.SUPPORTS_VIEW_COMMENT
            ):
                expected_tbl_comment = comments.get(model_name).get("table", None)
                if expected_tbl_comment:
                    actual_tbl_comment = ctx.get_table_comment(
                        "sqlmesh__sushi",
                        physical_table_name,
                        table_kind="VIEW"
                        if physical_layer_models[model_name]["is_view"]
                        else "BASE TABLE",
                    )
                    assert expected_tbl_comment == actual_tbl_comment

            expected_col_comments = comments.get(model_name).get("column", None)
            if expected_col_comments:
                actual_col_comments = ctx.get_column_comments("sqlmesh__sushi", physical_table_name)
                for column_name, expected_col_comment in expected_col_comments.items():
                    expected_col_comment = expected_col_comments.get(column_name, None)
                    actual_col_comment = actual_col_comments.get(column_name, None)
                    assert expected_col_comment == actual_col_comment

    # Ensure that the plan has been applied successfully.
    no_change_plan = context.plan(
        environment="test_dev",
        start=start,
        end=end,
        skip_tests=True,
        no_prompts=True,
        include_unmodified=True,
    )
    assert not no_change_plan.requires_backfill
    assert no_change_plan.context_diff.is_new_environment

    # make and validate unmodified dev environment
    no_change_plan.apply()

    data_validator.validate(
        "sushi.customer_revenue_lifetime",
        start,
        yesterday(),
        env_name="test_dev",
        dialect=ctx.dialect,
    )


def test_dialects(ctx: TestContext):
    if ctx.test_type != "query":
        pytest.skip("Dialect tests only need to run once so we skip anything not query")

    from sqlglot import Dialect, parse_one

    dialect = Dialect[ctx.dialect]

    if dialect.NORMALIZATION_STRATEGY == "CASE_INSENSITIVE":
        a = '"a"'
        b = '"b"'
        c = '"c"'
        d = '"d"'
    elif dialect.NORMALIZATION_STRATEGY == "LOWERCASE":
        a = '"a"'
        b = '"B"'
        c = '"c"'
        d = '"d"'
    # https://dev.mysql.com/doc/refman/8.0/en/identifier-case-sensitivity.html
    # if these tests fail for mysql it means you're running on os x or windows
    elif dialect.NORMALIZATION_STRATEGY == "CASE_SENSITIVE":
        a = '"a"'
        b = '"B"'
        c = '"c"'
        d = '"D"'
    else:
        a = '"a"'
        b = '"B"'
        c = '"C"'
        d = '"D"'

    q = parse_one(
        f"""
        WITH
          "a" AS (SELECT 1 w),
          "B" AS (SELECT 1 x),
          c AS (SELECT 1 y),
          D AS (SELECT 1 z)

          SELECT *
          FROM {a}
          CROSS JOIN {b}
          CROSS JOIN {c}
          CROSS JOIN {d}
    """
    )
    df = ctx.engine_adapter.fetchdf(q)
    expected_columns = ["W", "X", "Y", "Z"] if ctx.dialect == "snowflake" else ["w", "x", "y", "z"]
    pd.testing.assert_frame_equal(
        df, pd.DataFrame([[1, 1, 1, 1]], columns=expected_columns), check_dtype=False
    )
