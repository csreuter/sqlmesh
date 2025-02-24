from __future__ import annotations

import functools
import logging
import typing as t
from collections import defaultdict

from hyperscript import h
from IPython.core.display import display
from IPython.core.magic import (
    Magics,
    cell_magic,
    line_cell_magic,
    line_magic,
    magics_class,
)
from IPython.core.magic_arguments import argument, magic_arguments, parse_argstring
from rich.jupyter import JupyterRenderable

from sqlmesh.cli.example_project import ProjectTemplate, init_example_project
from sqlmesh.core import constants as c
from sqlmesh.core.config import load_configs
from sqlmesh.core.console import get_console
from sqlmesh.core.context import Context
from sqlmesh.core.dialect import format_model_expressions, parse
from sqlmesh.core.model import load_sql_based_model
from sqlmesh.core.test import ModelTestMetadata, get_all_model_tests
from sqlmesh.utils import sqlglot_dialects, yaml
from sqlmesh.utils.errors import MagicError, MissingContextException, SQLMeshError

logger = logging.getLogger(__name__)

CONTEXT_VARIABLE_NAMES = [
    "context",
    "ctx",
    "sqlmesh",
]


def pass_sqlmesh_context(func: t.Callable) -> t.Callable:
    @functools.wraps(func)
    def wrapper(self: SQLMeshMagics, *args: t.Any, **kwargs: t.Any) -> None:
        for variable_name in CONTEXT_VARIABLE_NAMES:
            context = self._shell.user_ns.get(variable_name)
            if isinstance(context, Context):
                break
        else:
            raise MissingContextException(
                f"Context must be defined and initialized with one of these names: {', '.join(CONTEXT_VARIABLE_NAMES)}"
            )
        old_console = context.console
        context.console = get_console(display=self.display)
        context.refresh()
        func(self, context, *args, **kwargs)
        context.console = old_console

    return wrapper


