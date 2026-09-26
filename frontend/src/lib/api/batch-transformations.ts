// LOCAL addition (not upstream): client for the notebook-wide transformation
// endpoints. Upstream can only run a transformation on one source at a time.
// Backend: api/routers/batch_transformations.py
import apiClient from './client'

export interface BatchTransformationCounts {
  id: string
  name?: string | null
  /** Also the insight_type written on every insight this transformation makes. */
  title: string
  /** Sources in this notebook that already have this insight. */
  applied: number
  /** Sources in this notebook that do not. */
  missing: number
}

export interface BatchTransformationStatus {
  notebook_id: string
  total_sources: number
  transformations: BatchTransformationCounts[]
}

export interface BatchTransformationApplyResponse {
  notebook_id: string
  transformation_id: string
  title: string
  total_sources: number
  skipped: number
  queued: number
  failed: number
}

export const batchTransformationsApi = {
  getStatus: async (notebookId: string): Promise<BatchTransformationStatus> => {
    const response = await apiClient.get<BatchTransformationStatus>(
      `/notebooks/${notebookId}/batch-transformations`
    )
    return response.data
  },

  apply: async (
    notebookId: string,
    transformationId: string
  ): Promise<BatchTransformationApplyResponse> => {
    const response = await apiClient.post<BatchTransformationApplyResponse>(
      `/notebooks/${notebookId}/batch-transformations/${transformationId}/apply`
    )
    return response.data
  },
}
