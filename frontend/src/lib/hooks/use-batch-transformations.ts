// LOCAL addition (not upstream): TanStack Query hooks for notebook-wide
// transformations. Follows the shape of the other hooks in this folder.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { isAxiosError } from 'axios'
import {
  batchTransformationsApi,
  type BatchTransformationStatus,
} from '@/lib/api/batch-transformations'
import { useToast } from '@/lib/hooks/use-toast'

export const BATCH_TRANSFORMATION_QUERY_KEYS = {
  status: (notebookId: string) =>
    ['batch-transformations', 'status', notebookId] as const,
}

// The counts only move when the worker finishes a job, and jobs take tens of
// seconds each, so a slow poll is enough to watch a batch drain. It is also the
// only progress signal the app has: nothing on a source row changes while its
// transformation is queued.
const REFRESH_MS = 15_000

/** Per-transformation applied/missing counts for one notebook. */
export function useBatchTransformationStatus(notebookId: string, enabled = true) {
  return useQuery<BatchTransformationStatus>({
    queryKey: BATCH_TRANSFORMATION_QUERY_KEYS.status(notebookId),
    queryFn: () => batchTransformationsApi.getStatus(notebookId),
    enabled: enabled && Boolean(notebookId),
    staleTime: 0,
    refetchInterval: REFRESH_MS,
  })
}

function describeError(error: unknown): string {
  if (isAxiosError(error)) {
    const detail = error.response?.data?.detail
    if (typeof detail === 'string' && detail.length > 0) return detail
    return error.message
  }
  return error instanceof Error ? error.message : String(error)
}

/**
 * Queue one transformation across every source in the notebook that lacks it.
 * Safe to repeat: the backend skips sources that already have the insight.
 */
export function useApplyBatchTransformation(notebookId: string) {
  const queryClient = useQueryClient()
  const { toast } = useToast()

  return useMutation({
    mutationFn: (transformationId: string) =>
      batchTransformationsApi.apply(notebookId, transformationId),
    onSuccess: (data) => {
      const parts = [`${data.queued} queued`]
      if (data.skipped > 0) parts.push(`${data.skipped} already had it`)
      if (data.failed > 0) parts.push(`${data.failed} could not be queued`)
      toast({
        title: `${data.title}: ${parts.join(', ')}`,
        description:
          data.queued > 0
            ? 'The worker runs them one at a time. The chat model must be loaded.'
            : 'Nothing to do — every source already has this insight.',
        variant: data.failed > 0 ? 'destructive' : 'default',
      })
    },
    onError: (error) => {
      toast({
        title: 'Could not start the batch',
        description: describeError(error),
        variant: 'destructive',
      })
    },
    onSettled: () => {
      queryClient.invalidateQueries({
        queryKey: BATCH_TRANSFORMATION_QUERY_KEYS.status(notebookId),
      })
    },
  })
}
