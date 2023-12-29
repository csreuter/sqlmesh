import typing as t
from datetime import timedelta

import pytest
from freezegun import freeze_time
from pytest_mock.plugin import MockerFixture
from sqlglot import parse_one

from sqlmesh.core.context import Context
from sqlmesh.core.model import IncrementalByTimeRangeKind, SeedKind, SeedModel, SqlModel
from sqlmesh.core.model.seed import Seed
from sqlmesh.core.plan import Plan, SnapshotIntervals
from sqlmesh.core.snapshot import (
    DeployabilityIndex,
    Snapshot,
    SnapshotChangeCategory,
    SnapshotDataVersion,
    SnapshotFingerprint,
    SnapshotId,
)
from sqlmesh.utils.dag import DAG
from sqlmesh.utils.date import (
    now,
    now_timestamp,
    to_date,
    to_datetime,
    to_timestamp,
    yesterday_ds,
)
from sqlmesh.utils.errors import PlanError


def test_forward_only_plan_sets_version(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds")))
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("select 2, ds")))
    snapshot_b.previous_versions = (
        SnapshotDataVersion(
            fingerprint=SnapshotFingerprint(
                data_hash="test_data_hash",
                metadata_hash="test_metadata_hash",
            ),
            version="test_version",
            change_category=SnapshotChangeCategory.FORWARD_ONLY,
        ),
    )
    assert not snapshot_b.version

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {
        snapshot_a.snapshot_id: snapshot_a,
        snapshot_b.snapshot_id: snapshot_b,
    }
    context_diff_mock.added = set()
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.modified_snapshots = {snapshot_b.name: (snapshot_b, snapshot_b)}
    context_diff_mock.new_snapshots = {snapshot_b.snapshot_id: snapshot_b}
    context_diff_mock.added_materialized_snapshot_ids = set()

    plan = Plan(context_diff_mock, forward_only=True)

    assert snapshot_b.version == "test_version"

    # Make sure that the choice can't be set manually.
    with pytest.raises(PlanError, match="Choice setting is not supported by a forward-only plan."):
        plan.set_choice(snapshot_b, SnapshotChangeCategory.BREAKING)


def test_forward_only_dev(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, ds"),
            kind=IncrementalByTimeRangeKind(time_column="ds"),
        )
    )

    expected_start = to_date("2022-01-02")
    expected_end = to_date("2022-01-03")
    expected_interval_end = to_timestamp(to_date("2022-01-04"))

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {snapshot_a.snapshot_id: snapshot_a}
    context_diff_mock.added = set()
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.modified_snapshots = {}
    context_diff_mock.new_snapshots = {snapshot_a.snapshot_id: snapshot_a}
    context_diff_mock.added_materialized_snapshot_ids = set()

    yesterday_ds_mock = mocker.patch("sqlmesh.core.plan.definition.yesterday_ds")
    yesterday_ds_mock.return_value = expected_start

    now_mock = mocker.patch("sqlmesh.core.snapshot.definition.now")
    now_mock.return_value = expected_end

    now_ds_mock = mocker.patch("sqlmesh.core.plan.definition.now")
    now_ds_mock.return_value = expected_end

    plan = Plan(context_diff_mock, forward_only=True, is_dev=True)

    assert plan.restatements == {
        snapshot_a.snapshot_id: (to_timestamp(expected_start), expected_interval_end)
    }
    assert plan.start == to_datetime(expected_start)
    assert plan.end == expected_end

    yesterday_ds_mock.assert_called_once()
    now_ds_mock.call_count == 2


def test_forward_only_plan_added_models(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 1 as a, ds")))

    snapshot_b = make_snapshot(
        SqlModel(name="b", query=parse_one("select a, ds from a")), nodes={"a": snapshot_a.node}
    )

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {
        snapshot_a.snapshot_id: snapshot_a,
        snapshot_b.snapshot_id: snapshot_b,
    }
    context_diff_mock.added = {snapshot_b.snapshot_id}
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.modified_snapshots = {snapshot_a.name: (snapshot_a, snapshot_a)}
    context_diff_mock.new_snapshots = {
        snapshot_a.snapshot_id: snapshot_a,
        snapshot_b.snapshot_id: snapshot_b,
    }
    context_diff_mock.added_materialized_snapshot_ids = {snapshot_b.snapshot_id}

    Plan(context_diff_mock, forward_only=True)
    assert snapshot_a.change_category == SnapshotChangeCategory.FORWARD_ONLY
    assert snapshot_b.change_category == SnapshotChangeCategory.BREAKING


def test_paused_forward_only_parent(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds")))
    snapshot_a.previous_versions = (
        SnapshotDataVersion(
            fingerprint=SnapshotFingerprint(
                data_hash="test_data_hash",
                metadata_hash="test_metadata_hash",
            ),
            version="test_version",
            change_category=SnapshotChangeCategory.BREAKING,
        ),
    )
    snapshot_a.categorize_as(SnapshotChangeCategory.FORWARD_ONLY)

    snapshot_b_old = make_snapshot(SqlModel(name="b", query=parse_one("select 2, ds from a")))
    snapshot_b_old.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("select 3, ds from a")))
    assert not snapshot_b.version

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {
        snapshot_a.snapshot_id: snapshot_a,
        snapshot_b.snapshot_id: snapshot_b,
    }
    context_diff_mock.added = set()
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.modified_snapshots = {snapshot_b.name: (snapshot_b, snapshot_b_old)}
    context_diff_mock.new_snapshots = {snapshot_b.snapshot_id: snapshot_b}
    context_diff_mock.added_materialized_snapshot_ids = set()

    Plan(context_diff_mock, forward_only=False)
    assert snapshot_b.change_category == SnapshotChangeCategory.BREAKING


