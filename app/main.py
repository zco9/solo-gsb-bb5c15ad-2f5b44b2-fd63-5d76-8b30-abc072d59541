"""DICOM 批量匿名化 API。"""
from __future__ import annotations

import asyncio
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .anonymizer import JobResult
from . import consistency
from .config import settings
from .jobs import registry
from .schemas import (
    ConsistencyCheckOut,
    ConsistencyCheckRequest,
    ConsistencyErrorDetail,
    FileOut,
    JobOut,
    PatientOut,
    SeriesOut,
    StudyOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry.setup()
    task = asyncio.create_task(registry.purge_loop())
    yield
    task.cancel()


app = FastAPI(
    title="DICOM 批量匿名化 API",
    version="1.0.0",
    description=(
        "上传影像 ZIP，使用用户提供的稳定密钥生成患者级伪名，重写 "
        "Patient/Study/Series/SOP/FrameOfReference UID 与身份字段，"
        "递归更新序列内引用并保持检查关系。提供标签变更预览与匿名化 ZIP 下载，"
        "并支持对多个已完成任务做跨批次密钥指纹/伪名/UID 映射一致性核对。"
    ),
    lifespan=lifespan,
)

_executor = ThreadPoolExecutor(max_workers=2)


def _serialize_job(job) -> JobOut:
    if job.status == "failed":
        return JobOut(
            id=job.id,
            status="failed",
            secret_fingerprint=job.secret_fingerprint,
            shift_dates=job.shift_dates,
            source_zip=job.source_name,
            created_at=job.created_at,
            expires_at=job.expires_at,
            file_count=0,
            output_file_count=0,
            error_count=1,
            warning_count=0,
            changes_capped=False,
            patients=[],
            studies=[],
            series=[],
            files=[],
            error=job.error,
        )

    result: JobResult = job.result
    files = [FileOut.model_validate(r.to_dict(include_changes=True)) for r in result.files]
    error_count = sum(r.status == "error" for r in result.files)
    warning_count = sum(r.status == "warning" for r in result.files)
    output_count = sum(bool(r.output_name) for r in result.files)

    return JobOut(
        id=job.id,
        status="ready",
        secret_fingerprint=job.secret_fingerprint,
        shift_dates=job.shift_dates,
        source_zip=job.source_name,
        created_at=job.created_at,
        expires_at=job.expires_at,
        file_count=len(result.files),
        output_file_count=output_count,
        error_count=error_count,
        warning_count=warning_count,
        changes_capped=result.total_changes,
        patients=[
            PatientOut(
                key=p.key,
                new_patient_id=p.new_patient_id,
                new_patient_name=p.new_patient_name,
                date_shift_days=p.date_shift_days,
                file_count=p.file_count,
            )
            for p in sorted(result.patients.values(), key=lambda x: x.key)
        ],
        studies=[
            StudyOut(**v) for v in sorted(
                result.studies.values(), key=lambda x: x["new_study_instance_uid"]
            )
        ],
        series=[
            SeriesOut(**v) for v in sorted(
                result.series.values(), key=lambda x: x["new_series_instance_uid"]
            )
        ],
        files=files,
    )


@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/jobs",
    response_model=JobOut,
    status_code=201,
    tags=["jobs"],
    summary="上传影像 ZIP 并创建匿名化任务（同步返回标签变更预览）",
)
async def create_job(
    file: UploadFile = File(..., description="包含 DICOM 文件的 ZIP 压缩包"),
    secret: str = Form(..., description="用户稳定密钥（>=8 字符），用于确定性派生伪名/UID"),
    shift_dates: bool = Form(True, description="是否按患者稳定偏移平移 DA/DT 日期"),
):
    if len(secret) < settings.min_secret_length:
        raise HTTPException(
            status_code=422,
            detail=f"稳定密钥长度至少 {settings.min_secret_length} 个字符",
        )
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=422, detail="仅接受 .zip 压缩包")

    # 流式落盘，超过大小上限立即拒绝
    spool = tempfile.NamedTemporaryFile(prefix="upload-", suffix=".zip", delete=False)
    total = 0
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > settings.max_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"上传文件超过大小上限 "
                        f"{settings.max_upload_bytes // (1024 * 1024)} MiB"
                    ),
                )
            spool.write(chunk)
        spool.close()
        if total == 0:
            raise HTTPException(status_code=422, detail="上传内容为空")

        loop = asyncio.get_running_loop()
        job = await loop.run_in_executor(
            _executor,
            registry.create_and_process,
            spool.name,
            file.filename,
            secret,
            shift_dates,
        )
    finally:
        if os.path.exists(spool.name):
            try:
                os.unlink(spool.name)
            except OSError:
                pass

    return _serialize_job(job)


