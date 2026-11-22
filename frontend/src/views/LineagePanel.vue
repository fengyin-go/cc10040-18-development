<template>
  <section class="lineage-panel" data-testid="lineage-panel">
    <header class="panel-head">
      <div>
        <h3>字段依赖版本与影响评估</h3>
        <p class="page-desc">
          每个模型字段按依赖版本打包成影响评估文件，交给口径重算引擎做历史数据试算。
          同名字段以「模型 + 字段」区分，下载只交付一版完整依赖。
        </p>
      </div>
      <button class="btn" data-testid="lineage-refresh" type="button" @click="reload">刷新</button>
    </header>

    <p v-if="errorMessage" class="error-text" data-testid="lineage-error">{{ errorMessage }}</p>

    <table class="data-table" data-testid="lineage-table">
      <thead>
        <tr>
          <th>模型</th>
          <th>字段</th>
          <th>引用类型</th>
          <th>关联指标</th>
          <th>指标日期</th>
          <th>最新版本</th>
          <th>可领取完整版本</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in identities" :key="item.field_identity" :data-testid="`identity-${item.field_identity}`">
          <td>{{ item.model }}</td>
          <td>{{ item.field }}</td>
          <td>{{ item.reference_types.map(refTypeName).join(' / ') }}</td>
          <td>{{ item.metrics.join('、') }}</td>
          <td>{{ item.metric_dates.join('、') }}</td>
          <td>
            <template v-if="item.latest_version">
              {{ item.latest_version.version_id }}
              <span :class="item.latest_version.complete ? 'tag-ok' : 'tag-pending'">
                {{ item.latest_version.status }}
              </span>
            </template>
            <template v-else>—</template>
          </td>
          <td>{{ item.latest_complete_version?.version_id ?? '暂无完整版本' }}</td>
          <td class="row-actions">
            <button
              class="link"
              type="button"
              data-testid="lineage-download"
              @click="download(item)"
            >
              领取影响评估文件
            </button>
            <button
              class="link"
              type="button"
              data-testid="lineage-switch"
              @click="openSwitch(item)"
            >
              切换引用
            </button>
          </td>
        </tr>
        <tr v-if="!identities.length">
          <td colspan="8" class="empty-state">暂无字段身份</td>
        </tr>
      </tbody>
    </table>

    <section v-if="activeIdentity" class="lineage-detail" data-testid="lineage-detail">
      <header class="panel-head">
        <h4>{{ activeIdentity.field_identity }} 依赖版本链</h4>
        <button class="btn ghost" type="button" @click="activeIdentity = null">收起</button>
      </header>
      <form class="filter-bar" @submit.prevent="applySwitch">
        <label class="filter-item">
          <span>新指标日期</span>
          <input v-model="switchDate" data-testid="switch-date" placeholder="2026-10-02" required />
        </label>
        <button class="btn" type="submit" data-testid="switch-query">仅切换查询</button>
        <button class="btn" type="submit" data-testid="switch-all" @click="switchAll = true">切换查询/接口/报告</button>
        <button class="btn primary" type="button" data-testid="publish-version" @click="publishFull">
          发布整轮版本
        </button>
      </form>
      <table class="data-table" data-testid="version-table">
        <thead>
          <tr>
            <th>版本</th>
            <th>状态</th>
            <th>上游数</th>
            <th>引用数</th>
            <th>回算单元</th>
            <th>发布时间</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="version in versions" :key="version.version_id" :data-testid="`version-${version.version_id}`">
            <td>{{ version.version_id }}</td>
            <td>
              <span :class="version.complete ? 'tag-ok' : 'tag-pending'">{{ version.status }}</span>
            </td>
            <td>{{ version.upstream_count }}</td>
            <td>{{ version.reference_count }}</td>
            <td>{{ version.recalc_unit_count }}</td>
            <td>{{ version.published_at }}</td>
            <td>{{ version.note || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="lineage-detail" data-testid="estimate-panel">
      <header class="panel-head">
        <h4>口径重算引擎 · 影响估算</h4>
        <span class="page-desc">把下载到的原文件提交回来，按文件里的依赖版本核算。</span>
      </header>
      <div class="filter-bar">
        <input
          type="file"
          accept="application/json,.json"
          data-testid="estimate-file"
          @change="onFileSelected"
        />
        <button
          class="btn primary"
          type="button"
          data-testid="estimate-submit"
          :disabled="!estimateFile || estimating"
          @click="submitEstimate"
        >
          {{ estimating ? '试算中…' : '提交试算' }}
        </button>
      </div>
      <p v-if="estimateError" class="error-text" data-testid="estimate-error">{{ estimateError }}</p>
      <div v-if="estimate" data-testid="estimate-result">
        <p>
          文件：<strong>{{ estimate.field_identity }}</strong> ·
          依赖版本 <strong data-testid="estimate-version">{{ estimate.version_id }}</strong> ·
          回算单元 {{ estimate.summary.recalc_unit_count }} 个 ·
          路径 {{ estimate.summary.path_count }} 条
        </p>
        <table class="data-table">
          <thead>
            <tr>
              <th>回算指标</th>
              <th>日期</th>
              <th>回算次数</th>
              <th>路径数</th>
              <th>路径明细</th>
              <th>可追溯</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="unit in estimate.estimate_units" :key="`${unit.metric}@${unit.metric_date}`" :data-testid="`estimate-unit-${unit.metric}-${unit.metric_date}`">
              <td>{{ unit.metric }}</td>
              <td>{{ unit.metric_date }}</td>
              <td>{{ unit.recalc_count }}</td>
              <td>{{ unit.path_count }}</td>
              <td>
                <span v-for="path in unit.paths" :key="path.ref_key" class="path-chip" :title="path.upstream_ids.join('、')">
                  {{ path.ref_name }}({{ path.ref_type }}) → {{ path.upstream_ids.join('+') }}
                </span>
              </td>
              <td>{{ unit.traceable ? '是' : '否' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  estimateFromAssessment,
  fetchImpactAssessment,
  listFieldIdentities,
  listLineageVersions,
  publishLineageVersion,
  switchReferences,
} from '@/api/backend-service'
import type { EstimateResult, FieldIdentity, LineageVersionBrief } from '@/api/backend-service'

const identities = ref<FieldIdentity[]>([])
const versions = ref<LineageVersionBrief[]>([])
const activeIdentity = ref<FieldIdentity | null>(null)
const switchDate = ref('2026-10-02')
const switchAll = ref(false)
const errorMessage = ref('')
const estimateFile = ref<File | null>(null)
const estimate = ref<EstimateResult | null>(null)
const estimateError = ref('')
const estimating = ref(false)

const REF_TYPE_LABELS: Record<string, string> = { query: '查询', service: '接口', report: '报告' }

function refTypeName(type: string): string {
  return REF_TYPE_LABELS[type] ?? type
}

async function reload() {
  errorMessage.value = ''
  try {
    identities.value = await listFieldIdentities()
    if (activeIdentity.value) {
      versions.value = await listLineageVersions(activeIdentity.value.model, activeIdentity.value.field)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '字段身份读取失败'
  }
}

async function download(item: FieldIdentity) {
  errorMessage.value = ''
  try {
    const assessment = await fetchImpactAssessment(item.model, item.field)
    const blob = new Blob([JSON.stringify(assessment, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `影响评估_${assessment.field_identity}_${assessment.version.version_id}.json`
    document.body.appendChild(anchor)
    anchor.click()
    document.body.removeChild(anchor)
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '影响评估下载失败'
  }
}

async function openSwitch(item: FieldIdentity) {
  activeIdentity.value = item
  await reload()
}

async function applySwitch() {
  if (!activeIdentity.value) return
  errorMessage.value = ''
  const refTypes = switchAll.value ? ['query', 'service', 'report'] : ['query']
  try {
    await switchReferences(
      activeIdentity.value.model,
      activeIdentity.value.field,
      refTypes,
      activeIdentity.value.metrics[0] ?? '',
      switchDate.value,
    )
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '引用切换失败'
  } finally {
    switchAll.value = false
  }
}

async function publishFull() {
  if (!activeIdentity.value) return
  errorMessage.value = ''
  try {
    await publishLineageVersion(activeIdentity.value.model, activeIdentity.value.field, [], '整轮引用就绪')
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '依赖发布失败'
  }
}

function onFileSelected(event: Event) {
  const target = event.target as HTMLInputElement
  estimateFile.value = target.files?.[0] ?? null
  estimate.value = null
  estimateError.value = ''
}

async function submitEstimate() {
  if (!estimateFile.value) return
  estimating.value = true
  estimateError.value = ''
  try {
    const content = await estimateFile.value.text()
    estimate.value = await estimateFromAssessment(content, estimateFile.value.name)
  } catch (error) {
    estimateError.value = error instanceof Error ? error.message : '影响估算失败'
  } finally {
    estimating.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
.lineage-panel { margin-top: 24px; }
.panel-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.lineage-detail { margin-top: 16px; padding: 12px; background: #fff; border: 1px solid var(--border); border-radius: 8px; }
.tag-ok { color: #067647; background: #ecfdf3; border-radius: 4px; padding: 1px 6px; font-size: 12px; }
.tag-pending { color: #b54708; background: #fffaeb; border-radius: 4px; padding: 1px 6px; font-size: 12px; }
.path-chip { display: inline-block; margin: 2px 4px 2px 0; padding: 1px 6px; border-radius: 4px; background: #f0f4ff; font-size: 12px; }
</style>
