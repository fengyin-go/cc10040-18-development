import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ── 字段血缘依赖版本与影响评估 ──

export type FieldIdentity = {
  model: string
  field: string
  field_identity: string
  reference_types: string[]
  metrics: string[]
  metric_dates: string[]
  version_count: number
  latest_version: LineageVersionBrief | null
  latest_complete_version: LineageVersionBrief | null
}

export type LineageVersionBrief = {
  version_id: string
  version_no: number
  field_identity: string
  status: string
  complete: boolean
  published_at: string
  note: string
  upstream_count: number
  reference_count: number
  recalc_unit_count: number
  references?: AssessmentReference[]
  recalc_units?: AssessmentUnit[]
}

export type AssessmentReference = {
  ref_key: string
  ref_type: string
  ref_type_name: string
  ref_name: string
  metric: string
  metric_date: string
  upstream_ids: string[]
  path_key: string
}

export type AssessmentUnit = {
  unit_key: string
  metric: string
  metric_date: string
  ref_keys: string[]
  paths: { ref_key: string; ref_type: string; ref_name: string; path_key: string; upstream_ids: string[] }[]
  path_count: number
}

export type ImpactAssessment = {
  artifact: string
  format_version: number
  field_identity: string
  model: string
  field: string
  generated_at: string
  version: { version_id: string; version_no: number; status: string; complete: boolean; published_at: string; note: string }
  upstreams: { upstream_id: string; identity: string; table: string; field: string; transform: string }[]
  references: AssessmentReference[]
  recalc_units: AssessmentUnit[]
  summary: Record<string, unknown>
}

export type EstimateResult = {
  ok: boolean
  field_identity: string
  version_id: string
  dependency_fingerprint: string
  estimate_units: {
    metric: string
    metric_date: string
    recalc_count: number
    path_count: number
    paths: { ref_key: string; ref_type: string; ref_name: string; upstream_ids: string[]; version_id: string }[]
    traceable: boolean
  }[]
  traceability: { checked_units: number; traceable_units: number; all_traceable: boolean; version_id: string }
  summary: Record<string, unknown>
  platform_latest_complete_version?: string | null
  older_than_latest?: boolean
}

export async function listFieldIdentities(): Promise<FieldIdentity[]> {
  const response = await request('/api/cross/field-lineage/identities')
  if (!response.ok) throw new Error('字段身份读取失败')
  return (await response.json()).items
}

export async function listLineageVersions(model: string, field: string): Promise<LineageVersionBrief[]> {
  const path = `/api/cross/field-lineage/${encodeURIComponent(model)}/${encodeURIComponent(field)}/versions`
  const response = await request(path)
  if (!response.ok) throw new Error('依赖版本读取失败')
  return (await response.json()).items
}

export async function fetchImpactAssessment(model: string, field: string): Promise<ImpactAssessment> {
  const path = `/api/cross/field-lineage/${encodeURIComponent(model)}/${encodeURIComponent(field)}/impact-assessment`
  const response = await request(path)
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: '影响评估导出失败' }))
    throw new Error(detail.detail ?? '影响评估导出失败')
  }
  return await response.json()
}

export async function switchReferences(
  model: string,
  field: string,
  refTypes: string[],
  metric: string,
  metricDate: string,
  commit = true,
): Promise<void> {
  const params = new URLSearchParams({
    ref_types: refTypes.join(','),
    metric,
    metric_date: metricDate,
    commit: String(commit),
  })
  const path = `/api/cross/field-lineage/${encodeURIComponent(model)}/${encodeURIComponent(field)}/switch-references?${params}`
  const response = await request(path, { method: 'POST' })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: '引用切换失败' }))
    throw new Error(detail.detail ?? '引用切换失败')
  }
}

export async function publishLineageVersion(
  model: string,
  field: string,
  refTypes: string[] = [],
  note = '',
): Promise<LineageVersionBrief> {
  const params = new URLSearchParams({ note })
  if (refTypes.length) params.set('ref_types', refTypes.join(','))
  const path = `/api/cross/field-lineage/${encodeURIComponent(model)}/${encodeURIComponent(field)}/publish?${params}`
  const response = await request(path, { method: 'POST' })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: '依赖发布失败' }))
    throw new Error(detail.detail ?? '依赖发布失败')
  }
  return (await response.json()).entry
}

export async function estimateFromAssessment(content: string, filename: string): Promise<EstimateResult> {
  const response = await request('/api/cross/recalc/impact-assessment/estimate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Filename': encodeURIComponent(filename) },
    body: content,
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: '影响估算失败' }))
    throw new Error(detail.detail ?? '影响估算失败')
  }
  return await response.json()
}

