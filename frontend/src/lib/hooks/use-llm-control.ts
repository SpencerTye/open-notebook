// LOCAL addition (not upstream): TanStack Query hooks for the model-memory
// (VRAM) control. Follows the shape of the other hooks in this folder.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { isAxiosError } from 'axios'
import {
  isModelBusy,
  llmControlApi,
  type LlmModelAction,
  type LlmStatus,
} from '@/lib/api/llm-control'
import { useToast } from '@/lib/hooks/use-toast'

export const LLM_QUERY_KEYS = {
  status: ['llm', 'status'] as const,
}

const IDLE_REFRESH_MS = 10_000
const BUSY_REFRESH_MS = 2_000
// The model server answers a load/unload request before its model list shows
// the change, so look again a few times after every action.
const FOLLOW_UP_REFRESH_MS = [2_000, 5_000, 10_000]

/**
 * What the model server holds right now. Polls slowly while idle and quickly
 * while a model is loading, so the switch settles on its own.
 */
export function useLlmStatus() {
  return useQuery({
    queryKey: LLM_QUERY_KEYS.status,
    queryFn: () => llmControlApi.getStatus(),
    staleTime: 0,
    refetchInterval: (query) => {
      const data = query.state.data as LlmStatus | undefined
      const busy = data?.models.some((model) => isModelBusy(model.status)) ?? false
      return busy ? BUSY_REFRESH_MS : IDLE_REFRESH_MS
    },
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

export interface LlmModelActionVariables {
  model: string
  action: LlmModelAction
}

/** Load or unload one model. Refreshes the status afterwards and toasts on failure. */
export function useLlmModelAction() {
  const queryClient = useQueryClient()
  const { toast } = useToast()

  const mutation = useMutation({
    mutationFn: ({ model, action }: LlmModelActionVariables) =>
      action === 'unload' ? llmControlApi.unload(model) : llmControlApi.load(model),
    onError: (error, variables) => {
      toast({
        title: `Could not ${variables.action} ${variables.model}`,
        description: describeError(error),
        variant: 'destructive',
      })
    },
    onSettled: () => {
      const refresh = () => {
        queryClient.invalidateQueries({ queryKey: LLM_QUERY_KEYS.status })
      }
      refresh()
      FOLLOW_UP_REFRESH_MS.forEach((delay) => setTimeout(refresh, delay))
    },
  })

  return {
    ...mutation,
    pendingModel: mutation.isPending ? (mutation.variables?.model ?? null) : null,
  }
}
