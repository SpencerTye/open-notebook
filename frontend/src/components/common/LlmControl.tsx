'use client'

// LOCAL addition (not upstream): the model-memory (VRAM) control in the
// sidebar. One switch per model the llama.cpp server knows about: on = the
// model's weights are in GPU memory, off = they are not. Flipping a switch
// asks the server to load or unload that model; the status underneath is what
// the server reports, refreshed every few seconds.
//
// English only on purpose: this build serves one operator, so the upstream
// rule "every string in all 14 locales" is not applied here.

import { MemoryStick } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { useLlmStatus, useLlmModelAction } from '@/lib/hooks/use-llm-control'
import { isModelBusy, type LlmModelState } from '@/lib/api/llm-control'

const TEXT = {
  title: 'Model memory',
  gpu: 'GPU memory in use',
  chat: 'Chat model',
  embedding: 'Embedding model',
  unreachable: 'Model server unreachable',
  checking: 'checking…',
  loaded: 'loaded',
  loading: 'loading…',
  unloading: 'unloading…',
  downloading: 'downloading…',
  sleeping: 'sleeping',
  unloaded: 'unloaded',
  failed: 'load failed',
  noModels: 'The model server lists no models.',
} as const

function formatGb(mib: number): string {
  return (mib / 1024).toFixed(1)
}

function modelLabel(model: LlmModelState): string {
  return model.is_embedding ? TEXT.embedding : TEXT.chat
}

function isOn(model: LlmModelState): boolean {
  return model.status === 'loaded' || model.status === 'loading'
}

function statusText(model: LlmModelState, inFlight: boolean): string {
  if (inFlight) return isOn(model) ? TEXT.unloading : TEXT.loading
  if (model.failed && model.status !== 'loaded') return TEXT.failed
  switch (model.status) {
    case 'loaded':
      return TEXT.loaded
    case 'loading':
      return TEXT.loading
    case 'downloading':
      return TEXT.downloading
    case 'sleeping':
      return TEXT.sleeping
    default:
      return TEXT.unloaded
  }
}

function statusColor(model: LlmModelState, inFlight: boolean): string {
  if (inFlight || isModelBusy(model.status)) return 'text-amber-600 dark:text-amber-400'
  if (model.failed && model.status !== 'loaded') return 'text-destructive'
  if (model.status === 'loaded') return 'text-green-600 dark:text-green-400'
  return 'text-muted-foreground'
}

interface SwitchProps {
  checked: boolean
  disabled?: boolean
  label: string
  onClick: () => void
}

function Switch({ checked, disabled = false, label, onClick }: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        'relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border-2 border-transparent transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
        'disabled:cursor-not-allowed disabled:opacity-50',
        checked ? 'bg-primary' : 'bg-muted-foreground/40'
      )}
    >
      <span
        className={cn(
          'pointer-events-none block h-4 w-4 rounded-full bg-background shadow-sm transition-transform',
          checked ? 'translate-x-4' : 'translate-x-0'
        )}
      />
    </button>
  )
}

function Panel() {
  const { data, isLoading } = useLlmStatus()
  const action = useLlmModelAction()

  if (isLoading || !data) {
    return <p className="text-xs text-muted-foreground">{TEXT.checking}</p>
  }

  const gpuText = data.gpu
    ? `${formatGb(data.gpu.used_mib)} / ${formatGb(data.gpu.total_mib)} GB`
    : '—'

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
        <span>{TEXT.gpu}</span>
        <span className="font-mono">{gpuText}</span>
      </div>

      {!data.reachable ? (
        <p className="text-xs text-destructive" title={data.error ?? undefined}>
          {TEXT.unreachable}
        </p>
      ) : data.models.length === 0 ? (
        <p className="text-xs text-muted-foreground">{TEXT.noModels}</p>
      ) : (
        data.models.map((model) => {
          const inFlight = action.pendingModel === model.id
          const busy = inFlight || isModelBusy(model.status)
          const on = isOn(model)
          const label = modelLabel(model)
          return (
            <div key={model.id} className="flex items-center gap-2">
              <Switch
                checked={on}
                disabled={busy}
                label={label}
                onClick={() => action.mutate({ model: model.id, action: on ? 'unload' : 'load' })}
              />
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-2 text-xs">
                  <span className="truncate text-sidebar-foreground">{label}</span>
                  <span className={cn('shrink-0', statusColor(model, inFlight))}>
                    {statusText(model, inFlight)}
                  </span>
                </div>
                <div
                  className="truncate font-mono text-[10px] text-muted-foreground"
                  title={model.id}
                >
                  {model.id}
                </div>
              </div>
            </div>
          )
        })
      )}
    </div>
  )
}

interface LlmControlProps {
  collapsed?: boolean
}

export function LlmControl({ collapsed = false }: LlmControlProps) {
  const { data } = useLlmStatus()
  const chatLoaded = data?.reachable
    ? data.models.some((model) => !model.is_embedding && model.status === 'loaded')
    : false
  const anyBusy = data?.models.some((model) => isModelBusy(model.status)) ?? false

  if (collapsed) {
    return (
      <Popover>
        <PopoverTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className="relative h-9 w-full sidebar-menu-item"
            aria-label={TEXT.title}
          >
            <MemoryStick className="h-[1.2rem] w-[1.2rem]" />
            <span
              aria-hidden="true"
              className={cn(
                'absolute right-1.5 top-1.5 h-2 w-2 rounded-full',
                anyBusy ? 'bg-amber-500' : chatLoaded ? 'bg-green-500' : 'bg-muted-foreground/50'
              )}
            />
          </Button>
        </PopoverTrigger>
        <PopoverContent side="right" align="end" className="w-72">
          <div className="mb-2 flex items-center gap-1.5 text-sm font-medium">
            <MemoryStick className="h-4 w-4" />
            {TEXT.title}
          </div>
          <Panel />
        </PopoverContent>
      </Popover>
    )
  }

  return (
    <div className="rounded-md border border-sidebar-border bg-sidebar-accent/30 px-3 py-2">
      <div className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-sidebar-foreground/60">
        <MemoryStick className="h-3 w-3" />
        {TEXT.title}
      </div>
      <Panel />
    </div>
  )
}
