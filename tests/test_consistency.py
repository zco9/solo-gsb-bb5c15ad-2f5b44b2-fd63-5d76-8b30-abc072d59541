"""一致性比对核心逻辑的单元测试（用轻量假 Job 构造冲突/碰撞场景）。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.anonymizer import JobResult, PatientInfo
from app import consistency
from app.schemas import ConsistencyCheckOut


def _make_job(
    job_id: str,
    *,
    fingerprint: str = "fp0000000001",
    shift_dates: bool = True,
    patients: dict | None = None,
    studies: dict | None = None,
    series: dict | None = None,
    sops: dict | None = None,
    files: list | None = None,
    status: str = "ready",
) -> SimpleNamespace:
    result = JobResult(
        files=files or [],
        patients=patients or {},
        studies=studies or {},
        series=series or {},
        sops=sops or {},
        output_dir="/tmp/unused",
    )
    return SimpleNamespace(
        id=job_id,
        status=status,
        source_name=f"{job_id}.zip",
        secret_fingerprint=fingerprint,
        shift_dates=shift_dates,
        created_at=1000.0,
        expires_at=1000.0 + 3600,
        ttl=3600,
        result=result,
        error=None,
    )


def _patient(pid: str, name: str, shift: int) -> PatientInfo:
    return PatientInfo(
        key=f"id:{pid}", new_patient_id=pid, new_patient_name=name,
        date_shift_days=shift, file_count=1,
    )


def _validated(report: dict) -> ConsistencyCheckOut:
    """报告必须能通过响应模型校验。"""
    return ConsistencyCheckOut.model_validate(report)


def test_identical_jobs_are_consistent():
    patients = {"id:P1": _patient("PID1", "ANON^PID1", 10)}
    studies = {"2.25.1": {"new_study_instance_uid": "2.25.1",
                          "old_study_instance_uid": "1.1.1",
                          "patient_key": "id:P1", "file_count": 1}}
    series = {"2.25.2": {"new_series_instance_uid": "2.25.2",
                         "old_series_instance_uid": "1.1.2",
                         "new_study_instance_uid": "2.25.1", "file_count": 1}}
    sops = {"1.1.3": "2.25.3"}
    j1 = _make_job("j1", patients=dict(patients), studies=dict(studies),
                   series=dict(series), sops=dict(sops))
    j2 = _make_job("j2", patients=dict(patients), studies=dict(studies),
                   series=dict(series), sops=dict(sops))

    report = consistency.build_report([j1, j2])
    out = _validated(report)
    assert out.overall_status == "consistent"
    assert out.patient_mappings.checked_keys == 1
    assert out.patient_mappings.only_in_one_job_count == 0
    for layer in out.uid_mappings:
        assert layer.conflict_count == 0
        assert layer.only_in_one_job_count == 0
        assert layer.new_uid_collision_count == 0
        assert layer.checked_old_uids == 1


def test_patient_pseudonym_conflict_detected():
    j1 = _make_job("j1", patients={"id:P1": _patient("PID1", "ANON^PID1", 10)})
    j2 = _make_job("j2", patients={"id:P1": _patient("PID9", "ANON^PID9", 10)})

    out = _validated(consistency.build_report([j1, j2]))
    assert out.overall_status == "inconsistent"
    assert out.summary["patient_conflict"] == 1
    diff = out.patient_mappings.differences[0]
    assert diff.difference_type == "conflict"
    assert diff.values["j1"].new_patient_id == "PID1"
    assert diff.values["j2"].new_patient_id == "PID9"
    assert set(diff.present_in) == {"j1", "j2"}


def test_date_shift_mismatch_distinct_from_pseudonym_conflict():
    j1 = _make_job("j1", shift_dates=True,
                   patients={"id:P1": _patient("PID1", "ANON^PID1", 10)})
    j2 = _make_job("j2", shift_dates=False,
                   patients={"id:P1": _patient("PID1", "ANON^PID1", 0)})

    out = _validated(consistency.build_report([j1, j2]))
    # 日期偏移不同 -> 仍标记为不一致，但类型是 date_shift_mismatch
    assert out.overall_status == "inconsistent"
    assert out.patient_mappings.conflict_count == 0
    assert out.patient_mappings.date_shift_mismatch_count == 1
    assert out.patient_mappings.shift_dates_consistent is False
    assert out.summary["patient_date_shift_mismatch"] == 1
    assert out.patient_mappings.differences[0].difference_type == "date_shift_mismatch"


def test_patient_only_in_one_job_is_coverage_not_conflict():
    j1 = _make_job("j1", patients={
        "id:P1": _patient("PID1", "ANON^PID1", 10),
        "id:P2": _patient("PID2", "ANON^PID2", 20),
    })
    j2 = _make_job("j2", patients={"id:P1": _patient("PID1", "ANON^PID1", 10)})

    out = _validated(consistency.build_report([j1, j2]))
    assert out.overall_status == "consistent"
    assert out.patient_mappings.checked_keys == 1
    assert out.patient_mappings.only_in_one_job_count == 1
    diff = next(d for d in out.patient_mappings.differences
                if d.patient_key == "id:P2")
    assert diff.difference_type == "only_in_one_job"
    assert diff.present_in == ["j1"]
    assert diff.missing_in == ["j2"]


def test_uid_conflict_detected_for_each_level():
    def jobs_with(level_entries):
        studies1 = studies2 = {}
        series1 = series2 = {}
        sops1 = sops2 = {}
        if level_entries == "study":
            common = {"patient_key": "id:P1", "file_count": 1}
            studies1 = {"2.25.a": {"new_study_instance_uid": "2.25.a",
                                   "old_study_instance_uid": "1.1.1", **common}}
            studies2 = {"2.25.b": {"new_study_instance_uid": "2.25.b",
                                   "old_study_instance_uid": "1.1.1", **common}}
        elif level_entries == "series":
            series1 = {"2.25.a": {"new_series_instance_uid": "2.25.a",
                                  "old_series_instance_uid": "1.1.2",
                                  "new_study_instance_uid": "2.25.s", "file_count": 1}}
            series2 = {"2.25.b": {"new_series_instance_uid": "2.25.b",
                                  "old_series_instance_uid": "1.1.2",
                                  "new_study_instance_uid": "2.25.s", "file_count": 1}}
        else:
            sops1 = {"1.1.3": "2.25.a"}
            sops2 = {"1.1.3": "2.25.b"}
        return (
            _make_job("j1", studies=studies1, series=series1, sops=sops1),
            _make_job("j2", studies=studies2, series=series2, sops=sops2),
        )

    for level in ("study", "series", "sop"):
        out = _validated(consistency.build_report(list(jobs_with(level))))
        assert out.overall_status == "inconsistent"
        layer = next(l for l in out.uid_mappings if l.level == level)
        assert layer.conflict_count == 1
        assert layer.checked_old_uids == 1
        diff = layer.differences[0]
        assert diff.difference_type == "conflict"
        assert diff.mappings == {"j1": f"2.25.a", "j2": f"2.25.b"}
        assert out.summary["uid_conflict"] >= 1


def test_uid_new_collision_detected():
    # 不同原始 UID 在两个任务中派生出同一个新 UID
    j1 = _make_job("j1", sops={"1.1.1": "2.25.same"})
    j2 = _make_job("j2", sops={"1.1.2": "2.25.same"})

    out = _validated(consistency.build_report([j1, j2]))
    assert out.overall_status == "inconsistent"
    sop = next(l for l in out.uid_mappings if l.level == "sop")
    # 两个原始 UID 各自只出现在一个任务（覆盖差异），外加一条碰撞
    assert sop.only_in_one_job_count == 2
    assert sop.new_uid_collision_count == 1
    collision = next(d for d in sop.differences
                     if d.difference_type == "uid_new_collision")
    assert collision.new_uid == "2.25.same"
    assert collision.sources == {"j1": "1.1.1", "j2": "1.1.2"}
    assert out.summary["uid_new_collision"] == 1


def test_uid_only_in_one_job_is_coverage_not_conflict():
    j1 = _make_job("j1", sops={"1.1.1": "2.25.1", "1.1.2": "2.25.2"})
    j2 = _make_job("j2", sops={"1.1.1": "2.25.1"})

    out = _validated(consistency.build_report([j1, j2]))
    assert out.overall_status == "consistent"
    sop = next(l for l in out.uid_mappings if l.level == "sop")
    assert sop.checked_old_uids == 1
    assert sop.only_in_one_job_count == 1
    diff = sop.differences[0]
    assert diff.difference_type == "only_in_one_job"
    assert diff.old_uid == "1.1.2"
    assert diff.present_in == ["j1"]
    assert diff.missing_in == ["j2"]


def test_missing_uid_placeholders_excluded_from_comparison():
    # 顶层 UID 缺失时引擎写入占位 old（"missing:..."），不应参与跨批次比对
    j1 = _make_job("j1", studies={"2.25.9": {
        "new_study_instance_uid": "2.25.9",
        "old_study_instance_uid": "missing:studyinstanceuid:abc",
        "patient_key": "id:P1", "file_count": 1}})
    j2 = _make_job("j2", studies={"2.25.8": {
        "new_study_instance_uid": "2.25.8",
        "old_study_instance_uid": "missing:studyinstanceuid:def",
        "patient_key": "id:P1", "file_count": 1}})

    out = _validated(consistency.build_report([j1, j2]))
    study = next(l for l in out.uid_mappings if l.level == "study")
    assert study.checked_old_uids == 0
    assert study.only_in_one_job_count == 0
    assert study.conflict_count == 0
    assert out.overall_status == "consistent"


def test_job_summary_counts():
    files = [
        SimpleNamespace(status="ok", output_name="a.dcm"),
        SimpleNamespace(status="warning", output_name="b.dcm"),
        SimpleNamespace(status="error", output_name=None),
    ]
    j1 = _make_job("j1", files=files, sops={"1.1.1": "2.25.1"})
    out = _validated(consistency.build_report([j1, _make_job("j2")]))
    summary = next(j for j in out.jobs if j.id == "j1")
    assert summary.file_count == 3
    assert summary.output_file_count == 2
    assert summary.error_count == 1
    assert summary.warning_count == 1
    assert summary.sop_count == 1
    assert summary.status == "ready"
    assert summary.secret_fingerprint == "fp0000000001"


def test_coverage_differences_are_truncated_but_counts_kept(monkeypatch):
    monkeypatch.setattr(consistency, "_MAX_DIFF_ENTRIES_PER_LEVEL", 3)
    # 2 条碰撞（关键，必保留）+ 5 条覆盖差异 -> 只保留 1 条覆盖差异
    j1 = _make_job("j1", sops={
        "1.1.1": "2.25.c1", "1.1.3": "2.25.c3",
        **{f"1.2.{i}": f"2.25.{100 + i}" for i in range(5)},
    })
    j2 = _make_job("j2", sops={
        "1.1.2": "2.25.c1", "1.1.4": "2.25.c3",
    })

    out = _validated(consistency.build_report([j1, j2]))
    sop = next(l for l in out.uid_mappings if l.level == "sop")
    assert sop.new_uid_collision_count == 2
    # 5 个 j1 独有 + 4 个碰撞来源 UID（各仅在一个任务出现）= 9 条覆盖差异
    assert sop.only_in_one_job_count == 9
    assert sop.differences_truncated is True
    types = [d.difference_type for d in sop.differences]
    assert types.count("uid_new_collision") == 2
    assert types.count("only_in_one_job") == 1


def test_five_jobs_presence_ordering():
    jobs = [
        _make_job(f"j{i}", patients={"id:P1": _patient("PID1", "ANON^PID1", 10)})
        for i in range(5)
    ]
    out = _validated(consistency.build_report(jobs))
    assert [j.id for j in out.jobs] == ["j0", "j1", "j2", "j3", "j4"]
    assert out.overall_status == "consistent"
    assert out.patient_mappings.checked_keys == 1


# --------------------------------------------------------------------------- #
# 差异明细分页（固定业务键排序 + keyset 翻页）
# --------------------------------------------------------------------------- #
def test_patient_differences_sorted_by_patient_key():
    j1 = _make_job("j1", patients={
        f"id:P{i}": _patient(f"PID{i}", f"ANON^PID{i}", 10) for i in (3, 1, 2)
    })
    j2 = _make_job("j2", patients={
        "id:P1": _patient("PID1", "ANON^PID1", 10),
        "id:P3": _patient("PIDX", "ANON^PIDX", 10),  # P3 伪名冲突
    })

    items = consistency.level_differences([j1, j2], "patient")
    keys = [d["patient_key"] for d in items]
    assert keys == sorted(keys)
    # P1 共同出现且一致 -> 不在差异列表；P2 覆盖差异；P3 冲突
    assert keys == ["id:P2", "id:P3"]
    assert next(d for d in items if d["patient_key"] == "id:P3")[
        "difference_type"
    ] == "conflict"


def test_paginate_walks_all_items_without_gaps_or_duplicates():
    j1 = _make_job("j1", sops={f"1.1.{i}": f"2.25.{100 + i}" for i in range(5)})
    j2 = _make_job("j2", sops={"1.1.0": "2.25.100"})  # 仅 UID 0 共同出现

    seen = []
    position = None
    for _ in range(10):
        page = consistency.paginate([j1, j2], "sop", 2, after=position)
        seen.extend(page["items"])
        assert len(page["items"]) <= 2
        if not page["has_more"]:
            assert page["next_position"] is None
            break
        position = page["next_position"]
    else:  # pragma: no cover - 分页未能收敛
        raise AssertionError("分页未在预期页数内结束")

    # 4 个 j1 独有 UID（1.1.0 共同出现且映射一致，不产生差异）= 4 条覆盖差异
    keys = [d.get("old_uid") or d["new_uid"] for d in seen]
    assert len(keys) == 4
    assert len(keys) == len(set(keys))
    assert keys == sorted(keys)


def test_paginate_total_matches_full_level_list():
    jobs = [
        _make_job("j1", sops={"1.1.1": "2.25.1", "1.1.2": "2.25.2"}),
        _make_job("j2", sops={"1.1.1": "2.25.1"}),
    ]
    full = consistency.level_differences(jobs, "sop")
    page = consistency.paginate(jobs, "sop", 100)
    assert len(full) == 1
    assert page["has_more"] is False
    assert page["items"][0]["old_uid"] == "1.1.2"
    assert page["items"][0]["difference_type"] == "only_in_one_job"


def test_paginate_limit_larger_than_total():
    jobs = [
        _make_job("j1", patients={"id:P1": _patient("A", "ANON^A", 1)}),
        _make_job("j2"),
    ]
    page = consistency.paginate(jobs, "patient", 200)
    assert page["has_more"] is False
    assert len(page["items"]) == 1


def test_uid_page_key_ordering_collisions_after_coverage():
    # 同一新 UID 碰撞：两个原始 UID 各只在一个任务出现（覆盖差异）+ 一条碰撞
    j1 = _make_job("j1", sops={"1.1.1": "2.25.same"})
    j2 = _make_job("j2", sops={"1.1.2": "2.25.same"})

    items = consistency.level_differences([j1, j2], "sop")
    ranked = [(d["difference_type"], d.get("old_uid") or d["new_uid"])
              for d in items]
    # only_in_one_job(rank=0) 在 collision(rank=9) 之前；组内按业务键
    assert ranked == [
        ("only_in_one_job", "1.1.1"),
        ("only_in_one_job", "1.1.2"),
        ("uid_new_collision", "2.25.same"),
    ]


def test_paginate_deterministic_across_rebuilds():
    # keyset 定位基于确定性重建的列表：同样的定位点连续翻页结果一致
    jobs = [
        _make_job("j1", sops={f"1.1.{i}": f"2.25.{i}" for i in range(4)}),
        _make_job("j2", sops={"1.1.0": "2.25.0"}),
    ]
    page1 = consistency.paginate(jobs, "sop", 2)
    page2 = consistency.paginate(jobs, "sop", 2, after=page1["next_position"])
    page2_rebuilt = consistency.paginate(
        jobs, "sop", 2, after=page1["next_position"]
    )
    assert [d["old_uid"] for d in page2["items"]] == [
        d["old_uid"] for d in page2_rebuilt["items"]
    ]


def test_level_differences_empty_for_consistent_identical_jobs():
    payload = dict(sops={"1.1.1": "2.25.1"})
    j1 = _make_job("j1", **payload)
    j2 = _make_job("j2", **payload)
    for level in ("patient", "study", "series", "sop"):
        assert consistency.level_differences([j1, j2], level) == []
        page = consistency.paginate([j1, j2], level, 50)
        assert page["items"] == [] and page["has_more"] is False
