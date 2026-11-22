<template>
  <section class="recalc-panel" data-testid="recalc-impact-panel">
    <header class="panel-head">
      <div>
        <h3>影响估算入口</h3>
        <p class="page-desc">
          提交字段血缘导出的影响评估文件；按文件里的依赖版本把查询/接口/报告引用归到
          实际要回算的指标与日期，同一指标同一天只计一个回算单元。
        </p>
      </div>
      <button class="btn" data-testid="recalc-refresh" type="button" @click="loadRuns">刷新受理记录</button>
    </header>

    <div class="filter-bar">
      <input
        type="file"
        accept="application/json,.json"
        data-testid="recalc-file"
        @change="onFileSelected"
      />
      <button
        class="btn primary"
        type="button"
        data-testid="recalc-submit"
        :disabled="!file || estimating"
        @click="submit"
      >
        {{ estimating ? '试算中…' : '提交影响估算' }}
      </button>
    </div>
    <p v-if="errorMessage" class="error-text" data-testid="recalc-error">{{ errorMessage }}</p>

    <div v-if="estimate" data-testid="recalc-result">
      <p>
        文件 <strong>{{ estimate.field_identity }}</strong> ·
        依赖版本 <strong data-testid="recalc-version">{{ estimate.version_id }}</strong> ·
        回算单元 {{ estimate.summary.recalc_unit_count }} 个 ·
        路径 {{ estimate.summary.path_count }} 条 ·
        全部可追溯：{{ estimate.traceability.all_traceable ? '是' : '否' }}
      </p>
      <p v-if="estimate.older_than_latest" class="page-desc" data-testid="recalc-stale-notice">
        文件按自身版本核算；平台当前最新完整版本是
        {{ estimate.platform_latest_complete_version }}。
      </p>
      <table class="data-table">
        <thead>
          <tr>
            <th>回算指标</th>
            <th>日期</th>
            <th>回算次数</th>
            <th>路径明细（含依赖版本）</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="unit in estimate.estimate_units" :key="`${unit.metric}@${unit.metric_date}`" :data-testid="`recalc-unit-${unit.metric}-${unit.metric_date}`">
            <td>{{ unit.metric }}</td>
            <td>{{ unit.metric_date }}</td>
            <td>{{ unit.recalc_count }}</td>
            <td>
              <span v-for="path in unit.paths" :key="path.ref_key" class="path-chip">
                {{ path.ref_name }}({{ path.ref_type }}) → {{ path.upstream_ids.join('+') }} @{{ path.version_id }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <table class="data-table" data-testid="recalc-runs">
      <thead>
        <tr>
          <th>试算时间</th>
          <th>字段身份</th>
          <th>依赖版本</th>
          <th>回算单元数</th>
          <th>文件</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="run in runs" :key="String(run.id)">
          <td>{{ run['试算时间'] }}</td>
          <td>{{ run.field_identity }}</td>
          <td>{{ run.version_id }}</td>
          <td>{{ run['回算单元数'] }}</td>
          <td>{{ run.filename || '—' }}</td>
        </tr>
        <tr v-if="!runs.length">
          <td colspan="5" class="empty-state">还没有影响评估试算记录</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'
import { estimateFromAssessment } from '@/api/backend-service'
import type { EstimateResult } from '@/api/backend-service'

type RunRecord = {
  id: number
  field_identity: string
  version_id: string
  回算单元数: number
  试算时间: string
  filename: string
}

const file = ref<File | null>(null)
const estimate = ref<EstimateResult | null>(null)
const runs = ref<RunRecord[]>([])
const errorMessage = ref('')
const estimating = ref(false)

function onFileSelected(event: Event) {
  const target = event.target as HTMLInputElement
  file.value = target.files?.[0] ?? null
  estimate.value = null
  errorMessage.value = ''
}

async function submit() {
  if (!file.value) return
  estimating.value = true
  errorMessage.value = ''
  try {
    estimate.value = await estimateFromAssessment(await file.value.text(), file.value.name)
    await loadRuns()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '影响估算失败'
  } finally {
    estimating.value = false
  }
}

async function loadRuns() {
  try {
    const response = await request('/api/cross/recalc/impact-assessments')
    if (!response.ok) throw new Error('受理记录读取失败')
    runs.value = (await response.json()).items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '受理记录读取失败'
  }
}

onMounted(loadRuns)
</script>

<style scoped>
.recalc-panel { margin-top: 24px; }
.panel-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.path-chip { display: inline-block; margin: 2px 4px 2px 0; padding: 1px 6px; border-radius: 4px; background: #f0f4ff; font-size: 12px; }
</style>
