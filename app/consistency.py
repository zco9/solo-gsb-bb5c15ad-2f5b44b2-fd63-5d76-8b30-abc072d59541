"""跨任务一致性核对。

多个独立批次（任务）若使用同一把稳定密钥，则确定性派生保证：
* 同一患者身份（patient key）应得到相同伪名与日期偏移；
* 同一原始 Study/Series/SOP UID 应映射到同一新 UID；
* 不同原始 UID 不应碰撞到同一新 UID。

本模块对 2～5 个**已完成**任务做纯内存比对，输出差异报告：
* ``conflict`` —— 同一 key/原始 UID 在不同任务派生出不同结果（正常情况下不会发生，
  出现意味着版本/密钥派生逻辑不一致）；
* ``only_in_one_job`` —— 该标识仅出现在部分任务中，属于批次覆盖范围差异，不是冲突；
* ``uid_new_collision`` —— 不同原始 UID 在不同任务中派生出相同新 UID。

差异分页（``level_differences`` / ``paginate``）按**固定业务键**排序：
* patient：业务键为 patient_key；
* study/series/sop：业务键为 (difference_type_rank, old_uid 或 new_uid)，
  其中 uid_new_collision 以新 UID 作为业务键。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

from .anonymizer import JobResult
from .jobs import Job

# 每个 UID 层级差异明细条目上限（计数始终完整，仅截断非分页响应中的明细，防止 JSON 过大）
_MAX_DIFF_ENTRIES_PER_LEVEL = 500
# 患者层明细条目上限（同上）
_MAX_PATIENT_DIFF_ENTRIES = 500

# uid_new_collision 没有原始 UID，排序时排在同类型内部并以新 UID 定位
_COLLISION_RANK = 9

_LEVELS = ("patient", "study", "series", "sop")


def _job_counts(job: Job) -> dict[str, int]:
    result: JobResult = job.result
    return {
        "file_count": len(result.files),
        "output_file_count": sum(bool(r.output_name) for r in result.files),
        "error_count": sum(r.status == "error" for r in result.files),
        "warning_count": sum(r.status == "warning" for r in result.files),
        "patient_count": len(result.patients),
        "study_count": len(result.studies),
        "series_count": len(result.series),
        "sop_count": len(result.sops),
    }


def build_job_summary(job: Job) -> dict:
    counts = _job_counts(job)
    return {
        "id": job.id,
        "status": "ready",
        "secret_fingerprint": job.secret_fingerprint,
        "shift_dates": job.shift_dates,
        "source_zip": job.source_name,
        "created_at": job.created_at,
        "expires_at": job.expires_at,
        **counts,
    }


# --------------------------------------------------------------------------- #
# 患者映射
# --------------------------------------------------------------------------- #
def _build_patient_differences(jobs: list[Job]) -> tuple[list[dict], int]:
    """返回 (按 patient_key 排序的全部差异, 实际比对过的 key 数)。"""
    # patient_key -> job_id -> 伪名/偏移
    per_key: dict[str, dict[str, dict]] = defaultdict(dict)
    for job in jobs:
        for key, p in job.result.patients.items():
            per_key[key][job.id] = {
                "new_patient_id": p.new_patient_id,
                "new_patient_name": p.new_patient_name,
                "date_shift_days": p.date_shift_days,
            }

    job_ids = [j.id for j in jobs]
    differences: list[dict] = []
    checked_keys = 0

    for key in sorted(per_key):
        appearances = per_key[key]
        present = [jid for jid in job_ids if jid in appearances]
        missing = [jid for jid in job_ids if jid not in appearances]
        values = {jid: appearances[jid] for jid in present}

        if len(appearances) >= 2:
            checked_keys += 1
            ids = {v["new_patient_id"] for v in appearances.values()}
            names = {v["new_patient_name"] for v in appearances.values()}
            shifts = {v["date_shift_days"] for v in appearances.values()}
            if len(ids) > 1 or len(names) > 1:
                # 同一患者派生出不同伪名：确定性派生被破坏（真正的冲突）
                differences.append({
                    "patient_key": key,
                    "difference_type": "conflict",
                    "values": values,
                    "present_in": present,
                    "missing_in": [],
                })
            elif len(shifts) > 1:
                # 伪名一致但日期偏移不同：通常因各任务 shift_dates 开关不同
                differences.append({
                    "patient_key": key,
                    "difference_type": "date_shift_mismatch",
                    "values": values,
                    "present_in": present,
                    "missing_in": [],
                })
        else:
            differences.append({
                "patient_key": key,
                "difference_type": "only_in_one_job",
                "values": values,
                "present_in": present,
                "missing_in": missing,
            })

    return differences, checked_keys


def _compare_patients(jobs: list[Job]) -> dict:
    differences, checked_keys = _build_patient_differences(jobs)

    def _count(diff_type: str) -> int:
        return sum(d["difference_type"] == diff_type for d in differences)

    # 非分页响应仍对明细做上限截断（计数始终完整）
    kept = differences[:_MAX_PATIENT_DIFF_ENTRIES]
    shift_consistent = len({j.shift_dates for j in jobs}) == 1
    return {
        "checked_keys": checked_keys,
        "conflict_count": _count("conflict"),
        "date_shift_mismatch_count": _count("date_shift_mismatch"),
        "only_in_one_job_count": _count("only_in_one_job"),
        "differences_truncated": len(kept) < len(differences),
        "differences": kept,
        "shift_dates_consistent": shift_consistent,
    }


# --------------------------------------------------------------------------- #
# UID 映射
# --------------------------------------------------------------------------- #
def _uid_layer(jobs: list[Job], level: str) -> tuple[dict[str, dict[str, str]],
                                                      dict[str, dict[str, str]]]:
    """返回 (按原始 UID 索引, 按新 UID 索引) 的 {标识 -> {job_id: 对端 UID}}。"""
    by_old: dict[str, dict[str, str]] = defaultdict(dict)
    by_new: dict[str, dict[str, str]] = defaultdict(dict)
    for job in jobs:
        result: JobResult = job.result
        if level == "study":
            entries = (
                (v["old_study_instance_uid"], v["new_study_instance_uid"])
                for v in result.studies.values()
            )
        elif level == "series":
            entries = (
                (v["old_series_instance_uid"], v["new_series_instance_uid"])
                for v in result.series.values()
            )
        else:
            entries = result.sops.items()
        for old_uid, new_uid in entries:
            # 顶层 UID 缺失时会用确定性占位 UID 补齐，old 端为空/占位不具备跨批次可比性
            if not old_uid or old_uid.startswith("missing:"):
                continue
            by_old[old_uid][job.id] = new_uid
            by_new[new_uid][job.id] = old_uid
    return by_old, by_new


def _build_uid_differences(
    jobs: list[Job], level: str
) -> tuple[list[dict], list[dict], list[dict], int, int]:
    """收集单个 UID 层级的全部差异（不截断）。

    返回 (conflicts, collisions, coverage, checked, only_count)，
    其中 collisions/coverage 已按固定业务键排序，conflicts 保持原始 UID 顺序。
    """
    by_old, by_new = _uid_layer(jobs, level)
    job_ids = [j.id for j in jobs]
    conflicts: list[dict] = []
    collisions: list[dict] = []
    coverage: list[dict] = []
    checked = 0
    only_count = 0

    # 1) 同一原始 UID 的映射
    for old_uid in sorted(by_old):
        appearances = by_old[old_uid]
        present = [jid for jid in job_ids if jid in appearances]
        missing = [jid for jid in job_ids if jid not in appearances]

        if len(appearances) >= 2:
            checked += 1
            if len(set(appearances.values())) > 1:
                conflicts.append({
                    "level": level,
                    "old_uid": old_uid,
                    "difference_type": "conflict",
                    "mappings": {jid: appearances[jid] for jid in present},
                    "present_in": present,
                    "missing_in": [],
                })
        else:
            only_count += 1
            coverage.append({
                "level": level,
                "old_uid": old_uid,
                "difference_type": "only_in_one_job",
                "mappings": {jid: appearances[jid] for jid in present},
                "present_in": present,
                "missing_in": missing,
            })

    # 2) 新 UID 碰撞：同一新 UID 对应 >=2 个不同原始 UID
    for new_uid in sorted(by_new):
        sources = by_new[new_uid]
        if len(set(sources.values())) > 1:
            collisions.append({
                "level": level,
                "old_uid": None,
                "difference_type": "uid_new_collision",
                "new_uid": new_uid,
                "sources": dict(sources),
                "present_in": [jid for jid in job_ids if jid in sources],
                "missing_in": [jid for jid in job_ids if jid not in sources],
            })

    return conflicts, collisions, coverage, checked, only_count


def _compare_uid_level(jobs: list[Job], level: str) -> dict:
    conflicts, collisions, coverage, checked, only_count = _build_uid_differences(
        jobs, level
    )

    # 非分页响应：冲突/碰撞是关键差异，始终完整给出；覆盖范围差异可能成千上万，
    # 仅保留前若干条（conflicts + collisions + 前 N 条 coverage）
    budget = _MAX_DIFF_ENTRIES_PER_LEVEL - len(conflicts) - len(collisions)
    kept_coverage = coverage[:max(budget, 0)]
    all_count = len(conflicts) + len(collisions) + len(coverage)
    kept = conflicts + collisions + kept_coverage
    return {
        "level": level,
        "checked_old_uids": checked,
        "conflict_count": len(conflicts),
        "only_in_one_job_count": only_count,
        "new_uid_collision_count": len(collisions),
        "differences_truncated": len(kept) < all_count,
        "differences": kept,
    }


# --------------------------------------------------------------------------- #
# 差异分页（固定业务键排序 + keyset cursor 定位）
# --------------------------------------------------------------------------- #
def _page_key(diff: dict) -> list[Any]:
    """返回差异条目的固定排序键（同时作为 cursor 定位点）。"""
    diff_type = diff["difference_type"]
    if "patient_key" in diff:
        return [diff["patient_key"]]
    if diff_type == "uid_new_collision":
        return [str(_COLLISION_RANK), diff["new_uid"]]
    # conflict / only_in_one_job 以原始 UID 为业务键；用 rank 段隔离两类
    rank = 0 if diff_type == "only_in_one_job" else 1
    return [str(rank), diff["old_uid"]]


def level_differences(jobs: list[Job], level: str) -> list[dict]:
    """收集指定层级的全部差异，按固定业务键排序。"""
    if level == "patient":
        differences, _ = _build_patient_differences(jobs)
        return sorted(differences, key=_page_key)

    conflicts, collisions, coverage, _, _ = _build_uid_differences(jobs, level)
    return sorted(conflicts + collisions + coverage, key=_page_key)


def paginate(
    jobs: list[Job],
    level: str,
    limit: int,
    after: Optional[list[Any]] = None,
) -> dict:
    """按固定业务键返回一页差异。

    返回 ``{"items", "total", "has_more", "next_position"}``；``next_position`` 为
    本页最后一条的业务键（调用方负责签发 cursor），无下一页时为 None。
    """
    ordered = level_differences(jobs, level)
    total = len(ordered)
    start = 0
    if after is not None:
        # 定位点必须严格大于 cursor 中的业务键；数据确定性重建，直接线性跳过
        for index, diff in enumerate(ordered):
            if _page_key(diff) >= after:
                start = index
                break
        else:
            start = total
        # 跳过与定位点相等的条目（上一页最后一条本身）
        while start < total and _page_key(ordered[start]) == after:
            start += 1

    page = ordered[start:start + limit]
    has_more = start + limit < total
    return {
        "items": page,
        "total": total,
        "has_more": has_more,
        "next_position": _page_key(page[-1]) if has_more and page else None,
    }


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def build_report(jobs: list[Job]) -> dict:
    """对已校验（存在、ready、同密钥指纹）的任务构建一致性报告。"""
    patients = _compare_patients(jobs)
    uid_layers = [_compare_uid_level(jobs, level) for level in ("study", "series", "sop")]

    uid_conflicts = sum(layer["conflict_count"] for layer in uid_layers)
    uid_collisions = sum(layer["new_uid_collision_count"] for layer in uid_layers)
    uid_only = sum(layer["only_in_one_job_count"] for layer in uid_layers)

    inconsistent = (
        patients["conflict_count"] > 0
        or patients["date_shift_mismatch_count"] > 0
        or uid_conflicts > 0
        or uid_collisions > 0
    )
    return {
        "overall_status": "inconsistent" if inconsistent else "consistent",
        "secret_fingerprint": jobs[0].secret_fingerprint,
        "jobs": [build_job_summary(j) for j in jobs],
        "patient_mappings": patients,
        "uid_mappings": uid_layers,
        "summary": {
            "patient_conflict": patients["conflict_count"],
            "patient_date_shift_mismatch": patients["date_shift_mismatch_count"],
            "uid_conflict": uid_conflicts,
            "uid_new_collision": uid_collisions,
            "patient_only_in_one_job": patients["only_in_one_job_count"],
            "uid_only_in_one_job": uid_only,
        },
    }
