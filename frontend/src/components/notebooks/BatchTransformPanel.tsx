'use client'

// LOCAL addition (not upstream): run one transformation across every source in
// a notebook that does not already have its insight.
//
// Upstream only offers the per-source route (open a source, pick a
// transformation, run it), which is unusable for a reference library of ~100
// papers. This bar sits above the source list: pick a transformation, read how
// many sources are missing it, queue them all in one click.
//
// The "missing" count is also the progress display. Nothing on a source row
// changes while its transformation is queued — a source's badge reports its
// extraction command and nothing else — so the count falling is the only signal
// the app can give. It refreshes on its own every 15 seconds.
//
// English only on purpose: this build has a single user, so the upstream rule
// "every string in all 14 locales" is not applied here.

import { useEffect, useMemo, useState } from 'react'
import { Loader2, Sparkles } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  useApplyBatchTransformation,
  useBatchTransformationStatus,
} from '@/lib/hooks/use-batch-transformations'

const TEXT = {
  label: 'Apply to all sources',
  placeholder: 'Pick a transformation…',
  apply: 'Apply',
  queueing: 'Queueing…',
  allDone: 'All done',
  none: 'No transformations defined.',
  loadFailed: 'Could not read transformation counts.',
} as const

interface BatchTransformPanelProps {
  notebookId: string
}

function missingLabel(missing: number, total: number): string {
  if (total === 0) return 'no sources'
  if (missing === 0) return `all ${total} done`
  return `${missing} of ${total} missing`
}

export function BatchTransformPanel({ notebookId }: BatchTransformPanelProps) {
  const { data, isLoading, isError } = useBatchTransformationStatus(notebookId)
  const applyBatch = useApplyBatchTransformation(notebookId)
  const [selectedId, setSelectedId] = useState<string>('')

  // Memoised so the effect below sees a stable array between renders.
  const transformations = useMemo(() => data?.transformations ?? [], [data])
  const total = data?.total_sources ?? 0

  // Keep the picker on a transformation that still exists; default to the one
  // with the most work outstanding so the common case is one click.
  useEffect(() => {
    if (transformations.length === 0) return
    if (transformations.some((item) => item.id === selectedId)) return
    const busiest = [...transformations].sort((a, b) => b.missing - a.missing)[0]
    setSelectedId(busiest.id)
  }, [transformations, selectedId])

  // A notebook with no sources has nothing to apply anything to.
  if (!isLoading && !isError && total === 0) return null

  const selected = transformations.find((item) => item.id === selectedId)
  const missing = selected?.missing ?? 0
  const canApply = Boolean(selected) && missing > 0 && !applyBatch.isPending

  return (
    <div className="flex flex-col gap-1.5 px-1 pb-3">
      <div className="flex items-center gap-2">
        <Select
          value={selectedId}
          onValueChange={setSelectedId}
          disabled={isLoading || isError || transformations.length === 0}
        >
          <SelectTrigger className="h-8 flex-1 text-xs" aria-label={TEXT.label}>
            <SelectValue placeholder={TEXT.placeholder} />
          </SelectTrigger>
          <SelectContent>
            {transformations.map((item) => (
              <SelectItem key={item.id} value={item.id} className="text-xs">
                <span className="flex w-full items-center justify-between gap-3">
                  <span>{item.title}</span>
                  <span className="text-muted-foreground">
                    {missingLabel(item.missing, total)}
                  </span>
                </span>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Button
          size="sm"
          className="h-8"
          disabled={!canApply}
          onClick={() => selected && applyBatch.mutate(selected.id)}
        >
          {applyBatch.isPending ? (
            <>
              <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />
              {TEXT.queueing}
            </>
          ) : (
            <>
              <Sparkles className="mr-2 h-3.5 w-3.5" />
              {missing > 0 ? `${TEXT.apply} to ${missing}` : TEXT.allDone}
            </>
          )}
        </Button>
      </div>

      <p className="text-xs text-muted-foreground">
        {isError
          ? TEXT.loadFailed
          : isLoading
            ? '…'
            : transformations.length === 0
              ? TEXT.none
              : selected
                ? `${selected.applied} of ${total} sources already have “${selected.title}”.`
                : ''}
      </p>
    </div>
  )
}
