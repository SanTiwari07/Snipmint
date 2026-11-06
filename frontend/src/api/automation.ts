import { getJson, json, postJson, send } from './client'
import type { Privacy } from './youtube'

export type TaskStatus =
  | 'AWAITING_APPROVAL'
  | 'READY'
  | 'SCHEDULED'
  | 'PROCESSING'
  | 'UPLOADING'
  | 'PUBLISHED'
  | 'FAILED'
  | 'CANCELLED'
  | 'SKIPPED'

export const OPEN_TASK: TaskStatus[] = ['AWAITING_APPROVAL', 'READY', 'SCHEDULED', 'PROCESSING', 'UPLOADING']

export interface AutomationSettings {
  enabled: boolean
  auto_approve: boolean
  language: string
  privacy: Privacy
  made_for_kids: boolean
  batch_delay_seconds: number
  batch_concurrency: number
  max_attempts: number
  max_lateness_minutes: number
}

export interface AutomationStatus {
  configured: boolean
  config_error: string | null
  webhook_url_set: boolean
  reachable: boolean
  unreachable_reason: string | null
  settings: AutomationSettings
  task_counts: Partial<Record<TaskStatus, number>>
  events: { pending: number; delivered: number; dead: number }
}

export interface ClipRef {
  job_id: string
  clip_file: string
  clip_id: string
  source_id: string
}

export interface AutomationTask {
  id: string
  kind: 'publish' | 'scheduled' | 'batch'
  status: TaskStatus
  label: string
  clip: ClipRef
  title: string
  privacy: Privacy
  run_at: string | null
  batch_id: string | null
  attempts: number
  max_attempts: number
  next_attempt_at: number | null
  error: string | null
  video_url: string | null
  approved: boolean
  has_metadata: boolean
  history: { at: number; status: string; message: string }[]
  created: number
  updated: number
}

export interface Execution {
  id: string
  workflow: string
  status: 'running' | 'success' | 'error' | 'waiting' | 'cancelled'
  task_id: string | null
  batch_id: string | null
  message: string
  started: number
  updated: number
}

export interface CreateTasks {
  clips: ClipRef[]
  privacy?: Privacy
  made_for_kids?: boolean
  run_at?: string | null
  batch?: boolean
  delay_seconds?: number
  concurrency?: number
  channel_id?: string | null
  language?: string
}

const enc = encodeURIComponent

export const automationApi = {
  status: () => getJson<AutomationStatus>('/api/automation/status'),
  updateSettings: async (changes: Partial<AutomationSettings>): Promise<AutomationSettings> =>
    (await send('/api/automation/settings', { ...json(changes), method: 'PUT' })).json(),
  create: (body: CreateTasks) =>
    postJson<{ batch_id: string | null; tasks: AutomationTask[] }>('/api/automation/tasks', body),
  tasks: () => getJson<AutomationTask[]>('/api/automation/tasks?limit=50'),
  executions: () => getJson<Execution[]>('/api/automation/executions?limit=20'),
  cancel: (id: string) => postJson<AutomationTask>(`/api/automation/tasks/${enc(id)}/cancel`, {}),
  retry: (id: string) => postJson<AutomationTask>(`/api/automation/tasks/${enc(id)}/retry`, {}),
  reschedule: (id: string, runAt: string) =>
    postJson<AutomationTask>(`/api/automation/tasks/${enc(id)}/reschedule`, { run_at: runAt }),
  retryEvents: () => postJson<{ requeued: number }>('/api/automation/events/retry', {}),
}

/** The statuses people see. */
export const TASK_LABEL: Record<TaskStatus, string> = {
  AWAITING_APPROVAL: 'Waiting for approval',
  READY: 'Processing',
  SCHEDULED: 'Scheduled',
  PROCESSING: 'Processing',
  UPLOADING: 'Uploading',
  PUBLISHED: 'Published',
  FAILED: 'Failed',
  CANCELLED: 'Cancelled',
  SKIPPED: 'Skipped',
}
