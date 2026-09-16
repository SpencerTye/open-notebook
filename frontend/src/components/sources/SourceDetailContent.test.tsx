import { render, screen, waitFor, fireEvent, within } from '@testing-library/react' // LOCAL: fireEvent, within
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { SourceDetailContent } from './SourceDetailContent'
import { sourcesApi } from '@/lib/api/sources'
import { insightsApi } from '@/lib/api/insights' // LOCAL
import { QUERY_KEYS } from '@/lib/api/query-client'
import { SourceDetailResponse } from '@/lib/types/api'

// useTranslation is mocked globally in setup.ts (t returns the key string)

vi.mock('@/lib/api/sources', () => ({
  sourcesApi: {
    get: vi.fn(),
  },
}))

vi.mock('@/lib/api/insights', () => ({
  insightsApi: {
    listForSource: vi.fn().mockResolvedValue([]),
  },
}))

vi.mock('@/lib/api/transformations', () => ({
  transformationsApi: {
    list: vi.fn().mockResolvedValue([]),
  },
}))

vi.mock('@/lib/api/embedding', () => ({
  embeddingApi: {
    embedSource: vi.fn(),
  },
}))

vi.mock('@/components/sources/SourceInsightDialog', () => ({
  SourceInsightDialog: () => null,
}))

vi.mock('@/components/sources/NotebookAssociations', () => ({
  NotebookAssociations: () => null,
}))

const mockSourcesGet = vi.mocked(sourcesApi.get)

const notFoundError = Object.assign(new Error('Request failed with status code 404'), {
  isAxiosError: true,
  response: { status: 404 },
})

const networkError = Object.assign(new Error('Network Error'), {
  isAxiosError: true,
  response: undefined,
})

function renderContent(onClose?: () => void) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <SourceDetailContent sourceId="source:missing" onClose={onClose} />
    </QueryClientProvider>
  )
}

describe('SourceDetailContent', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows the shared not-found state when the source returns 404', async () => {
    mockSourcesGet.mockRejectedValue(notFoundError)

    renderContent()

    await waitFor(() => {
      expect(screen.getByTestId('content-unavailable')).toBeInTheDocument()
    })
    expect(screen.getByText('common.contentUnavailable.notFoundTitle')).toBeInTheDocument()
    expect(screen.getByText('common.contentUnavailable.notFoundDescription')).toBeInTheDocument()
  })

  it('shows the shared load-error state for non-404 failures', async () => {
    mockSourcesGet.mockRejectedValue(networkError)

    renderContent()

    await waitFor(() => {
      expect(screen.getByTestId('content-unavailable')).toBeInTheDocument()
    })
    expect(screen.getByText('common.contentUnavailable.errorTitle')).toBeInTheDocument()
    expect(
      screen.queryByText('common.contentUnavailable.notFoundTitle')
    ).not.toBeInTheDocument()
  })

  it('shows the not-found state over stale cached data when a refetch returns 404', async () => {
    // Simulates the orphan-reference path: the source was viewed (cached),
    // then deleted; reopening it serves the retained cache while the
    // background refetch 404s. React Query keeps the previous data alongside
    // the error — the definitive 404 must still win over the stale render.
    mockSourcesGet.mockRejectedValue(notFoundError)

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const cachedSource: SourceDetailResponse = {
      id: 'source:stale',
      title: 'Deleted but cached',
      asset: null,
      embedded: false,
      embedded_chunks: 0,
      insights_count: 0,
      created: '2026-01-01T00:00:00Z',
      updated: '2026-01-01T00:00:00Z',
      full_text: 'stale content',
    }
    // Mark the cached entry as stale (older than useSource's 30s staleTime)
    // so mounting triggers a refetch, which rejects with the 404 above.
    queryClient.setQueryData(QUERY_KEYS.source('source:stale'), cachedSource, {
      updatedAt: Date.now() - 60_000,
    })

    render(
      <QueryClientProvider client={queryClient}>
        <SourceDetailContent sourceId="source:stale" />
      </QueryClientProvider>
    )

    await waitFor(() => {
      expect(screen.getByTestId('content-unavailable')).toBeInTheDocument()
    })
    expect(screen.getByText('common.contentUnavailable.notFoundTitle')).toBeInTheDocument()
    expect(screen.queryByText('Deleted but cached')).not.toBeInTheDocument()
  })

  it('invokes onClose from the not-found close button', async () => {
    mockSourcesGet.mockRejectedValue(notFoundError)
    const onClose = vi.fn()

    renderContent(onClose)

    await waitFor(() => {
      expect(screen.getByText('common.close')).toBeInTheDocument()
    })
    screen.getByText('common.close').click()
    expect(onClose).toHaveBeenCalled()
  })
})

// LOCAL: each insight on the Insights tab shows whether it has a vector
// (embedded / not embedded), from the `embedded` field the list endpoint
// returns. Nothing else on the tab changes.
describe('SourceDetailContent insights tab, embedded mark (LOCAL)', () => {
  const source: SourceDetailResponse = {
    id: 'source:s',
    title: 'A paper',
    asset: null,
    embedded: true,
    embedded_chunks: 12,
    insights_count: 2,
    created: '2026-01-01T00:00:00Z',
    updated: '2026-01-01T00:00:00Z',
    full_text: 'the text',
  }

  const insight = (id: string, content: string, embedded: boolean) => ({
    id,
    source_id: 'source:s',
    insight_type: 'Paper Analysis',
    content,
    created: null,
    updated: null,
    embedded,
  })

  function renderSource() {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <QueryClientProvider client={queryClient}>
        <SourceDetailContent sourceId="source:s" />
      </QueryClientProvider>
    )
  }

  async function openInsightsTab() {
    const tab = await screen.findByRole('tab', { name: /common\.insights/ })
    // Radix activates a tab on mouse down (left button), not on click.
    fireEvent.mouseDown(tab)
  }

  const cardWith = (content: string) => screen.getByText(content).parentElement as HTMLElement

  beforeEach(() => {
    vi.clearAllMocks()
    mockSourcesGet.mockResolvedValue(source)
  })

  it('marks an insight with a vector as embedded and one without as not embedded', async () => {
    vi.mocked(insightsApi.listForSource).mockResolvedValue([
      insight('source_insight:with_vector', 'one', true),
      insight('source_insight:without_vector', 'two', false),
    ])

    renderSource()
    await openInsightsTab()
    await waitFor(() => expect(screen.getByText('two')).toBeInTheDocument())

    expect(within(cardWith('one')).getByText('sources.embedded')).toBeInTheDocument()
    expect(within(cardWith('one')).queryByText('sources.notEmbedded')).not.toBeInTheDocument()
    expect(within(cardWith('two')).getByText('sources.notEmbedded')).toBeInTheDocument()
    expect(within(cardWith('two')).queryByText('sources.embedded')).not.toBeInTheDocument()
  })

  it('shows no mark when the list does not say', async () => {
    vi.mocked(insightsApi.listForSource).mockResolvedValue([
      { ...insight('source_insight:unknown', 'three', true), embedded: undefined },
    ])

    renderSource()
    await openInsightsTab()
    await waitFor(() => expect(screen.getByText('three')).toBeInTheDocument())

    expect(screen.queryByText('sources.embedded')).not.toBeInTheDocument()
    expect(screen.queryByText('sources.notEmbedded')).not.toBeInTheDocument()
  })
})
