"""口径重算引擎领域服务：根据新口径版本重算历史数据。

跨模块数据流: metric_definition -> caliber_version -> recalc_engine -> query_builder(缓存失效)
口径变更时触发重算，重算完成后失效相关缓存。

影响估算入口消费字段血缘导出物（影响评估文件）：
读取文件里的上游身份与下游引用边，把查询/接口/报告引用归到
实际要回算的指标与日期；同一指标同一天的多条路径保留明细但只算一个回算单元。
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.store import store
from app.services.base import BaseModuleService


class AssessmentError(ValueError):
    """影响评估文件不可用（格式、版本或内容不合法）。"""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class RecalcEngineService(BaseModuleService):
    MODULE = "recalc_engine"
    ENTITY = "重算任务"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["关联指标", "新口径版本", "历史时间范围", "重算状态", "产出差异"]
    REQUIRED_FIELDS = ["关联指标", "新口径版本", "历史时间范围"]
    STATUS_ORDER = ["待调度", "重算中", "已完成", "重算失败"]
    ACTION_RULES = {"启动重算": "重算中", "取消重算": "重算失败"}
    NEGATIVE_ACTIONS = []

    def start_recalc(self, entry_id: int) -> dict[str, Any] | None:
        """启动重算任务。

        跨模块数据流: meter_definition -> caliber_version -> recalc_engine
        """
        task = store.find(self.MODULE, entry_id)
        if task is None:
            return {"error": f"重算任务 {entry_id} 不存在"}

        metric_name = task.get("关联指标", "")
        new_version = task.get("新口径版本", "")
        time_range = task.get("历史时间范围", "")

        # 查找指标及其当前口径
        calibers = store.find_by_field("caliber_version", "所属指标", metric_name)
        active_caliber = next((c for c in calibers if c.get("status") == "已生效"), None)
        old_version = active_caliber.get("版本号") if active_caliber else "unknown"

        # 模拟重算 - 计算差异
        task["status"] = "重算中"
        task["pending"] = True

        diff_summary = f"口径从 {old_version} 变更为 {new_version}，覆盖时间范围 {time_range}"
        diff_percentage = 5.5  # 模拟差异百分比

        # 完成重算
        task["status"] = "已完成"
        task["pending"] = False
        task["产出差异"] = f"{diff_summary}，历史数据差异约 {diff_percentage}%"

        # 记录重算历史
        store.record_recalc(
            metric_id=0,
            old_version=old_version,
            new_version=new_version,
            time_range=time_range,
            diff_summary=task.get("产出差异", ""),
        )

        # 失效该指标所有缓存
        from app.services.cache_layer import CacheLayerService
        cache_svc = CacheLayerService()
        invalidation = cache_svc.invalidate_by_metric(metric_name)

        return {
            "task_id": entry_id,
            "metric": metric_name,
            "old_caliber": old_version,
            "new_caliber": new_version,
            "time_range": time_range,
            "diff_summary": task.get("产出差异"),
            "status": "已完成",
            "cache_invalidated": invalidation.get("invalidated_entries", 0),
        }

    def get_recalc_history(self, metric_name: str | None = None) -> list[dict[str, Any]]:
        """获取重算历史记录。"""
        history = store.recalc_history()
        if metric_name:
            history = [h for h in history if h.get("关联指标") == metric_name]
        return history

    def estimate_recalc_impact(self, entry_id: int) -> dict[str, Any] | None:
        """预估算重算影响范围。

        跨模块数据流: recalc_engine -> query_builder（预估缓存失效范围）
                     recalc_engine -> data_service（预估接口影响）
        """
        task = store.find(self.MODULE, entry_id)
        if task is None:
            return {"error": f"重算任务 {entry_id} 不存在"}

        metric_name = task.get("关联指标", "")

        # 查找关联的查询模板和服务接口
        queries = store.find_by_field("query_builder", "关联指标", metric_name)
        services = [row for row in store.rows("data_service")
                    if metric_name in str(row.get("关联指标或模型", ""))]
        subscriptions = store.find_by_field("data_subscription", "关联指标", metric_name)

        return {
            "metric": metric_name,
            "affected_query_templates": len(queries),
            "affected_service_apis": len(services),
            "affected_subscriptions": len(subscriptions),
            "estimated_cache_invalidation": len([q for q in queries if q.get("缓存策略")]),
            "recommended_action": "confirm_and_proceed",
        }

    # ── 影响评估文件试算 ──

    def ingest_assessment(
        self, content: bytes | str, *, filename: str = "",
    ) -> dict[str, Any]:
        """把一份字段血缘影响评估文件解析成可试算的回算单元。

        以文件自身的内容与版本为准：文件里记录的是哪一版依赖，就按哪一版
        核算；后续在平台上再提交同一份原文件，结果不会漂移。
        """
        assessment = self._parse_assessment(content)
        units = self._units_from_assessment(assessment)

        fingerprint = _canonical({
            "field_identity": assessment.get("field_identity"),
            "version_id": assessment.get("version_id"),
            "unit_keys": [unit["unit_key"] for unit in units],
            "upstreams": [item["identity"] for item in assessment["upstreams"]],
            "references": [ref["ref_key"] for ref in assessment["references"]],
        })
        return {
            "ok": True,
            "filename": filename,
            "artifact": assessment["artifact"],
            "format_version": assessment["format_version"],
            "field_identity": assessment["field_identity"],
            "model": assessment["model"],
            "field": assessment["field"],
            "version": assessment["version"],
            "version_id": assessment["version_id"],
            "dependency_fingerprint": fingerprint,
            "upstreams": assessment["upstreams"],
            "references": assessment["references"],
            "units": units,
            "summary": {
                "upstream_count": len(assessment["upstreams"]),
                "reference_count": len(assessment["references"]),
                "recalc_unit_count": len(units),
                "path_count": sum(len(unit["paths"]) for unit in units),
                "metric_dates": sorted({unit["metric_date"] for unit in units}),
            },
        }

    def estimate_from_assessment(
        self, content: bytes | str, *, filename: str = "",
    ) -> dict[str, Any]:
        """影响估算入口：按文件中的依赖版本完成跨模块历史数据试算。"""
        ingested = self.ingest_assessment(content, filename=filename)
        estimate_units = []
        for unit in ingested["units"]:
            estimate_units.append({
                "metric": unit["metric"],
                "metric_date": unit["metric_date"],
                "recalc_count": 1,
                "path_count": len(unit["paths"]),
                "paths": unit["paths"],
                "traceable": _unit_traceable(unit, ingested),
            })

        record = store.record_assessment_run({
            "field_identity": ingested["field_identity"],
            "version_id": ingested["version_id"],
            "filename": filename,
            "历史时间范围": " ~ ".join(ingested["summary"]["metric_dates"]),
            "回算单元数": len(estimate_units),
            "dependency_fingerprint": ingested["dependency_fingerprint"],
            "试算时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

        # 同一份原文件无论什么时候再提交，都按文件里的依赖版本核算；
        # 平台当前状态只用来提示文件是否早于最新完整版本。
        latest = _current_complete_version(ingested["model"], ingested["field"])
        stale = bool(latest and latest != ingested["version_id"])

        return {
            "ok": True,
            "filename": filename,
            "run_id": record["id"],
            "artifact": ingested["artifact"],
            "format_version": ingested["format_version"],
            "field_identity": ingested["field_identity"],
            "model": ingested["model"],
            "field": ingested["field"],
            "version": ingested["version"],
            "version_id": ingested["version_id"],
            "dependency_fingerprint": ingested["dependency_fingerprint"],
            "estimate_units": estimate_units,
            "summary": {
                **ingested["summary"],
                "recalc_unit_count": len(estimate_units),
            },
            "upstreams": ingested["upstreams"],
            "traceability": _traceability(ingested),
            "platform_latest_complete_version": latest,
            "older_than_latest": stale,
        }

    # ── 解析 ──

    def _parse_assessment(self, content: bytes | str) -> dict[str, Any]:
        if isinstance(content, bytes):
            try:
                text = content.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise AssessmentError(f"影响评估文件不是 UTF-8 编码：{exc}") from exc
        else:
            text = content
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AssessmentError(f"影响评估文件不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise AssessmentError("影响评估文件根节点必须是对象")
        if payload.get("artifact") != "field_lineage_impact_assessment":
            raise AssessmentError("文件不是字段血缘影响评估文件")
        if int(payload.get("format_version", 0) or 0) != 1:
            raise AssessmentError(
                f"不支持的影响评估文件格式版本：{payload.get('format_version')}")

        version = payload.get("version") or {}
        if not version.get("version_id"):
            raise AssessmentError("影响评估文件缺少依赖版本标识")
        if not version.get("complete", False):
            raise AssessmentError(
                f"影响评估文件中的依赖版本 {version.get('version_id')} 不是完整版本")

        field_identity = str(payload.get("field_identity") or "")
        model = str(payload.get("model") or "")
        field = str(payload.get("field") or "")
        if not model or not field:
            if "." not in field_identity:
                raise AssessmentError("影响评估文件缺少模型/字段身份")
            model, _, field = field_identity.partition(".")
        if field_identity and field_identity != f"{model}.{field}":
            raise AssessmentError("影响评估文件的字段身份与模型/字段不一致")

        upstreams = payload.get("upstreams")
        references = payload.get("references")
        if not isinstance(upstreams, list) or not isinstance(references, list):
            raise AssessmentError("影响评估文件缺少上游身份或下游引用边")

        upstream_ids = {str(item.get("upstream_id")) for item in upstreams}
        normalized_refs = []
        for ref in references:
            ref_key = str(ref.get("ref_key") or "")
            metric = str(ref.get("metric") or "")
            metric_date = str(ref.get("metric_date") or "")
            if not ref_key or not metric or not metric_date:
                raise AssessmentError("下游引用边缺少 ref_key/指标/日期")
            if ref.get("ref_type") not in ("query", "service", "report"):
                raise AssessmentError(f"未知引用类型：{ref.get('ref_type')}")
            ref_upstreams = [str(uid) for uid in ref.get("upstream_ids") or []]
            missing = [uid for uid in ref_upstreams if uid not in upstream_ids]
            if missing:
                raise AssessmentError(
                    f"引用 {ref_key} 指向未声明的上游身份：{'、'.join(missing)}")
            if not ref_upstreams:
                raise AssessmentError(f"引用 {ref_key} 没有上游身份，无法追到依赖版本")
            normalized_refs.append({**ref, "upstream_ids": ref_upstreams})

        return {
            "artifact": payload["artifact"],
            "format_version": int(payload["format_version"]),
            "model": model,
            "field": field,
            "field_identity": f"{model}.{field}",
            "version": version,
            "version_id": str(version["version_id"]),
            "upstreams": upstreams,
            "references": normalized_refs,
        }

    def _units_from_assessment(self, assessment: dict[str, Any]) -> list[dict[str, Any]]:
        """把引用边归到 (指标, 日期)：同一天多路径保留来源，回算单元只计一次。"""
        units: dict[tuple[str, str], dict[str, Any]] = {}
        for ref in assessment["references"]:
            key = (ref["metric"], ref["metric_date"])
            unit = units.setdefault(key, {
                "unit_key": f"{ref['metric']}@{ref['metric_date']}",
                "metric": ref["metric"],
                "metric_date": ref["metric_date"],
                "paths": [],
            })
            unit["paths"].append({
                "ref_key": ref["ref_key"],
                "ref_type": ref["ref_type"],
                "ref_name": ref.get("ref_name", ""),
                "metric": ref["metric"],
                "metric_date": ref["metric_date"],
                "upstream_ids": ref["upstream_ids"],
                "field_identity": assessment["field_identity"],
                "version_id": assessment["version_id"],
            })
        return sorted(units.values(), key=lambda unit: (unit["metric"], unit["metric_date"]))


def _current_complete_version(model: str, field: str) -> str | None:
    latest = store.select_lineage_version(model, field)
    return latest.get("version_id") if latest else None


def _unit_traceable(unit: dict[str, Any], ingested: dict[str, Any]) -> bool:
    declared = {str(item.get("upstream_id")) for item in ingested["upstreams"]}
    paths = unit["paths"]
    return bool(paths) and all(
        path["upstream_ids"]
        and set(path["upstream_ids"]) <= declared
        and path["version_id"] == ingested["version_id"]
        for path in paths
    )


def _traceability(ingested: dict[str, Any]) -> dict[str, Any]:
    """逐条核对每个回算指标日的路径明细能否追到文件中的依赖版本。"""
    traceable = 0
    unresolvable: list[str] = []
    for unit in ingested["units"]:
        if _unit_traceable(unit, ingested):
            traceable += 1
        else:
            unresolvable.append(unit["unit_key"])
    return {
        "checked_units": len(ingested["units"]),
        "traceable_units": traceable,
        "all_traceable": traceable == len(ingested["units"]),
        "version_id": ingested["version_id"],
        "unresolvable_units": unresolvable,
    }