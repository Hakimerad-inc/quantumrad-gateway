/** S10-T7 (RED): Disk-full dashboard — capacity card + 90% warning banner. */

import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { Dashboard } from './App';
import type { DiskStatus } from './api';

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>();
  return {
    ...actual,
    fetchSystemStatus: vi.fn().mockRejectedValue(new Error('n/a')),
    fetchQueueStats: vi.fn().mockRejectedValue(new Error('n/a')),
    fetchDiskStatus: vi.fn().mockRejectedValue(new Error('n/a')),
  };
});

import * as api from './api';

function disk(over: Partial<DiskStatus>): DiskStatus {
  return {
    usage_pct: 50,
    total_bytes: 100_000_000_000,
    used_bytes: 30_000_000_000,
    free_bytes: 50_000_000_000,
    warning_pct: 90,
    over_threshold: false,
    purge_on_disk_full: false,
    ...over,
  };
}

describe('Dashboard storage (S10-T7)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows a capacity gauge plus available/used GB without a warning under threshold', async () => {
    vi.mocked(api.fetchDiskStatus).mockResolvedValue(disk({}));

    render(<Dashboard />);

    expect(await screen.findByText('Storage')).toBeInTheDocument();
    expect(screen.getByText('50.0%')).toBeInTheDocument();
    expect(screen.getByText('warning at 90%')).toBeInTheDocument();
    expect(screen.getByText('50.0 GB')).toBeInTheDocument(); // available
    expect(screen.getByText('disabled')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('renders a warning banner and marks auto-purge as armed when over threshold', async () => {
    vi.mocked(api.fetchDiskStatus).mockResolvedValue(
      disk({ usage_pct: 95, over_threshold: true, warning_pct: 90, purge_on_disk_full: true }),
    );

    render(<Dashboard />);

    const banner = await screen.findByRole('alert');
    expect(banner).toHaveTextContent('95.0%');
    expect(banner).toHaveTextContent('90%');
    expect(banner).toHaveTextContent('auto-purge of oldest delivered studies is armed');
    expect(screen.getByText('armed')).toBeInTheDocument();
  });
});