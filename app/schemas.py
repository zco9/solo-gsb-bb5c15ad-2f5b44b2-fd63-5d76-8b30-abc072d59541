"""API 响应模型（Pydantic v2）。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator


class DiagnosticOut(BaseModel):
    code: str
    severity: str = Field(description="info | warning | error")
    message: str


class ChangeOut(BaseModel):
    path: str = ""
    tag: Optional[str] = None
    keyword: str
    action: str = Field(description="REPLACE | EMPTY | REMOVE")
    old: Optional[str] = None
    new: Optional[str] = None


class FileOut(BaseModel):
    name: str
    status: str = Field(description="ok | warning | error | skipped")
    output_name: Optional[str] = None
    patient_key: Optional[str] = None
    study_instance_uid: Optional[str] = None
    series_instance_uid: Optional[str] = None
    sop_instance_uid: Optional[str] = None
    max_severity: Optional[str] = None
    diagnostics: list[DiagnosticOut] = []
    changes: list[ChangeOut] = []
    changes_truncated: bool = False


class PatientOut(BaseModel):
    key: str
    new_patient_id: str
    new_patient_name: str
    date_shift_days: int
    file_count: int


class StudyOut(BaseModel):
    old_study_instance_uid: str
    new_study_instance_uid: str
    patient_key: str
    file_count: int


class SeriesOut(BaseModel):
    old_series_instance_uid: str
    new_series_instance_uid: str
    new_study_instance_uid: str
    file_count: int


class JobOut(BaseModel):
    id: str
    status: str = Field(description="ready | failed")
    secret_fingerprint: str
    shift_dates: bool
    source_zip: str
    created_at: float
    expires_at: float
    file_count: int
    output_file_count: int
    error_count: int
    warning_count: int
    changes_capped: bool
    patients: list[PatientOut]
    studies: list[StudyOut]
    series: list[SeriesOut]
    files: list[FileOut]
    error: Optional[str] = None


class JobSummary(BaseModel):
    id: str
    status: str
    source_zip: str
    file_count: int
    output_file_count: int
    error_count: int
    warning_count: int
    created_at: float
    expires_at: float


# --------------------------------------------------------------------------- #
# 跨任务一致性核对（POST /jobs/consistency-check）
# --------------------------------------------------------------------------- #
class ConsistencyCheckRequest(BaseModel):
    job_ids: list[str] = Field(
        description="已完成（status=ready）任务 ID，2～5 个；不得重复",
        min_length=2,
        max_length=5,
    )

    @field_validator("job_ids")
    @classmethod
    def _strip_and_reject_duplicates(cls, value: list[str]) -> list[str]:
        ids = [str(jid).strip() for jid in value]
        if any(not jid for jid in ids):
            raise ValueError("任务 ID 不能为空字符串")
        if len(set(ids)) != len(ids):
            raise ValueError("任务 ID 不得重复")
        return ids


class ConsistencyJobSummary(BaseModel):
    id: str
    status: str = Field(description="固定为 ready（未完成任务会直接返回 409）")
    secret_fingerprint: str
    shift_dates: bool
    source_zip: str
    created_at: float
    expires_at: float
    file_count: int
    output_file_count: int
    error_count: int
    warning_count: int
    patient_count: int
    study_count: int
    series_count: int
    sop_count: int


class PatientMappingValue(BaseModel):
    new_patient_id: str
    new_patient_name: str
    date_shift_days: int


class PatientMappingDifference(BaseModel):
    patient_key: str
    difference_type: str = Field(
        description="conflict（同 key 在不同任务派生出不同伪名） | "
        "date_shift_mismatch（伪名一致但日期偏移不同，通常因 shift_dates 开关不同） | "
        "only_in_one_job（该患者仅出现在部分任务中，属于覆盖范围差异，不是冲突）"
    )
    values: dict[str, PatientMappingValue] = Field(
        description="任务 ID -> 该任务下的稳定伪名/日期偏移（仅包含出现该患者的任务）"
    )
    present_in: list[str] = Field(description="出现该患者的任务 ID")
    missing_in: list[str] = Field(default=[], description="未出现该患者的任务 ID")


class PatientMappingReport(BaseModel):
    checked_keys: int = Field(description="在 >=2 个任务中共同出现、实际比对过的患者 key 数")
    conflict_count: int
    date_shift_mismatch_count: int
    only_in_one_job_count: int
    differences: list[PatientMappingDifference]
    shift_dates_consistent: bool = Field(
        description="所有参与任务的 shift_dates 开关是否一致（开关不同会导致日期偏移策略不一致）"
    )


class UidMappingDifference(BaseModel):
    level: str = Field(description="study | series | sop")
    old_uid: Optional[str] = Field(
        default=None, description="原始 UID；uid_new_collision 时可能为空"
    )
    difference_type: str = Field(
        description="conflict（同一原始 UID 映射到不同新 UID） | "
        "only_in_one_job（覆盖范围差异） | "
        "uid_new_collision（同一新 UID 在不同任务中对应不同原始 UID）"
    )
    mappings: dict[str, str] = Field(
        default={}, description="任务 ID -> 新 UID（conflict / only_in_one_job 时给出）"
    )
    new_uid: Optional[str] = Field(default=None, description="uid_new_collision 时的新 UID")
    sources: dict[str, str] = Field(
        default={}, description="uid_new_collision 时：任务 ID -> 各自的原始 UID"
    )
    present_in: list[str] = Field(default=[])
    missing_in: list[str] = Field(default=[])


class UidLevelReport(BaseModel):
    level: str = Field(description="study | series | sop")
    checked_old_uids: int = Field(description="在 >=2 个任务中共同出现、实际比对过的原始 UID 数")
    conflict_count: int
    only_in_one_job_count: int
    new_uid_collision_count: int
    differences_truncated: bool = Field(
        description="差异条目过多时已截断（计数仍然完整）"
    )
    differences: list[UidMappingDifference]


class ConsistencyCheckOut(BaseModel):
    overall_status: str = Field(
        description="consistent（无任何冲突） | inconsistent（存在伪名或 UID 冲突）"
    )
    secret_fingerprint: str = Field(description="参与任务共同的密钥指纹（SHA-256 前 12 位）")
    jobs: list[ConsistencyJobSummary]
    patient_mappings: PatientMappingReport
    uid_mappings: list[UidLevelReport] = Field(
        description="按 Study / Series / SOP 三个层级给出的映射差异"
    )
    summary: dict[str, int] = Field(
        description="patient_conflict / patient_date_shift_mismatch / "
        "uid_conflict / uid_new_collision / "
        "patient_only_in_one_job / uid_only_in_one_job 计数"
    )


class ConsistencyErrorDetail(BaseModel):
    code: str = Field(
        description="JOB_NOT_FOUND | JOBS_NOT_COMPLETED | SECRET_FINGERPRINT_MISMATCH"
    )
    message: str
    missing_job_ids: list[str] = []
    not_ready_jobs: list[dict[str, str]] = []
    fingerprints: dict[str, str] = {}


class ErrorOut(BaseModel):
    error: str
    detail: Optional[str] = None
