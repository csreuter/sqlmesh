# type: ignore
import typing as t

import pandas as pd
import pytest
from sqlglot import parse_one

from sqlmesh.core.engine_adapter import DatabricksEngineAdapter
from tests.core.engine_adapter import to_sql_calls

pytestmark = [pytest.mark.databricks, pytest.mark.engine]


def test_replace_query(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(DatabricksEngineAdapter)
    adapter.replace_query("test_table", parse_one("SELECT a FROM tbl"), {"a": "int"})

    assert to_sql_calls(adapter) == [
        "INSERT OVERWRITE TABLE `test_table` (`a`) SELECT `a` FROM (SELECT `a` FROM `tbl`) AS `_subquery` WHERE TRUE",
    ]


def test_replace_query_pandas(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(DatabricksEngineAdapter)
    df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
    adapter.replace_query("test_table", df, {"a": "int", "b": "int"})

    assert to_sql_calls(adapter) == [
        "INSERT OVERWRITE TABLE `test_table` (`a`, `b`) SELECT `a`, `b` FROM (SELECT CAST(`a` AS INT) AS `a`, CAST(`b` AS INT) AS `b` FROM VALUES (1, 4), (2, 5), (3, 6) AS `t`(`a`, `b`)) AS `_subquery` WHERE TRUE",
    ]


def test_clone_table(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(DatabricksEngineAdapter)
    adapter.clone_table("target_table", "source_table")
    adapter.cursor.execute.assert_called_once_with(
        "CREATE TABLE `target_table` SHALLOW CLONE `source_table`"
    )


def test_set_current_catalog(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(DatabricksEngineAdapter)
    adapter.set_current_catalog("test_catalog")

    assert to_sql_calls(adapter) == ["USE CATALOG `test_catalog`"]


def test_get_current_catalog(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(DatabricksEngineAdapter)
    adapter.cursor.fetchone.return_value = ("test_catalog",)

    assert adapter.get_current_catalog() == "test_catalog"
    assert to_sql_calls(adapter) == ["SELECT CURRENT_CATALOG()"]


def test_get_current_database(make_mocked_engine_adapter: t.Callable):
    adapter = make_mocked_engine_adapter(DatabricksEngineAdapter)
    adapter.cursor.fetchone.return_value = ("test_database",)

    assert adapter.get_current_database() == "test_database"
    assert to_sql_calls(adapter) == ["SELECT CURRENT_DATABASE()"]
