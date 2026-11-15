/* 图标库：SF Symbols 风格线性 SVG（stroke currentColor） */

const PATHS = {
  wand: '<path d="M15 4V2M15 8v-2M11 6h2M17 6h2M6.5 6.5 3 10l4 4 8.5-8.5z"/><path d="m19 14-6 6M17.5 15.5 18 17M14 14.5 15.5 16M21 17l-1 1"/>',
  stack: '<path d="M12 3 3 8l9 5 9-5z"/><path d="m3 13 9 5 9-5"/>',
  doc: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  sliders: '<path d="M4 8h10M18 8h2M4 16h4M12 16h8"/><circle cx="16" cy="8" r="2"/><circle cx="10" cy="16" r="2"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  check: '<path d="m4 12.5 5 5L20 6.5"/>',
  xmark: '<path d="M6 6l12 12M18 6 6 18"/>',
  chevronR: '<path d="m9 5 7 7-7 7"/>',
  chevronD: '<path d="m5 9 7 7 7-7"/>',
  upload: '<path d="M12 16V4m0 0L7 9m5-5 5 5"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
  download: '<path d="M12 4v12m0 0 5-5m-5 5-5-5"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/>',
  moon: '<path d="M20 13.5A8.5 8.5 0 0 1 10.5 4 7 7 0 1 0 20 13.5z"/>',
  flask: '<path d="M10 3v6L4.5 18a2 2 0 0 0 1.7 3h11.6a2 2 0 0 0 1.7-3L14 9V3"/><path d="M8 3h8M7.5 14h9"/>',
  target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="0.5"/>',
  printer: '<path d="M7 8V4h10v4"/><rect x="4" y="8" width="16" height="8" rx="2"/><path d="M7 14h10v6H7z"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 8h0M12 11v6"/>',
  warn: '<path d="M12 3 2.5 20h19z"/><path d="M12 9.5V14M12 17h0"/>',
  swap: '<path d="M7 4 3 8l4 4"/><path d="M3 8h13a4 4 0 0 1 0 8"/>',
  lab: '<circle cx="12" cy="12" r="9"/><path d="m15.5 15.5 5-5"/><path d="M17 9a5.5 5.5 0 0 1 4 4"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
};

export function icon(name) {
  const p = PATHS[name] || PATHS.info;
  return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${p}</svg>`;
}
