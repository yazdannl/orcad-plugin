const PATHS = {
  box: <><path d="M12 3 20 7.5v9L12 21l-8-4.5v-9Z" /><path d="M4 7.5 12 12l8-4.5M12 12v9" /></>,
  cylinder: <><ellipse cx="12" cy="6" rx="7" ry="3" /><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6" /></>,
  tube: <><ellipse cx="12" cy="6" rx="7" ry="3" /><ellipse cx="12" cy="6" rx="3.2" ry="1.3" /><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6" /></>,
  bracket: <><path d="M3 10.5 12 6l9 4.5-9 4.5Z" /><path d="M3 10.5V13l9 4.5 9-4.5v-2.5" /><ellipse cx="8" cy="10.5" rx="1.4" ry=".7" /><ellipse cx="16" cy="10.5" rx="1.4" ry=".7" /></>,
  bin: <><path d="M3 8.5 12 4l9 4.5v8L12 21l-9-4.5Z" /><path d="M3 8.5 12 13l9-4.5M12 13v8" /><path d="M7.5 6.3 16.5 10.8" /></>,
  baseplate: <><path d="M2.5 12 12 7l9.5 5-9.5 5Z" /><path d="M2.5 12v2l9.5 5 9.5-5v-2" /><path d="M7.2 9.5 16.8 14.5M16.8 9.5 7.2 14.5" /></>,
  code: <><path d="m8 7-5 5 5 5M16 7l5 5-5 5M13.5 4l-3 16" /></>,
  library: <><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></>,
  search: <><circle cx="11" cy="11" r="6.5" /><path d="m20 20-4.2-4.2" /></>,
  reset: <><path d="M4 12a8 8 0 1 0 2.4-5.7L4 8.6" /><path d="M4 4v4.6h4.6" /></>,
  download: <><path d="M12 4v11m-4.5-4.5L12 15l4.5-4.5M5 20h14" /></>,
  plate: <><path d="M3 15.5 12 11l9 4.5L12 20Z" /><path d="M12 3v6.5m-3-3 3 3 3-3" /></>,
  play: <><path d="M7 4.5v15l12-7.5Z" /></>,
  folder: <><path d="M3 6.5A1.5 1.5 0 0 1 4.5 5H9l2 2.5h8.5A1.5 1.5 0 0 1 21 9v9.5a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5Z" /></>,
  copy: <><rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V5.5A1.5 1.5 0 0 0 14.5 4h-9A1.5 1.5 0 0 0 4 5.5v9A1.5 1.5 0 0 0 5.5 16H8" /></>,
  fit: <><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" /></>,
  iso: <><path d="M12 3 20 7.5v9L12 21l-8-4.5v-9Z" /><path d="M4 7.5 12 12l8-4.5M12 12v9" /></>,
  front: <><rect x="5" y="5" width="14" height="14" rx="1.5" /><path d="M5 15h14" /></>,
  top: <><rect x="5" y="5" width="14" height="14" rx="1.5" /><circle cx="12" cy="12" r="2" /></>,
  right: <><rect x="5" y="5" width="14" height="14" rx="1.5" /><path d="M15 5v14" /></>,
  wireframe: <><path d="M12 3 20 7.5v9L12 21l-8-4.5v-9Z" /><path d="M4 7.5 20 16.5M20 7.5 4 16.5M12 3v18" /></>,
  edges: <><path d="M12 3 20 7.5v9L12 21l-8-4.5v-9Z" strokeWidth="2.2" /></>,
  grid: <><path d="M3 9h18M3 15h18M9 3v18M15 3v18" /></>,
  help: <><circle cx="12" cy="12" r="9" /><path d="M9.6 9.3a2.5 2.5 0 1 1 3.4 2.3c-.6.3-1 .8-1 1.5v.4M12 16.8v.2" /></>,
  close: <><path d="M6 6l12 12M18 6 6 18" /></>,
  alert: <><path d="M12 3.5 21.5 20h-19Z" /><path d="M12 10v4.5M12 17.2v.2" /></>,
  check: <><path d="m5 12.5 4.5 4.5L19 7.5" /></>,
  chevron: <><path d="m8 10 4 4 4-4" /></>,
  terminal: <><rect x="3" y="4.5" width="18" height="15" rx="2" /><path d="m7 9.5 3 2.5-3 2.5M12.5 15H17" /></>,
  brick: <><rect x="4" y="7" width="16" height="10" rx="1.5" /><rect x="7" y="4.5" width="3" height="2.5" rx="0.8" /><rect x="14" y="4.5" width="3" height="2.5" rx="0.8" /></>,
  gear: <><circle cx="12" cy="12" r="3.2" /><path d="M12 2.6v3M12 18.4v3M2.6 12h3M18.4 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M18.7 5.3l-2.1 2.1M7.4 16.6l-2.1 2.1" /></>,
  wrench: <><path d="M14.5 3.5a4.5 4.5 0 0 0 5.9 5.9l-8.6 8.6a2.1 2.1 0 0 1-3-3Z" /><path d="M4.2 19.8a2.1 2.1 0 1 0 3-3" /></>,
  screw: <><path d="M9 3.2h6M10.2 6.2h3.6M9.8 9.2h4.4M9 12.2h6M10.2 15.2h3.6" /><path d="M9.6 18.1 12 20.7l2.4-2.6" /></>,
  sphere: <><circle cx="12" cy="12" r="8.4" /><path d="M4.6 9.2c4.4 2.6 10.4 2.6 14.8 0M6 16.4c3.6-3.4 8.4-3.4 12 0" /></>,
  pillow: <><path d="M4 9.5A2.5 2.5 0 0 1 6.5 7h11A2.5 2.5 0 0 1 20 9.5v5A2.5 2.5 0 0 1 17.5 17h-11A2.5 2.5 0 0 1 4 14.5Z" /><path d="M8 9.5A2.5 2.5 0 0 1 12 7.5 2.5 2.5 0 0 1 16 9.5v5a2.5 2.5 0 0 1-4 1.6 2.5 2.5 0 0 1-4-1.6Z" /></>,
  rocket: <><path d="M12 3.2c3.4 1.9 5.2 5.4 5.2 9.4L12 17l-5.2-4.4c0-4 1.8-7.5 5.2-9.4Z" /><circle cx="12" cy="10" r="1.7" /><path d="M8.8 15.6 6.6 21l3.6-1.6M15.2 15.6 17.4 21l-3.6-1.6" /></>,
  server: <><rect x="3.2" y="4.4" width="17.6" height="5.2" rx="1.4" /><rect x="3.2" y="11.4" width="17.6" height="5.2" rx="1.4" /><circle cx="7" cy="7" r="0.9" /><circle cx="7" cy="14" r="0.9" /><path d="M10.4 7h6M10.4 14h6" /></>,
  dial: <><circle cx="12" cy="12" r="8.2" /><path d="M12 12l3.6-3.6" /><circle cx="12" cy="12" r="0.9" /></>,
  chip: <><rect x="6.6" y="6.6" width="10.8" height="10.8" rx="1.6" /><path d="M10 3.4v3.2M14 3.4v3.2M10 17.4v3.2M14 17.4v3.2M3.4 10h3.2M3.4 14h3.2M17.4 10h3.2M17.4 14h3.2" /></>,
  bike: <><circle cx="6.4" cy="16.6" r="3.4" /><circle cx="17.6" cy="16.6" r="3.4" /><path d="M6.4 16.6 10 9.4h4.4l2.6 4.2M10 9.4 8.6 6h2.2" /></>,
  cube: <><path d="M12 2.8 20.3 7.4v9.2L12 21.2l-8.3-4.6V7.4Z" /><path d="M3.7 7.4 12 12l8.3-4.6M12 12v9.2" /></>,
  sign: <><rect x="3" y="5.5" width="18" height="13" rx="2.5" /><path d="M7 10h10M7 14h6" /></>,
}


export function Icon({ name, size = 18, className = '' }) {
  return (
    <svg className={`icon ${className}`} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      {PATHS[name] || PATHS.cube}
    </svg>
  )
}

export function Logo() {
  return (
    <svg className="logo" viewBox="0 0 32 32" width="30" height="30" aria-hidden="true">
      <defs>
        <linearGradient id="orcad-logo" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#2dd4bf" />
          <stop offset=".55" stopColor="#22d3ee" />
          <stop offset="1" stopColor="#818cf8" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="9" fill="url(#orcad-logo)" />
      <path d="M16 6.5 24.5 11v10L16 25.5 7.5 21V11Z" fill="none" stroke="#fff" strokeWidth="2" strokeLinejoin="round" />
      <path d="M7.5 11 16 15.5 24.5 11M16 15.5v10" fill="none" stroke="#fff" strokeWidth="2" strokeLinejoin="round" opacity=".85" />
    </svg>
  )
}
