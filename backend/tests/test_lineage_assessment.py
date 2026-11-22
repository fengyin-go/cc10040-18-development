"""字段血缘依赖版本 -> 影响评估文件 -> 口径重算引擎试算的行为测试。

覆盖：
- 两个模型各自导出同名字段的影响评估文件（按模型 + 字段区分身份）
- 查询已更新、接口/报告未切换时仍可领取上一完整版本
- 整轮引用就绪后领取新版，把新接入指标纳入估算
- 依赖发布与下载并发时每笔下载只交付一版完整依赖
- 同一指标同一天多路径保留来源、回算单元只计一次
- 后续变更后再次提交原文件仍按原文件版本核算
"""
from __future__ import annotations

import json
import threading

import pytest

from app.services.field_lineage import FieldLineageService, LinkageError
from app.services.recalc_engine import AssessmentError, RecalcEngineService
from app.store import store

EC = "电商交易模型"
STORE = "门店零售模型"


@pytest.fixture(autouse=True)
def reset_lineage():
    store.reset_lineage()
    yield
    store.reset_lineage()


def _assessment_bytes(model: str, field: str = "amount", **kwargs) -> bytes:
    payload = FieldLineageService().build_impact_assessment(model, field, **kwargs)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class TestFieldIdentityAndExport:
    def test_same_named_fields_are_distinct_identities(self):
        identities = {item["field_identity"]
                      for item in FieldLineageService().list_field_identities()}
        assert f"{EC}.amount" in identities
        assert f"{STORE}.amount" in identities

        ec = FieldLineageService().build_impact_assessment(EC, "amount")
        st = FieldLineageService().build_impact_assessment(STORE, "amount")
        assert ec["field_identity"] != st["field_identity"]
        assert {u["identity"] for u in ec["upstreams"]} != {u["identity"] for u in st["upstreams"]}
        assert all(ref["model"] == EC for ref in ec["references"])

    def test_export_marks_version(self):
        payload = json.loads(_assessment_bytes(EC))
        assert payload["artifact"] == "field_lineage_impact_assessment"
        assert payload["version"]["version_id"].startswith("LV-")
        assert payload["version"]["complete"] is True
        assert payload["field_identity"] == f"{EC}.amount"

    def test_export_leaves_incomplete_versions_alone(self):
        """只切了查询的一版是「切换中」，不会被当作可领取版本。"""
        svc = FieldLineageService()
        svc.switch_references(EC, "amount", ref_types=["query"],
                              metric="GMV", metric_date="2026-10-02")
        partial = svc.publish_dependency_version(EC, "amount", ref_types=["query"])
        assert partial["complete"] is False
        assert svc.build_impact_assessment(EC, "amount")["version"]["version_id"] != partial["version_id"]
        with pytest.raises(LinkageError):
            svc.build_impact_assessment(EC, "amount", version_id=partial["version_id"])


