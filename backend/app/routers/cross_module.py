"""跨模块数据流API路由：暴露跨模块数据流链路。

链1: metric_definition -> caliber_version -> query_builder -> data_service -> cache_layer -> query_journal
链2: dimension_model -> drill_down -> pivot_table -> report_template
链3: user_cohort -> experiment_group -> segment_compare -> anomaly_detector
字段血缘 -> 口径重算引擎: 依赖发布 -> 影响评估文件 -> 历史数据试算
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, unquote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from app.services.metric_definition import MetricDefinitionService
from app.services.caliber_version import CaliberVersionService
from app.services.query_builder import QueryBuilderService
from app.services.data_service import DataServiceService
from app.services.cache_layer import CacheLayerService
from app.services.query_journal import QueryJournalService
from app.services.dimension_model import DimensionModelService
from app.services.drill_down import DrillDownService
from app.services.pivot_table import PivotTableService
from app.services.report_template import ReportTemplateService
from app.services.user_cohort import UserCohortService
from app.services.experiment_group import ExperimentGroupService
from app.services.segment_compare import SegmentCompareService
from app.services.anomaly_detector import AnomalyDetectorService
from app.services.recalc_engine import (
    AssessmentError,
    RecalcEngineService,
)
from app.services.field_lineage import FieldLineageService, LinkageError
from app.store import store

router = APIRouter(prefix="/api/cross", tags=["跨模块数据流"])


# ── 链1: 指标口径查询链 ──

@router.get("/metric-chain/{metric_id}")
def get_metric_chain(metric_id: int):
    """获取指标完整数据链：指标 -> 口径 -> 查询模板 -> 数据服务 -> 缓存 -> 日志"""
    svc = MetricDefinitionService()
    return svc.get_metric_chain(metric_id)


@router.post("/metric/{metric_id}/publish-with-caliber")
def publish_with_caliber(metric_id: int, caliber_desc: str = "默认计算口径"):
    """发布指标并自动创建口径版本"""
    svc = MetricDefinitionService()
    result, msg = svc.publish_with_caliber(metric_id, caliber_desc)
    return {"ok": result is not None, "message": msg, "entry": result}


@router.post("/caliber/{version_id}/activate")
def activate_caliber(version_id: int):
    """启用口径版本，自动过期旧版本并失效缓存"""
    svc = CaliberVersionService()
    result, msg = svc.activate_version(version_id)
    return {"ok": result is not None, "message": msg, "entry": result}


@router.get("/caliber/{metric_name}/history")
def get_caliber_history(metric_name: str):
    """获取指标的口径版本历史"""
    svc = CaliberVersionService()
    return svc.get_version_history(metric_name)


@router.post("/query/{template_id}/execute")
def execute_query(template_id: int, operator: str = "系统"):
    """执行查询模板，返回结果与日志"""
    svc = QueryBuilderService()
    return svc.execute_query(template_id, operator=operator)


@router.post("/data-service/{api_id}/call")
def call_data_service(api_id: int, operator: str = "客户端"):
    """调用数据服务API"""
    svc = DataServiceService()
    return svc.call_service(api_id, operator=operator)


@router.get("/data-service/{api_id}/health")
def service_health(api_id: int):
    """获取服务接口健康状态"""
    svc = DataServiceService()
    return svc.get_service_health(api_id)


@router.post("/cache/invalidate-by-metric/{metric_name}")
def invalidate_cache_by_metric(metric_name: str):
    """根据指标名称失效缓存"""
    svc = CacheLayerService()
    return svc.invalidate_by_metric(metric_name)


@router.post("/cache/warm/{metric_name}")
def warm_cache(metric_name: str):
    """预热指标查询缓存"""
    svc = CacheLayerService()
    return svc.warm_cache(metric_name)


@router.get("/cache/stats")
def cache_stats():
    """获取缓存统计"""
    svc = CacheLayerService()
    return svc.get_cache_stats()


@router.get("/query-journal/slow-queries")
def slow_queries(threshold_ms: float = 100):
    """获取慢查询列表"""
    svc = QueryJournalService()
    return svc.get_slow_queries(threshold_ms)


@router.get("/query-journal/stats-by-metric")
def query_stats_by_metric():
    """按指标统计查询情况"""
    svc = QueryJournalService()
    return svc.get_query_stats_by_metric()


@router.get("/query-journal/performance-insights")
def performance_insights():
    """获取性能洞察"""
    svc = QueryJournalService()
    return svc.get_performance_insights()


# ── 链2: 维度分析下钻链 ──

@router.get("/dimension-model/{model_id}/with-analytics")
def get_dimension_model_with_analytics(model_id: int):
    """获取维度模型及其关联分析配置"""
    svc = DimensionModelService()
    return svc.get_model_with_analytics(model_id)


@router.get("/dimension-model/{model_id}/hierarchy")
def get_dimension_hierarchy(model_id: int):
    """获取维度层次结构"""
    svc = DimensionModelService()
    return svc.get_dimension_hierarchy(model_id)


@router.post("/drill-down/{path_id}/execute")
def execute_drill_down(path_id: int, current_level: int = 0):
    """执行下钻分析"""
    svc = DrillDownService()
    return svc.execute_drill_down(path_id, current_level)


@router.get("/drill-down/{path_id}/tree")
def get_drill_tree(path_id: int):
    """获取下钻路径树"""
    svc = DrillDownService()
    return svc.get_drill_tree(path_id)


@router.get("/drill-down/{path_id}/export-to-pivot")
def export_drill_to_pivot(path_id: int):
    """将下钻路径导出为透视表配置建议"""
    svc = DrillDownService()
    return svc.export_to_pivot(path_id)


@router.post("/pivot-table/{config_id}/compute")
def compute_pivot(config_id: int):
    """计算透视表"""
    svc = PivotTableService()
    return svc.compute_pivot(config_id)


@router.post("/pivot-table/{config_id}/share-to-report")
def share_pivot_to_report(config_id: int, report_template_id: int):
    """将透视表共享到报告模板"""
    svc = PivotTableService()
    return svc.share_to_report(config_id, report_template_id)


@router.get("/report-template/{template_id}/generate")
def generate_report(template_id: int):
    """生成报告"""
    svc = ReportTemplateService()
    return svc.generate_report(template_id)


# ── 链3: 分群对比异常链 ──

@router.post("/user-cohort/{cohort_id}/recompute")
def recompute_cohort(cohort_id: int):
    """重新计算分群"""
    svc = UserCohortService()
    return svc.recompute_cohort(cohort_id)


@router.get("/user-cohort/{cohort_id}/detail")
def get_cohort_detail(cohort_id: int):
    """获取分群详细信息"""
    svc = UserCohortService()
    return svc.get_cohort_detail(cohort_id)


@router.get("/experiment-group/{group_id}/evaluate")
def evaluate_experiment_groups(group_a_id: int, group_b_id: int):
    """评估两个实验分组"""
    svc = ExperimentGroupService()
    return svc.evaluate_groups(group_a_id, group_b_id)


@router.post("/segment-compare/{task_id}/execute")
def execute_segment_compare(task_id: int):
    """执行分群对比分析"""
    svc = SegmentCompareService()
    return svc.execute_compare(task_id)


@router.get("/segment-compare/{task_id}/insights")
def segment_compare_insights(task_id: int):
    """获取跨分群洞察"""
    svc = SegmentCompareService()
    return svc.get_cross_segment_insights(task_id)


@router.post("/anomaly-detector/{rule_id}/detect")
def run_anomaly_detection(rule_id: int):
    """执行异常检测"""
    svc = AnomalyDetectorService()
    return svc.run_detection(rule_id)


@router.get("/anomaly-detector/report")
def anomaly_report(metric_name: str | None = None):
    """获取异常报告"""
    svc = AnomalyDetectorService()
    return svc.get_anomaly_report(metric_name)


@router.get("/anomaly-detector/coverage")
def detection_coverage():
    """获取异常检测覆盖率"""
    svc = AnomalyDetectorService()
    return svc.get_detection_coverage()


# ── 重算引擎 ──

@router.post("/recalc/{task_id}/start")
def start_recalc(task_id: int):
    """启动口径重算"""
    svc = RecalcEngineService()
    return svc.start_recalc(task_id)


@router.get("/recalc/history")
def recalc_history(metric_name: str | None = None):
    """获取重算历史"""
    svc = RecalcEngineService()
    return svc.get_recalc_history(metric_name)


@router.get("/recalc/{task_id}/estimate-impact")
def estimate_recalc_impact(task_id: int):
    """预估算重算影响"""
    svc = RecalcEngineService()
    return svc.estimate_recalc_impact(task_id)


@router.post("/metric/{metric_id}/recalc")
def recalc_metric(metric_id: int, new_caliber_version: str, time_range: str):
    """触发指标历史数据重算"""
    svc = MetricDefinitionService()
    result, msg = svc.recalc_metric_history(metric_id, new_caliber_version, time_range)
    return {"ok": result is not None, "message": msg, "entry": result}


# ── 字段血缘 ──

@router.get("/field-lineage/{field_name}/trace")
def trace_field(field_name: str):
    """追溯字段血缘"""
    svc = FieldLineageService()
    return svc.trace_field(field_name)


@router.get("/field-lineage/{field_name}/downstream-impact")
def field_downstream_impact(field_name: str):
    """分析字段变更下游影响"""
    svc = FieldLineageService()
    return svc.get_downstream_impact(field_name)


# ── 字段血缘依赖发布 / 影响评估导出 ──

@router.get("/field-lineage/identities")
def list_field_identities():
    """列出可导出影响评估的字段身份（模型 + 字段）。"""
    return {"items": FieldLineageService().list_field_identities()}


@router.get("/field-lineage/{model}/{field}/versions")
def list_lineage_versions(model: str, field: str):
    """列出某个字段身份的依赖版本链。"""
    svc = FieldLineageService()
    try:
        svc.require_identity(model, field)
    except LinkageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"field_identity": f"{model}.{field}",
            "items": svc.list_versions(model, field)}


@router.post("/field-lineage/{model}/{field}/switch-references")
def switch_lineage_references(
    model: str,
    field: str,
    ref_types: str = Query(description="逗号分隔：query,service,report"),
    metric: str = Query(),
    metric_date: str = Query(),
    commit: bool = Query(default=True),
):
    """把查询/接口/报告的引用切到新的指标日期。

    commit=false 只登记待发布状态，等这一轮引用全部就绪后再一起发布。
    """
    svc = FieldLineageService()
    try:
        result = svc.switch_references(
            model, field,
            ref_types=[item.strip() for item in ref_types.split(",") if item.strip()],
            metric=metric,
            metric_date=metric_date,
            commit=commit,
        )
    except LinkageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "message": "引用切换已登记" if commit else "引用切换已暂存",
            "entry": result}


@router.post("/field-lineage/{model}/{field}/references")
def attach_lineage_reference(
    model: str,
    field: str,
    ref_key: str = Query(),
    ref_type: str = Query(description="query/service/report"),
    ref_name: str = Query(default=""),
    metric: str = Query(),
    metric_date: str = Query(),
    upstream_ids: str = Query(description="逗号分隔的上游身份 ID"),
    ref_id: int = Query(default=0),
    path_key: str = Query(default="direct"),
):
    """接入一条新的下游引用（例如这一轮才接入的指标）。"""
    svc = FieldLineageService()
    try:
        result = svc.attach_reference(
            model, field,
            ref_key=ref_key,
            ref_type=ref_type,
            ref_name=ref_name,
            metric=metric,
            metric_date=metric_date,
            upstream_ids=[item.strip() for item in upstream_ids.split(",") if item.strip()],
            ref_id=ref_id,
            path_key=path_key,
        )
    except LinkageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "message": "下游引用已接入", "entry": result}


@router.post("/field-lineage/{model}/{field}/publish")
def publish_lineage_version(
    model: str,
    field: str,
    ref_types: str | None = Query(default=None, description="留空表示整轮引用就绪"),
    note: str = Query(default=""),
):
    """发布一版字段依赖（整轮就绪才算完整版本）。"""
    svc = FieldLineageService()
    try:
        result = svc.publish_dependency_version(
            model, field,
            ref_types=([item.strip() for item in ref_types.split(",") if item.strip()]
                       if ref_types else None),
            note=note,
        )
    except LinkageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "message": f"已发布依赖版本 {result['version_id']}", "entry": result}


@router.get("/field-lineage/{model}/{field}/impact-assessment")
def download_impact_assessment(
    model: str, field: str,
    version_id: str | None = Query(default=None),
):
    """导出字段血缘影响评估文件（只交付一版完整依赖，版本写在文件里）。"""
    svc = FieldLineageService()
    try:
        assessment = svc.build_impact_assessment(model, field, version_id=version_id)
    except LinkageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    body = json.dumps(assessment, ensure_ascii=False, indent=2).encode("utf-8")
    identity = assessment["field_identity"]
    version_id = assessment["version"]["version_id"]
    ascii_name = f"impact-assessment-{version_id}.json"
    utf8_name = quote(f"影响评估_{identity}_{version_id}.json")
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition":
                f"attachment; filename=\"{ascii_name}\"; "
                f"filename*=UTF-8''{utf8_name}",
            "X-Assessment-Version": version_id,
            "X-Field-Identity": quote(identity),
        },
    )


# ── 影响评估文件试算（口径重算引擎） ──

@router.post("/recalc/impact-assessment/inspect")
async def inspect_impact_assessment(request: Request):
    """解析影响评估文件内容，返回回算单元与路径明细（不上报历史记录）。"""
    raw = await request.body()
    try:
        return RecalcEngineService().ingest_assessment(raw)
    except AssessmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/recalc/impact-assessment/estimate")
async def estimate_impact_assessment(request: Request):
    """按影响评估文件的依赖版本执行跨模块历史数据试算。"""
    raw = await request.body()
    filename = unquote(request.headers.get("X-Filename", ""))
    try:
        return RecalcEngineService().estimate_from_assessment(raw, filename=filename)
    except AssessmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/recalc/impact-assessments")
def list_assessment_runs():
    """影响评估试算的受理记录（按文件里的依赖版本留档）。"""
    runs = store.assessment_runs()
    return {"items": runs, "total": len(runs)}


# ── 全链路快照 ──

@router.get("/full-chain-snapshot")
def full_chain_snapshot():
    """获取所有跨模块链路的状态快照"""
    return {
        "chain_1_metric_query": {
            "description": "指标口径查询链: metric_definition -> caliber_version -> query_builder -> data_service -> cache_layer -> query_journal",
            "modules": {
                "metric_definition": MetricDefinitionService().domain_snapshot(),
                "caliber_version": CaliberVersionService().domain_snapshot(),
                "query_builder": QueryBuilderService().domain_snapshot(),
                "data_service": DataServiceService().domain_snapshot(),
                "cache_layer": CacheLayerService().domain_snapshot(),
                "query_journal": QueryJournalService().domain_snapshot(),
            },
        },
        "chain_2_dimension_analysis": {
            "description": "维度分析下钻链: dimension_model -> drill_down -> pivot_table -> report_template",
            "modules": {
                "dimension_model": DimensionModelService().domain_snapshot(),
                "drill_down": DrillDownService().domain_snapshot(),
                "pivot_table": PivotTableService().domain_snapshot(),
                "report_template": ReportTemplateService().domain_snapshot(),
            },
        },
        "chain_3_cohort_anomaly": {
            "description": "分群对比异常链: user_cohort -> experiment_group -> segment_compare -> anomaly_detector",
            "modules": {
                "user_cohort": UserCohortService().domain_snapshot(),
                "experiment_group": ExperimentGroupService().domain_snapshot(),
                "segment_compare": SegmentCompareService().domain_snapshot(),
                "anomaly_detector": AnomalyDetectorService().domain_snapshot(),
            },
        },
        "engines": {
            "recalc_engine": RecalcEngineService().domain_snapshot(),
            "field_lineage": FieldLineageService().domain_snapshot(),
        },
    }