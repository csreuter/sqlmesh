from __future__ import annotations

import logging
import typing as t

from sqlglot import exp

from sqlmesh.core.engine_adapter.base_postgres import BasePostgresEngineAdapter
from sqlmesh.core.engine_adapter.mixins import (
    GetCurrentCatalogFromFunctionMixin,
    LogicalReplaceQueryMixin,
    PandasNativeFetchDFSupportMixin,
)
from sqlmesh.core.engine_adapter.shared import set_catalog

if t.TYPE_CHECKING:
    from sqlmesh.core.engine_adapter._typing import DF

logger = logging.getLogger(__name__)


@set_catalog()
class PostgresEngineAdapter(
    BasePostgresEngineAdapter,
    LogicalReplaceQueryMixin,
    PandasNativeFetchDFSupportMixin,
    GetCurrentCatalogFromFunctionMixin,
):
    DIALECT = "postgres"
    SUPPORTS_INDEXES = True
    HAS_VIEW_BINDING = True
    CURRENT_CATALOG_EXPRESSION = exp.column("current_catalog")

    def _fetch_native_df(
        self, query: t.Union[exp.Expression, str], quote_identifiers: bool = False
    ) -> DF:
        """
        `read_sql_query` when using psycopg will result on a hanging transaction that must be committed

        https://github.com/pandas-dev/pandas/pull/42277
        """
        df = super()._fetch_native_df(query, quote_identifiers)
        if not self._connection_pool.is_transaction_active:
            self._connection_pool.commit()
        return df