class TestReleaseRoundAndDownloads:
    def test_query_updated_but_others_not_yet(self):
        """查询已更新、接口/报告未切换时，负责人可先领取上一完整版本。"""
        svc = FieldLineageService()
        before = svc.build_impact_assessment(STORE, "amount")

        svc.switch_references(STORE, "amount", ref_types=["query"],
                              metric="门店销售额", metric_date="2026-10-02")
        svc.publish_dependency_version(STORE, "amount", ref_types=["query"], note="仅查询")

        after = svc.build_impact_assessment(STORE, "amount")
        assert after["version"]["version_id"] == before["version"]["version_id"]
        assert {ref["metric_date"] for ref in after["references"]} == {"2026-10-01"}

    def test_wait_for_full_round_includes_newly_attached_metric(self):
        """等这一轮引用全部就绪再领新版，刚接入的指标也纳入估算。"""
        svc = FieldLineageService()
        svc.attach_reference(
            STORE, "amount",
            ref_key="SUB-ST-AMT", ref_type="service", ref_name="门店销售额订阅接口",
            metric="门店销售额", metric_date="2026-10-02",
            upstream_ids=["UP-ST-01", "UP-ST-02"], ref_id=108, path_key="subscription",
        )
        svc.switch_references(STORE, "amount", ref_types=["query", "service", "report"],
                              metric="门店销售额", metric_date="2026-10-02")
        published = svc.publish_dependency_version(STORE, "amount", note="整轮就绪")
        assert published["complete"] is True

        assessment = svc.build_impact_assessment(STORE, "amount")
        assert assessment["version"]["version_id"] == published["version_id"]
        unit = assessment["recalc_units"][0]
        assert unit["unit_key"] == "门店销售额@2026-10-02"
        assert "SUB-ST-AMT" in unit["ref_keys"]
        assert unit["path_count"] == 4

    def test_partial_switch_never_becomes_complete_even_if_publish_all_types(self):
        """只切了查询时，即使按「整轮」发布，日期不一致也不能算完整版本。"""
        svc = FieldLineageService()
        before = svc.build_impact_assessment(EC, "amount")
        svc.switch_references(EC, "amount", ref_types=["query"],
                              metric="GMV", metric_date="2026-10-02")
        published = svc.publish_dependency_version(EC, "amount", note="误按整轮发布")
        assert published["complete"] is False
        assert published["status"] == "切换中"
        assert svc.build_impact_assessment(EC, "amount")["version"]["version_id"] == \
            before["version"]["version_id"]

    def test_no_complete_version_yet_refuses_download(self):
        svc = FieldLineageService()
        store.reset_lineage()
        # 清掉初始完整版本，只留切换中的半版
        store._lineage_versions.clear()
        svc.switch_references(EC, "amount", ref_types=["query"],
                              metric="GMV", metric_date="2026-10-02")
        svc.publish_dependency_version(EC, "amount", ref_types=["query"], note="仅查询")
        with pytest.raises(LinkageError):
            svc.build_impact_assessment(EC, "amount")

    def test_concurrent_publish_and_download_deliver_one_complete_version(self):
        """发布与下载并发时，每笔下载拿到的都是某一完整版本，且标明版本。"""
        svc = FieldLineageService()
        results: list[dict] = []
        errors: list[str] = []
        barrier = threading.Barrier(6)

        def publisher(index: int):
            barrier.wait()
            for round_no in range(6):
                date = f"2026-10-{index + 2:02d}"
                for ref_type in ("query", "service", "report"):
                    svc.switch_references(EC, "amount", ref_types=[ref_type],
                                          metric="GMV", metric_date=date)
                svc.publish_dependency_version(EC, "amount", note=f"publisher-{index}-{round_no}")

        def downloader(worker: int):
            barrier.wait()
            for _ in range(12):
                try:
                    payload = svc.build_impact_assessment(EC, "amount")
                except LinkageError as exc:  # 允许短暂无完整版本
                    errors.append(str(exc))
                    continue
                payload["_worker"] = worker
                results.append(payload)

        threads = [threading.Thread(target=publisher, args=(i,)) for i in range(3)]
        threads += [threading.Thread(target=downloader, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(results) >= 20
        assert not errors
        for payload in results:
            assert payload["version"]["complete"] is True
            assert payload["version"]["version_id"].startswith("LV-")
            ref_types = {ref["ref_type"] for ref in payload["references"]}
            assert ref_types == {"query", "service", "report"}
            # 文件内容必须与留档的那一版逐字一致，说明拿到的是一整版而不是拼装。
            stored = store.lineage_version(payload["version"]["version_id"])
            assert stored is not None
            assert [ref["ref_key"] for ref in stored["downstream"]] == \
                [ref["ref_key"] for ref in payload["references"]]
            assert [(ref["ref_key"], ref["metric_date"]) for ref in stored["downstream"]] == \
                [(ref["ref_key"], ref["metric_date"]) for ref in payload["references"]]
        # 至少观察到两个不同版本，说明确实发生在发布过程中
        assert len({payload["version"]["version_id"] for payload in results}) >= 2


class TestRecalcEstimate:
    def test_multi_path_same_day_keeps_sources_but_one_unit(self):
        svc = FieldLineageService()
        payload = svc.build_impact_assessment(EC, "amount")
        result = RecalcEngineService().estimate_from_assessment(
            json.dumps(payload, ensure_ascii=False))

        assert result["version_id"] == payload["version"]["version_id"]
        assert result["summary"]["recalc_unit_count"] == 1
        assert result["summary"]["path_count"] == len(payload["references"])
        unit = result["estimate_units"][0]
        assert unit["recalc_count"] == 1
        path_keys = {path["ref_key"] for path in unit["paths"]}
        assert "API-EC-GMV-ALT" in path_keys
        assert "QRY-EC-GMV" in path_keys
        assert unit["traceable"] is True
        assert result["traceability"]["all_traceable"] is True

    def test_each_model_estimates_its_own_scope(self):
        engine = RecalcEngineService()
        ec = engine.estimate_from_assessment(_assessment_bytes(EC))
        st = engine.estimate_from_assessment(_assessment_bytes(STORE))
        assert ec["field_identity"] != st["field_identity"]
        assert {u["metric"] for u in ec["estimate_units"]} == {"GMV"}
        assert {u["metric"] for u in st["estimate_units"]} == {"门店销售额"}
        upstream_ids = {item["upstream_id"] for item in ec["upstreams"]}
        assert upstream_ids == {"UP-EC-01", "UP-EC-02", "UP-EXT-01"}

    def test_resubmitting_original_file_keeps_original_version(self):
        """后续变更后再次提交最早的原文件，仍按原文件里的依赖核算。"""
        svc = FieldLineageService()
        engine = RecalcEngineService()
        original = _assessment_bytes(STORE)
        first = engine.estimate_from_assessment(original)

        svc.switch_references(STORE, "amount", ref_types=["query", "service", "report"],
                              metric="门店销售额", metric_date="2026-10-05")
        recent = svc.publish_dependency_version(STORE, "amount", note="后续变更")
        newer_download = svc.build_impact_assessment(STORE, "amount")
        assert newer_download["version"]["version_id"] == recent["version_id"]

        again = engine.estimate_from_assessment(original)
        assert again["version_id"] == first["version_id"]
        assert again["dependency_fingerprint"] == first["dependency_fingerprint"]
        assert [u["metric_date"] for u in again["estimate_units"]] == ["2026-10-01"]
        assert again["platform_latest_complete_version"] == recent["version_id"]
        assert again["older_than_latest"] is True

    def test_acceptance_flow_three_files_three_versions(self):
        """验收路径：两个模型各领一份；一个模型查询先切、整轮完成各领一次；
        三份文件分别提交试算，逐单元核对能否追到文件里的依赖版本。"""
        svc = FieldLineageService()
        engine = RecalcEngineService()

        ec = svc.build_impact_assessment(EC, "amount")
        store_before = svc.build_impact_assessment(STORE, "amount")
        assert ec["version"]["version_id"] != store_before["version"]["version_id"]

        # 把门店模型的查询/接口/报告切到另一个指标日期
        svc.switch_references(STORE, "amount", ref_types=["query"],
                              metric="门店销售额", metric_date="2026-10-07")
        during_query_only = svc.build_impact_assessment(STORE, "amount")
        assert during_query_only["version"]["version_id"] == \
            store_before["version"]["version_id"]

        svc.switch_references(STORE, "amount", ref_types=["service", "report"],
                              metric="门店销售额", metric_date="2026-10-07")
        completed = svc.publish_dependency_version(STORE, "amount", note="整轮完成")
        after_release = svc.build_impact_assessment(STORE, "amount")
        assert after_release["version"]["version_id"] == completed["version_id"]

        files = {
            "ec-original": json.dumps(ec, ensure_ascii=False).encode(),
            "store-query-only": json.dumps(during_query_only, ensure_ascii=False).encode(),
            "store-after-release": json.dumps(after_release, ensure_ascii=False).encode(),
        }
        results = {name: engine.estimate_from_assessment(blob, filename=name)
                   for name, blob in files.items()}

        assert results["ec-original"]["version_id"] == ec["version"]["version_id"]
        assert results["store-query-only"]["version_id"] == \
            store_before["version"]["version_id"]
        assert results["store-after-release"]["version_id"] == completed["version_id"]

        for name, result in results.items():
            assert result["traceability"]["all_traceable"] is True, name
            for unit in result["estimate_units"]:
                assert unit["recalc_count"] == 1
                assert unit["traceable"] is True
                for path in unit["paths"]:
                    assert path["version_id"] == result["version_id"]
                    declared = {item["upstream_id"] for item in result["upstreams"]}
                    assert set(path["upstream_ids"]) <= declared

        assert results["store-query-only"]["estimate_units"][0]["metric_date"] == "2026-10-01"
        assert results["store-after-release"]["estimate_units"][0]["metric_date"] == "2026-10-07"

    def test_rejects_tampered_or_incomplete_assessment(self):
        engine = RecalcEngineService()
        payload = json.loads(_assessment_bytes(EC))
        payload["version"]["complete"] = False
        with pytest.raises(AssessmentError):
            engine.estimate_from_assessment(json.dumps(payload, ensure_ascii=False))

        payload = json.loads(_assessment_bytes(EC))
        payload["references"][0]["upstream_ids"] = ["UP-NOT-DECLARED"]
        with pytest.raises(AssessmentError):
            engine.estimate_from_assessment(json.dumps(payload, ensure_ascii=False))

        with pytest.raises(AssessmentError):
            engine.estimate_from_assessment(b"not-json")

    def test_illegal_reference_type_and_bad_encoding_rejected(self):
        engine = RecalcEngineService()
        payload = json.loads(_assessment_bytes(EC))
        payload["references"][0]["ref_type"] = "dashboard"
        with pytest.raises(AssessmentError):
            engine.estimate_from_assessment(json.dumps(payload, ensure_ascii=False))

        payload = json.loads(_assessment_bytes(EC))
        del payload["version"]["version_id"]
        with pytest.raises(AssessmentError):
            engine.estimate_from_assessment(json.dumps(payload, ensure_ascii=False))

        with pytest.raises(AssessmentError):
            engine.estimate_from_assessment(_assessment_bytes(EC).decode("utf-8").encode("gb18030"))
