// Inline SVG icon set — single 24×24 grid, 1.5px stroke, currentColor.
// Lucide-style paths kept in one place so stroke width and caps stay
// consistent across the app (no emoji, no mixed icon families).

import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function Icon({ size = 16, children, ...rest }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  );
}

export const IconPulse = (p: IconProps) => (
  <Icon {...p}><path d="M3 12h4l2.5-7 4 14 2.5-7H21" /></Icon>
);

export const IconQueue = (p: IconProps) => (
  <Icon {...p}>
    <path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
    <path d="m15 5 4 4" />
  </Icon>
);

export const IconBolt = (p: IconProps) => (
  <Icon {...p}><path d="M13 2 4.5 13.5H11L10 22l8.5-11.5H12L13 2Z" /></Icon>
);

export const IconFileText = (p: IconProps) => (
  <Icon {...p}>
    <path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" />
    <path d="M14 2v4a2 2 0 0 0 2 2h4" />
    <path d="M9 13h6M9 17h6" />
  </Icon>
);

export const IconShield = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 2 4 5v6c0 5.25 3.4 9.74 8 11 4.6-1.26 8-5.75 8-11V5Z" />
    <path d="m9 12 2 2 4-4" />
  </Icon>
);

export const IconSettings = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="3" />
    <path d="M12 2v3M12 19v3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M2 12h3M19 12h3M4.9 19.1 7 17M17 7l2.1-2.1" />
  </Icon>
);

export const IconTerminal = (p: IconProps) => (
  <Icon {...p}>
    <path d="m4 17 6-6-6-6" />
    <path d="M12 19h8" />
  </Icon>
);

export const IconCheck = (p: IconProps) => (
  <Icon {...p}><path d="m5 12.5 4.5 4.5L19 7.5" /></Icon>
);

export const IconX = (p: IconProps) => (
  <Icon {...p}><path d="M18 6 6 18M6 6l12 12" /></Icon>
);

export const IconChevronLeft = (p: IconProps) => (
  <Icon {...p}><path d="m15 18-6-6 6-6" /></Icon>
);

export const IconChevronRight = (p: IconProps) => (
  <Icon {...p}><path d="m9 18 6-6-6-6" /></Icon>
);

export const IconInbox = (p: IconProps) => (
  <Icon {...p}>
    <path d="M22 12h-6l-2 3h-4l-2-3H2" />
    <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11Z" />
  </Icon>
);

export const IconFileStack = (p: IconProps) => (
  <Icon {...p}>
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z" />
    <path d="M14 2v6h6" />
    <path d="M4 6h2M4 10h2M4 14h2" />
  </Icon>
);

export const IconList = (p: IconProps) => (
  <Icon {...p}>
    <path d="M8 6h13M8 12h13M8 18h13" />
    <path d="M3 6h.01M3 12h.01M3 18h.01" />
  </Icon>
);

export const IconRefresh = (p: IconProps) => (
  <Icon {...p}>
    <path d="M21 12a9 9 0 1 1-2.64-6.36" />
    <path d="M21 3v6h-6" />
  </Icon>
);

export const IconWorkflow = (p: IconProps) => (
  <Icon {...p}>
    <rect x="3" y="3" width="7" height="7" rx="1.5" />
    <rect x="14" y="14" width="7" height="7" rx="1.5" />
    <path d="M10 6.5h5.5a1.5 1.5 0 0 1 1.5 1.5v2" />
    <path d="M6.5 10v5.5A1.5 1.5 0 0 0 8 17h2" />
    <path d="m14.5 12.5 2 2 2-2" opacity="0" />
  </Icon>
);

export const IconBrand = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 2a10 10 0 0 1 10 10h-5a5 5 0 0 0-5-5V2Z" />
    <path d="M12 22a10 10 0 0 1-10-10h5a5 5 0 0 0 5 5v5Z" />
    <circle cx="12" cy="12" r="2.5" />
  </Icon>
);

// ── QuantumRAD-Gateway brand mark ────────────────────────────────────────
// The QuantumPACS "Orbit" (design-system/quantumpacs/BRAND-KIT §2): nucleus,
// orbit ring + ellipse, scan-plane axis. Drawn on the 24px UI grid with the
// non-negotiable three-stop brand gradient (blue-600 → cyan → teal).

export const BrandGradientDefs = () => (
  <defs>
    <linearGradient id="qrad-brand-gradient" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stopColor="#0077B6" />
      <stop offset="0.5" stopColor="#22D3EE" />
      <stop offset="1" stopColor="#059669" />
    </linearGradient>
  </defs>
);

/** Full-color orbit mark for brand moments (sidebar, login). */
export const BrandMark = ({ size = 24 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" role="img" aria-label="QuantumRAD-Gateway">
    <BrandGradientDefs />
    <circle cx="12" cy="12" r="9.5" stroke="url(#qrad-brand-gradient)" strokeWidth="2" />
    <circle cx="12" cy="12" r="5.5" stroke="#22D3EE" strokeWidth="1.2" opacity="0.5" />
    <ellipse cx="12" cy="12" rx="3.4" ry="1.5" stroke="#059669" strokeWidth="0.9" opacity="0.6" />
    <line x1="12" y1="2.5" x2="12" y2="5.5" stroke="url(#qrad-brand-gradient)" strokeWidth="1.4" strokeLinecap="round" />
    <line x1="12" y1="18.5" x2="12" y2="21.5" stroke="url(#qrad-brand-gradient)" strokeWidth="1.4" strokeLinecap="round" />
    <circle cx="12" cy="12" r="1.6" fill="url(#qrad-brand-gradient)" />
  </svg>
);

/** White reversed mark for dark surfaces (login gradient). */
export const BrandMarkMonoLight = ({ size = 24 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" role="img" aria-label="QuantumRAD-Gateway">
    <circle cx="12" cy="12" r="9.5" stroke="#FFFFFF" strokeWidth="2" />
    <circle cx="12" cy="12" r="5.5" stroke="#FFFFFF" strokeWidth="1.2" opacity="0.5" />
    <ellipse cx="12" cy="12" rx="3.4" ry="1.5" stroke="#FFFFFF" strokeWidth="0.9" opacity="0.6" />
    <line x1="12" y1="2.5" x2="12" y2="5.5" stroke="#FFFFFF" strokeWidth="1.4" strokeLinecap="round" />
    <line x1="12" y1="18.5" x2="12" y2="21.5" stroke="#FFFFFF" strokeWidth="1.4" strokeLinecap="round" />
    <circle cx="12" cy="12" r="1.6" fill="#FFFFFF" />
  </svg>
);

export const IconLogout = (p: IconProps) => (
  <Icon {...p}>
    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
    <polyline points="16 17 21 12 16 7" />
    <line x1="21" x2="9" y1="12" y2="12" />
  </Icon>
);
