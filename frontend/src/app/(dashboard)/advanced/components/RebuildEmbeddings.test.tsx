import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RebuildEmbeddings } from './RebuildEmbeddings'
import { embeddingApi } from '@/lib/api/embedding'

// LOCAL: the Rebuild Embeddings card gained a third mode, "missing" (only
// records without a vector yet). These tests cover that addition.

vi.mock('@/lib/api/embedding', () => ({
  embeddingApi: {
    rebuildEmbeddings: vi.fn(),
    getRebuildStatus: vi.fn(),
  },
}))

vi.mock('@/lib/hooks/use-translation', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))

// Radix Select renders through a portal; a plain <select> keeps these tests
// about the card's own logic rather than the primitive's.
vi.mock('@/components/ui/select', () => ({
  Select: ({
    children,
    value,
    onValueChange,
  }: {
    children: React.ReactNode
    value: string
    onValueChange: (value: string) => void
  }) => (
    <select data-testid="mode" value={value} onChange={(event) => onValueChange(event.target.value)}>
      {children}
    </select>
  ),
  SelectTrigger: () => null,
  SelectValue: () => null,
  SelectContent: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  SelectItem: ({ value, children }: { value: string; children: React.ReactNode }) => (
    <option value={value}>{children}</option>
  ),
}))

function renderCard() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <RebuildEmbeddings />
    </QueryClientProvider>
  )
}

describe('RebuildEmbeddings mode picker', () => {
  beforeEach(() => {
    vi.mocked(embeddingApi.rebuildEmbeddings).mockReset()
  })

  it('offers Existing, All and Missing, opening on Existing', () => {
    renderCard()

    const values = screen.getAllByRole('option').map((option) => (option as HTMLOptionElement).value)
    expect(values).toEqual(['existing', 'all', 'missing'])
    expect((screen.getByTestId('mode') as HTMLSelectElement).value).toBe('existing')
  })

  it('explains the Missing mode once it is picked', () => {
    renderCard()

    fireEvent.change(screen.getByTestId('mode'), { target: { value: 'missing' } })

    expect(screen.getByText(/no embedding yet/i)).toBeInTheDocument()
  })

  it('sends mode "missing" to the API', async () => {
    vi.mocked(embeddingApi.rebuildEmbeddings).mockResolvedValue({
      command_id: 'command:1',
      message: '',
      estimated_items: 2,
    })
    renderCard()

    fireEvent.change(screen.getByTestId('mode'), { target: { value: 'missing' } })
    fireEvent.click(screen.getByRole('button', { name: 'advanced.rebuild.startBtn' }))

    await waitFor(() => expect(embeddingApi.rebuildEmbeddings).toHaveBeenCalledTimes(1))
    expect(vi.mocked(embeddingApi.rebuildEmbeddings).mock.calls[0][0]).toMatchObject({
      mode: 'missing',
      include_sources: true,
      include_notes: true,
      include_insights: true,
    })
  })
})
