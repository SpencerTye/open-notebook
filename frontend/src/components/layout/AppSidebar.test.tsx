/* eslint-disable @typescript-eslint/no-explicit-any */
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { AppSidebar } from './AppSidebar'
import { useSidebarStore } from '@/lib/stores/sidebar-store'

// Mock Tooltip components to avoid Radix UI async issues in tests
vi.mock('@/components/ui/tooltip', () => ({
  TooltipProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  Tooltip: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  TooltipTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  TooltipContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}))

// LOCAL: the sidebar hosts the model-memory (VRAM) control, which needs a
// QueryClient; stand it in here so the sidebar tests stay independent of it.
vi.mock('@/components/common/LlmControl', () => ({
  LlmControl: ({ collapsed }: { collapsed?: boolean }) => (
    <div data-testid="llm-control" data-collapsed={collapsed ? 'true' : 'false'} />
  ),
}))

describe('AppSidebar', () => {
  it('renders correctly when expanded', () => {
    render(<AppSidebar />)

    // With mocked t() returning keys, check for translation key strings
    expect(screen.getByText('common.appName')).toBeDefined()
    expect(screen.getByText('navigation.sources')).toBeDefined()
    expect(screen.getByText('navigation.notebooks')).toBeDefined()
  })

  it('toggles collapse state when clicking handle', () => {
    const toggleCollapse = vi.fn()
    vi.mocked(useSidebarStore).mockReturnValue({
      isCollapsed: false,
      toggleCollapse,
    } as any)

    render(<AppSidebar />)

    fireEvent.click(screen.getByTestId('sidebar-toggle'))

    expect(toggleCollapse).toHaveBeenCalled()
  })

  it('shows collapsed view when isCollapsed is true', () => {
    vi.mocked(useSidebarStore).mockReturnValue({
      isCollapsed: true,
      toggleCollapse: vi.fn(),
    } as any)

    render(<AppSidebar />)

    // In collapsed mode, app name shouldn't be visible (as text)
    expect(screen.queryByText('common.appName')).toBeNull()
  })

  // LOCAL: on a short window the sidebar's content is taller than the window.
  // The app shell clips overflow, so without these rules the bottom block
  // (theme, language, sign out) is pushed out of view with no way to reach it
  // (seen at 1366x768 on 2026-09-15). The menu section must scroll instead,
  // and the bottom block must keep its size.
  it('lets the menu section scroll on short windows and keeps the bottom block its full size', () => {
    vi.mocked(useSidebarStore).mockReturnValue({
      isCollapsed: false,
      toggleCollapse: vi.fn(),
    } as any)

    const { container } = render(<AppSidebar />)

    const nav = container.querySelector('nav')
    expect(nav).not.toBeNull()
    expect(nav!.className).toContain('min-h-0')
    expect(nav!.className).toContain('overflow-y-auto')
    const bottomBlock = nav!.nextElementSibling as HTMLElement
    expect(bottomBlock.className).toContain('shrink-0')
  })

  it('hosts the model-memory control and tells it the collapse state', () => {
    vi.mocked(useSidebarStore).mockReturnValue({
      isCollapsed: true,
      toggleCollapse: vi.fn(),
    } as any)

    render(<AppSidebar />)

    expect(screen.getByTestId('llm-control')).toHaveAttribute('data-collapsed', 'true')
  })
})
