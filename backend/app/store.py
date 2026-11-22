"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
同时提供跨模块查询和数据流处理能力。
"""
from __future__ import annotations

import copy
import time
import threading
from typing import Any

from app.seed import (
    LINEAGE_REFERENCE_SEEDS,
    LINEAGE_UPSTREAMS,
    LINEAGE_VERSION_SEEDS,
    SEED_ROWS,
)


def _recalc_units(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把引用边按 (指标, 日期) 去重成回算单元，同时保留每一条到达路径。"""
    units: dict[tuple[str, str], dict[str, Any]] = {}
    for ref in refs:
        metric = str(ref.get("metric", ""))
        metric_date = str(ref.get("metric_date", ""))
        unit = units.setdefault((metric, metric_date), {
            "unit_key": f"{metric}@{metric_date}",
            "metric": metric,
            "metric_date": metric_date,
            "paths": [],
        })
        unit["paths"].append({
            "ref_key": ref.get("ref_key"),
            "ref_type": ref.get("ref_type"),
            "ref_name": ref.get("ref_name"),
            "path_key": ref.get("path_key", ""),
            "upstream_ids": list(ref.get("upstream_ids") or []),
        })
    return list(units.values())


def _dates_consistent(refs: list[dict[str, Any]], ready: set[str]) -> bool:
    """同一指标在所有已就绪引用类型上是否落在同一个日期。"""
    by_metric: dict[str, set[str]] = {}
    for ref in refs:
        if ref.get("ref_type") not in ready:
            continue
        by_metric.setdefault(str(ref.get("metric", "")), set()).add(
            str(ref.get("metric_date", "")))
    return all(len(dates) == 1 for dates in by_metric.values())


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._id_counters: dict[str, int] = {}
        # Cache layer storage
        self._cache: dict[str, tuple[float, Any]] = {}
        # Cross-module journal
        self._query_log: list[dict[str, Any]] = []
        # Cohort snapshots
        self._cohort_snapshots: dict[str, list[dict[str, Any]]] = {}
        # Anomaly records
        self._anomaly_records: list[dict[str, Any]] = []
        # Recalc history
        self._recalc_history: list[dict[str, Any]] = []
        # 影响评估试算受理记录（字段血缘 -> 口径重算引擎）
        self._assessment_runs: list[dict[str, Any]] = []
        # 字段血缘依赖版本（影响评估单据的冻结快照）
        self._lineage_versions: list[dict[str, Any]] = []
        self._lineage_version_seq = 0
        # 字段血缘的当前上游身份与下游引用边（未发布前只改这里，不产生版本）
        self._lineage_upstreams: list[dict[str, Any]] = [
            dict(item) for item in LINEAGE_UPSTREAMS
        ]
        self._lineage_references: list[dict[str, Any]] = [
            dict(item) for item in LINEAGE_REFERENCE_SEEDS
        ]
        # 已登记但尚未提交的切换：(模型, 字段) -> {ref_type -> {metric -> 新日期}}
        self._lineage_pending_ref_types: dict[
            tuple[str, str], dict[str, dict[str, str]]
        ] = {}
        # Lock for thread safety; RLock allows nested snapshot helpers.
        self._lock = threading.RLock()
        self._seed_lineage()

    def _seed_lineage(self) -> None:
        for seed in LINEAGE_VERSION_SEEDS:
            self.publish_current_lineage(
                seed["model"], seed["field"], note=seed["note"])


    @property
    def lock(self):
        return self._lock

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        rows = self.rows(module)
        if not rows:
            return 1
        return max(int(row.get("id", 0)) for row in rows) + 1

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ── Cross-module query helpers ──

    def find_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        """查找模块中 field 等于 value 的所有记录。"""
        return [row for row in self.rows(module) if row.get(field) == value]

    def find_by_status(self, module: str, status: str) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get("status") == status]

    # ── Query journal ──

    def log_query(self, metric_id: int, operator: str, sql_summary: str,
                  row_count: int, duration_ms: float, status: str = "正常") -> dict[str, Any]:
        entry = {
            "id": len(self._query_log) + 1,
            "查询时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "操作人": operator,
            "涉及指标": f"metric_{metric_id}",
            "查询语句摘要": sql_summary,
            "返回行数": str(row_count),
            "status": status,
            "duration_ms": duration_ms,
            "pending": status != "正常",
            "abnormal": status in ("慢查询", "异常终止"),
        }
        self._query_log.append(entry)
        return entry

    def query_logs(self) -> list[dict[str, Any]]:
        return self._query_log

    # ── Cache operations ──

    def cache_get(self, key: str) -> Any | None:
        """获取缓存，过期返回 None。"""
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.time() > expires_at:
            del self._cache[key]
            return None
        return value

    def cache_set(self, key: str, value: Any, ttl: int) -> None:
        """设置缓存，ttl 单位为秒。"""
        self._cache[key] = (time.time() + ttl, value)

    def cache_invalidate(self, key: str) -> bool:
        return self._cache.pop(key, None) is not None

    def cache_invalidate_by_prefix(self, prefix: str) -> int:
        count = 0
        for key in list(self._cache):
            if key.startswith(prefix):
                del self._cache[key]
                count += 1
        return count

    # ── Cohort operations ──

    def save_cohort_snapshot(self, cohort_id: int, members: list[dict[str, Any]]) -> None:
        self._cohort_snapshots[str(cohort_id)] = members

    def get_cohort_snapshot(self, cohort_id: int) -> list[dict[str, Any]] | None:
        return self._cohort_snapshots.get(str(cohort_id))

    # ── Anomaly detection operations ──

    def record_anomaly(self, metric_id: int, severity: str, description: str,
                       value: float, threshold: float) -> dict[str, Any]:
        record = {
            "id": len(self._anomaly_records) + 1,
            "指标ID": metric_id,
            "严重程度": severity,
            "描述": description,
            "当前值": value,
            "阈值": threshold,
            "时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._anomaly_records.append(record)
        return record

    def anomaly_records(self) -> list[dict[str, Any]]:
        return self._anomaly_records

    # ── Recalc operations ──

    def record_recalc(self, metric_id: int, old_version: str, new_version: str,
                      time_range: str, diff_summary: str) -> dict[str, Any]:
        record = {
            "id": len(self._recalc_history) + 1,
            "关联指标": f"metric_{metric_id}",
            "旧口径": old_version,
            "新口径": new_version,
            "历史时间范围": time_range,
            "产出差异": diff_summary,
            "重算时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._recalc_history.append(record)
        return record

    def recalc_history(self) -> list[dict[str, Any]]:
        return self._recalc_history

    def record_assessment_run(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            entry = {"id": len(self._assessment_runs) + 1, **record}
            self._assessment_runs.append(entry)
            return dict(entry)

    def assessment_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in reversed(self._assessment_runs)]

    # ── 字段血缘依赖版本（影响评估单据） ──

    def publish_lineage_version(
        self,
        model: str,
        field: str,
        *,
        upstream: list[dict[str, Any]],
        downstream: list[dict[str, Any]],
        metrics: list[dict[str, Any]],
        note: str = "",
        complete: bool = True,
        ref_types: list[str] | None = None,
        metric_dates: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """登记一版字段血缘依赖。

        同一模型下的同名字段共享一条版本链，链内序号单调递增；
        发布线程与下载线程通过 store.lock 串行化，保证每笔下载看到的是
        某一完整版本，而不是发布到一半的状态。
        """
        with self._lock:
            self._lineage_version_seq += 1
            chain = [
                row for row in self._lineage_versions
                if row.get("model") == model and row.get("field") == field
            ]
            version = {
                "version_id": f"LV-{self._lineage_version_seq:04d}",
                "version_no": max((int(row.get("version_no", 0)) for row in chain), default=0) + 1,
                "model": model,
                "field": field,
                "field_identity": f"{model}.{field}",
                "published_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "note": note,
                "status": "已发布" if complete else "切换中",
                "complete": complete,
                "ref_types": ref_types,
                "metric_dates": metric_dates,
                "pending": not complete,
                "abnormal": False,
                "upstream": [dict(item) for item in upstream],
                "downstream": [dict(item) for item in downstream],
                "metrics": [dict(item) for item in metrics],
            }
            self._lineage_versions.append(version)
            return version

    def lineage_versions(
        self, model: str | None = None, field: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = [copy.deepcopy(row) for row in self._lineage_versions]
        if model is not None:
            rows = [row for row in rows if row.get("model") == model]
        if field is not None:
            rows = [row for row in rows if row.get("field") == field]
        return sorted(rows, key=lambda row: int(row.get("version_no", 0)), reverse=True)

    # ── 血缘发布：把当前发布的引用整体冻结成一版 ──

    def lineage_upstreams(self, model: str | None = None, field: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(item) for item in self._lineage_upstreams]
        if model is not None:
            rows = [row for row in rows if row.get("model") == model]
        if field is not None:
            rows = [row for row in rows if row.get("target_field") == field]
        return rows

    def lineage_references(self, model: str | None = None, ref_type: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(item) for item in self._lineage_references]
        if model is not None:
            rows = [row for row in rows if row.get("model") == model]
        if ref_type is not None:
            rows = [row for row in rows if row.get("ref_type") == ref_type]
        return rows

    def pending_switches(self, model: str, field: str) -> dict[str, dict[str, str]]:
        """某个字段身份尚未提交的切换：ref_type -> {metric -> 新日期}。"""
        with self._lock:
            scoped = self._lineage_pending_ref_types.get((model, field), {})
            return {
                ref_type: dict(dates)
                for ref_type, dates in scoped.items() if dates
            }

    def publish_current_lineage(
        self, model: str, field: str, *, ref_types: list[str] | None = None,
        metric_dates: dict[str, str] | None = None,
        note: str = "",
        complete: bool | None = None,
    ) -> dict[str, Any]:
        """把某个模型字段当前的引用冻结成新版本。

        查询、接口、报告各自的切换进度分开记：只传入 ref_types 时，这一版只
        覆盖已切过去的那几类引用，未切换的引用仍留在上一版里；只有三类都
        就绪、且这一轮没有未提交的切换时，这一版才算完整版本。
        """
        with self._lock:
            refs = [
                dict(ref) for ref in self._lineage_references
                if ref.get("model") == model and ref.get("field") == field
                and ref.get("active", True)
                and (ref_types is None or ref.get("ref_type") in ref_types)
            ]
            pending = self._lineage_pending_ref_types.get((model, field), {})
            for ref in refs:
                ref_type = str(ref.get("ref_type", ""))
                metric = str(ref.get("metric", ""))
                if metric_dates and metric in metric_dates:
                    ref["metric_date"] = metric_dates[metric]
                elif metric in pending.get(ref_type, {}):
                    ref["metric_date"] = pending[ref_type][metric]
            upstream_ids = {
                uid for ref in refs for uid in ref.get("upstream_ids", [])
            }
            upstream = [
                dict(item) for item in self._lineage_upstreams
                if item.get("model") == model and item.get("target_field") == field
                and item.get("upstream_id") in upstream_ids
            ]
            if complete is None:
                ready = self._ready_ref_types(model, field)
                covers_all = (
                    set(ref_types) >= ready if ref_types is not None else True)
                no_pending = not any(pending.values())
                # 只切了查询就发布时，同一指标的引用会落在两个日期上，
                # 这一版不能算完整版本，否则下载会拿到新旧混排的范围。
                complete = covers_all and no_pending and _dates_consistent(
                    refs, ready)
            return self.publish_lineage_version(
                model,
                field,
                upstream=upstream,
                downstream=refs,
                metrics=_recalc_units(refs),
                note=note,
                complete=complete,
                ref_types=ref_types,
            )

    def switch_lineage_references(
        self,
        model: str,
        field: str,
        *,
        ref_types: list[str],
        metric: str,
        metric_date: str,
        commit: bool = True,
    ) -> int:
        """把某一类下游引用（查询/接口/报告）切到新的指标日期。

        commit=False 时只登记为待发布状态，等整轮引用全部就绪后再一次性
        体现在新版本里。
        """
        with self._lock:
            scoped = self._lineage_pending_ref_types.setdefault((model, field), {})
            if not commit:
                for ref_type in ref_types:
                    scoped.setdefault(ref_type, {})[metric] = metric_date
                return len(ref_types)
            changed = 0
            for ref in self._lineage_references:
                if (ref.get("model") == model and ref.get("field") == field
                        and ref.get("active", True)
                        and ref.get("ref_type") in ref_types
                        and ref.get("metric") == metric):
                    ref["metric_date"] = metric_date
                    changed += 1
            for ref_type in ref_types:
                scoped.get(ref_type, {}).pop(metric, None)
            return changed

    def _ready_ref_types(self, model: str, field: str) -> set[str]:
        return {
            str(ref.get("ref_type")) for ref in self._lineage_references
            if ref.get("model") == model and ref.get("field") == field
            and ref.get("active", True)
        }

    def lineage_version(self, version_id: str) -> dict[str, Any] | None:
        with self._lock:
            for row in self._lineage_versions:
                if row.get("version_id") == version_id:
                    return copy.deepcopy(row)
        return None

    def select_lineage_version(
        self, model: str, field: str, version_id: str | None = None,
    ) -> dict[str, Any] | None:
        """在同一把锁内挑定一版依赖，避免下载读到发布到一半的状态。

        未指定 version_id 时只挑最新一版「完整」依赖；切换中的版本不会被选中。
        """
        with self._lock:
            chain = [
                row for row in self._lineage_versions
                if row.get("model") == model and row.get("field") == field
            ]
            if version_id:
                match = next(
                    (row for row in chain if row.get("version_id") == version_id), None)
                if match is None:
                    return None
                if not match.get("complete"):
                    return None
                return copy.deepcopy(match)
            complete = [row for row in chain if row.get("complete")]
            if not complete:
                return None
            latest = max(complete, key=lambda row: int(row.get("version_no", 0)))
            return copy.deepcopy(latest)

    def add_lineage_reference(self, reference: dict[str, Any]) -> dict[str, Any]:
        """登记一条新的下游引用边（例如这一轮才接入的指标）。"""
        with self._lock:
            ref = dict(reference)
            ref.setdefault("active", True)
            self._lineage_references.append(ref)
            return dict(ref)

    def reset_lineage(self) -> None:
        """把字段血缘依赖恢复成初始快照（演示与测试用）。"""
        with self._lock:
            self._lineage_versions.clear()
            self._lineage_version_seq = 0
            self._lineage_upstreams = [dict(item) for item in LINEAGE_UPSTREAMS]
            self._lineage_references = [
                dict(item) for item in LINEAGE_REFERENCE_SEEDS
            ]
            self._lineage_pending_ref_types.clear()
            self._seed_lineage()

    # ── Data artifacts for cross-module consumption ──

    def get_metric_data(self, metric_id: int) -> dict[str, Any] | None:
        """Get metric data with its calibers, queries, and services."""
        metric = self.find("metric_definition", metric_id)
        if metric is None:
            return None
        return {
            "metric": metric,
            "calibers": self.find_by_field("caliber_version", "所属指标",
                                           metric.get("指标名称", "")),
            "queries": self.find_by_field("query_builder", "关联指标",
                                          metric.get("指标名称", "")),
            "services": [row for row in self.rows("data_service")
                         if metric.get("指标名称", "") in str(row.get("关联指标或模型", ""))],
            "subscriptions": self.find_by_field("data_subscription", "关联指标",
                                                 metric.get("指标名称", "")),
        }

    def get_dimension_model_data(self, model_id: int) -> dict[str, Any] | None:
        """Get dimension model with its drill-down paths and pivot configs."""
        model = self.find("dimension_model", model_id)
        if model is None:
            return None
        return {
            "model": model,
            "drill_paths": self.find_by_field("drill_down", "起始指标",
                                              model.get("模型名称", "")),
            "pivot_configs": self.find_by_field("pivot_table", "数据源指标",
                                                 model.get("模型名称", "")),
        }

    def get_cohort_analysis(self, cohort_id: int) -> dict[str, Any] | None:
        """Get cohort with experiments and segment comparisons."""
        cohort = self.find("user_cohort", cohort_id)
        if cohort is None:
            return None
        cohort_name = cohort.get("分群名称", "")
        return {
            "cohort": cohort,
            "experiments": [row for row in self.rows("experiment_group")
                            if cohort_name in str(row.get("筛选条件", ""))],
            "comparisons": [row for row in self.rows("segment_compare")
                            if cohort_name in (str(row.get("分群A", "")) + str(row.get("分群B", "")))],
            "anomaly_rules": [row for row in self.rows("anomaly_detector")
                              if cohort_name in str(row.get("目标指标", ""))],
        }

    def get_field_lineage(self, field_name: str) -> dict[str, Any]:
        """Trace a field's complete lineage chain."""
        entries = self.find_by_field("field_lineage", "目标字段", field_name)
        if not entries:
            return {"field": field_name, "lineage": [], "depth": 0}

        lineage = [{
            "source_fields": entry.get("来源字段列表", ""),
            "transform": entry.get("转换逻辑", ""),
            "intermediate_table": entry.get("中间表", ""),
            "depth": entry.get("血缘深度", ""),
            "status": entry.get("status", ""),
        } for entry in entries]

        return {
            "field": field_name,
            "lineage": lineage,
            "depth": len(lineage),
        }


store = Store()