@magics_class
class SQLMeshMagics(Magics):
    @property
    def display(self) -> t.Callable:
        from sqlmesh import RuntimeEnv

        if RuntimeEnv.get().is_databricks:
            # Use Databricks' special display instead of the normal IPython display
            return self._shell.user_ns["display"]
        return display

    @property
    def _shell(self) -> t.Any:
        # Make mypy happy.
        if not self.shell:
            raise RuntimeError("IPython Magics are in invalid state")
        return self.shell

    @magic_arguments()
    @argument(
        "paths",
        type=str,
        nargs="+",
        default="",
        help="The path(s) to the SQLMesh project(s).",
    )
    @argument(
        "--config",
        type=str,
        help="Name of the config object. Only applicable to configuration defined using Python script.",
    )
    @argument("--gateway", type=str, help="The name of the gateway.")
    @argument("--ignore-warnings", action="store_true", help="Ignore warnings.")
    @argument("--debug", action="store_true", help="Enable debug mode.")
    @line_magic
    def context(self, line: str) -> None:
        """Sets the context in the user namespace."""
        from sqlmesh import configure_logging

        args = parse_argstring(self.context, line)
        configs = load_configs(args.config, args.paths)
        log_limit = list(configs.values())[0].log_limit
        configure_logging(args.debug, args.ignore_warnings, log_limit=log_limit)
        try:
            context = Context(paths=args.paths, config=configs, gateway=args.gateway)
            self._shell.user_ns["context"] = context
        except Exception:
            if args.debug:
                logger.exception("Failed to initialize SQLMesh context")
            raise
        context.console.log_success(f"SQLMesh project context set to: {', '.join(args.paths)}")

    @magic_arguments()
    @argument("path", type=str, help="The path where the new SQLMesh project should be created.")
    @argument(
        "sql_dialect",
        type=str,
        help=f"Default model SQL dialect. Supported values: {sqlglot_dialects()}.",
    )
    @argument(
        "--template",
        "-t",
        type=str,
        help="Project template. Supported values: airflow, dbt, default, empty.",
    )
    @line_magic
    def init(self, line: str) -> None:
        """Creates a SQLMesh project scaffold with a default SQL dialect."""
        args = parse_argstring(self.init, line)
        try:
            project_template = ProjectTemplate(
                args.template.lower() if args.template else "default"
            )
        except ValueError:
            raise MagicError(f"Invalid project template '{args.template}'")
        init_example_project(args.path, args.sql_dialect, project_template)
        html = str(
            h(
                "div",
                h(
                    "span",
                    {"style": {"color": "green", "font-weight": "bold"}},
                    "SQLMesh project scaffold created",
                ),
            )
        )
        self.display(JupyterRenderable(html=html, text=""))

    @magic_arguments()
    @argument("model", type=str, help="The model.")
    @argument("--start", "-s", type=str, help="Start date to render.")
    @argument("--end", "-e", type=str, help="End date to render.")
    @argument("--execution-time", type=str, help="Execution time.")
    @argument("--dialect", "-d", type=str, help="The rendered dialect.")
    @line_cell_magic
    @pass_sqlmesh_context
    def model(self, context: Context, line: str, sql: t.Optional[str] = None) -> None:
        """Renders the model and automatically fills in an editable cell with the model definition."""
        args = parse_argstring(self.model, line)
        model = context.get_model(args.model, raise_if_missing=True)

        if sql:
            config = context.config_for_node(model)
            loaded = load_sql_based_model(
                parse(sql, default_dialect=config.dialect),
                macros=context._macros,
                jinja_macros=context._jinja_macros,
                path=model._path,
                dialect=config.dialect,
                time_column_format=config.time_column_format,
                physical_schema_override=context.config.physical_schema_override,
                default_catalog=context.default_catalog,
            )

            if loaded.name == args.model:
                model = loaded

        context.upsert_model(model)
        expressions = model.render_definition(include_python=False)

        formatted = format_model_expressions(expressions, model.dialect)

        self._shell.set_next_input(
            "\n".join(
                [
                    " ".join(["%%model", line]),
                    formatted,
                ]
            ),
            replace=True,
        )

        with open(model._path, "w", encoding="utf-8") as file:
            file.write(formatted)

        if sql:
            context.console.log_success(f"Model `{args.model}` updated")

        context.upsert_model(model)
        context.console.show_sql(
            context.render(
                model.name,
                start=args.start,
                end=args.end,
                execution_time=args.execution_time,
            ).sql(pretty=True, dialect=args.dialect or model.dialect)
        )

    @magic_arguments()
    @argument("model", type=str, help="The model.")
    @argument("test_name", type=str, nargs="?", default=None, help="The test name to display")
    @argument("--ls", action="store_true", help="List tests associated with a model")
    @line_cell_magic
    @pass_sqlmesh_context
    def test(self, context: Context, line: str, test_def_raw: t.Optional[str] = None) -> None:
        """Allow the user to list tests for a model, output a specific test, and then write their changes back"""
        args = parse_argstring(self.test, line)
        if not args.test_name and not args.ls:
            raise MagicError("Must provide either test name or `--ls` to list tests")

        test_meta = []

        for path, config in context.configs.items():
            test_meta.extend(
                get_all_model_tests(
                    path / c.TESTS,
                    ignore_patterns=config.ignore_patterns,
                )
            )

        tests: t.Dict[str, t.Dict[str, ModelTestMetadata]] = defaultdict(dict)
        for model_test_metadata in test_meta:
            model = model_test_metadata.body.get("model")
            if not model:
                context.console.log_error(
                    f"Test found that does not have `model` defined: {model_test_metadata.path}"
                )
            else:
                tests[model][model_test_metadata.test_name] = model_test_metadata

        model = context.get_model(args.model, raise_if_missing=True)

        if args.ls:
            # TODO: Provide better UI for displaying tests
            for test_name in tests[model.name]:
                context.console.log_status_update(test_name)
            return

        test = tests[model.name][args.test_name]
        test_def = yaml.load(test_def_raw) if test_def_raw else test.body
        test_def_output = yaml.dump(test_def)

        self._shell.set_next_input(
            "\n".join(
                [
                    " ".join(["%%test", line]),
                    test_def_output,
                ]
            ),
            replace=True,
        )

        with open(test.path, "r+", encoding="utf-8") as file:
            content = yaml.load(file.read())
            content[args.test_name] = test_def
            file.seek(0)
            yaml.dump(content, file)
            file.truncate()

    @magic_arguments()
    @argument(
        "environment",
        nargs="?",
        type=str,
        help="The environment to run the plan against",
    )
    @argument("--start", "-s", type=str, help="Start date to backfill.")
    @argument("--end", "-e", type=str, help="End date to backfill.")
    @argument("--execution-time", type=str, help="Execution time.")
    @argument(
        "--create-from",
        type=str,
        help="The environment to create the target environment from if it doesn't exist. Default: prod.",
    )
    @argument(
        "--skip-tests",
        "-t",
        action="store_true",
        help="Skip the unit tests defined for the model.",
    )
    @argument(
        "--restate-model",
        "-r",
        type=str,
        nargs="*",
        help="Restate data for specified models (and models downstream from the one specified). For production environment, all related model versions will have their intervals wiped, but only the current versions will be backfilled. For development environment, only the current model versions will be affected.",
    )
    @argument(
        "--no-gaps",
        "-g",
        action="store_true",
        help="Ensure that new snapshots have no data gaps when comparing to existing snapshots for matching models in the target environment.",
    )
    @argument(
        "--skip-backfill",
        action="store_true",
        help="Skip the backfill step.",
    )
    @argument(
        "--forward-only",
        action="store_true",
        help="Create a plan for forward-only changes.",
    )
    @argument(
        "--effective-from",
        type=str,
        help="The effective date from which to apply forward-only changes on production.",
    )
    @argument(
        "--no-prompts",
        action="store_true",
        help="Disables interactive prompts for the backfill time range. Please note that if this flag is set and there are uncategorized changes, plan creation will fail.",
    )
    @argument(
        "--auto-apply",
        action="store_true",
        help="Automatically applies the new plan after creation.",
    )
    @argument(
        "--no-auto-categorization",
        action="store_true",
        help="Disable automatic change categorization.",
        default=None,
    )
    @argument(
        "--include-unmodified",
        action="store_true",
        help="Include unmodified models in the target environment.",
        default=None,
    )
    @argument(
        "--select-model",
        type=str,
        nargs="*",
        help="Select specific model changes that should be included in the plan.",
    )
    @argument(
        "--backfill-model",
        type=str,
        nargs="*",
        help="Backfill only the models whose names match the expression. This is supported only when targeting a development environment.",
    )
    @argument(
        "--no-diff",
        action="store_true",
        help="Hide text differences for changed models.",
    )
    @argument(
        "--run",
        action="store_true",
        help="Run latest intervals as part of the plan application (prod environment only).",
    )
    @line_magic
    @pass_sqlmesh_context
    def plan(self, context: Context, line: str) -> None:
        """Goes through a set of prompts to both establish a plan and apply it"""
        args = parse_argstring(self.plan, line)

        context.plan(
            args.environment,
            start=args.start,
            end=args.end,
            execution_time=args.execution_time,
            create_from=args.create_from,
            skip_tests=args.skip_tests,
            restate_models=args.restate_model,
            backfill_models=args.backfill_model,
            no_gaps=args.no_gaps,
            skip_backfill=args.skip_backfill,
            forward_only=args.forward_only,
            no_prompts=args.no_prompts,
            auto_apply=args.auto_apply,
            no_auto_categorization=args.no_auto_categorization,
            effective_from=args.effective_from,
            include_unmodified=args.include_unmodified,
            select_models=args.select_model,
            no_diff=args.no_diff,
            run=args.run,
        )

    @magic_arguments()
    @argument(
        "environment",
        nargs="?",
        type=str,
        help="The environment to run against",
    )
    @argument("--start", "-s", type=str, help="Start date to evaluate.")
    @argument("--end", "-e", type=str, help="End date to evaluate.")
    @argument("--skip-janitor", action="store_true", help="Skip the janitor task.")
    @argument(
        "--ignore-cron",
        action="store_true",
        help="Run for all missing intervals, ignoring individual cron schedules.",
    )
    @line_magic
    @pass_sqlmesh_context
    def run_dag(self, context: Context, line: str) -> None:
        """Evaluate the DAG of models using the built-in scheduler."""
        args = parse_argstring(self.run_dag, line)

        success = context.run(
            args.environment,
            start=args.start,
            end=args.end,
            skip_janitor=args.skip_janitor,
            ignore_cron=args.ignore_cron,
        )
        if not success:
            raise SQLMeshError("Error Running DAG. Check logs for details.")

    @magic_arguments()
    @argument("model", type=str, help="The model.")
    @argument("--start", "-s", type=str, help="Start date to render.")
    @argument("--end", "-e", type=str, help="End date to render.")
    @argument("--execution-time", type=str, help="Execution time.")
    @argument(
        "--limit",
        type=int,
        help="The number of rows which the query should be limited to.",
    )
    @line_magic
    @pass_sqlmesh_context
    def evaluate(self, context: Context, line: str) -> None:
        """Evaluate a model query and fetches a dataframe."""
        context.refresh()
        args = parse_argstring(self.evaluate, line)

        df = context.evaluate(
            args.model,
            start=args.start,
            end=args.end,
            execution_time=args.execution_time,
            limit=args.limit,
        )
        self.display(df)

    @magic_arguments()
    @argument("model", type=str, help="The model.")
    @argument("--start", "-s", type=str, help="Start date to render.")
    @argument("--end", "-e", type=str, help="End date to render.")
    @argument("--execution-time", type=str, help="Execution time.")
    @argument(
        "--expand",
        type=t.Union[bool, t.Iterable[str]],
        help="Whether or not to use expand materialized models, defaults to False. If True, all referenced models are expanded as raw queries. If a list, only referenced models are expanded as raw queries.",
    )
    @argument("--dialect", type=str, help="SQL dialect to render.")
    @argument("--no-format", action="store_true", help="Disable fancy formatting of the query.")
    @line_magic
    @pass_sqlmesh_context
    def render(self, context: Context, line: str) -> None:
        """Renders a model's query, optionally expanding referenced models."""
        context.refresh()
        args = parse_argstring(self.render, line)

        query = context.render(
            args.model,
            start=args.start,
            end=args.end,
            execution_time=args.execution_time,
            expand=args.expand,
        )

        sql = query.sql(pretty=True, dialect=args.dialect or context.config.dialect)
        if args.no_format:
            context.console.log_status_update(sql)
        else:
            context.console.show_sql(sql)

    @magic_arguments()
    @argument(
        "df_var",
        default=None,
        nargs="?",
        type=str,
        help="An optional variable name to store the resulting dataframe.",
    )
    @cell_magic
    @pass_sqlmesh_context
    def fetchdf(self, context: Context, line: str, sql: str) -> None:
        """Fetches a dataframe from sql, optionally storing it in a variable."""
        args = parse_argstring(self.fetchdf, line)
        df = context.fetchdf(sql)
        if args.df_var:
            self._shell.user_ns[args.df_var] = df
        self.display(df)

    @magic_arguments()
    @argument("--file", "-f", type=str, help="An optional file path to write the HTML output to.")
    @argument(
        "--select-model",
        type=str,
        nargs="*",
        help="Select specific models to include in the dag.",
    )
    @line_magic
    @pass_sqlmesh_context
    def dag(self, context: Context, line: str) -> None:
        """Displays the HTML DAG."""
        args = parse_argstring(self.dag, line)
        dag = context.get_dag(args.select_model)
        if args.file:
            with open(args.file, "w") as file:
                file.write(str(dag))
        # TODO: Have this go through console instead of calling display directly
        self.display(dag)

    @magic_arguments()
    @line_magic
    @pass_sqlmesh_context
    def migrate(self, context: Context, line: str) -> None:
        """Migrate SQLMesh to the current running version."""
        context.migrate()
        context.console.log_success("Migration complete")

    @magic_arguments()
    @line_magic
    @pass_sqlmesh_context
    def create_external_models(self, context: Context, line: str) -> None:
        """Create a schema file containing external model schemas."""
        context.create_external_models()

    @magic_arguments()
    @argument(
        "source_to_target",
        type=str,
        metavar="SOURCE:TARGET",
        help="Source and target in `SOURCE:TARGET` format",
    )
    @argument(
        "--on",
        type=str,
        nargs="*",
        help="The column to join on. Can be specified multiple times. The model grain will be used if not specified.",
    )
    @argument(
        "--model",
        type=str,
        help="The model to diff against when source and target are environments and not tables.",
    )
    @argument(
        "--where",
        type=str,
        help="An optional where statement to filter results.",
    )
    @argument(
        "--limit",
        type=int,
        default=20,
        help="The limit of the sample dataframe.",
    )
    @argument(
        "--show-sample",
        action="store_true",
        help="Show a sample of the rows that differ. With many columns, the output can be very wide.",
    )
    @line_magic
    @pass_sqlmesh_context
    def table_diff(self, context: Context, line: str) -> None:
        """Show the diff between two tables.

        Can either be two tables or two environments and a model.
        """
        args = parse_argstring(self.table_diff, line)
        source, target = args.source_to_target.split(":")
        context.table_diff(
            source=source,
            target=target,
            on=args.on,
            model_or_snapshot=args.model,
            where=args.where,
            limit=args.limit,
            show_sample=args.show_sample,
        )

    @magic_arguments()
    @argument(
        "--read",
        type=str,
        default="",
        help="The input dialect of the sql string.",
    )
    @argument(
        "--write",
        type=str,
        default="",
        help="The output dialect of the sql string.",
    )
    @line_cell_magic
    @pass_sqlmesh_context
    def rewrite(self, context: Context, line: str, sql: str) -> None:
        """Rewrite a sql expression with semantic references into an executable query.

        https://sqlmesh.readthedocs.io/en/latest/concepts/metrics/overview/
        """
        args = parse_argstring(self.rewrite, line)
        context.console.show_sql(
            context.rewrite(sql, args.read).sql(
                dialect=args.write or context.config.dialect, pretty=True
            )
        )

    @magic_arguments()
    @argument(
        "--transpile",
        "-t",
        type=str,
        help="Transpile project models to the specified dialect.",
    )
    @argument(
        "--new-line",
        action="store_true",
        help="The output dialect of the sql string.",
    )
    @line_magic
    @pass_sqlmesh_context
    def format(self, context: Context, line: str) -> None:
        """Format all SQL models."""
        args = parse_argstring(self.format, line)
        context.format(args.transpile, args.new_line)

    @magic_arguments()
    @argument("environment", type=str, help="The environment to diff local state against.")
    @line_magic
    @pass_sqlmesh_context
    def diff(self, context: Context, line: str) -> None:
        """Show the diff between the local state and the target environment."""
        args = parse_argstring(self.diff, line)
        context.diff(args.environment)

    @magic_arguments()
    @argument("environment", type=str, help="The environment to invalidate.")
    @line_magic
    @pass_sqlmesh_context
    def invalidate(self, context: Context, line: str) -> None:
        """Invalidate the target environment, forcing its removal during the next run of the janitor process."""
        args = parse_argstring(self.invalidate, line)
        context.invalidate_environment(args.environment)

    @magic_arguments()
    @argument("model", type=str)
    @argument(
        "--query",
        "-q",
        type=str,
        nargs="+",
        required=True,
        help="Queries that will be used to generate data for the model's dependencies.",
    )
    @argument(
        "--overwrite",
        "-o",
        action="store_true",
        help="When true, the fixture file will be overwritten in case it already exists.",
    )
    @argument(
        "--var",
        "-v",
        type=str,
        nargs="+",
        help="Key-value pairs that will define variables needed by the model.",
    )
    @argument(
        "--path",
        "-p",
        type=str,
        help="The file path corresponding to the fixture, relative to the test directory. "
        "By default, the fixture will be created under the test directory and the file "
        "name will be inferred based on the test's name.",
    )
    @argument(
        "--name",
        "-n",
        type=str,
        help="The name of the test that will be created. By default, it's inferred based on the model's name.",
    )
    @line_magic
    @pass_sqlmesh_context
    def create_test(self, context: Context, line: str) -> None:
        """Generate a unit test fixture for a given model."""
        args = parse_argstring(self.create_test, line)
        queries = iter(args.query)
        variables = iter(args.var) if args.var else None
        context.create_test(
            args.model,
            input_queries={k: v.strip('"') for k, v in dict(zip(queries, queries)).items()},
            overwrite=args.overwrite,
            variables=dict(zip(variables, variables)) if variables else None,
            name=args.name,
            path=args.path,
        )

    @magic_arguments()
    @argument("tests", nargs="*", type=str)
    @argument(
        "--pattern",
        "-k",
        nargs="*",
        type=str,
        help="Only run tests that match the pattern of substring.",
    )
    @argument("--verbose", "-v", action="store_true", help="Verbose output.")
    @line_magic
    @pass_sqlmesh_context
    def run_test(self, context: Context, line: str) -> None:
        """Run unit test(s)."""
        args = parse_argstring(self.run_test, line)
        context.test(match_patterns=args.pattern, tests=args.tests, verbose=args.verbose)

    @magic_arguments()
    @argument(
        "models", type=str, nargs="*", help="A model to audit. Multiple models can be audited."
    )
    @argument("--start", "-s", type=str, help="Start date to audit.")
    @argument("--end", "-e", type=str, help="End date to audit.")
    @argument("--execution-time", type=str, help="Execution time.")
    @line_magic
    @pass_sqlmesh_context
    def audit(self, context: Context, line: str) -> None:
        """Run audit(s)"""
        args = parse_argstring(self.audit, line)
        context.audit(
            models=args.models, start=args.start, end=args.end, execution_time=args.execution_time
        )

    @magic_arguments()
    @line_magic
    @pass_sqlmesh_context
    def info(self, context: Context, line: str) -> None:
        """Display SQLMesh project information."""
        context.print_info()

    @magic_arguments()
    @line_magic
    @pass_sqlmesh_context
    def rollback(self, context: Context, line: str) -> None:
        """Rollback SQLMesh to the previous migration."""
        context.rollback()


def register_magics() -> None:
    try:
        shell = get_ipython()  # type: ignore
        shell.register_magics(SQLMeshMagics)
    except NameError:
        pass
