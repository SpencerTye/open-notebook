// LOCAL addition (not upstream): client for the /api/llm/* endpoints that
// show and change which models the llama.cpp server holds in GPU memory.
// Backend: api/routers/llm_control.py
import apiClient from './client'

export interface LlmModelState {
  id: string
  /** loaded | loading | unloaded | sleeping | downloading | unknown */
  status: string
  failed: boolean
  is_embedding: boolean
  size_bytes?: number | null
  n_ctx?: number | null
  input_modalities: string[]
}

export interface LlmGpuMemory {
  used_mib: number
  total_mib: number
}

export interface LlmStatus {
  server_url: string
  reachable: boolean
  error?: string | null
  models: LlmModelState[]
  gpu?: LlmGpuMemory | null
}

export interface LlmModelActionResponse {
  success: boolean
  model: string
  status: string
}

export type LlmModelAction = 'load' | 'unload'

/** States in which the server is still working on a model (poll faster, no switching). */
const BUSY_STATES = new Set(['loading', 'downloading'])

export function isModelBusy(status: string): boolean {
  return BUSY_STATES.has(status)
}

export const llmControlApi = {
  getStatus: async (): Promise<LlmStatus> => {
    const response = await apiClient.get<LlmStatus>('/llm/status')
    return response.data
  },

  load: async (model: string): Promise<LlmModelActionResponse> => {
    const response = await apiClient.post<LlmModelActionResponse>('/llm/load', { model })
    return response.data
  },

  unload: async (model: string): Promise<LlmModelActionResponse> => {
    const response = await apiClient.post<LlmModelActionResponse>('/llm/unload', { model })
    return response.data
  },
}
