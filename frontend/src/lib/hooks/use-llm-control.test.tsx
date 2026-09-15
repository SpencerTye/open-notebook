import { renderHook, waitFor, act } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useLlmStatus, useLlmModelAction, LLM_QUERY_KEYS } from './use-llm-control'
import { llmControlApi } from '@/lib/api/llm-control'

vi.mock('@/lib/api/llm-control', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api/llm-control')>()
  return {
    ...actual,
    llmControlApi: {
      getStatus: vi.fn(),
      load: vi.fn(),
      unload: vi.fn(),
    },
  }
})

const STATUS = {
  server_url: 'http://router',
  reachable: true,
  error: null,
  gpu: { used_mib: 1, total_mib: 2 },
  models: [
    {
      id: 'g',
      status: 'loaded',
      failed: false,
      is_embedding: false,
      size_bytes: null,
      n_ctx: null,
      input_modalities: ['text'],
    },
  ],
}

function wrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return {
    client,
    Wrapper: ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  }
}

describe('use-llm-control', () => {
  beforeEach(() => {
    vi.mocked(llmControlApi.getStatus).mockReset().mockResolvedValue(STATUS)
    vi.mocked(llmControlApi.load)
      .mockReset()
      .mockResolvedValue({ success: true, model: 'g', status: 'loading' })
    vi.mocked(llmControlApi.unload)
      .mockReset()
      .mockResolvedValue({ success: true, model: 'g', status: 'unloaded' })
  })

  it('useLlmStatus returns the status reported by the API', async () => {
    const { Wrapper } = wrapper()
    const { result } = renderHook(() => useLlmStatus(), { wrapper: Wrapper })
    await waitFor(() => expect(result.current.data).toEqual(STATUS))
    expect(llmControlApi.getStatus).toHaveBeenCalledTimes(1)
  })

  it('useLlmModelAction calls unload or load for the given model and refreshes the status', async () => {
    const { Wrapper, client } = wrapper()
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    const { result } = renderHook(() => useLlmModelAction(), { wrapper: Wrapper })

    await act(async () => {
      await result.current.mutateAsync({ model: 'g', action: 'unload' })
    })
    expect(llmControlApi.unload).toHaveBeenCalledWith('g')

    await act(async () => {
      await result.current.mutateAsync({ model: 'g', action: 'load' })
    })
    expect(llmControlApi.load).toHaveBeenCalledWith('g')
    expect(invalidate).toHaveBeenCalledWith({ queryKey: LLM_QUERY_KEYS.status })
  })

  it('useLlmModelAction keeps refreshing the status for a few seconds after an action', async () => {
    // The model server answers the unload/load request before the change is
    // visible in its model list, so one refresh right after the request is
    // not enough; the panel must look again over the next seconds.
    vi.useFakeTimers()
    try {
      const { Wrapper, client } = wrapper()
      const invalidate = vi.spyOn(client, 'invalidateQueries')
      const { result } = renderHook(() => useLlmModelAction(), { wrapper: Wrapper })
      await act(async () => {
        await result.current.mutateAsync({ model: 'g', action: 'unload' })
      })
      const rightAfter = invalidate.mock.calls.length
      expect(rightAfter).toBeGreaterThan(0)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(12_000)
      })
      expect(invalidate.mock.calls.length).toBeGreaterThan(rightAfter)
    } finally {
      vi.useRealTimers()
    }
  })

  it('useLlmModelAction exposes which model has a request in flight', async () => {
    const { Wrapper } = wrapper()
    let release: () => void = () => {}
    vi.mocked(llmControlApi.unload).mockReturnValue(
      new Promise((resolve) => {
        release = () => resolve({ success: true, model: 'g', status: 'unloaded' })
      })
    )
    const { result } = renderHook(() => useLlmModelAction(), { wrapper: Wrapper })
    act(() => {
      result.current.mutate({ model: 'g', action: 'unload' })
    })
    await waitFor(() => expect(result.current.pendingModel).toBe('g'))
    await act(async () => {
      release()
    })
    await waitFor(() => expect(result.current.pendingModel).toBeNull())
  })
})
