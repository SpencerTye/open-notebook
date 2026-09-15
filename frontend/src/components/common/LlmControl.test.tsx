import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { LlmControl } from './LlmControl'
import { useLlmStatus, useLlmModelAction } from '@/lib/hooks/use-llm-control'
import type { LlmStatus } from '@/lib/api/llm-control'

vi.mock('@/lib/hooks/use-llm-control', () => ({
  useLlmStatus: vi.fn(),
  useLlmModelAction: vi.fn(),
}))

// Radix popover renders through a portal; keep the collapsed test simple.
vi.mock('@/components/ui/popover', () => ({
  Popover: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  PopoverTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  PopoverContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}))

const status = (over: Partial<LlmStatus> = {}): LlmStatus => ({
  server_url: 'http://host.docker.internal:8080',
  reachable: true,
  error: null,
  gpu: { used_mib: 17612, total_mib: 24576 },
  models: [
    {
      id: 'gemma-4-26b-a4b',
      status: 'loaded',
      failed: false,
      is_embedding: false,
      size_bytes: 14233222264,
      n_ctx: 65536,
      input_modalities: ['text'],
    },
    {
      id: 'qwen3-embedding-4b',
      status: 'unloaded',
      failed: false,
      is_embedding: true,
      size_bytes: null,
      n_ctx: null,
      input_modalities: ['text'],
    },
  ],
  ...over,
})

const mutate = vi.fn()

function arrange(
  data: LlmStatus | undefined,
  extra: Partial<ReturnType<typeof useLlmModelAction>> = {}
) {
  vi.mocked(useLlmStatus).mockReturnValue({
    data,
    isLoading: data === undefined,
  } as ReturnType<typeof useLlmStatus>)
  vi.mocked(useLlmModelAction).mockReturnValue({
    mutate,
    isPending: false,
    pendingModel: null,
    ...extra,
  } as ReturnType<typeof useLlmModelAction>)
}

describe('LlmControl', () => {
  beforeEach(() => {
    mutate.mockReset()
  })

  it('renders one switch per model, checked when the model is loaded', () => {
    arrange(status())
    render(<LlmControl />)
    const switches = screen.getAllByRole('switch')
    expect(switches).toHaveLength(2)
    expect(switches[0]).toHaveAttribute('aria-checked', 'true')
    expect(switches[1]).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByText('Chat model')).toBeInTheDocument()
    expect(screen.getByText('Embedding model')).toBeInTheDocument()
    expect(screen.getByText('gemma-4-26b-a4b')).toBeInTheDocument()
  })

  it('unloads a loaded model and loads an unloaded one when its switch is clicked', () => {
    arrange(status())
    render(<LlmControl />)
    const [chat, embed] = screen.getAllByRole('switch')
    fireEvent.click(chat)
    expect(mutate).toHaveBeenLastCalledWith({ model: 'gemma-4-26b-a4b', action: 'unload' })
    fireEvent.click(embed)
    expect(mutate).toHaveBeenLastCalledWith({ model: 'qwen3-embedding-4b', action: 'load' })
  })

  it('shows GPU memory in use as gigabytes', () => {
    arrange(status())
    render(<LlmControl />)
    expect(screen.getByText('17.2 / 24.0 GB')).toBeInTheDocument()
  })

  it('disables a switch while its model is loading and says so', () => {
    const s = status()
    s.models[0].status = 'loading'
    arrange(s)
    render(<LlmControl />)
    expect(screen.getAllByRole('switch')[0]).toBeDisabled()
    expect(screen.getByText('loading…')).toBeInTheDocument()
  })

  it('disables the switch of the model whose request is in flight', () => {
    arrange(status(), { isPending: true, pendingModel: 'gemma-4-26b-a4b' })
    render(<LlmControl />)
    const [chat, embed] = screen.getAllByRole('switch')
    expect(chat).toBeDisabled()
    expect(embed).not.toBeDisabled()
  })

  it('reports an unreachable model server instead of showing switches', () => {
    arrange(status({ reachable: false, error: 'connection refused', models: [], gpu: null }))
    render(<LlmControl />)
    expect(screen.queryAllByRole('switch')).toHaveLength(0)
    expect(screen.getByText(/model server unreachable/i)).toBeInTheDocument()
  })

  it('flags a model whose last load failed', () => {
    const s = status()
    s.models[0].status = 'unloaded'
    s.models[0].failed = true
    arrange(s)
    render(<LlmControl />)
    expect(screen.getByText('load failed')).toBeInTheDocument()
  })

  it('collapsed mode shows only an icon button that opens the panel', () => {
    arrange(status())
    render(<LlmControl collapsed />)
    expect(screen.getByRole('button', { name: 'Model memory' })).toBeInTheDocument()
  })
})
