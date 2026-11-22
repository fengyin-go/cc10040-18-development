"""字段血缘领域服务：追踪字段的来源、转换逻辑和下游影响。

跨模块数据流: metric_definition -> field_lineage（追踪指标字段来源）
             caliber_version -> field_lineage（口径变更影响分析）

字段级依赖发布：把某个模型字段在某一时刻的上游身份和下游引用边冻结成一版
依赖，供离线的影响评估文件使用；同名字段以「模型 + 字段」作为字段身份。
"""
from __future__ import annotations

import time
from typing import Any

from app.store import store
from app.services.base import BaseModuleService

# 下游引用类型：查询、接口、报告
REF_TYPES = ("query", "service", "report")
REF_TYPE_NAMES = {"query": "查询", "service": "接口", "report": "报告"}
ASSESSMENT_ARTIFACT = "field_lineage_impact_assessment"
ASSESSMENT_FORMAT_VERSION = 1


class LinkageError(ValueError):
    """血缘版本不可用（没有完整版本、字段身份不存在等）。"""


class FieldLineageService(BaseModuleService):
    MODULE = "field_lineage"
    ENTITY = "字段血缘"
    OPERATION_TERM = "转换"
    LIST_FIELDS = ["目标字段", "来源字段列表", "转换逻辑", "中间表", "血缘深度"]
    REQUIRED_FIELDS = ["目标字段", "来源字段列表", "转换逻辑"]
    STATUS_ORDER = ["已建立", "待验证", "已断裂"]
    ACTION_RULES = {"刷新血缘": "已建立", "标记断裂": "已断裂"}
    NEGATIVE_ACTIONS = []

    def trace_field(self, field_name: str) -> dict[str, Any]:
        """追溯字段的完整血缘链路。

        跨模块数据流: field_lineage -> metric_definition（关联指标信息）
        """
        lineage_data = store.get_field_lineage(field_name)
        if lineage_data is None:
            return {"field": field_name, "lineage": [], "depth": 0}

        # 关联指标信息
        metric_relations = []
        for m in store.rows("metric_definition"):
            if field_name in str(m.get("计算口径", "")) or field_name in str(m.get("指标名称", "")):
                metric_relations.append({
                    "metric_name": m.get("指标名称"),
                    "metric_id": m.get("id"),
                    "status": m.get("status"),
                })

        lineage_data["related_metrics"] = metric_relations
        lineage_data["related_metric_count"] = len(metric_relations)
        return lineage_data

    def get_downstream_impact(self, field_name: str) -> dict[str, Any]:
        """分析字段变更对下游的影响。

        跨模块数据流: field_lineage -> query_builder, data_service, report_template
        """
        lineage_data = store.get_field_lineage(field_name)

        # 查找下游依赖
        affected_queries = []
        affected_services = []
        affected_reports = []

        for tmpl in store.rows("query_builder"):
            if field_name in str(tmpl.get("关联指标", "")) or field_name in str(tmpl.get("维度组合", "")):
                affected_queries.append(tmpl.get("模板名称"))

        for api in store.rows("data_service"):
            if field_name in str(api.get("关联指标或模型", "")):
                affected_services.append(api.get("接口名称"))

        for report in store.rows("report_template"):
            if field_name in str(report.get("包含指标集", "")):
                affected_reports.append(report.get("模板名称"))

        return {
            "field": field_name,
            "lineage_depth": lineage_data.get("depth", 0) if lineage_data else 0,
            "downstream_queries": affected_queries,
            "affected_queries_count": len(affected_queries),
            "downstream_services": affected_services,
            "affected_services_count": len(affected_services),
            "downstream_reports": affected_reports,
            "affected_reports_count": len(affected_reports),
            "total_downstream_impact": len(affected_queries) + len(affected_services) + len(affected_reports),
        }

    # ── 字段依赖版本 ──

    def list_field_identities(self) -> list[dict[str, Any]]:
        """列出所有可导出影响评估的 (模型, 字段) 身份及其版本情况。"""
        seen: dict[tuple[str, str], dict[str, Any]] = {}
        for ref in store.lineage_references():
            key = (str(ref.get("model", "")), str(ref.get("field", "")))
            item = seen.setdefault(key, {
                "model": key[0],
                "field": key[1],
                "field_identity": f"{key[0]}.{key[1]}",
                "reference_types": [],
                "metrics": [],
                "metric_dates": [],
            })
            ref_type = str(ref.get("ref_type", ""))
            if ref_type not in item["reference_types"]:
                item["reference_types"].append(ref_type)
            metric = str(ref.get("metric", ""))
            if metric and metric not in item["metrics"]:
                item["metrics"].append(metric)
            metric_date = str(ref.get("metric_date", ""))
            if metric_date and metric_date not in item["metric_dates"]:
                item["metric_dates"].append(metric_date)

        identities = []
        for (model, field), item in sorted(seen.items()):
            versions = store.lineage_versions(model, field)
            complete = [v for v in versions if v.get("complete")]
            item["reference_types"].sort()
            item["version_count"] = len(versions)
            item["latest_version"] = _version_brief(versions[0]) if versions else None
            item["latest_complete_version"] = (
                _version_brief(complete[0]) if complete else None)
            identities.append(item)
        return identities

    def list_versions(self, model: str, field: str) -> list[dict[str, Any]]:
        return [_version_brief(version, detailed=True)
                for version in store.lineage_versions(model, field)]

    def attach_reference(
        self,
        model: str,
        field: str,
        *,
        ref_key: str,
        ref_type: str,
        ref_name: str,
        metric: str,
        metric_date: str,
        upstream_ids: list[str],
        ref_id: int = 0,
        path_key: str = "direct",
    ) -> dict[str, Any]:
        """接入一条新的下游引用（例如这一轮才接入的指标）。"""
        self.require_identity(model, field)
        if ref_type not in REF_TYPES:
            raise LinkageError(f"未知引用类型：{ref_type}")
        known = {item["upstream_id"] for item in store.lineage_upstreams(model, field)}
        missing = [uid for uid in upstream_ids if uid not in known]
        if missing:
            raise LinkageError(f"未登记的上游身份：{'、'.join(missing)}")
        return store.add_lineage_reference({
            "ref_key": ref_key,
            "ref_type": ref_type,
            "ref_id": ref_id,
            "ref_name": ref_name,
            "model": model,
            "field": field,
            "metric": metric,
            "metric_date": metric_date,
            "upstream_ids": list(upstream_ids),
            "path_key": path_key,
        })

    def switch_references(
        self,
        model: str,
        field: str,
        *,
        ref_types: list[str],
        metric: str,
        metric_date: str,
        commit: bool = True,
    ) -> dict[str, Any]:
        """把某一批下游引用（查询/接口/报告）切到新的指标日期。

        commit=False 时只登记待发布状态，等这一轮引用全部就绪后再一起发布。
        """
        self.require_identity(model, field)
        unknown = [ref_type for ref_type in ref_types if ref_type not in REF_TYPES]
        if unknown:
            raise LinkageError(f"未知引用类型：{'、'.join(unknown)}")
        if not metric.strip():
            raise LinkageError("切换引用必须指定指标")
        changed = store.switch_lineage_references(
            model, field,
            ref_types=list(ref_types),
            metric=metric,
            metric_date=metric_date,
            commit=commit,
        )
        return {
            "field_identity": f"{model}.{field}",
            "ref_types": list(ref_types),
            "metric": metric,
            "metric_date": metric_date,
            "committed": commit,
            "changed_references": changed,
            "pending_switches": store.pending_switches(model, field),
        }

    def publish_dependency_version(
        self,
        model: str,
        field: str,
        *,
        ref_types: list[str] | None = None,
        metric_dates: dict[str, str] | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """发布一版字段依赖。

        ref_types=None 时把当前全部下游引用冻结成完整版本；
        只传部分类型时生成「切换中」版本，下载会继续使用上一完整版本。
        """
        self.require_identity(model, field)
        if ref_types is not None:
            unknown = [ref_type for ref_type in ref_types if ref_type not in REF_TYPES]
            if unknown:
                raise LinkageError(f"未知引用类型：{'、'.join(unknown)}")
        version = store.publish_current_lineage(
            model,
            field,
            ref_types=ref_types,
            metric_dates=metric_dates,
            note=note,
        )
        return _version_brief(version, detailed=True)

    def build_impact_assessment(
        self,
        model: str,
        field: str,
        *,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        """生成离线影响评估文件的内容。

        只交付一版完整依赖；指定了版本时按指定版本交付，绝不把切换中的
        半版引用打包。未指定时取「最新一版已完整的依赖」——查询已切、接口/
        报告尚未切换期间，负责人领取到的仍是上一完整版本的原范围。
        """
        self.require_identity(model, field)
        snapshot = store.select_lineage_version(model, field, version_id)
        if snapshot is None:
            if version_id:
                raise LinkageError(
                    f"依赖版本 {version_id} 不存在或不是完整版本，不能导出")
            raise LinkageError(
                f"字段 {model}.{field} 还没有完整依赖版本，请等本轮引用全部就绪后再领取")

        upstreams = snapshot.get("upstream", [])
        references = snapshot.get("downstream", [])
        units = _units_from_references(references)
        metric_dates = sorted({unit["metric_date"] for unit in units if unit["metric_date"]})

        return {
            "artifact": ASSESSMENT_ARTIFACT,
            "format_version": ASSESSMENT_FORMAT_VERSION,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "field_identity": snapshot.get("field_identity", f"{model}.{field}"),
            "model": model,
            "field": field,
            "version": {
                "version_id": snapshot.get("version_id"),
                "version_no": snapshot.get("version_no"),
                "status": snapshot.get("status"),
                "complete": bool(snapshot.get("complete")),
                "published_at": snapshot.get("published_at"),
                "note": snapshot.get("note", ""),
            },
            "upstreams": [_upstream_entry(item) for item in upstreams],
            "references": [_reference_entry(item) for item in references],
            "recalc_units": units,
            "summary": {
                "upstream_count": len(upstreams),
                "reference_count": len(references),
                "recalc_unit_count": len(units),
                "path_count": len(references),
                "metric_dates": metric_dates,
            },
        }

    def require_identity(self, model: str, field: str) -> None:
        """校验字段身份存在（模型 + 字段），不存在时抛 LinkageError。"""
        known = {
            (str(ref.get("model", "")), str(ref.get("field", "")))
            for ref in store.lineage_references()
        }
        if (model, field) not in known:
            raise LinkageError(f"没有登记字段身份 {model}.{field}")


def _version_brief(version: dict[str, Any], *, detailed: bool = False) -> dict[str, Any]:
    brief = {
        "version_id": version.get("version_id"),
        "version_no": version.get("version_no"),
        "model": version.get("model"),
        "field": version.get("field"),
        "field_identity": version.get("field_identity"),
        "status": version.get("status"),
        "complete": bool(version.get("complete")),
        "published_at": version.get("published_at"),
        "note": version.get("note", ""),
        "ref_types": list(version.get("ref_types") or []),
        "upstream_count": len(version.get("upstream", [])),
        "reference_count": len(version.get("downstream", [])),
        "recalc_unit_count": len(_units_from_references(version.get("downstream", []))),
    }
    if detailed:
        brief["upstreams"] = [_upstream_entry(item) for item in version.get("upstream", [])]
        brief["references"] = [_reference_entry(item) for item in version.get("downstream", [])]
        brief["recalc_units"] = _units_from_references(version.get("downstream", []))
    return brief


def _upstream_entry(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "upstream_id": item.get("upstream_id"),
        "identity": item.get("identity"),
        "source_type": item.get("source_type"),
        "table": item.get("table"),
        "field": item.get("field"),
        "model": item.get("model"),
        "transform": item.get("transform", ""),
    }


def _reference_entry(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref_key": item.get("ref_key"),
        "ref_type": item.get("ref_type"),
        "ref_type_name": REF_TYPE_NAMES.get(str(item.get("ref_type")), ""),
        "ref_id": item.get("ref_id"),
        "ref_name": item.get("ref_name"),
        "model": item.get("model"),
        "field": item.get("field"),
        "metric": item.get("metric"),
        "metric_date": item.get("metric_date"),
        "upstream_ids": list(item.get("upstream_ids") or []),
        "path_key": item.get("path_key", ""),
    }


def _units_from_references(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 (指标, 日期) 归并回算单元，同一单元的每条路径都保留。"""
    units: dict[tuple[str, str], dict[str, Any]] = {}
    for ref in references:
        metric = str(ref.get("metric", ""))
        metric_date = str(ref.get("metric_date", ""))
        unit = units.setdefault((metric, metric_date), {
            "unit_key": f"{metric}@{metric_date}",
            "metric": metric,
            "metric_date": metric_date,
            "ref_keys": [],
            "paths": [],
        })
        ref_key = ref.get("ref_key")
        if ref_key in unit["ref_keys"]:
            continue
        unit["ref_keys"].append(ref_key)
        unit["paths"].append({
            "ref_key": ref_key,
            "ref_type": ref.get("ref_type"),
            "ref_name": ref.get("ref_name", ""),
            "path_key": ref.get("path_key", ""),
            "upstream_ids": list(ref.get("upstream_ids") or []),
        })
    for unit in units.values():
        unit["path_count"] = len(unit["paths"])
    return list(units.values())