@freeze_time()
def test_restate_models(sushi_context_pre_scheduling: Context):
    plan = sushi_context_pre_scheduling.plan(
        restate_models=["sushi.waiter_revenue_by_day"], no_prompts=True
    )
    assert plan.restatements == {
        SnapshotId(name='"memory"."sushi"."waiter_revenue_by_day"', identifier="643718449"): (
            to_timestamp(plan.start),
            to_timestamp(to_date("today")),
        ),
        SnapshotId(name='"memory"."sushi"."top_waiters"', identifier="630183694"): (
            to_timestamp(plan.start),
            to_timestamp(to_date("today")),
        ),
    }
    assert plan.requires_backfill

    with pytest.raises(PlanError, match=r"""Cannot restate from '"unknown_model"'.*"""):
        sushi_context_pre_scheduling.plan(restate_models=['"unknown_model"'], no_prompts=True)


@freeze_time()
def test_restate_models_with_existing_missing_intervals(sushi_context: Context):
    yesterday_ts = to_timestamp(yesterday_ds())

    assert not sushi_context.plan(no_prompts=True).requires_backfill
    waiter_revenue_by_day = sushi_context.snapshots['"memory"."sushi"."waiter_revenue_by_day"']
    waiter_revenue_by_day.intervals = [
        (waiter_revenue_by_day.intervals[0][0], yesterday_ts),
    ]
    assert sushi_context.plan(no_prompts=True).requires_backfill

    plan = sushi_context.plan(restate_models=["sushi.waiter_revenue_by_day"], no_prompts=True)

    one_day_ms = 24 * 60 * 60 * 1000

    today_ts = to_timestamp(to_date("today"))
    plan_start_ts = to_timestamp(plan.start)
    assert plan_start_ts == today_ts - 7 * one_day_ms

    expected_missing_intervals = [
        (i, i + one_day_ms) for i in range(plan_start_ts, today_ts, one_day_ms)
    ]
    assert len(expected_missing_intervals) == 7

    assert plan.restatements == {
        SnapshotId(name='"memory"."sushi"."waiter_revenue_by_day"', identifier="643718449"): (
            plan_start_ts,
            today_ts,
        ),
        SnapshotId(name='"memory"."sushi"."top_waiters"', identifier="630183694"): (
            plan_start_ts,
            today_ts,
        ),
    }
    assert plan.missing_intervals == [
        SnapshotIntervals(
            snapshot_id=SnapshotId(
                name='"memory"."sushi"."waiter_revenue_by_day"', identifier="643718449"
            ),
            intervals=expected_missing_intervals,
        ),
        SnapshotIntervals(
            snapshot_id=SnapshotId(name='"memory"."sushi"."top_waiters"', identifier="630183694"),
            intervals=expected_missing_intervals,
        ),
    ]
    assert plan.requires_backfill


def test_restate_model_with_merge_strategy(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, key"),
            kind="EMBEDDED",
        )
    )

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {snapshot_a.snapshot_id: snapshot_a}
    context_diff_mock.added = set()
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.modified_snapshots = {}
    context_diff_mock.new_snapshots = {}
    context_diff_mock.added_materialized_snapshot_ids = set()

    with pytest.raises(
        PlanError,
        match="Cannot restate from 'a'. Either such model doesn't exist, no other materialized model references it.*",
    ):
        Plan(context_diff_mock, restate_models=["a"])


def test_new_snapshots_with_restatements(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds")))

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {snapshot_a.snapshot_id: snapshot_a}
    context_diff_mock.added = set()
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.modified_snapshots = {}
    context_diff_mock.new_snapshots = {snapshot_a.snapshot_id: snapshot_a}

    with pytest.raises(
        PlanError,
        match=r"Model changes and restatements can't be a part of the same plan.*",
    ):
        Plan(context_diff_mock, restate_models=["a"])


def test_end_validation(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, ds"),
            kind=IncrementalByTimeRangeKind(time_column="ds"),
        )
    )

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {snapshot_a.snapshot_id: snapshot_a}
    context_diff_mock.added = set()
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.modified_snapshots = {}
    context_diff_mock.new_snapshots = {snapshot_a.snapshot_id: snapshot_a}

    dev_plan = Plan(context_diff_mock, end="2022-01-03", is_dev=True)
    assert dev_plan.end == "2022-01-03"
    dev_plan.end = "2022-01-04"
    assert dev_plan.end == "2022-01-04"

    start_end_not_allowed_message = (
        "The start and end dates can't be set for a production plan without restatements."
    )

    with pytest.raises(PlanError, match=start_end_not_allowed_message):
        Plan(context_diff_mock, end="2022-01-03")

    with pytest.raises(PlanError, match=start_end_not_allowed_message):
        Plan(context_diff_mock, start="2022-01-03")

    prod_plan = Plan(context_diff_mock)

    with pytest.raises(PlanError, match=start_end_not_allowed_message):
        prod_plan.end = "2022-01-03"

    with pytest.raises(PlanError, match=start_end_not_allowed_message):
        prod_plan.start = "2022-01-03"

    context_diff_mock.new_snapshots = {}
    restatement_prod_plan = Plan(
        context_diff_mock,
        start="2022-01-01",
        end="2022-01-03",
        restate_models=['"a"'],
    )
    assert restatement_prod_plan.end == "2022-01-03"
    restatement_prod_plan.end = "2022-01-04"
    assert restatement_prod_plan.end == "2022-01-04"


