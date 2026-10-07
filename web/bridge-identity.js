/* ------------------------------------------------------------------
   MCP Session Bridge — shared identity marks

   Model voiceprints, group pictograms and file-kind pictograms, so every
   workspace draws the same symbol for the same thing. Copied verbatim from
   admin-viewer.html; keep the two in step until the Sessions page loads
   this file too.
------------------------------------------------------------------- */
(() => {
  "use strict";

  function modelIdentity(name) {
    const raw = (name == null ? "" : String(name)).trim();
    const key = raw.toLowerCase();
    const make = (color, mono, label) => ({ color, mono, label: label || raw || "Model" });
    if (!raw) return make("#8b8ff8", "·", "Model");
    if (key === "user") return make("#6366f1", "U", "USER");
    if (key.includes("claude")) return make("#f59e0b", "C", raw);
    if (key.includes("gpt") || key.includes("chatgpt") || key.includes("openai") || /\bo[134]\b/.test(key)) return make("#10b981", "G", raw);
    if (key.includes("codex")) return make("#a78bfa", "<>", raw);
    if (key.includes("gemini") || key.includes("bard")) return make("#38bdf8", "✦", raw);
    if (key.includes("llama") || key.includes("meta")) return make("#60a5fa", "L", raw);
    if (key.includes("mistral") || key.includes("mixtral")) return make("#fb923c", "M", raw);
    if (key.includes("grok")) return make("#e879f9", "x", raw);
    if (key.includes("deepseek")) return make("#2dd4bf", "D", raw);
    if (key.includes("qwen")) return make("#c084fc", "Q", raw);
    const palette = ["#f472b6", "#facc15", "#34d399", "#22d3ee", "#c084fc", "#fb923c", "#a3e635", "#5eead4"];
    let h = 0;
    for (let i = 0; i < raw.length; i += 1) h = ((h << 5) - h + raw.charCodeAt(i)) | 0;
    return make(palette[Math.abs(h) % palette.length], raw[0].toUpperCase(), raw);
  }

  function nameHash(value) {
    let h = 2166136261;
    const s = String(value == null ? "" : value).trim().toLowerCase();
    for (let i = 0; i < s.length; i += 1) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }

  const VOICE_ENVELOPES = [
    [0.35, 0.6, 0.85, 1.0, 0.72, 0.5, 0.3],   // peak center
    [0.25, 0.36, 0.5, 0.64, 0.78, 0.9, 1.0],  // rising
    [1.0, 0.9, 0.78, 0.64, 0.5, 0.36, 0.25],  // falling
    [0.3, 0.85, 0.45, 1.0, 0.4, 0.8, 0.3],    // twin peaks
    [0.95, 0.65, 0.4, 0.28, 0.42, 0.7, 1.0],  // valley
    [0.75, 0.35, 0.9, 0.45, 0.85, 0.3, 0.65], // zigzag
  ];

  function voiceprintSvg(name) {
    const h = nameHash(name);
    const env = VOICE_ENVELOPES[h % VOICE_ENVELOPES.length];
    const accent = (h >>> 3) % env.length;
    let bars = "";
    for (let i = 0; i < env.length; i += 1) {
      const n = ((h >>> (i * 4)) & 15) / 15;      // 0..1, deterministic per bar
      const noise = 1 + (n - 0.5) * 0.2;          // 0.9 .. 1.1
      const height = Math.max(4.5, Math.min(21, env[i] * noise * 21));
      const y = 12 - height / 2;
      const x = 1.2 + i * 3.2;
      const opacity = i === accent ? "1" : "0.62";
      bars += '<rect x="' + x.toFixed(2) + '" y="' + y.toFixed(2) + '" width="2.2" height="' + height.toFixed(2) + '" rx="1.1" fill-opacity="' + opacity + '"></rect>';
    }
    return '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' + bars + "</svg>";
  }

  const GROUP_ICON_SVG_ATTRS = 'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"';

  const GROUP_ICON_PATHS = {
      all_sessions: '<rect x="3" y="3" width="7" height="7" rx="1.5"></rect><rect x="14" y="3" width="7" height="7" rx="1.5"></rect><rect x="3" y="14" width="7" height="7" rx="1.5"></rect><rect x="14" y="14" width="7" height="7" rx="1.5"></rect>',
      folder: '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H10l2 2h6.5A2.5 2.5 0 0 1 21 9.5v7A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5z"></path>',
      brain: '<path d="M9 4.5a3 3 0 0 0-3 3 3 3 0 0 0-2 5.4A3.2 3.2 0 0 0 7.2 18H9"></path><path d="M15 4.5a3 3 0 0 1 3 3 3 3 0 0 1 2 5.4A3.2 3.2 0 0 1 16.8 18H15"></path><path d="M9 4.5V20"></path><path d="M15 4.5V20"></path><path d="M9 9H7"></path><path d="M15 9h2"></path><path d="M9 14H7.5"></path><path d="M15 14h1.5"></path>',
      medical_plus: '<path d="M9 3h6v6h6v6h-6v6H9v-6H3V9h6z"></path>',
      ideas: '<path d="M9 18h6"></path><path d="M10 22h4"></path><path d="M8.5 14.5A6 6 0 1 1 15.5 14c-.9.7-1.5 1.7-1.7 3h-3.6c-.2-1.1-.8-1.9-1.7-2.5z"></path>',
      heart: '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.6l-1-1a5.5 5.5 0 0 0-7.8 7.8l1 1L12 21l7.8-7.6 1-1a5.5 5.5 0 0 0 0-7.8z"></path>',
      book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"></path><path d="M4 4.5A2.5 2.5 0 0 1 6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5z"></path>',
      graduation: '<path d="M22 10 12 5 2 10l10 5z"></path><path d="M6 12v5c3 2 9 2 12 0v-5"></path>',
      pencil: '<path d="m4 20 4.5-1 10-10a2.1 2.1 0 0 0-3-3l-10 10z"></path><path d="m14 6 4 4"></path>',
      code: '<path d="m8 8-4 4 4 4"></path><path d="m16 8 4 4-4 4"></path><path d="m14 4-4 16"></path>',
      terminal: '<path d="m4 7 5 5-5 5"></path><path d="M12 17h8"></path>',
      music: '<path d="M9 18V5l10-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="16" cy="16" r="3"></circle>',
      food: '<path d="M7 3v8"></path><path d="M4 3v8"></path><path d="M10 3v8"></path><path d="M4 11h6"></path><path d="M7 11v10"></path><path d="M16 3c2 2 3 5 3 8s-1 5-3 6v4"></path>',
      palette: '<path d="M12 3a9 9 0 0 0 0 18h1.5a2 2 0 0 0 0-4H12a2 2 0 0 1 0-4h2a7 7 0 0 0-2-10z"></path><circle cx="7.5" cy="10" r=".7"></circle><circle cx="10" cy="7.5" r=".7"></circle><circle cx="14" cy="7.5" r=".7"></circle>',
      tools: '<path d="M14.7 6.3a4 4 0 0 0 5 5L11 20l-5-5 8.7-8.7z"></path><path d="m5 15-2 2 4 4 2-2"></path>',
      travel: '<path d="M10.5 21 13 14l7-2-7-2-2.5-7L8 10l-7 2 7 2z"></path>',
      world: '<circle cx="12" cy="12" r="9"></circle><path d="M3 12h18"></path><path d="M12 3a14 14 0 0 1 0 18"></path><path d="M12 3a14 14 0 0 0 0 18"></path>',
      legal: '<path d="M12 3v18"></path><path d="M5 6h14"></path><path d="m6 6-3 7h6z"></path><path d="m18 6-3 7h6z"></path>',
      science: '<path d="M9 3h6"></path><path d="M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6A2 2 0 0 0 19 18l-5-9V3"></path><path d="M8 15h8"></path>',
      plants: '<path d="M12 21V10"></path><path d="M12 13c-5 0-7-3-7-7 5 0 7 3 7 7z"></path><path d="M12 15c5 0 7-3 7-7-5 0-7 3-7 7z"></path>',
      money: '<circle cx="12" cy="12" r="9"></circle><path d="M15 9.5A3 3 0 0 0 12 8c-1.7 0-3 .8-3 2s1.3 1.8 3 2 3 .8 3 2-1.3 2-3 2a3.5 3.5 0 0 1-3.2-1.7"></path><path d="M12 6v12"></path>',
      archive: '<rect x="3" y="5" width="18" height="4" rx="1"></rect><path d="M5 9v10h14V9"></path><path d="M9 13h6"></path>',
      star: '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-2.9-5.6 2.9 1.1-6.2L3 9.6l6.2-.9z"></path>',
      calendar: '<rect x="3" y="5" width="18" height="16" rx="2"></rect><path d="M16 3v4M8 3v4M3 10h18"></path>',
      clock: '<circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path>',
      chat: '<path d="M21 12a8 8 0 0 1-8 8H6l-4 2 1.5-5A8 8 0 1 1 21 12z"></path><path d="M8 12h.01M12 12h.01M16 12h.01"></path>',
      users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"></path>',
      person: '<circle cx="12" cy="7" r="4"></circle><path d="M4 21a8 8 0 0 1 16 0"></path>',
      home: '<path d="m3 11 9-8 9 8"></path><path d="M5 10v11h14V10M9 21v-7h6v7"></path>',
      briefcase: '<rect x="3" y="7" width="18" height="13" rx="2"></rect><path d="M8 7V4h8v3M3 12h18M10 12v2h4v-2"></path>',
      camera: '<path d="M4 7h3l2-3h6l2 3h3a2 2 0 0 1 2 2v10H2V9a2 2 0 0 1 2-2z"></path><circle cx="12" cy="13" r="4"></circle>',
      video: '<rect x="3" y="6" width="13" height="12" rx="2"></rect><path d="m16 10 5-3v10l-5-3z"></path>',
      microphone: '<rect x="9" y="3" width="6" height="12" rx="3"></rect><path d="M5 11a7 7 0 0 0 14 0M12 18v3M9 21h6"></path>',
      phone: '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 2 .7 2.9a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.2-1.2a2 2 0 0 1 2.1-.5c.9.3 1.9.6 2.9.7a2 2 0 0 1 1.7 2z"></path>',
      mail: '<rect x="3" y="5" width="18" height="14" rx="2"></rect><path d="m3 7 9 6 9-6"></path>',
      shopping: '<path d="M6 8h12l1 13H5z"></path><path d="M9 9V6a3 3 0 0 1 6 0v3"></path>',
      gift: '<rect x="3" y="9" width="18" height="12" rx="1"></rect><path d="M12 9v12M3 13h18M7.5 9C5 9 4 7.7 4 6.5S5 4 6.5 4C9 4 12 9 12 9M16.5 9C19 9 20 7.7 20 6.5S19 4 17.5 4C15 4 12 9 12 9"></path>',
      game: '<path d="M7 7h10a4 4 0 0 1 3.8 5.3l-1.5 4.5a2 2 0 0 1-3.4.7L13.5 15h-3l-2.4 2.5a2 2 0 0 1-3.4-.7l-1.5-4.5A4 4 0 0 1 7 7z"></path><path d="M8 10v4M6 12h4M16 11h.01M18 13h.01"></path>',
      rocket: '<path d="M14 4c3-2 6-2 6-2s0 3-2 6l-5 5-4-4z"></path><path d="m9 9-4 1-3 3 6 1M13 13l-1 4-3 3-1-6M15 7h.01"></path>',
      shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path>',
      lock: '<rect x="4" y="10" width="16" height="11" rx="2"></rect><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3"></path>',
      key: '<circle cx="8" cy="15" r="4"></circle><path d="m11 12 9-9M17 6l2 2M14 9l2 2"></path>',
      map: '<path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3z"></path><path d="M9 3v15M15 6v15"></path>',
      pin: '<path d="M20 10c0 5-8 12-8 12S4 15 4 10a8 8 0 1 1 16 0z"></path><circle cx="12" cy="10" r="3"></circle>',
      car: '<path d="m5 16-2-1v-4l2-1 2-5h10l2 5 2 1v4l-2 1"></path><path d="M5 10h14M7 16v2M17 16v2M7 14h.01M17 14h.01"></path>',
      plane: '<path d="m22 2-7 20-4-9-9-4z"></path><path d="M22 2 11 13"></path>',
      coffee: '<path d="M4 8h13v7a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5z"></path><path d="M17 10h2a3 3 0 0 1 0 6h-2M7 3v2M11 3v2M15 3v2"></path>',
      fitness: '<path d="M6 7v10M3 9v6M18 7v10M21 9v6M6 12h12"></path>',
      pet: '<circle cx="12" cy="14" r="5"></circle><circle cx="5" cy="9" r="2"></circle><circle cx="9" cy="5" r="2"></circle><circle cx="15" cy="5" r="2"></circle><circle cx="19" cy="9" r="2"></circle>'
  };

  function sensitiveIconSvg() {
    return `<svg ${GROUP_ICON_SVG_ATTRS}><path d="M10.73 5.08A10.4 10.4 0 0 1 12 5c5.5 0 9 7 9 7a16.7 16.7 0 0 1-2.1 3"></path><path d="M14.1 14.2a3 3 0 0 1-4.3-4.3"></path><path d="M17.5 17.5A9.7 9.7 0 0 1 12 19c-5.5 0-9-7-9-7a16 16 0 0 1 3.6-4.7"></path><path d="M3 3l18 18"></path></svg>`;
  }

  const FILE_KINDS = {
    text: { name: "Text & Markdown", icon: '<path d="M6 3h8l4 4v14H6z"></path><path d="M9 11h6M9 15h6M9 19h4"></path>' },
    data: { name: "Data", icon: '<rect x="4" y="4" width="16" height="16" rx="2"></rect><path d="M4 10h16M4 15h16M10 4v16"></path>' },
    pdf: { name: "PDF", icon: '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H11v16H5.5A1.5 1.5 0 0 1 4 18.5z"></path><path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H13v16h5.5a1.5 1.5 0 0 0 1.5-1.5z"></path>' },
    image: { name: "Images", icon: '<rect x="3" y="5" width="18" height="14" rx="2"></rect><circle cx="9" cy="10" r="1.6"></circle><path d="m21 16-5-5-8 8"></path>' },
    web: { name: "HTML & code", icon: '<path d="m8 8-4 4 4 4M16 8l4 4-4 4M13.5 5l-3 14"></path>' },
  };

  function groupIconSvg(key) {
    return `<svg ${GROUP_ICON_SVG_ATTRS}>${GROUP_ICON_PATHS[key] || GROUP_ICON_PATHS.folder}</svg>`;
  }

  function fileKindSvg(kind) {
    const entry = FILE_KINDS[kind] || FILE_KINDS.text;
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${entry.icon}</svg>`;
  }

  window.BridgeIdentity = Object.freeze({
    modelIdentity,
    voiceprintSvg,
    groupIconSvg,
    sensitiveIconSvg,
    fileKindSvg,
    FILE_KINDS,
  });
})();
