"""FastAPI 端到端测试：上传、预览、诊断、下载、错误处理。"""
from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from tests.sample_data import build_sample_zip


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 必须在导入应用配置前设置工作目录
    monkeypatch.setenv("DICOM_ANON_WORK_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("DICOM_ANON_JOB_TTL_SECONDS", "3600")
    yield from _client_fixture(tmp_path)


@pytest.fixture()
def client_short_cursor_ttl(tmp_path, monkeypatch):
    # cursor TTL=0：签出的 cursor 立即过期，用于验证 400 INVALID_CURSOR
    monkeypatch.setenv("DICOM_ANON_WORK_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("DICOM_ANON_JOB_TTL_SECONDS", "3600")
    monkeypatch.setenv("DICOM_ANON_CURSOR_TTL_SECONDS", "0")
    yield from _client_fixture(tmp_path)


def _client_fixture(tmp_path):
    # 清理已导入模块以让配置重新生效
    import importlib
    import app.config
    import app.jobs
    importlib.reload(app.config)
    importlib.reload(app.jobs)
    import app.main
    importlib.reload(app.main)
    with TestClient(app.main.app) as c:
        yield c


@pytest.fixture()
def sample_zip_bytes(tmp_path):
    p = tmp_path / "sample.zip"
    build_sample_zip(str(p))
    return p.read_bytes()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_full_flow_upload_preview_download(client, sample_zip_bytes):
    r = client.post(
        "/jobs",
        files={"file": ("study.zip", sample_zip_bytes, "application/zip")},
        data={"secret": "api-secret-1234567890", "shift_dates": "true"},
    )
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["status"] == "ready"
    assert job["file_count"] == 6
    assert job["output_file_count"] == 4
    assert job["error_count"] == 2
    assert len(job["patients"]) == 2
    # 预览中包含逐文件变更
    ct = next(f for f in job["files"] if f["name"] == "patient_1/CT0001.dcm")
    assert ct["status"] == "ok"
    keywords = {c["keyword"] for c in ct["changes"]}
    assert "PatientID" in keywords and "StudyInstanceUID" in keywords

    job_id = job["id"]
    # GET /jobs/{id}
    r2 = client.get(f"/jobs/{job_id}")
    assert r2.status_code == 200
    assert r2.json()["id"] == job_id

    # /files 子资源
    r3 = client.get(f"/jobs/{job_id}/files")
    assert r3.status_code == 200
    assert len(r3.json()) == 6

    # 下载
    r4 = client.get(f"/jobs/{job_id}/download")
    assert r4.status_code == 200
    assert r4.headers["content-type"] == "application/zip"
    body = io.BytesIO(r4.content)
    with zipfile.ZipFile(body) as zf:
        assert len(zf.namelist()) == 4
        assert all(n.endswith(".dcm") for n in zf.namelist())


def test_dangling_reference_diagnostic_visible(client, sample_zip_bytes):
    job = client.post(
        "/jobs",
        files={"file": ("study.zip", sample_zip_bytes, "application/zip")},
        data={"secret": "api-secret-1234567890"},
    ).json()
    rt = next(f for f in job["files"] if f["name"].endswith("RS0001.dcm"))
    assert rt["status"] == "warning"
    codes = {d["code"] for d in rt["diagnostics"]}
    assert "DANGLING_REFERENCE" in codes


def test_invalid_dicom_diagnostic_visible(client, sample_zip_bytes):
    job = client.post(
        "/jobs",
        files={"file": ("study.zip", sample_zip_bytes, "application/zip")},
        data={"secret": "api-secret-1234567890"},
    ).json()
    bad = next(f for f in job["files"] if f["name"] == "broken/bad.dcm")
    assert bad["status"] == "error"
    assert any(d["code"] == "INVALID_DICOM" and d["severity"] == "error"
               for d in bad["diagnostics"])


def test_short_secret_rejected(client, sample_zip_bytes):
    r = client.post(
        "/jobs",
        files={"file": ("study.zip", sample_zip_bytes, "application/zip")},
        data={"secret": "short"},
    )
    assert r.status_code == 422


def test_non_zip_rejected(client):
    r = client.post(
        "/jobs",
        files={"file": ("study.txt", b"hello", "text/plain")},
        data={"secret": "api-secret-1234567890"},
    )
    assert r.status_code == 422


def test_empty_upload_rejected(client):
    r = client.post(
        "/jobs",
        files={"file": ("empty.zip", b"", "application/zip")},
        data={"secret": "api-secret-1234567890"},
    )
    assert r.status_code == 422


def test_bad_zip_reports_failed_job(client):
    r = client.post(
        "/jobs",
        files={"file": ("broken.zip", b"PK\x03\x04 garbage", "application/zip")},
        data={"secret": "api-secret-1234567890"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "failed"
    assert body["error"]


def test_job_not_found(client):
    assert client.get("/jobs/nope/download").status_code == 404
    assert client.get("/jobs/nope").status_code == 404


def test_same_secret_cross_batch_same_pseudonym(client, sample_zip_bytes):
    def upload():
        return client.post(
            "/jobs",
            files={"file": ("study.zip", sample_zip_bytes, "application/zip")},
            data={"secret": "cross-batch-secret-123"},
        ).json()

    j1 = upload()
    j2 = upload()
    p1 = {p["key"]: p["new_patient_id"] for p in j1["patients"]}
    p2 = {p["key"]: p["new_patient_id"] for p in j2["patients"]}
    assert p1 == p2


# --------------------------------------------------------------------------- #
# POST /jobs/consistency-check
# --------------------------------------------------------------------------- #
def _upload(client, sample_zip_bytes, secret, *, shift_dates="true"):
    return client.post(
        "/jobs",
        files={"file": ("study.zip", sample_zip_bytes, "application/zip")},
        data={"secret": secret, "shift_dates": shift_dates},
    ).json()


def test_consistency_check_same_secret_same_batch(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "consistency-secret-1")
    j2 = _upload(client, sample_zip_bytes, "consistency-secret-1")

    r = client.post("/jobs/consistency-check", json={"job_ids": [j1["id"], j2["id"]]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["overall_status"] == "consistent"
    assert body["secret_fingerprint"] == j1["secret_fingerprint"] == j2["secret_fingerprint"]
    assert {j["id"] for j in body["jobs"]} == {j1["id"], j2["id"]}
    # 两批数据完全相同：患者/UID 全部共同出现且映射一致
    pm = body["patient_mappings"]
    assert pm["checked_keys"] == 2
    assert pm["conflict_count"] == 0
    assert pm["only_in_one_job_count"] == 0
    assert pm["shift_dates_consistent"] is True
    for layer in body["uid_mappings"]:
        assert layer["conflict_count"] == 0
        assert layer["new_uid_collision_count"] == 0
        assert layer["only_in_one_job_count"] == 0
        assert layer["checked_old_uids"] > 0
    assert body["summary"]["patient_conflict"] == 0
    assert body["summary"]["uid_conflict"] == 0


def test_consistency_check_overlapping_batches_reports_coverage(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "overlap-secret-123")
    j2 = _upload(client, sample_zip_bytes, "overlap-secret-123")
    # 删除第二个任务里的一名患者及其 UID，模拟两个批次覆盖范围不同
    victim = next(p for p in j2["patients"] if p["key"] == "id:P-002")
    j2_obj = _registry().get(j2["id"])
    victim_studies = {
        v["new_study_instance_uid"]
        for v in j2_obj.result.studies.values() if v["patient_key"] == victim["key"]
    }
    j2_obj.result.patients.pop(victim["key"], None)
    j2_obj.result.studies = {
        k: v for k, v in j2_obj.result.studies.items()
        if v["patient_key"] != victim["key"]
    }
    j2_obj.result.series = {
        k: v for k, v in j2_obj.result.series.items()
        if v["new_study_instance_uid"] not in victim_studies
    }
    j2_obj.result.sops = dict(list(j2_obj.result.sops.items())[:4])

    r = client.post("/jobs/consistency-check", json={"job_ids": [j1["id"], j2["id"]]})
    assert r.status_code == 200, r.text
    body = r.json()
    # 覆盖范围差异不构成冲突
    assert body["overall_status"] == "consistent"
    pm = body["patient_mappings"]
    diff = next(d for d in pm["differences"] if d["patient_key"] == "id:P-002")
    assert diff["difference_type"] == "only_in_one_job"
    assert diff["present_in"] == [j1["id"]]
    assert diff["missing_in"] == [j2["id"]]
    assert pm["only_in_one_job_count"] == 1
    assert body["summary"]["patient_only_in_one_job"] == 1
    assert body["summary"]["uid_only_in_one_job"] > 0


def test_consistency_check_different_secret_409(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "consistency-secret-A")
    j2 = _upload(client, sample_zip_bytes, "consistency-secret-B")

    r = client.post("/jobs/consistency-check", json={"job_ids": [j1["id"], j2["id"]]})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "SECRET_FINGERPRINT_MISMATCH"
    assert set(detail["fingerprints"]) == {j1["id"], j2["id"]}
    assert len(set(detail["fingerprints"].values())) == 2


def test_consistency_check_missing_job_404(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "consistency-secret-1")
    r = client.post(
        "/jobs/consistency-check", json={"job_ids": [j1["id"], "does-not-exist"]}
    )
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "JOB_NOT_FOUND"
    assert detail["missing_job_ids"] == ["does-not-exist"]


def test_consistency_check_failed_job_409(client):
    # 构造一个 failed 任务
    failed = client.post(
        "/jobs",
        files={"file": ("broken.zip", b"PK\x03\x04 garbage", "application/zip")},
        data={"secret": "consistency-secret-1"},
    ).json()
    # 另一个 ready 任务通过篡改 failed 任务的前一步不易构造，直接手工在注册表中造一个
    jid = failed["id"]
    r = client.post("/jobs/consistency-check", json={"job_ids": [jid, jid]})
    # 重复 ID 先被 422 拦截
    assert r.status_code == 422

    # failed + 不存在的组合：不存在优先返回 404
    r2 = client.post(
        "/jobs/consistency-check", json={"job_ids": [jid, "nope"]}
    )
    assert r2.status_code == 404

    # 需要两个 failed 任务触发 not-ready 分支
    failed2 = client.post(
        "/jobs",
        files={"file": ("broken2.zip", b"PK\x03\x04 garbage2", "application/zip")},
        data={"secret": "consistency-secret-1"},
    ).json()
    r3 = client.post(
        "/jobs/consistency-check", json={"job_ids": [jid, failed2["id"]]}
    )
    assert r3.status_code == 409
    detail = r3.json()["detail"]
    assert detail["code"] == "JOBS_NOT_COMPLETED"
    assert {j["id"] for j in detail["not_ready_jobs"]} == {jid, failed2["id"]}
    assert all(j["status"] == "failed" for j in detail["not_ready_jobs"])


def test_consistency_check_validation_cardinality(client, sample_zip_bytes):
    j = _upload(client, sample_zip_bytes, "consistency-secret-1")

    assert client.post("/jobs/consistency-check", json={"job_ids": [j["id"]]}).status_code == 422
    assert client.post(
        "/jobs/consistency-check",
        json={"job_ids": [j["id"]] * 6},
    ).status_code == 422
    assert client.post("/jobs/consistency-check", json={"job_ids": []}).status_code == 422
    assert client.post("/jobs/consistency-check", json={}).status_code == 422
    # 重复 ID
    assert client.post(
        "/jobs/consistency-check", json={"job_ids": [j["id"], j["id"]]}
    ).status_code == 422
    # 空白 ID
    assert client.post(
        "/jobs/consistency-check", json={"job_ids": [j["id"], "   "]}
    ).status_code == 422


def test_consistency_check_accepts_up_to_five_jobs(client, sample_zip_bytes):
    ids = [_upload(client, sample_zip_bytes, "five-jobs-secret-12")["id"] for _ in range(5)]
    r = client.post("/jobs/consistency-check", json={"job_ids": ids})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["jobs"]) == 5
    assert body["overall_status"] == "consistent"


def _registry():
    # 测试夹具 reload 过 app.jobs，必须经由 app.main 拿到同一份 registry
    from app.main import registry
    return registry


def _prune_second_job(j2: dict, keep_sops: int = 4):
    """裁剪第二个任务：移除一名患者及其 study/series，sop 只保留前 keep_sops 个。"""
    victim = next(p for p in j2["patients"] if p["key"] == "id:P-002")
    j2_obj = _registry().get(j2["id"])
    victim_studies = {
        v["new_study_instance_uid"]
        for v in j2_obj.result.studies.values() if v["patient_key"] == victim["key"]
    }
    j2_obj.result.patients.pop(victim["key"], None)
    j2_obj.result.studies = {
        k: v for k, v in j2_obj.result.studies.items()
        if v["patient_key"] != victim["key"]
    }
    j2_obj.result.series = {
        k: v for k, v in j2_obj.result.series.items()
        if v["new_study_instance_uid"] not in victim_studies
    }
    j2_obj.result.sops = dict(list(j2_obj.result.sops.items())[:keep_sops])
    return j2_obj


def _paged_pair(client, sample_zip_bytes, secret="page-secret-12345",
                extra_sops=10, keep_sops=2):
    """构造覆盖范围不同的两个任务，并在 j1 中注入额外 SOP 映射以便分页。"""
    j1 = _upload(client, sample_zip_bytes, secret)
    j2 = _upload(client, sample_zip_bytes, secret)
    j1_obj = _registry().get(j1["id"])
    for i in range(extra_sops):
        j1_obj.result.sops[f"9.9.{i:03d}"] = f"2.25.900{i:03d}"
    _prune_second_job(j2, keep_sops=keep_sops)
    return j1, j2


# --------------------------------------------------------------------------- #
# POST /jobs/consistency-check 差异明细分页
# --------------------------------------------------------------------------- #
def test_consistency_check_page_preserves_report_and_counts(client, sample_zip_bytes):
    j1, j2 = _paged_pair(client, sample_zip_bytes)

    r = client.post("/jobs/consistency-check", json={
        "job_ids": [j1["id"], j2["id"]], "level": "sop", "limit": 2,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    # 原有完整计数与 overall_status 保持不变
    assert body["overall_status"] == "consistent"
    assert body["summary"]["uid_only_in_one_job"] > 0

    page = body["difference_page"]
    assert page["level"] == "sop"
    assert page["limit"] == 2
    sop_report = next(l for l in body["uid_mappings"] if l["level"] == "sop")
    # total 与该层级完整计数一致（分页不截断计数）
    assert page["total"] == sop_report["only_in_one_job_count"]
    assert body["summary"]["uid_only_in_one_job"] >= page["total"]
    assert len(page["items"]) == 2
    assert page["has_more"] is True
    assert page["next_cursor"]


def test_consistency_check_walk_pages_with_cursor(client, sample_zip_bytes):
    j1, j2 = _paged_pair(client, sample_zip_bytes)
    ids = [j1["id"], j2["id"]]

    collected = []
    cursor = None
    pages = 0
    while True:
        payload = {"job_ids": ids, "level": "sop", "limit": 2}
        if cursor:
            payload["cursor"] = cursor
        body = client.post("/jobs/consistency-check", json=payload).json()
        page = body["difference_page"]
        collected.extend(page["items"])
        pages += 1
        assert page["limit"] == 2
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]

    assert pages >= 2
    keys = [d["old_uid"] for d in collected]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))
    # 全量收集条数与 total（该层级完整计数）一致
    first = client.post("/jobs/consistency-check",
                        json={"job_ids": ids, "level": "sop", "limit": 200}).json()
    assert len(collected) == first["difference_page"]["total"]


def test_consistency_check_patient_level_page(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2 = _upload(client, sample_zip_bytes, "page-secret-12345")
    _prune_second_job(j2)

    body = client.post("/jobs/consistency-check", json={
        "job_ids": [j1["id"], j2["id"]], "level": "patient",
    }).json()
    page = body["difference_page"]
    assert page["level"] == "patient"
    assert page["total"] == 1
    assert page["items"][0]["patient_key"] == "id:P-002"
    assert page["items"][0]["difference_type"] == "only_in_one_job"
    assert page["has_more"] is False and page["next_cursor"] is None


def test_consistency_check_no_level_keeps_legacy_shape(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2 = _upload(client, sample_zip_bytes, "page-secret-12345")
    _prune_second_job(j2)

    body = client.post(
        "/jobs/consistency-check", json={"job_ids": [j1["id"], j2["id"]]}
    ).json()
    assert "difference_page" in body
    assert body["difference_page"] is None
    # 旧字段原样存在
    assert body["overall_status"] == "consistent"
    assert body["patient_mappings"]["only_in_one_job_count"] == 1


def test_consistency_check_items_sorted_by_business_key(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2 = _upload(client, sample_zip_bytes, "page-secret-12345")
    _prune_second_job(j2)

    body = client.post("/jobs/consistency-check", json={
        "job_ids": [j1["id"], j2["id"]], "level": "study", "limit": 200,
    }).json()
    keys = [d["old_uid"] for d in body["difference_page"]["items"]]
    assert keys == sorted(keys)


def test_consistency_check_default_limit_100(client, sample_zip_bytes):
    j1, j2 = _paged_pair(client, sample_zip_bytes, extra_sops=5)
    body = client.post("/jobs/consistency-check", json={
        "job_ids": [j1["id"], j2["id"]], "level": "sop",
    }).json()
    assert body["difference_page"]["limit"] == 100
    assert body["difference_page"]["has_more"] is False


def test_consistency_check_limit_bounds_rejected(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2 = _upload(client, sample_zip_bytes, "page-secret-12345")
    for bad_limit in (0, -1, 201, 1000):
        r = client.post("/jobs/consistency-check", json={
            "job_ids": [j1["id"], j2["id"]], "level": "sop", "limit": bad_limit,
        })
        assert r.status_code == 422, bad_limit


def test_consistency_check_bad_level_rejected(client, sample_zip_bytes):
    j1 = _upload(client, sample_zip_bytes, "page-secret-12345")
    j2 = _upload(client, sample_zip_bytes, "page-secret-12345")
    r = client.post("/jobs/consistency-check", json={
        "job_ids": [j1["id"], j2["id"]], "level": "instance",
    })
    assert r.status_code == 422


def _first_page_cursor(client, ids, level="sop", limit=2):
    body = client.post("/jobs/consistency-check", json={
        "job_ids": ids, "level": level, "limit": limit,
    }).json()
    return body["difference_page"]["next_cursor"]


def test_consistency_check_tampered_cursor_400(client, sample_zip_bytes):
    j1, j2 = _paged_pair(client, sample_zip_bytes)
    ids = [j1["id"], j2["id"]]
    cursor = _first_page_cursor(client, ids)

    # 在 body 前插入一段合法 base64 字符：载荷变化而签名不变，验签必然失败
    head, _, sig = cursor.rpartition(".")
    tampered = f"AAAA{head}.{sig}"
    r = client.post("/jobs/consistency-check", json={
        "job_ids": ids, "level": "sop", "limit": 2, "cursor": tampered,
    })
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_CURSOR"

    # 完全非法
    r2 = client.post("/jobs/consistency-check", json={
        "job_ids": ids, "level": "sop", "cursor": "not-a-cursor!!!",
    })
    assert r2.status_code == 400
    assert r2.json()["detail"]["code"] == "INVALID_CURSOR"


def test_consistency_check_cursor_cross_job_set_400(client, sample_zip_bytes):
    j1, j2 = _paged_pair(client, sample_zip_bytes)
    j3 = _upload(client, sample_zip_bytes, "page-secret-12345")
    cursor = _first_page_cursor(client, [j1["id"], j2["id"]])

    # 换任务集合
    r = client.post("/jobs/consistency-check", json={
        "job_ids": [j1["id"], j3["id"]], "level": "sop", "cursor": cursor,
    })
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_CURSOR"

    # 同一组任务但顺序不同也不允许（cursor 绑定有序集合）
    r2 = client.post("/jobs/consistency-check", json={
        "job_ids": [j2["id"], j1["id"]], "level": "sop", "cursor": cursor,
    })
    assert r2.status_code == 400


def test_consistency_check_cursor_cross_level_400(client, sample_zip_bytes):
    j1, j2 = _paged_pair(client, sample_zip_bytes)
    ids = [j1["id"], j2["id"]]
    cursor = _first_page_cursor(client, ids, level="sop")

    r = client.post("/jobs/consistency-check", json={
        "job_ids": ids, "level": "study", "cursor": cursor,
    })
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_CURSOR"


def test_consistency_check_cursor_omits_level_and_continues(client, sample_zip_bytes):
    j1, j2 = _paged_pair(client, sample_zip_bytes)
    ids = [j1["id"], j2["id"]]
    first = client.post("/jobs/consistency-check", json={
        "job_ids": ids, "level": "sop", "limit": 2,
    }).json()["difference_page"]
    assert first["has_more"]

    # 翻页请求不带 level：由 cursor 自身携带的层级继续
    second = client.post("/jobs/consistency-check", json={
        "job_ids": ids, "cursor": first["next_cursor"], "limit": 2,
    }).json()["difference_page"]
    assert second["level"] == "sop"
    first_keys = {d["old_uid"] for d in first["items"]}
    assert all(d["old_uid"] not in first_keys for d in second["items"])


def test_consistency_check_expired_cursor_400(client_short_cursor_ttl, sample_zip_bytes):
    client = client_short_cursor_ttl
    j1, j2 = _paged_pair(client, sample_zip_bytes)
    ids = [j1["id"], j2["id"]]

    body = client.post("/jobs/consistency-check", json={
        "job_ids": ids, "level": "sop", "limit": 2,
    }).json()
    cursor = body["difference_page"]["next_cursor"]
    assert cursor  # 数据量足以产生下一页

    r = client.post("/jobs/consistency-check", json={
        "job_ids": ids, "level": "sop", "cursor": cursor,
    })
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "INVALID_CURSOR"
    assert "过期" in detail["message"]


def test_consistency_check_pagination_with_missing_job_404(client, sample_zip_bytes):
    j1, j2 = _paged_pair(client, sample_zip_bytes)
    cursor = _first_page_cursor(client, [j1["id"], j2["id"]])

    # 任务校验优先于 cursor 校验
    r = client.post("/jobs/consistency-check", json={
        "job_ids": [j1["id"], "gone"], "level": "sop", "cursor": cursor,
    })
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "JOB_NOT_FOUND"