@app.post(
    "/jobs/consistency-check",
    response_model=ConsistencyCheckOut,
    tags=["jobs"],
    summary="核对多个已完成任务的密钥指纹、患者稳定伪名与 Study/Series/SOP UID 映射",
    responses={
        404: {"model": ConsistencyErrorDetail, "description": "部分任务不存在或已过期"},
        409: {"model": ConsistencyErrorDetail,
              "description": "部分任务未完成，或任务间密钥指纹不一致"},
    },
)
def consistency_check(body: ConsistencyCheckRequest) -> ConsistencyCheckOut:
    jobs = []
    missing = []
    for job_id in body.job_ids:
        job = registry.get(job_id)
        if job is None:
            missing.append(job_id)
        else:
            jobs.append(job)
    if missing:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "JOB_NOT_FOUND",
                "message": "部分任务不存在或已过期",
                "missing_job_ids": missing,
            },
        )

    not_ready = [
        {"id": job.id, "status": job.status, "error": job.error or ""}
        for job in jobs
        if job.status != "ready"
    ]
    if not_ready:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "JOBS_NOT_COMPLETED",
                "message": "仅支持已完成（status=ready）的任务",
                "not_ready_jobs": not_ready,
            },
        )

    fingerprints = {job.id: job.secret_fingerprint for job in jobs}
    if len(set(fingerprints.values())) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SECRET_FINGERPRINT_MISMATCH",
                "message": "任务使用了不同的稳定密钥（指纹不一致），无法进行一致性核对",
                "fingerprints": fingerprints,
            },
        )

    return ConsistencyCheckOut.model_validate(consistency.build_report(jobs))


@app.get(
    "/jobs/{job_id}",
    response_model=JobOut,
    tags=["jobs"],
    summary="获取任务标签变更预览与逐文件诊断",
)
def get_job(job_id: str) -> JobOut:
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return _serialize_job(job)


@app.get(
    "/jobs/{job_id}/files",
    response_model=list[FileOut],
    tags=["jobs"],
    summary="仅获取逐文件诊断与变更列表",
)
def list_job_files(job_id: str) -> list[FileOut]:
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if job.status != "ready":
        return []
    return [FileOut.model_validate(r.to_dict(include_changes=True)) for r in job.result.files]


@app.get(
    "/jobs/{job_id}/download",
    tags=["jobs"],
    summary="下载匿名化后的 ZIP（扁平文件名，不含原始路径）",
)
def download_job(job_id: str):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if job.status != "ready":
        raise HTTPException(status_code=409, detail=f"任务处理失败：{job.error}")
    zip_path = registry.ensure_zip(job)
    error_count = sum(r.status == "error" for r in job.result.files)
    name = f"anonymized-{job.id[:8]}.zip"
    headers = {
        "X-Job-File-Count": str(len(job.result.files)),
        "X-Job-Error-Count": str(error_count),
    }
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=name,
        headers=headers,
    )


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {
        "service": "dicom-anon-api",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "endpoints": [
            "POST /jobs",
            "POST /jobs/consistency-check",
            "GET /jobs/{job_id}",
            "GET /jobs/{job_id}/files",
            "GET /jobs/{job_id}/download",
            "GET /health",
        ],
    }