def test_forward_only_revert_not_allowed(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds")))
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    assert not snapshot.is_forward_only

    forward_only_snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 2, ds")))
    forward_only_snapshot.categorize_as(SnapshotChangeCategory.FORWARD_ONLY)
    forward_only_snapshot.version = snapshot.version
    forward_only_snapshot.unpaused_ts = now_timestamp()
    assert forward_only_snapshot.is_forward_only

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {snapshot.snapshot_id: snapshot}
    context_diff_mock.added = set()
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.modified_snapshots = {snapshot.name: (snapshot, forward_only_snapshot)}
    context_diff_mock.new_snapshots = {}
    context_diff_mock.added_materialized_snapshot_ids = set()

    with pytest.raises(
        PlanError,
        match=r"Attempted to revert to an unrevertable version of model.*",
    ):
        Plan(context_diff_mock, forward_only=True)

    # Make sure the plan can be created if a new snapshot version was enforced.
    new_version_snapshot = make_snapshot(
        SqlModel(name="a", query=parse_one("select 1, ds"), stamp="test_stamp")
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    context_diff_mock.modified_snapshots = {
        snapshot.name: (new_version_snapshot, forward_only_snapshot)
    }
    context_diff_mock.new_snapshots = {new_version_snapshot.snapshot_id: new_version_snapshot}
    Plan(context_diff_mock, forward_only=True)


def test_forward_only_plan_seed_models(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(
        SeedModel(
            name="a",
            kind=SeedKind(path="./path/to/seed"),
            seed=Seed(content="content"),
            column_hashes={"col": "hash1"},
            depends_on=set(),
        )
    )
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_a_updated = make_snapshot(
        SeedModel(
            name="a",
            kind=SeedKind(path="./path/to/seed"),
            seed=Seed(content="new_content"),
            column_hashes={"col": "hash2"},
            depends_on=set(),
        )
    )
    assert snapshot_a_updated.version is None
    assert snapshot_a_updated.change_category is None

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {snapshot_a_updated.snapshot_id: snapshot_a_updated}
    context_diff_mock.added = set()
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.modified_snapshots = {
        snapshot_a_updated.name: (snapshot_a_updated, snapshot_a)
    }
    context_diff_mock.new_snapshots = {snapshot_a_updated.snapshot_id: snapshot_a_updated}
    context_diff_mock.added_materialized_snapshot_ids = set()

    Plan(context_diff_mock, forward_only=True)
    assert snapshot_a_updated.version == snapshot_a_updated.fingerprint.to_version()
    assert snapshot_a_updated.change_category == SnapshotChangeCategory.NON_BREAKING


def test_start_inference(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(
        SqlModel(name="a", query=parse_one("select 1, ds"), start="2022-01-01")
    )
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("select 2, ds")))
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {
        snapshot_a.snapshot_id: snapshot_a,
        snapshot_b.snapshot_id: snapshot_b,
    }
    context_diff_mock.added = set()
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.modified_snapshots = {}
    context_diff_mock.new_snapshots = {}

    snapshot_b.add_interval("2022-01-01", now())

    plan = Plan(context_diff_mock)
    assert len(plan.missing_intervals) == 1
    assert plan.missing_intervals[0].snapshot_id == snapshot_a.snapshot_id
    assert plan.start == to_timestamp("2022-01-01")

    # Test inference from existing intervals
    context_diff_mock.snapshots = {snapshot_b.snapshot_id: snapshot_b}
    plan = Plan(context_diff_mock)
    assert not plan.missing_intervals
    assert plan.start == to_datetime("2022-01-01")


def test_auto_categorization(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds")))
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    updated_snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 2, ds")))

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {updated_snapshot.snapshot_id: updated_snapshot}
    context_diff_mock.added = set()
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.modified_snapshots = {updated_snapshot.name: (updated_snapshot, snapshot)}
    context_diff_mock.new_snapshots = {updated_snapshot.snapshot_id: updated_snapshot}

    Plan(context_diff_mock)

    assert updated_snapshot.version == updated_snapshot.fingerprint.to_version()
    assert updated_snapshot.change_category == SnapshotChangeCategory.BREAKING


def test_auto_categorization_missing_schema_downstream(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds")))
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    updated_snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 1, 2, ds")))

    # selects * from `tbl` which is not defined and has an unknown schema
    # therefore we can't be sure what is included in the star select
    downstream_snapshot = make_snapshot(
        SqlModel(name="b", query=parse_one("select * from tbl"), depends_on={'"a"'}),
        nodes={'"a"': snapshot.model},
    )
    downstream_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    updated_downstream_snapshot = make_snapshot(
        downstream_snapshot.model,
        nodes={'"a"': updated_snapshot.model},
    )

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {
        updated_snapshot.snapshot_id: updated_snapshot,
        updated_downstream_snapshot.snapshot_id: updated_downstream_snapshot,
    }
    context_diff_mock.added = set()
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.modified_snapshots = {
        updated_snapshot.name: (updated_snapshot, snapshot),
        updated_downstream_snapshot.name: (updated_downstream_snapshot, downstream_snapshot),
    }
    context_diff_mock.new_snapshots = {updated_snapshot.snapshot_id: updated_snapshot}
    context_diff_mock.directly_modified.side_effect = lambda name: name == '"a"'

    Plan(context_diff_mock)

    assert updated_snapshot.version
    assert updated_snapshot.change_category == SnapshotChangeCategory.BREAKING


def test_broken_references(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds")))
    snapshot_b = make_snapshot(
        SqlModel(name="b", query=parse_one("select 2, ds FROM a")), nodes={'"a"': snapshot_a.node}
    )
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {snapshot_b.snapshot_id: snapshot_b}
    context_diff_mock.added = set()
    context_diff_mock.removed_snapshots = {snapshot_a.snapshot_id: snapshot_a}
    context_diff_mock.modified_snapshots = {}
    context_diff_mock.new_snapshots = {}

    with pytest.raises(
        PlanError,
        match=r"""Removed '"a"' are referenced in '"b"'.*""",
    ):
        Plan(context_diff_mock)


def test_effective_from(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(
        SqlModel(name="a", query=parse_one("select 1, ds FROM b"), start="2023-01-01")
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot.add_interval("2023-01-01", "2023-03-01")

    updated_snapshot = make_snapshot(
        SqlModel(name="a", query=parse_one("select 2, ds FROM b"), start="2023-01-01")
    )
    updated_snapshot.previous_versions = snapshot.all_versions

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {updated_snapshot.snapshot_id: updated_snapshot}
    context_diff_mock.added = set()
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.modified_snapshots = {updated_snapshot.name: (updated_snapshot, snapshot)}
    context_diff_mock.new_snapshots = {updated_snapshot.snapshot_id: updated_snapshot}
    context_diff_mock.added_materialized_snapshot_ids = set()

    with pytest.raises(
        PlanError,
        match="Effective date can only be set for a forward-only plan.",
    ):
        plan = Plan(context_diff_mock)
        plan.effective_from = "2023-02-01"

    # The snapshot gets categorized as breaking in previous step so we want to reset that back to None
    updated_snapshot.change_category = None
    plan = Plan(
        context_diff_mock, forward_only=True, start="2023-01-01", end="2023-03-01", is_dev=True
    )
    updated_snapshot.add_interval("2023-01-01", "2023-03-01")

    with pytest.raises(
        PlanError,
        match="Effective date cannot be in the future.",
    ):
        plan.effective_from = now() + timedelta(days=1)

    assert plan.effective_from is None
    assert updated_snapshot.effective_from is None
    assert not plan.missing_intervals

    plan.effective_from = "2023-02-01"
    assert plan.effective_from == "2023-02-01"
    assert updated_snapshot.effective_from == "2023-02-01"

    assert len(plan.missing_intervals) == 1
    missing_intervals = plan.missing_intervals[0]
    assert missing_intervals.intervals[0][0] == to_timestamp("2023-02-01")
    assert missing_intervals.intervals[-1][-1] == to_timestamp("2023-03-02")

    plan.effective_from = None
    assert plan.effective_from is None
    assert updated_snapshot.effective_from is None


def test_new_environment_no_changes(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds")))
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {snapshot.snapshot_id: snapshot}
    context_diff_mock.added = set()
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.modified_snapshots = {}
    context_diff_mock.promotable_snapshot_ids = set()
    context_diff_mock.new_snapshots = {}
    context_diff_mock.is_new_environment = True
    context_diff_mock.has_snapshot_changes = False
    context_diff_mock.environment = "test_dev"
    context_diff_mock.previous_plan_id = "previous_plan_id"

    with pytest.raises(PlanError, match="No changes were detected.*"):
        Plan(context_diff_mock, is_dev=True)

    assert Plan(context_diff_mock).environment.promoted_snapshot_ids is None
    assert (
        Plan(
            context_diff_mock, is_dev=True, include_unmodified=True
        ).environment.promoted_snapshot_ids
        is None
    )


def test_new_environment_with_changes(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds")))
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)
    updated_snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 3, ds")))

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("select 2, ds")))
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {
        updated_snapshot_a.snapshot_id: updated_snapshot_a,
        snapshot_b.snapshot_id: snapshot_b,
    }
    context_diff_mock.added = set()
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.modified_snapshots = {
        updated_snapshot_a.name: (updated_snapshot_a, snapshot_a)
    }
    context_diff_mock.promotable_snapshot_ids = {updated_snapshot_a.snapshot_id}
    context_diff_mock.new_snapshots = {updated_snapshot_a.snapshot_id: updated_snapshot_a}
    context_diff_mock.is_new_environment = True
    context_diff_mock.has_snapshot_changes = True
    context_diff_mock.environment = "test_dev"
    context_diff_mock.previous_plan_id = "previous_plan_id"

    # Modified the existing model.
    assert Plan(context_diff_mock, is_dev=True).environment.promoted_snapshot_ids == [
        updated_snapshot_a.snapshot_id
    ]

    # Updating the existing environment with a previously promoted snapshot.
    context_diff_mock.promotable_snapshot_ids = {
        updated_snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
    }
    context_diff_mock.is_new_environment = False
    assert set(Plan(context_diff_mock, is_dev=True).environment.promoted_snapshot_ids or []) == {
        updated_snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
    }

    # Adding a new model
    snapshot_c = make_snapshot(SqlModel(name="c", query=parse_one("select 4, ds")))
    snapshot_c.categorize_as(SnapshotChangeCategory.BREAKING)
    context_diff_mock.snapshots = {
        updated_snapshot_a.snapshot_id: updated_snapshot_a,
        snapshot_b.snapshot_id: snapshot_b,
        snapshot_c.snapshot_id: snapshot_c,
    }
    context_diff_mock.added = {snapshot_c.snapshot_id}
    context_diff_mock.modified_snapshots = {}
    context_diff_mock.new_snapshots = {snapshot_c.snapshot_id: snapshot_c}
    context_diff_mock.promotable_snapshot_ids = {
        updated_snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
        snapshot_c.snapshot_id,
    }
    assert set(Plan(context_diff_mock, is_dev=True).environment.promoted_snapshot_ids or []) == {
        updated_snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
        snapshot_c.snapshot_id,
    }


def test_forward_only_models(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(SqlModel(name="a", query=parse_one("select 1, ds")))
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    updated_snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 3, ds"),
            kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
        )
    )
    updated_snapshot.previous_versions = snapshot.all_versions

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {updated_snapshot.snapshot_id: updated_snapshot}
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.added = set()
    context_diff_mock.added_materialized_snapshot_ids = set()
    context_diff_mock.modified_snapshots = {updated_snapshot.name: (updated_snapshot, snapshot)}
    context_diff_mock.new_snapshots = {updated_snapshot.snapshot_id: updated_snapshot}
    context_diff_mock.has_snapshot_changes = True
    context_diff_mock.environment = "test_dev"
    context_diff_mock.previous_plan_id = "previous_plan_id"

    Plan(context_diff_mock, is_dev=True)
    assert updated_snapshot.change_category == SnapshotChangeCategory.FORWARD_ONLY

    updated_snapshot.change_category = None
    updated_snapshot.version = None
    Plan(context_diff_mock, is_dev=True, forward_only=True)
    assert updated_snapshot.change_category == SnapshotChangeCategory.FORWARD_ONLY

    updated_snapshot.change_category = None
    updated_snapshot.version = None
    Plan(context_diff_mock, forward_only=True)
    assert updated_snapshot.change_category == SnapshotChangeCategory.FORWARD_ONLY


def test_indirectly_modified_forward_only_model(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 1 as a, ds")))
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)
    updated_snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 2 as a, ds")))
    updated_snapshot_a.previous_versions = snapshot_a.all_versions

    snapshot_b = make_snapshot(
        SqlModel(
            name="b",
            query=parse_one("select a, ds from a"),
            kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
        ),
        nodes={'"a"': snapshot_a.model},
    )
    snapshot_b.categorize_as(SnapshotChangeCategory.FORWARD_ONLY)
    updated_snapshot_b = make_snapshot(snapshot_b.model, nodes={'"a"': updated_snapshot_a.model})
    updated_snapshot_b.previous_versions = snapshot_b.all_versions

    snapshot_c = make_snapshot(
        SqlModel(name="c", query=parse_one("select a, ds from b")), nodes={'"b"': snapshot_b.model}
    )
    snapshot_c.categorize_as(SnapshotChangeCategory.BREAKING)
    updated_snapshot_c = make_snapshot(
        snapshot_c.model, nodes={'"b"': updated_snapshot_b.model, '"a"': updated_snapshot_a.model}
    )
    updated_snapshot_c.previous_versions = snapshot_c.all_versions

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {
        updated_snapshot_a.snapshot_id: updated_snapshot_a,
        updated_snapshot_b.snapshot_id: updated_snapshot_b,
        updated_snapshot_c.snapshot_id: updated_snapshot_c,
    }
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.added = set()
    context_diff_mock.added_materialized_snapshot_ids = set()
    context_diff_mock.modified_snapshots = {
        updated_snapshot_a.name: (updated_snapshot_a, snapshot_a),
        updated_snapshot_b.name: (updated_snapshot_b, snapshot_b),
        updated_snapshot_c.name: (updated_snapshot_c, snapshot_c),
    }
    context_diff_mock.new_snapshots = {
        updated_snapshot_a.snapshot_id: updated_snapshot_a,
        updated_snapshot_b.snapshot_id: updated_snapshot_b,
        updated_snapshot_c.snapshot_id: updated_snapshot_c,
    }
    context_diff_mock.has_snapshot_changes = True
    context_diff_mock.environment = "test_dev"
    context_diff_mock.previous_plan_id = "previous_plan_id"
    context_diff_mock.directly_modified.side_effect = lambda name: name == '"a"'

    plan = Plan(context_diff_mock, is_dev=True)
    assert plan.indirectly_modified == {
        updated_snapshot_a.snapshot_id: {
            updated_snapshot_b.snapshot_id,
            updated_snapshot_c.snapshot_id,
        }
    }

    assert len(plan.directly_modified) == 1
    assert plan.directly_modified[0].snapshot_id == updated_snapshot_a.snapshot_id

    assert updated_snapshot_a.change_category == SnapshotChangeCategory.BREAKING
    assert updated_snapshot_b.change_category == SnapshotChangeCategory.FORWARD_ONLY
    assert updated_snapshot_c.change_category == SnapshotChangeCategory.INDIRECT_BREAKING

    deployability_index = DeployabilityIndex.create(
        {
            updated_snapshot_a.snapshot_id: updated_snapshot_a,
            updated_snapshot_b.snapshot_id: updated_snapshot_b,
            updated_snapshot_c.snapshot_id: updated_snapshot_c,
        }
    )
    assert deployability_index.is_representative(updated_snapshot_a)
    assert not deployability_index.is_representative(updated_snapshot_b)
    assert not deployability_index.is_representative(updated_snapshot_c)


def test_added_model_with_forward_only_parent(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 1 as a, ds")))
    snapshot_a.categorize_as(SnapshotChangeCategory.FORWARD_ONLY)

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("select a, ds from a")))

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {
        snapshot_a.snapshot_id: snapshot_a,
        snapshot_b.snapshot_id: snapshot_b,
    }
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.added = {snapshot_b.snapshot_id}
    context_diff_mock.added_materialized_snapshot_ids = set()
    context_diff_mock.modified_snapshots = {}
    context_diff_mock.new_snapshots = {
        snapshot_b.snapshot_id: snapshot_b,
    }
    context_diff_mock.has_snapshot_changes = True
    context_diff_mock.environment = "test_dev"
    context_diff_mock.previous_plan_id = "previous_plan_id"

    Plan(context_diff_mock, is_dev=True)
    assert snapshot_b.change_category == SnapshotChangeCategory.BREAKING


def test_added_forward_only_model(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1 as a, ds"),
            kind=IncrementalByTimeRangeKind(time_column="ds", forward_only=True),
        )
    )

    snapshot_b = make_snapshot(SqlModel(name="b", query=parse_one("select a, ds from a")))

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {
        snapshot_a.snapshot_id: snapshot_a,
        snapshot_b.snapshot_id: snapshot_b,
    }
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.added = {snapshot_a.snapshot_id, snapshot_b.snapshot_id}
    context_diff_mock.added_materialized_snapshot_ids = set()
    context_diff_mock.modified_snapshots = {}
    context_diff_mock.new_snapshots = {
        snapshot_a.snapshot_id: snapshot_a,
        snapshot_b.snapshot_id: snapshot_b,
    }
    context_diff_mock.has_snapshot_changes = True
    context_diff_mock.environment = "test_dev"
    context_diff_mock.previous_plan_id = "previous_plan_id"

    Plan(context_diff_mock)
    assert snapshot_a.change_category == SnapshotChangeCategory.BREAKING
    assert snapshot_b.change_category == SnapshotChangeCategory.BREAKING


def test_disable_restatement(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            query=parse_one("select 1, ds"),
            kind=IncrementalByTimeRangeKind(time_column="ds", disable_restatement=True),
        )
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {snapshot.snapshot_id: snapshot}
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.added = set()
    context_diff_mock.added_materialized_snapshot_ids = set()
    context_diff_mock.modified_snapshots = {}
    context_diff_mock.new_snapshots = {}
    context_diff_mock.has_snapshot_changes = False
    context_diff_mock.is_new_environment = False
    context_diff_mock.environment = "test_dev"
    context_diff_mock.previous_plan_id = "previous_plan_id"

    with pytest.raises(PlanError, match="""Cannot restate from '"a"'.*"""):
        Plan(context_diff_mock, restate_models=['"a"'])

    # Effective from doesn't apply to snapshots for which restatements are disabled.
    plan = Plan(context_diff_mock, forward_only=True, effective_from="2023-01-01")
    assert plan.effective_from == "2023-01-01"
    assert snapshot.effective_from is None

    # Restatements should still be supported when in dev.
    plan = Plan(context_diff_mock, is_dev=True, restate_models=['"a"'])
    assert plan.restatements == {
        snapshot.snapshot_id: (to_timestamp(plan.start), to_timestamp(to_date("today")))
    }


def test_revert_to_previous_value(make_snapshot, mocker: MockerFixture):
    """
    Make sure we can revert to previous snapshots with intervals if it already exists and not modify
    it's existing change category
    """
    old_snapshot_a = make_snapshot(
        SqlModel(name="a", query=parse_one("select 1, ds"), depends_on=set())
    )
    old_snapshot_b = make_snapshot(
        SqlModel(name="b", query=parse_one("select 1, ds FROM a"), depends_on={"a"})
    )
    snapshot_a = make_snapshot(
        SqlModel(name="a", query=parse_one("select 2, ds"), depends_on=set())
    )
    snapshot_b = make_snapshot(
        SqlModel(name="b", query=parse_one("select 1, ds FROM a"), depends_on={"a"})
    )
    snapshot_b.categorize_as(SnapshotChangeCategory.FORWARD_ONLY)
    snapshot_b.add_interval("2022-01-01", now())

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {
        snapshot_a.snapshot_id: snapshot_a,
        snapshot_b.snapshot_id: snapshot_b,
    }
    context_diff_mock.added = set()
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.directly_modified.side_effect = lambda name: name == "a"
    context_diff_mock.modified_snapshots = {
        snapshot_a.name: (snapshot_a, old_snapshot_a),
        snapshot_b.name: (snapshot_b, old_snapshot_b),
    }
    context_diff_mock.new_snapshots = {snapshot_a.snapshot_id: snapshot_a}
    context_diff_mock.added_materialized_snapshot_ids = set()

    plan = Plan(context_diff_mock)
    plan.set_choice(snapshot_a, SnapshotChangeCategory.BREAKING)
    # Make sure it does not get assigned INDIRECT_BREAKING
    assert snapshot_b.change_category == SnapshotChangeCategory.FORWARD_ONLY


test_add_restatement_fixtures = [
    (
        "No dependencies single depends on past",
        {
            '"a"': {},
            '"b"': {},
        },
        {'"b"'},
        {'"a"', '"b"'},
        "1 week ago",
        "1 week ago",
        "1 day ago",
        {
            '"a"': ("1 week ago", "6 days ago"),
            '"b"': ("1 week ago", "today"),
        },
    ),
    (
        "Simple dependency with leaf depends on past",
        {
            '"a"': {},
            '"b"': {'"a"'},
        },
        {'"b"'},
        {'"a"', '"b"'},
        "1 week ago",
        "1 week ago",
        "1 day ago",
        {
            '"a"': ("1 week ago", "6 days ago"),
            '"b"': ("1 week ago", "today"),
        },
    ),
    (
        "Simple dependency with root depends on past",
        {
            '"a"': {},
            '"b"': {'"a"'},
        },
        {'"a"'},
        {'"a"', '"b"'},
        "1 week ago",
        "1 week ago",
        "1 day ago",
        {
            '"a"': ("1 week ago", "today"),
            '"b"': ("1 week ago", "today"),
        },
    ),
    (
        "Two unrelated subgraphs with root depends on past",
        {
            '"a"': {},
            '"b"': {},
            '"c"': {'"a"'},
            '"d"': {'"b"'},
        },
        {'"a"'},
        {'"a"', '"b"'},
        "1 week ago",
        "1 week ago",
        "1 day ago",
        {
            '"a"': ("1 week ago", "today"),
            '"b"': ("1 week ago", "6 days ago"),
            '"c"': ("1 week ago", "today"),
            '"d"': ("1 week ago", "6 days ago"),
        },
    ),
    (
        "Simple root depends on past with adjusted execution time",
        {
            '"a"': {},
            '"b"': {'"a"'},
        },
        {'"a"'},
        {'"a"', '"b"'},
        "1 week ago",
        "1 week ago",
        "3 day ago",
        {
            '"a"': ("1 week ago", "2 days ago"),
            '"b"': ("1 week ago", "2 days ago"),
        },
    ),
    (
        """
        a -> c -> d
        b -> c -> e -> g
        b -> f -> g
        c depends on past
        restate a and b
        """,
        {
            '"a"': {},
            '"b"': {},
            '"c"': {'"a"', '"b"'},
            '"d"': {'"c"'},
            '"e"': {'"c"'},
            '"f"': {'"b"'},
            '"g"': {'"f"', '"e"'},
        },
        {'"c"'},
        {'"a"', '"b"'},
        "1 week ago",
        "1 week ago",
        "1 day ago",
        {
            '"a"': ("1 week ago", "6 days ago"),
            '"b"': ("1 week ago", "6 days ago"),
            '"c"': ("1 week ago", "today"),
            '"d"': ("1 week ago", "today"),
            '"e"': ("1 week ago", "today"),
            '"f"': ("1 week ago", "6 days ago"),
            '"g"': ("1 week ago", "today"),
        },
    ),
    (
        """
        a -> c -> d
        b -> c -> e -> g
        b -> f -> g
        c depends on past
        restate e
        """,
        {
            '"a"': {},
            '"b"': {},
            '"c"': {'"a"', '"b"'},
            '"d"': {'"c"'},
            '"e"': {'"c"'},
            '"f"': {'"b"'},
            '"g"': {'"f"', '"e"'},
        },
        {'"c"'},
        {'"e"'},
        "1 week ago",
        "1 week ago",
        "1 day ago",
        {
            '"e"': ("1 week ago", "6 days ago"),
            '"g"': ("1 week ago", "6 days ago"),
        },
    ),
]


@pytest.mark.parametrize(
    "graph,depends_on_past_names,restatement_names,start,end,execution_time,expected",
    [test[1:] for test in test_add_restatement_fixtures],
    ids=[test[0] for test in test_add_restatement_fixtures],
)
def test_add_restatements(
    graph: t.Dict[str, t.Set[str]],
    depends_on_past_names: t.Set[str],
    restatement_names: t.Set[str],
    start: str,
    end: str,
    execution_time: str,
    expected: t.Dict[str, t.Tuple[str, str]],
    make_snapshot,
    mocker,
):
    dag = DAG(graph)
    context_diff_mock = mocker.Mock()
    snapshots: t.Dict[str, Snapshot] = {}
    for snapshot_name in dag:
        depends_on = dag.upstream(snapshot_name)
        snapshots[snapshot_name] = make_snapshot(
            SqlModel(
                name=snapshot_name,
                kind=IncrementalByTimeRangeKind(time_column="ds"),
                cron="@daily",
                start="1 week ago",
                query=parse_one(
                    f"SELECT 1 FROM {snapshot_name}"
                    if snapshot_name in depends_on_past_names
                    else "SELECT 1"
                ),
                depends_on=depends_on,
            ),
            nodes={
                upstream_snapshot_name: snapshots[upstream_snapshot_name].model
                for upstream_snapshot_name in depends_on
            },
        )
    context_diff_mock.snapshots = {
        snapshot.snapshot_id: snapshot for snapshot in snapshots.values()
    }
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.added = set()
    context_diff_mock.added_materialized_snapshot_ids = set()
    context_diff_mock.modified_snapshots = {}
    context_diff_mock.new_snapshots = {}
    context_diff_mock.has_snapshot_changes = False
    context_diff_mock.is_new_environment = False
    context_diff_mock.environment = "test_dev"
    context_diff_mock.previous_plan_id = "previous_plan_id"
    plan = Plan(
        context_diff_mock,
        start=to_date(start),
        end=to_date(end),
        execution_time=to_date(execution_time),
        restate_models=restatement_names,
    )
    assert {s_id.name: interval for s_id, interval in plan.restatements.items()} == {
        name: (to_timestamp(to_date(start)), to_timestamp(to_date(end)))
        for name, (start, end) in expected.items()
    }


def test_dev_plan_depends_past(make_snapshot, mocker: MockerFixture):
    snapshot = make_snapshot(
        SqlModel(
            name="a",
            # self reference query so it depends_on_past
            query=parse_one("select 1, ds FROM a"),
            start="2023-01-01",
            kind=IncrementalByTimeRangeKind(time_column="ds"),
        ),
    )
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    snapshot_child = make_snapshot(
        SqlModel(
            name="a_child",
            query=parse_one("select 1, ds FROM a"),
            start="2023-01-01",
            kind=IncrementalByTimeRangeKind(time_column="ds"),
        ),
        nodes={'"a"': snapshot.model},
    )
    snapshot_child.categorize_as(SnapshotChangeCategory.BREAKING)
    unrelated_snapshot = make_snapshot(
        SqlModel(
            name="b",
            query=parse_one("select 1, ds"),
            start="2023-01-01",
            kind=IncrementalByTimeRangeKind(time_column="ds"),
        ),
    )
    unrelated_snapshot.categorize_as(SnapshotChangeCategory.BREAKING)
    assert snapshot.depends_on_past
    assert not snapshot_child.depends_on_past
    assert not unrelated_snapshot.depends_on_past
    assert snapshot_child.model.depends_on == {'"a"'}
    assert snapshot_child.parents == (snapshot.snapshot_id,)
    assert unrelated_snapshot.model.depends_on == set()

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {
        snapshot.snapshot_id: snapshot,
        snapshot_child.snapshot_id: snapshot_child,
        unrelated_snapshot.snapshot_id: unrelated_snapshot,
    }
    context_diff_mock.added = set()
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.modified_snapshots = {}
    context_diff_mock.new_snapshots = {
        snapshot.snapshot_id: snapshot,
        snapshot_child.snapshot_id: snapshot_child,
        unrelated_snapshot.snapshot_id: unrelated_snapshot,
    }
    context_diff_mock.environment = "dev"

    dev_plan_start_aligned = Plan(
        context_diff_mock, start="2023-01-01", end="2023-01-10", is_dev=True
    )
    assert len(dev_plan_start_aligned.new_snapshots) == 3
    assert sorted([x.name for x in dev_plan_start_aligned.new_snapshots]) == [
        '"a"',
        '"a_child"',
        '"b"',
    ]
    dev_plan_start_ahead_of_model = Plan(
        context_diff_mock, start="2023-01-02", end="2023-01-10", is_dev=True
    )
    assert len(dev_plan_start_ahead_of_model.new_snapshots) == 1
    assert [x.name for x in dev_plan_start_ahead_of_model.new_snapshots] == ['"b"']
    assert len(dev_plan_start_ahead_of_model.ignored_snapshot_ids) == 2
    assert sorted(list(dev_plan_start_ahead_of_model.ignored_snapshot_ids)) == [
        snapshot.snapshot_id,
        snapshot_child.snapshot_id,
    ]


def test_restatement_intervals_after_updating_start(sushi_context: Context):
    plan = sushi_context.plan(no_prompts=True, restate_models=["sushi.waiter_revenue_by_day"])
    snapshot_id = [
        snapshot.snapshot_id
        for snapshot in plan.snapshots
        if snapshot.name == '"memory"."sushi"."waiter_revenue_by_day"'
    ][0]
    restatement_interval = plan.restatements[snapshot_id]
    assert restatement_interval[0] == to_timestamp(plan.start)

    new_start = yesterday_ds()
    plan.start = new_start
    new_restatement_interval = plan.restatements[snapshot_id]
    assert new_restatement_interval[0] == to_timestamp(new_start)
    assert new_restatement_interval != restatement_interval


def test_models_selected_for_backfill(make_snapshot, mocker: MockerFixture):
    snapshot_a = make_snapshot(SqlModel(name="a", query=parse_one("select 1 as one, ds")))
    snapshot_a.categorize_as(SnapshotChangeCategory.BREAKING)

    snapshot_b = make_snapshot(
        SqlModel(name="b", query=parse_one("select one, ds from a")),
        nodes={'"a"': snapshot_a.model},
    )
    snapshot_b.categorize_as(SnapshotChangeCategory.BREAKING)

    context_diff_mock = mocker.Mock()
    context_diff_mock.snapshots = {
        snapshot_a.snapshot_id: snapshot_a,
        snapshot_b.snapshot_id: snapshot_b,
    }
    context_diff_mock.added = set()
    context_diff_mock.removed_snapshots = set()
    context_diff_mock.modified_snapshots = {}
    context_diff_mock.new_snapshots = {}
    context_diff_mock.added_materialized_models = set()

    with pytest.raises(
        PlanError,
        match="Selecting models to backfill is only supported for development environments",
    ):
        Plan(context_diff_mock, backfill_models={'"a"'})

    plan = Plan(context_diff_mock)
    assert plan.is_selected_for_backfill('"a"')
    assert plan.is_selected_for_backfill('"b"')
    assert plan.models_to_backfill is None
    assert {i.snapshot_id for i in plan.missing_intervals} == {
        snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
    }

    plan = Plan(context_diff_mock, is_dev=True, backfill_models={'"a"'})
    assert plan.is_selected_for_backfill('"a"')
    assert not plan.is_selected_for_backfill('"b"')
    assert plan.models_to_backfill == {'"a"'}
    assert {i.snapshot_id for i in plan.missing_intervals} == {snapshot_a.snapshot_id}

    plan = Plan(context_diff_mock, is_dev=True, backfill_models={'"b"'})
    assert plan.is_selected_for_backfill('"a"')
    assert plan.is_selected_for_backfill('"b"')
    assert plan.models_to_backfill == {'"a"', '"b"'}
    assert {i.snapshot_id for i in plan.missing_intervals} == {
        snapshot_a.snapshot_id,
        snapshot_b.snapshot_id,
    }
