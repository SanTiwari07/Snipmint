import { getJson, json, postJson, send } from './client'
import { postWithAi } from './clips'

// ------------------------------------------------------------------ YouTube channel + publishing

export type Privacy = 'private' | 'unlisted' | 'public'
export type PublishStatus = 'QUEUED' | 'PREPARING' | 'UPLOADING' | 'PUBLISHED' | 'FAILED' | 'CANCELLED'
export const ACTIVE_STATUSES: PublishStatus[] = ['QUEUED', 'PREPARING', 'UPLOADING']

export interface Channel {
  id: string
  title: string
  custom_url: string | null
  thumbnail: string | null
  connected_at: string
  scopes: string[]
}

export interface YouTubeStatus {
  configured: boolean
  encryption_available: boolean
  environment: 'development' | 'production'
  config_error: string | null
  redirect_uri: string | null
  channels: Channel[]
  default_channel: string | null
  concurrency: number
  scopes: string[]
}

export interface VideoDetails {
  title: string
  description: string
  tags: string[]
  category_id: string
  privacy: Privacy
  made_for_kids: boolean
  language: string | null
  publish_at: string | null
}

export interface PublishItem {
  id: string
  batch_id: string
  channel_id: string
  job_id: string
  clip_file: string
  clip_id: string | null
  source_id: string | null
  file_size: number
  details: VideoDetails
  status: PublishStatus
  progress: number
  attempts: number
  video_id: string | null
  video_url: string | null
  error: string | null
  note: string | null
  created: number
  updated: number
  scheduled: boolean
}

export interface ClipToPublish {
  job_id: string
  clip_file: string
  clip_id?: string | null
  source_id?: string | null
  title: string
  description: string
  hashtags: string[]
  tags: string[]
  category_id: string
  language?: string | null
}

export interface PublishRequest {
  channel_id?: string | null
  clips: ClipToPublish[]
  privacy: Privacy
  made_for_kids: boolean
  publish_at?: string | null
  allow_duplicate?: boolean
  confirmed: boolean
}

export interface PublishCreated {
  batch_id: string
  task_id: string
  items: PublishItem[]
}

// ------------------------------------------------------------------ metadata

export type FieldName =
  | 'title'
  | 'title_variants'
  | 'description'
  | 'hashtags'
  | 'tags'
  | 'hook'
  | 'pinned_comment'
  | 'category'
  | 'seo_keywords'
export type Origin = 'ai' | 'fallback' | 'edited'

export interface TitleVariant {
  title: string
  reason: string
}

export interface ClipMetadata {
  title: string
  title_variants: TitleVariant[]
  title_reason: string
  description: string
  hashtags: string[]
  tags: string[]
  hook: string
  pinned_comment: string
  language: string
  category_id: string
  category: string
  seo_keywords: string[]
}

export interface MetadataRecord {
  clip_id: string
  source_id: string
  start: number
  end: number
  metadata: ClipMetadata
  origin: Partial<Record<FieldName, Origin>>
  provider: 'gemini' | 'fallback'
  note: string | null
  variant: number
  updated: number
  /** Approved for publishing; any edit or regeneration withdraws it. */
  approved?: boolean
  approved_by?: 'user' | 'auto' | null
}

export interface MetadataOptions {
  languages: { code: string; label: string }[]
  categories: Record<string, string>
}

export interface ClipRef {
  clip_id: string
  start: number
  end: number
  title?: string
  hook_text?: string
  keywords?: string[]
}

const enc = encodeURIComponent

export const youtubeApi = {
  status: () => getJson<YouTubeStatus>('/api/youtube/status'),
  startOAuth: (returnTo = '/publish') =>
    postJson<{ auth_url: string }>('/api/youtube/oauth/start', { return_to: returnTo }),
  disconnect: (channelId?: string | null) =>
    postJson<{ disconnected: boolean; revoked: boolean }>('/api/youtube/disconnect', { channel_id: channelId ?? null }),
  setDefault: (channelId: string) => postJson<YouTubeStatus>(`/api/youtube/channels/${enc(channelId)}/default`, {}),
  publish: (req: PublishRequest) => postJson<PublishCreated>('/api/youtube/publish', req),
  items: () => getJson<PublishItem[]>('/api/youtube/publish'),
  scheduled: () => getJson<PublishItem[]>('/api/youtube/scheduled'),
  cancel: (id: string) => postJson<PublishItem>(`/api/youtube/publish/${enc(id)}/cancel`, {}),
  retry: (id: string) => postJson<PublishCreated>(`/api/youtube/publish/${enc(id)}/retry`, {}),
  remove: async (id: string) => {
    await send(`/api/youtube/publish/${enc(id)}`, { method: 'DELETE' })
  },
}

export const metadataApi = {
  options: () => getJson<MetadataOptions>('/api/metadata/options'),
  get: (sourceId: string) =>
    getJson<{ source_id: string; clips: Record<string, MetadataRecord> }>(`/api/metadata/${enc(sourceId)}`),
  generate: (sourceId: string, clips: ClipRef[], language: string) =>
    postWithAi<{ source_id: string; clips: Record<string, MetadataRecord> }>(`/api/metadata/${enc(sourceId)}/generate`, {
      clips,
      language,
    }),
  regenerate: (sourceId: string, clipId: string, field: FieldName | null, language?: string) =>
    postWithAi<MetadataRecord>(`/api/metadata/${enc(sourceId)}/${enc(clipId)}/regenerate`, {
      field,
      language: language ?? null,
    }),
  approve: (sourceId: string, clipId: string, approved: boolean) =>
    postJson<MetadataRecord>(`/api/metadata/${enc(sourceId)}/${enc(clipId)}/approve`, { approved }),
  edit: async (sourceId: string, clipId: string, changes: Partial<ClipMetadata>): Promise<MetadataRecord> =>
    (await send(`/api/metadata/${enc(sourceId)}/${enc(clipId)}`, { ...json({ changes }), method: 'PUT' })).json(),
}
