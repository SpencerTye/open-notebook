import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { BatchTransformPanel } from './BatchTransformPanel'
import {
  useApplyBatchTransformation,
  useBatchTransformationStatus,
} from '@/lib/hooks/use-batch-transformations'
import type { BatchTransformationStatus } from '@/lib/api/batch-transformations'

vi.mock('@/lib/hooks/use-batch-transformations', () => ({
  useBatchTransformationStatus: vi.fn(),
  useApplyBatchTransformation: vi.fn(),
}))

// Radix Select renders through a portal; a plain <select> keeps these tests
// about the panel's own logic rather than the primitive's.
vi.mock('@/components/ui/select', () => ({
  Select: ({
    children,
    value,
    onValueChange,
    disabled,
  }: {
    children: React.ReactNode
    value: string
    onValueChange: (value: string) => void
    disabled?: boolean
  }) => (
    <select
      data-testid="picker"
      value={value}
      disabled={disabled}
      onChange={(event) => onValueChange(event.target.value)}
    >
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

const NOTEBOOK = 'notebook:n1'

const status = (
  over: Partial<BatchTransformationStatus> = {}
): BatchTransformationStatus => ({
  notebook_id: NOTEBOOK,
  total_sources: 96,
  transformations: [
    {
      id: 'transformation:dense',
      name: 'dense',
      title: 'Dense Summary',
      applied: 25,
      missing: 71,
    },
    {
      id: 'transformation:toc',
      name: 'toc',
      title: 'Table of Contents',
      applied: 96,
      missing: 0,
    },
  ],
  ...over,
})

const mutate = vi.fn()

function arrange(
  data: BatchTransformationStatus | undefined,
  flags: { isLoading?: boolean; isError?: boolean; isPending?: boolean } = {}
) {
  vi.mocked(useBatchTransformationStatus).mockReturnValue({
    data,
    isLoading: flags.isLoading ?? data === undefined,
    isError: flags.isError ?? false,
  } as ReturnType<typeof useBatchTransformationStatus>)
  vi.mocked(useApplyBatchTransformation).mockReturnValue({
    mutate,
    isPending: flags.isPending ?? false,
  } as unknown as ReturnType<typeof useApplyBatchTransformation>)
}

describe('BatchTransformPanel', () => {
  beforeEach(() => {
    mutate.mockReset()
  })

  it('renders nothing when the notebook has no sources', () => {
    arrange(status({ total_sources: 0, transformations: [] }))

    const { container } = render(<BatchTransformPanel notebookId={NOTEBOOK} />)

    expect(container).toBeEmptyDOMElement()
  })

  it('preselects the transformation with the most sources outstanding', () => {
    arrange(status())

    render(<BatchTransformPanel notebookId={NOTEBOOK} />)

    expect(screen.getByTestId('picker')).toHaveValue('transformation:dense')
    expect(screen.getByRole('button', { name: /Apply to 71/ })).toBeEnabled()
  })

  it('states how many sources already have the selected insight', () => {
    arrange(status())

    render(<BatchTransformPanel notebookId={NOTEBOOK} />)

    expect(
      screen.getByText(/25 of 96 sources already have .*Dense Summary/)
    ).toBeInTheDocument()
  })

  it('queues the selected transformation when Apply is clicked', () => {
    arrange(status())

    render(<BatchTransformPanel notebookId={NOTEBOOK} />)
    fireEvent.click(screen.getByRole('button', { name: /Apply to 71/ }))

    expect(mutate).toHaveBeenCalledWith('transformation:dense')
  })

  it('disables Apply for a transformation every source already has', () => {
    arrange(status())

    render(<BatchTransformPanel notebookId={NOTEBOOK} />)
    fireEvent.change(screen.getByTestId('picker'), {
      target: { value: 'transformation:toc' },
    })

    const button = screen.getByRole('button', { name: /All done/ })
    expect(button).toBeDisabled()
    fireEvent.click(button)
    expect(mutate).not.toHaveBeenCalled()
  })

  it('shows each transformation with its own outstanding count', () => {
    arrange(status())

    render(<BatchTransformPanel notebookId={NOTEBOOK} />)

    expect(screen.getByText('71 of 96 missing')).toBeInTheDocument()
    expect(screen.getByText('all 96 done')).toBeInTheDocument()
  })

  it('reports a failed status read instead of an empty bar', () => {
    arrange(undefined, { isLoading: false, isError: true })

    render(<BatchTransformPanel notebookId={NOTEBOOK} />)

    expect(
      screen.getByText('Could not read transformation counts.')
    ).toBeInTheDocument()
  })
})
