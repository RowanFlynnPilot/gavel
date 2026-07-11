import { useState, useEffect } from "react";

const BASE_URL = import.meta.env.BASE_URL;

// Plausible custom-event helper. No-op if the script hasn't loaded (it's queued
// by the stub injected at boot) or is blocked. Each event name used here should
// be added as a Goal in Plausible → Site Settings to surface in reports.
function track(event, props) {
  try {
    if (typeof window !== "undefined" && typeof window.plausible === "function") {
      window.plausible(event, props ? { props } : undefined);
    }
  } catch (e) { /* analytics must never break the UI */ }
}

// ─── Runtime configuration ───────────────────────────────────────────────────
// Everything below is populated exactly once by _applyConfig() after
// instance.json + data/*.json resolve. The <Tracker> component tree only
// mounts after that, so every reference sees fully-built config. Theme colors
// and fonts flow through CSS custom properties set on <html>, so the same
// constants work in inline styles and the <style> block alike.

const TEAL  = "var(--primary)";
const CREAM = "var(--background)";
const INK   = "var(--ink)";
const RULE  = "var(--divider)";

const FONT_DISPLAY        = "var(--font-display)";
const FONT_DISPLAY_NARROW = "var(--font-display-narrow)";
const FONT_HEADLINE       = "var(--font-headline)";
const FONT_BODY           = "var(--font-body)";
const FONT_DATA           = "var(--font-data)";

let INSTANCE      = null; // instance.json "instance" block
let THEME         = null; // instance.json "theme" block
let SPONSOR       = null; // sponsor block + computed mailto body; null = no strip
let JURISDICTIONS = [];   // instance.json "jurisdictions" array
let MEETINGS      = [];   // data/meetings.json (newest first)
let UPCOMING      = {};   // data/upcoming.json (keyed by jurisdiction key)

let SOURCE_CONFIG          = {};
let COMMITTEE_STYLES       = {};
let _COMMITTEE_STYLES_NORM = {};
let FILTER_OPTIONS         = []; // "All Sources" first, then one per jurisdiction
let CAL_URLS               = {}; // FULL CALENDAR(S) destination per source filter

// Raw hex mirror of the ink token — the filter chips build "#RRGGBB15" alpha
// tints by string concatenation, which CSS variables can't do.
let INK_RAW       = "#111";
// Production hardcoded #1a5c57 under the header band; config derives it by
// darkening primary_dark by a fixed 0.70 factor (#3e847a → #2b5c55 for WPR).
let HEADER_BORDER = "#000";
let TITLE_LINE_1  = "";
let TITLE_LINE_2  = "";
let GOV_COUNT_WORD = "";

const _NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six",
  "seven", "eight", "nine", "ten", "eleven", "twelve"];

function _darkenHex(hex, factor) {
  const n = (hex || "").replace("#", "");
  if (n.length !== 6) return hex;
  const c = (i) => Math.max(0, Math.min(255, Math.round(parseInt(n.slice(i, i + 2), 16) * factor)));
  return "#" + [c(0), c(2), c(4)].map(v => v.toString(16).padStart(2, "0")).join("");
}

function _isMonoFont(name) {
  return !name || name.trim().toLowerCase() === "monospace";
}

// Google Fonts stylesheet URL from the theme's three faces. Weights mirror the
// link production shipped: display plain, headline wght 400;700;800, body
// ital+wght 0,400;0,600;1,400. "monospace" entries are skipped (system font).
function _googleFontsHref(theme) {
  const enc = (name) => name.trim().replace(/\s+/g, "+");
  const families = [];
  if (!_isMonoFont(theme.font_display))  families.push(`family=${enc(theme.font_display)}`);
  if (!_isMonoFont(theme.font_headline)) families.push(`family=${enc(theme.font_headline)}:wght@400;700;800`);
  if (!_isMonoFont(theme.font_body))     families.push(`family=${enc(theme.font_body)}:ital,wght@0,400;0,600;1,400`);
  if (!families.length) return null;
  return `https://fonts.googleapis.com/css2?${families.join("&")}&display=swap`;
}

// Append a <head> element once (React 18 StrictMode runs the boot effect twice
// in dev; injection must be idempotent).
function _injectOnce(id, create) {
  if (document.getElementById(id)) return;
  const el = create();
  el.id = id;
  document.head.appendChild(el);
}

function _applyConfig(config, meetings, upcoming) {
  // Fail loud on structurally-invalid config — no defaults, no partial boots.
  if (!config || !config.instance || !config.theme ||
      !Array.isArray(config.jurisdictions) || config.jurisdictions.length === 0) {
    throw new Error("instance.json (missing required instance/theme/jurisdictions blocks)");
  }
  INSTANCE      = config.instance;
  THEME         = config.theme;
  JURISDICTIONS = config.jurisdictions;
  MEETINGS      = Array.isArray(meetings) ? meetings : [];
  UPCOMING      = upcoming || {};

  // Theme tokens → CSS custom properties on the root element.
  const root = document.documentElement;
  root.style.setProperty("--primary",      THEME.primary);
  root.style.setProperty("--primary-dark", THEME.primary_dark);
  root.style.setProperty("--background",   THEME.background);
  root.style.setProperty("--ink",          THEME.ink);
  root.style.setProperty("--divider",      THEME.divider);
  root.style.setProperty("--font-display",
    _isMonoFont(THEME.font_display) ? "sans-serif" : `'${THEME.font_display}', sans-serif`);
  root.style.setProperty("--font-display-narrow",
    _isMonoFont(THEME.font_display) ? "'Arial Narrow', sans-serif" : `'${THEME.font_display}', 'Arial Narrow', sans-serif`);
  root.style.setProperty("--font-headline",
    _isMonoFont(THEME.font_headline) ? "Georgia, serif" : `'${THEME.font_headline}', Georgia, serif`);
  root.style.setProperty("--font-body",
    _isMonoFont(THEME.font_body) ? "Georgia, serif" : `'${THEME.font_body}', Georgia, serif`);
  root.style.setProperty("--font-data",
    _isMonoFont(THEME.font_data) ? "monospace" : `'${THEME.font_data}', monospace`);
  document.body.style.fontFamily = "var(--font-body)";

  INK_RAW       = THEME.ink;
  HEADER_BORDER = _darkenHex(THEME.primary_dark, 0.7);

  // Web fonts (production shipped these as static tags in index.html).
  const fontsHref = _googleFontsHref(THEME);
  if (fontsHref) {
    _injectOnce("gavel-fonts-preconnect-g", () => {
      const l = document.createElement("link");
      l.rel = "preconnect"; l.href = "https://fonts.googleapis.com";
      return l;
    });
    _injectOnce("gavel-fonts-preconnect-s", () => {
      const l = document.createElement("link");
      l.rel = "preconnect"; l.href = "https://fonts.gstatic.com"; l.crossOrigin = "";
      return l;
    });
    _injectOnce("gavel-fonts", () => {
      const l = document.createElement("link");
      l.rel = "stylesheet"; l.href = fontsHref;
      return l;
    });
  }

  document.title = `${INSTANCE.name} — ${INSTANCE.newsroom}`;
  let themeColor = document.querySelector('meta[name="theme-color"]');
  if (!themeColor) {
    themeColor = document.createElement("meta");
    themeColor.name = "theme-color";
    document.head.appendChild(themeColor);
  }
  themeColor.content = THEME.primary_dark;

  // Parity-soak deployments must never compete with the production tracker in
  // search: robots.txt already disallows crawling, and this covers the SPA
  // shell itself. Turns off automatically when seo.noindex flips at cutover.
  if (config.seo && config.seo.noindex) {
    _injectOnce("gavel-noindex", () => {
      const m = document.createElement("meta");
      m.name = "robots";
      m.content = "noindex, nofollow";
      return m;
    });
  }

  // Plausible Analytics. The tracker is embedded as an iframe on the newsroom
  // site, so the parent page's analytics can't see usage inside it — this
  // script captures the tool's own pageviews + engagement events and reports
  // them to the configured property. Custom events (Meeting Opened, Filter,
  // Bookmark, Outbound Link) must be added as Goals in Plausible → Site
  // Settings → Goals to appear in reports.
  const plausibleDomain = config.analytics && config.analytics.plausible_domain;
  if (plausibleDomain) {
    window.plausible = window.plausible || function () {
      (window.plausible.q = window.plausible.q || []).push(arguments);
    };
    _injectOnce("gavel-plausible", () => {
      const s = document.createElement("script");
      s.defer = true;
      s.setAttribute("data-domain", plausibleDomain);
      s.src = "https://plausible.io/js/script.js";
      return s;
    });
  }

  SOURCE_CONFIG = {};
  for (const j of JURISDICTIONS) {
    SOURCE_CONFIG[j.key] = {
      label:   j.name,
      short:   j.short,
      accent:  j.accent,
      channel: (j.video && j.video.channel_url) || j.calendar_url,
      docHost: j.doc_host,
      avatar:  BASE_URL + j.avatar,
    };
  }

  COMMITTEE_STYLES = config.committee_styles || {};
  _COMMITTEE_STYLES_NORM = Object.fromEntries(
    Object.entries(COMMITTEE_STYLES).map(([k, v]) => [_normalizeCommitteeKey(k), v])
  );

  // Source filter chips for the Recent + Upcoming panels. filter_label lets an
  // instance shorten chip labels ("Wausau" vs "City of Wausau") for parity
  // with production's hand-tuned list.
  FILTER_OPTIONS = [
    { key: "all", label: "All Sources", color: INK_RAW, avatar: null },
    ...JURISDICTIONS.map(j => ({
      key:    j.key,
      label:  j.filter_label || j.name,
      color:  j.accent,
      avatar: BASE_URL + j.avatar,
    })),
  ];

  // Calendar destination follows the source filter so the link doesn't always
  // drop the reader on the first jurisdiction's site.
  CAL_URLS = { all: JURISDICTIONS[0].calendar_url };
  for (const j of JURISDICTIONS) CAL_URLS[j.key] = j.calendar_url;

  // Sponsorship CTA configuration — strip renders only when the instance
  // defines a sponsor block. Body mirrors production's inquiry template.
  if (config.sponsor && config.sponsor.email) {
    const greeting = config.sponsor.contact_name ? `Hi ${config.sponsor.contact_name},` : "Hi,";
    SPONSOR = {
      ...config.sponsor,
      body: `${greeting}\n\nI'm interested in sponsoring the ${INSTANCE.name} on ${INSTANCE.newsroom}. Could you share placement options, audience details, and pricing?\n\nThanks!`,
    };
  } else {
    SPONSOR = null;
  }

  // Big masthead title, split at the midpoint word — "Central Wisconsin
  // Meeting Tracker" yields "CENTRAL WISCONSIN" + "MEETING TRACKER".
  const words = (INSTANCE.name || "").trim().split(/\s+/).filter(Boolean);
  const mid = Math.ceil(words.length / 2);
  TITLE_LINE_1 = words.slice(0, mid).join(" ").toUpperCase();
  TITLE_LINE_2 = words.slice(mid).join(" ").toUpperCase();

  GOV_COUNT_WORD = JURISDICTIONS.length < _NUMBER_WORDS.length
    ? _NUMBER_WORDS[JURISDICTIONS.length]
    : String(JURISDICTIONS.length);
}

// Below this width the tool uses the single-column "swap" layout (full-width
// list, tap a meeting for a full-width summary, back to return) instead of the
// two-pane list+detail split. The two-pane split needs ~420px of list PLUS a
// readable detail pane, so it only looks good at genuine desktop widths. When
// the tool is embedded in a column that shares space with a sidebar (e.g. the
// Wausau Pilot page), the container is ~700px — wide enough to trip the old
// 700px breakpoint into a cramped two-pane. 960px keeps it single-column there.
const COMPACT_BREAKPOINT = 960;

function useIsMobile() {
  const [m, setM] = useState(window.innerWidth < COMPACT_BREAKPOINT);
  useEffect(() => {
    const fn = () => setM(window.innerWidth < COMPACT_BREAKPOINT);
    window.addEventListener("resize", fn);
    return () => window.removeEventListener("resize", fn);
  }, []);
  return m;
}

// Full-text search across everything a reader might remember about a meeting:
// title, committee, date, topic tags, overview, discussion items and bodies,
// action items, and public comment. The lowercase blob is built once per
// meeting and cached (data is static per page load).
const _searchBlobCache = new Map();
function _searchBlob(m) {
  let blob = _searchBlobCache.get(m.id);
  if (blob === undefined) {
    blob = [
      m.title, m.committee, m.date, m.shortDate,
      ...(m.topics || []),
      m.overview,
      ...(m.discussions || []).flatMap(d => [d.item, d.body]),
      ...(m.actionItems || []),
      m.publicComment,
    ].filter(Boolean).join(" \n ").toLowerCase();
    _searchBlobCache.set(m.id, blob);
  }
  return blob;
}
function matchSearch(m, query) {
  const q = (query || "").trim().toLowerCase();
  if (!q) return true;
  // Every whitespace-separated term must appear somewhere in the meeting.
  return q.split(/\s+/).every(term => _searchBlob(m).includes(term));
}

// Normalize a committee name for fuzzy lookup: lowercase, "and"↔"&",
// strip trailing "Committee"/"Commission"/"Board" since the badge already
// shows the full label.
function _normalizeCommitteeKey(s) {
  return (s || "")
    .toLowerCase()
    .replace(/\s+and\s+/g, " & ")
    .replace(/\s*(committee|commission|board)\s*$/i, "")
    .trim();
}
function getCommitteeStyle(committee) {
  return COMMITTEE_STYLES[committee]
      || _COMMITTEE_STYLES_NORM[_normalizeCommitteeKey(committee)]
      || { bg: "#555", text: "#fff" };
}

function MeetingCard({ meeting, onClick, active }) {
  const cs  = getCommitteeStyle(meeting.committee);
  const src = SOURCE_CONFIG[meeting.source];
  const [hov, setHov] = useState(false);

  return (
    <button
      onClick={() => onClick(meeting)}
      onMouseEnter={() => setHov(true)}
      onMouseLeave={() => setHov(false)}
      style={{
        display: "block", width: "100%", textAlign: "left",
        background: active ? "#fffdf8" : hov ? `${src.accent}12` : "#fff",
        border: "none", cursor: "pointer",
        borderBottom: `1px solid ${RULE}`,
        borderLeft: active ? `4px solid ${src.accent}` : hov ? `4px solid ${src.accent}88` : "4px solid transparent",
        padding: 0, transition: "all 0.18s ease",
        transform: hov && !active ? "translateX(2px)" : "translateX(0)",
      }}
    >

      <div style={{
        background: cs.bg, padding: "4px 14px",
        display: "flex", alignItems: "center", justifyContent: "space-between",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>

          <span style={{
            fontFamily: FONT_DISPLAY_NARROW,
            fontSize: "9px", letterSpacing: "0.16em",
            background: "rgba(255,255,255,0.18)", color: "#fff",
            padding: "1px 5px", borderRadius: "1px",
          }}>{src.short}</span>
          <span style={{
            fontFamily: FONT_DISPLAY_NARROW,
            fontSize: "10px", letterSpacing: "0.16em", color: "rgba(255,255,255,0.85)",
          }}>{meeting.committee.toUpperCase()}</span>
        </div>
        <div style={{ display: "flex", gap: "4px", alignItems: "center" }}>
          {meeting.isAgendaOnly && (
            <span style={{
              background: "rgba(255,255,255,0.15)", color: "rgba(255,255,255,0.7)",
              fontSize: "8px", fontWeight: 700,
              letterSpacing: "0.1em", padding: "1px 4px", borderRadius: "1px",
              border: "1px solid rgba(255,255,255,0.2)",
            }}>AGENDA ONLY</span>
          )}
          {meeting.badge && (
            <span style={{
              background: "#FFE566", color: "#111",
              fontSize: "9px", fontWeight: 900,
              letterSpacing: "0.12em", padding: "1px 5px", borderRadius: "1px",
            }}>NEW</span>
          )}
        </div>
      </div>


      <div style={{ padding: "10px 14px 12px", display: "flex", gap: "11px", alignItems: "flex-start" }}>


        <div style={{
          flexShrink: 0, width: "42px",
          border: `1px solid ${RULE}`,
          borderRadius: "4px",
          overflow: "hidden",
          boxShadow: "0 1px 3px rgba(0,0,0,0.07)",
        }}>
          <div style={{
            background: src.accent,
            padding: "2px 0",
            textAlign: "center",
            fontFamily: FONT_DISPLAY,
            fontSize: "9px", letterSpacing: "0.14em", color: "#fff",
          }}>{meeting.shortDate.split(" ")[0]}</div>
          <div style={{
            background: "#fff",
            padding: "3px 0 4px",
            textAlign: "center",
            fontFamily: FONT_DISPLAY,
            fontSize: "22px", lineHeight: 1, color: INK, letterSpacing: "0.02em",
          }}>{meeting.shortDate.split(" ")[1]}</div>
        </div>

        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "flex-start", gap: "8px", marginBottom: "5px" }}>
            <div style={{
              fontFamily: FONT_HEADLINE,
              fontSize: "13px", fontWeight: 700, color: INK,
              lineHeight: 1.25, flex: 1, minWidth: 0,
            }}>{meeting.title}</div>
            <img
              src={src.avatar}
              alt={src.label}
              onError={e => { e.currentTarget.style.visibility = "hidden"; }}
              style={{
                width: "22px", height: "22px",
                borderRadius: "50%",
                objectFit: "cover",
                flexShrink: 0,
                border: `1.5px solid ${src.accent}`,
                opacity: active || hov ? 1 : 0.75,
                transition: "opacity 0.15s",
              }}
            />
          </div>
          {meeting.duration && (
            <div style={{ fontFamily: FONT_BODY, fontSize: "11px", color: "#666" }}>
              <span aria-hidden="true">{"›"}</span> {meeting.duration}
            </div>
          )}
        </div>
      </div>
    </button>
  );
}

// ── Shared sub-components ────────────────────────────────────────────────────

function ColHead({ children, dark, accent }) {
  return (
    <div style={{
      fontFamily: FONT_DISPLAY,
      fontSize: "13px", letterSpacing: "0.2em",
      color: "#fff",
      background: dark ? INK : (accent || TEAL),
      padding: "12px 14px",
      textAlign: "center",
    }}>{children}</div>
  );
}

function DocChips({ docs, accent }) {
  if (!docs || !docs.length) return null;
  const items = docs.map((doc, di) => {
    const docName = typeof doc === "string" ? doc : doc.name;
    const docUrl  = typeof doc === "string" ? null : doc.url;
    const chipStyle = {
      fontFamily: FONT_DISPLAY, fontSize: "9px",
      letterSpacing: "0.1em", padding: "2px 7px",
      background: docUrl ? "#fff" : CREAM,
      color: docUrl ? (accent || TEAL) : "#999",
      border: "1px solid " + (docUrl ? (accent || TEAL) : RULE),
      display: "inline-block",
    };
    const chip = <span style={chipStyle}>{docName}</span>;
    if (docUrl) {
      return <a key={di} href={docUrl} target="_blank" rel="noreferrer" style={{ textDecoration: "none" }}>{chip}</a>;
    }
    return <span key={di}>{chip}</span>;
  });
  return <div style={{ display: "flex", flexWrap: "wrap", gap: "4px", marginTop: "7px" }}>{items}</div>;
}

function VoteChip({ passed }) {
  return (
    <span style={{
      fontFamily: FONT_DISPLAY,
      fontSize: "10px", letterSpacing: "0.12em",
      padding: "2px 8px",
      background: passed ? "#1e5c2a" : "#7B2D2D",
      color: "#fff", flexShrink: 0,
    }}>{passed ? "PASSED" : "FAILED"}</span>
  );
}

function SummaryDetail({ meeting, onBack, isMobile, onTopicClick }) {
  const [tab, setTab] = useState("summary");
  const cs  = getCommitteeStyle(meeting.committee);
  const src = SOURCE_CONFIG[meeting.source];

  const hasCivic = !!(meeting.civicItems && meeting.civicItems.length);
  // Structured votes extracted from transcripts/minutes (non-CivicClerk
  // sources). CivicClerk's richer civicItems take precedence when present.
  const hasVotes = !hasCivic && !!(meeting.votes && meeting.votes.length);

  const voteTab = (hasCivic || hasVotes) ? [{ id: "votes", label: "Votes" }] : [];
  const tabs = [
    { id: "summary",    label: "Summary"    },
    { id: "agenda",     label: "Agenda"     },
    { id: "discussion", label: "Discussion" },
    { id: "actions",    label: "Actions"    },
    ...voteTab,
    { id: "documents",  label: "Documents"  },
  ];

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>


      <div style={{ background: "#fff", position: "relative", overflow: "hidden", flexShrink: 0, borderBottom: `1px solid ${RULE}` }}>
        <div style={{ background: src.accent, height: "4px" }} />


        <div style={{
          position: "absolute", right: "-10px", bottom: "-18px",
          fontFamily: FONT_DISPLAY_NARROW,
          fontSize: isMobile ? "68px" : "90px",
          color: "rgba(0,0,0,0.04)", letterSpacing: "0.05em",
          whiteSpace: "nowrap", pointerEvents: "none", userSelect: "none", lineHeight: 1,
        }}>{meeting.committee.toUpperCase()}</div>

        <div style={{ padding: isMobile ? "14px 18px 18px" : "20px 32px 22px", position: "relative" }}>
          {isMobile && (
            <button onClick={onBack} style={{
              background: "none", border: "none", cursor: "pointer",
              color: src.accent, fontFamily: FONT_BODY,
              fontSize: "12px", fontWeight: 600, padding: "0 0 12px",
            }}>{"<- All Meetings"}</button>
          )}


          <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "10px", flexWrap: "wrap" }}>
            <span style={{
              background: src.accent,
              fontFamily: FONT_DISPLAY,
              fontSize: "10px", letterSpacing: "0.16em",
              color: "#fff", padding: "3px 8px",
            }}>{src.label.toUpperCase()}</span>
            <span style={{
              background: cs.bg,
              fontFamily: FONT_DISPLAY,
              fontSize: "10px", letterSpacing: "0.16em",
              color: "#fff", padding: "3px 8px",
            }}>{meeting.committee.toUpperCase()}</span>
            <span style={{
              fontFamily: FONT_DISPLAY,
              fontSize: "10px", letterSpacing: "0.12em", color: "#5a5a5a",
            }}>{meeting.date.toUpperCase()}</span>
            {meeting.duration && (
              <span style={{ fontFamily: FONT_BODY, fontSize: "11px", color: "#666" }}>
                <span aria-hidden="true">{"›"}</span> {meeting.duration}
              </span>
            )}
            {meeting.badge && (
              <span style={{
                background: "#FFE566", color: "#111",
                fontSize: "9px", fontWeight: 900, letterSpacing: "0.12em", padding: "2px 6px",
              }}>NEW</span>
            )}
            {meeting.isAgendaOnly && (
              <span style={{
                background: "#F5E6C8", color: "#8B6914",
                fontSize: "9px", fontWeight: 700, letterSpacing: "0.1em", padding: "2px 6px",
                borderRadius: "1px",
              }}>AGENDA ONLY</span>
            )}
          </div>

          {meeting.isAgendaOnly && (
            <div style={{
              background: "#FFF8E7", borderLeft: "3px solid #D4A017",
              padding: "8px 12px", marginBottom: "10px",
              fontFamily: "'Source Sans 3', 'Source Sans Pro', sans-serif",
              fontSize: "12px", color: "#6B5A1E", lineHeight: 1.4,
            }}>
              This summary is based on the published agenda, not a recording of the meeting.
              It shows what was scheduled to be discussed, not necessarily what occurred or how votes were cast.
            </div>
          )}

          <a
            href={src.channel}
            target="_blank"
            rel="noreferrer"
            title={`${src.label} on YouTube`}
            style={{
              position: "absolute",
              top: isMobile ? "12px" : "16px",
              right: isMobile ? "14px" : "24px",
              lineHeight: 0, display: "block", zIndex: 2,
            }}
          >
            <img
              src={src.avatar}
              alt={src.label}
              style={{
                width: isMobile ? "56px" : "80px",
                height: isMobile ? "56px" : "80px",
                borderRadius: "50%",
                border: `2px solid ${src.accent}`,
                objectFit: "cover",
                display: "block",
                boxShadow: `0 0 0 3px #fff, 0 0 0 5px ${src.accent}`,
                transition: "transform 0.15s, box-shadow 0.15s",
              }}
              onMouseEnter={e => { e.target.style.transform="scale(1.08)"; e.target.style.boxShadow=`0 0 0 3px #fff, 0 0 0 6px ${src.accent}`; }}
              onMouseLeave={e => { e.target.style.transform="scale(1)";    e.target.style.boxShadow=`0 0 0 3px #fff, 0 0 0 5px ${src.accent}`; }}
              onError={e => { e.currentTarget.style.visibility = "hidden"; }}
            />
          </a>

          <h2 style={{
            fontFamily: FONT_HEADLINE,
            fontSize: isMobile ? "19px" : "24px",
            fontWeight: 700, color: INK,
            margin: "0 0 14px",
            lineHeight: 1.2,
            paddingRight: isMobile ? "56px" : "80px", // keep text clear of avatar
          }}>{meeting.title}</h2>

          {(() => {
            const isYoutube = meeting.url && (meeting.url.includes("youtube.com") || meeting.url.includes("youtu.be"));
            const sameDoc   = meeting.docUrl && meeting.docUrl === meeting.url;
            const primaryLabel = isYoutube
              ? "WATCH ON YOUTUBE"
              : (sameDoc || !meeting.docUrl) ? "VIEW AGENDA & PACKET" : "VIEW MEETING";
            const showSecondary = meeting.docUrl && !sameDoc;
            return (
              <div style={{ display: "flex", gap: "10px", flexWrap: "wrap" }}>
                <a href={meeting.url} target="_blank" rel="noreferrer"
                  onClick={() => track("Outbound Link", { source: meeting.source, kind: primaryLabel })}
                  style={{
                  display: "inline-flex", alignItems: "center", gap: "5px",
                  background: src.accent, color: "#fff",
                  fontFamily: FONT_DISPLAY, fontSize: "11px", letterSpacing: "0.14em",
                  padding: "7px 14px", textDecoration: "none", transition: "opacity 0.15s",
                }}
                onMouseEnter={e => e.currentTarget.style.opacity="0.8"}
                onMouseLeave={e => e.currentTarget.style.opacity="1"}
                >{primaryLabel}</a>
                {showSecondary && (
                  <a href={meeting.docUrl} target="_blank" rel="noreferrer" style={{
                    display: "inline-flex", alignItems: "center", gap: "5px",
                    border: `1px solid ${RULE}`, color: "#666",
                    background: "#f5f3ef",
                    fontFamily: FONT_DISPLAY, fontSize: "11px", letterSpacing: "0.14em",
                    padding: "7px 14px", textDecoration: "none", transition: "all 0.15s",
                  }}
                  onMouseEnter={e => { e.currentTarget.style.borderColor="#999"; e.currentTarget.style.color=INK; e.currentTarget.style.background="#eee"; }}
                  onMouseLeave={e => { e.currentTarget.style.borderColor=RULE; e.currentTarget.style.color="#666"; e.currentTarget.style.background="#f5f3ef"; }}
                  >AGENDA & PACKET</a>
                )}
              </div>
            );
          })()}

          {(meeting.topics || []).length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", marginTop: "12px" }}>
              {meeting.topics.map(t => (
                <button
                  key={t}
                  onClick={() => { track("Filter", { source: "topic", panel: "detail" }); onTopicClick && onTopicClick(t); }}
                  title={`See all meetings about ${t}`}
                  aria-label={`See all meetings about ${t}`}
                  style={{
                    fontFamily: FONT_DISPLAY, fontSize: "10px",
                    letterSpacing: "0.12em", cursor: "pointer",
                    color: src.accent, background: "transparent",
                    border: `1px solid ${src.accent}55`, borderRadius: "999px",
                    padding: "3px 10px", lineHeight: 1.4, transition: "all 0.15s",
                  }}
                  onMouseEnter={e => { e.currentTarget.style.background = `${src.accent}14`; e.currentTarget.style.borderColor = src.accent; }}
                  onMouseLeave={e => { e.currentTarget.style.background = "transparent"; e.currentTarget.style.borderColor = `${src.accent}55`; }}
                >{t.toUpperCase()}</button>
              ))}
            </div>
          )}
        </div>
      </div>


      <div role="tablist" aria-label="Meeting sections" style={{
        display: "flex", background: CREAM, borderBottom: `2px solid ${RULE}`,
        overflowX: "auto", WebkitOverflowScrolling: "touch", scrollbarWidth: "none", flexShrink: 0,
        padding: "10px 12px 0", gap: "4px",
      }}>
        {tabs.map(t => {
          const active = tab === t.id;
          return (
            <button
              key={t.id}
              role="tab"
              aria-selected={active}
              onClick={() => setTab(t.id)}
              onMouseEnter={e => {
                if (!active) {
                  e.currentTarget.style.background = "#fff";
                  e.currentTarget.style.color = src.accent;
                  e.currentTarget.style.borderColor = src.accent;
                }
              }}
              onMouseLeave={e => {
                if (!active) {
                  e.currentTarget.style.background = "transparent";
                  e.currentTarget.style.color = "#999";
                  e.currentTarget.style.borderColor = RULE;
                }
              }}
              style={{
                cursor: "pointer",
                fontFamily: FONT_DISPLAY,
                fontSize: "11px",
                letterSpacing: "0.14em",
                padding: isMobile ? "7px 11px" : "8px 16px",
                whiteSpace: "nowrap",
                transition: "all 0.15s",
                background:   active ? src.accent : "transparent",
                color:        active ? "#fff" : "#999",
                border:       `1px solid ${active ? src.accent : RULE}`,
                borderBottom: active ? `1px solid ${src.accent}` : `1px solid ${RULE}`,
                marginBottom: active ? "-2px" : "0",
                position: "relative",
              }}
            >{t.label.toUpperCase()}</button>
          );
        })}
      </div>


      <div style={{
        flex: 1, overflowY: "auto", WebkitOverflowScrolling: "touch",
        background: CREAM, padding: isMobile ? "20px 18px 40px" : "26px 32px 40px",
      }}>

        {tab === "summary" && <>
          <div style={{ borderLeft: `4px solid ${src.accent}`, paddingLeft: "20px", marginBottom: "26px" }}>
            <div style={{ fontFamily: FONT_DISPLAY, fontSize: "10px", letterSpacing: "0.18em", color: src.accent, marginBottom: "8px" }}>MEETING OVERVIEW</div>
            <p style={{ fontFamily: FONT_BODY, fontSize: "15px", lineHeight: 1.8, color: INK, margin: 0, fontStyle: "italic" }}>{meeting.overview}</p>
              {meeting.isAgendaOnly && (
                <div style={{
                  background: "#fffbea", border: "1px solid #e8d87a",
                  padding: "8px 14px", marginTop: "16px",
                  fontFamily: "'Source Sans 3', sans-serif", fontSize: "12px",
                  color: "#7a6a00", letterSpacing: "0.03em",
                }}>
                  <strong>AGENDA PREVIEW ONLY</strong> - This summary reflects the published agenda. Actual votes, decisions, and discussion outcomes may differ. A full transcript-based summary will be available once video captions are processed.
                </div>
              )}
          </div>
          <div style={{ borderTop: `1px solid ${RULE}`, paddingTop: "22px" }}>
            <div style={labelStyle}>Public Comment</div>
            <p style={bodyStyle}>{meeting.publicComment}</p>
          </div>
        </>}

        {tab === "agenda" && (() => {
          const vid = meeting.url.match(/(?:youtu\.be\/|v=)([A-Za-z0-9_-]{11})/)?.[1];
          const toSec = t => { const p = t.split(":").map(Number); return p.length === 3 ? p[0]*3600+p[1]*60+p[2] : p[0]*60+p[1]; };
          // Determine if ANY agenda item has a real timestamp
          const anyHasTimestamp = vid && meeting.agenda.some(e => e.time && e.time !== "N/A" && /^\d/.test(e.time));
          return (
            <>
              <div style={labelStyle}>Agenda Items</div>
              <div style={{ marginTop: "4px" }}>
                {meeting.agenda.map((entry, i) => {
                  const hasTimestamp = anyHasTimestamp && entry.time && entry.time !== "N/A" && /^\d/.test(entry.time);
                  const ytUrl = hasTimestamp ? `https://www.youtube.com/watch?v=${vid}&t=${toSec(entry.time)}s` : null;
                  return (
                    <div key={i} style={{ display: "flex", alignItems: "flex-start", gap: "14px", padding: "11px 0", borderBottom: `1px solid ${RULE}` }}>
                      {anyHasTimestamp && (
                        hasTimestamp ? (
                          <a href={ytUrl} target="_blank" rel="noreferrer" title={`Watch at ${entry.time}`}
                            style={{
                              fontFamily: FONT_DATA, fontSize: "11px", fontWeight: 700,
                              color: src.accent, minWidth: "50px", textAlign: "right",
                              flexShrink: 0, textDecoration: "none",
                              borderBottom: `1px dashed ${src.accent}`,
                              lineHeight: 1, paddingTop: "3px", transition: "opacity 0.15s",
                            }}
                            onMouseEnter={e => e.currentTarget.style.opacity="0.6"}
                            onMouseLeave={e => e.currentTarget.style.opacity="1"}
                          >{entry.time}</a>
                        ) : (
                          <span style={{
                            fontFamily: FONT_DATA, fontSize: "11px",
                            color: "#666", minWidth: "50px", textAlign: "right",
                            flexShrink: 0, lineHeight: 1, paddingTop: "3px",
                          }}>--:--</span>
                        )
                      )}
                      <div style={{ width: "6px", height: "6px", minWidth: "6px", borderRadius: "50%", background: src.accent, marginTop: "5px", flexShrink: 0 }} />
                      <span style={bodyStyle}>{entry.item}</span>
                    </div>
                  );
                })}
              </div>
            </>
          );
        })()}

        {tab === "discussion" && meeting.discussions.map((d, i) => (
          <div key={i} style={{
            padding: "16px 18px", marginBottom: "0",
            background: i % 2 === 0 ? "#fff" : CREAM,
            borderBottom: `1px solid ${RULE}`,
            cursor: "default", transition: "background 0.15s",
          }}
          onMouseEnter={e => e.currentTarget.style.background = `${src.accent}18`}
          onMouseLeave={e => e.currentTarget.style.background = i % 2 === 0 ? "#fff" : CREAM}
          >
            <div style={{ fontFamily: FONT_HEADLINE, fontSize: "15px", fontWeight: 700, color: INK, margin: "0 0 10px", paddingBottom: "7px", borderBottom: `2px solid ${RULE}` }}>{d.item}</div>
            <p style={bodyStyle}>{d.body}</p>
          </div>
        ))}

        {tab === "actions" && (() => {
          const topics = meeting.civicItems
            ? meeting.civicItems
                .filter(it => it.number !== "1" && !it.name.toLowerCase().startsWith("adjournment"))
                .flatMap(it => it.children && it.children.length ? it.children : [it])
                .filter(it => !it.name.toLowerCase().includes("minutes of the preceding") && !it.name.toLowerCase().startsWith("adjournment"))
            : meeting.discussions.map((d, i) => ({ number: String(i + 1), name: d.item, docs: [] }));

          const actions = meeting.actionItems;

          return (
            <div style={{ border: `1px solid ${RULE}`, overflow: "hidden" }}>


              <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "1fr 1fr" }}>
                <ColHead accent={src.accent}>Topics Discussed</ColHead>
                <div style={{ borderLeft: isMobile ? "none" : `1px solid #333`, borderTop: isMobile ? `1px solid #333` : "none" }}>
                  <ColHead dark accent={src.accent}>{"Actions & Next Steps"}</ColHead>
                </div>
              </div>


              <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "1fr 1fr", borderTop: `1px solid ${RULE}` }}>


                <div style={{ borderRight: isMobile ? "none" : `1px solid ${RULE}`, borderBottom: isMobile ? `1px solid ${RULE}` : "none" }}>
                  {topics.map((topic, i) => (
                    <div
                      key={i}
                      style={{
                        padding: "12px 14px",
                        borderBottom: i < topics.length - 1 ? `1px solid ${RULE}` : "none",
                        background: i % 2 === 0 ? "#fff" : CREAM,
                        cursor: "default",
                        transition: "background 0.15s",
                      }}
                      onMouseEnter={e => e.currentTarget.style.background = `${src.accent}18`}
                      onMouseLeave={e => e.currentTarget.style.background = i % 2 === 0 ? "#fff" : CREAM}
                    >
                      <div style={{ display: "flex", gap: "8px", alignItems: "flex-start" }}>
                        <span style={{
                          fontFamily: FONT_DISPLAY,
                          fontSize: "10px", letterSpacing: "0.08em",
                          color: src.accent, flexShrink: 0,
                          paddingTop: "2px", minWidth: "20px",
                        }}>{topic.number}</span>
                        <span style={{
                          fontFamily: FONT_BODY,
                          fontSize: "13px", lineHeight: 1.6, color: INK,
                        }}>{topic.name}</span>
                      </div>
                      <DocChips docs={topic.docs} accent={src.accent} />
                    </div>
                  ))}
                </div>


                <div>
                  {actions.map((action, i) => (
                    <div
                      key={i}
                      style={{
                        padding: "12px 14px",
                        borderBottom: i < actions.length - 1 ? `1px solid ${RULE}` : "none",
                        background: i % 2 === 0 ? "#fff" : CREAM,
                        cursor: "default",
                        transition: "background 0.15s",
                      }}
                      onMouseEnter={e => e.currentTarget.style.background = `${src.accent}18`}
                      onMouseLeave={e => e.currentTarget.style.background = i % 2 === 0 ? "#fff" : CREAM}
                    >
                      <div style={{ display: "flex", gap: "8px", alignItems: "flex-start" }}>
                        <span style={{
                          display: "inline-flex", alignItems: "center", justifyContent: "center",
                          width: "18px", height: "18px", minWidth: "18px",
                          background: src.accent, color: "#fff", borderRadius: "50%",
                          fontFamily: FONT_DISPLAY, fontSize: "10px",
                          marginTop: "2px", flexShrink: 0,
                        }}>{i + 1}</span>
                        <span style={{
                          fontFamily: FONT_BODY,
                          fontSize: "13px", lineHeight: 1.6, color: INK,
                        }}>{action}</span>
                      </div>
                    </div>
                  ))}
                  {actions.length === 0 && (
                    <div style={{ padding: "16px 14px", fontFamily: FONT_BODY, fontSize: "13px", color: "#666", fontStyle: "italic" }}>
                      No formal actions recorded.
                    </div>
                  )}
                </div>

              </div>
            </div>
          );
        })()}

        {tab === "votes" && hasCivic && (() => {
          const VoteBar = ({ votes, label, color }) => {
            if (!votes.length) return null;
            return (
              <div style={{ flex: 1 }}>
                <div style={{
                  background: color, padding: "5px 10px", marginBottom: "6px",
                  fontFamily: FONT_DISPLAY,
                  fontSize: "11px", letterSpacing: "0.12em", color: "#fff",
                  textAlign: "center",
                }}>{label}</div>
                <div style={{
                  fontFamily: FONT_BODY,
                  fontSize: "28px", fontWeight: 700, textAlign: "center",
                  color: INK, lineHeight: 1,
                }}>{votes.length}</div>
                <div style={{
                  fontFamily: FONT_BODY,
                  fontSize: "10px", color: "#888", marginTop: "8px", lineHeight: 1.6,
                  textAlign: "center",
                }}>
                  {votes.map((n, i) => <div key={i}>{n.trim()}</div>)}
                </div>
              </div>
            );
          };

          const isPublicCommentItem = (item) =>
            item.name.toLowerCase().startsWith("public comment");

          const renderItem = (item, depth = 0) => {
            const hasVotes    = item.votes && item.votes.length > 0;
            const hasDocs     = item.docs  && item.docs.length  > 0;
            const hasChildren = item.children && item.children.length > 0;
            const isPublicComment = isPublicCommentItem(item);
            const isEmpty = !hasVotes && !hasDocs && !hasChildren && !isPublicComment;

            return (
              <div key={item.number} style={{ marginBottom: depth === 0 ? "20px" : "14px" }}>

                <div style={{
                  display: "flex", alignItems: "flex-start", gap: "10px",
                  padding: depth === 0 ? "10px 0 8px" : "6px 0 4px",
                  borderTop: depth === 0 ? `2px solid ${RULE}` : "none",
                }}>
                  <span style={{
                    fontFamily: FONT_DISPLAY,
                    fontSize: "12px", letterSpacing: "0.1em",
                    color: depth === 0 ? src.accent : "#999",
                    flexShrink: 0, minWidth: "24px",
                    paddingTop: "1px",
                  }}>{item.number || (depth > 0 ? "•" : "")}</span>
                  <div style={{ flex: 1 }}>
                    <span style={{
                      fontFamily: depth === 0 ? FONT_HEADLINE : FONT_BODY,
                      fontSize: depth === 0 ? "14px" : "13px",
                      fontWeight: depth === 0 ? 700 : (isEmpty ? 400 : 600),
                      color: isEmpty ? "#666" : INK,
                      fontStyle: "normal",
                      lineHeight: 1.4,
                    }}>{item.name ? item.name.replace(/<[^>]*>/g, "") : ""}</span>
                  </div>
                </div>


                {isPublicComment && meeting.publicComment && (
                  <div style={{
                    marginLeft: "28px", marginBottom: "10px",
                    padding: "12px 16px",
                    background: "#fff",
                    border: `1px solid ${RULE}`,
                    borderLeft: `4px solid ${src.accent}`,
                  }}>
                    <div style={{
                      fontFamily: FONT_DISPLAY,
                      fontSize: "9px", letterSpacing: "0.16em",
                      color: src.accent, marginBottom: "7px",
                    }}>PUBLIC COMMENT</div>
                    <p style={{
                      fontFamily: FONT_BODY,
                      fontSize: "13px", lineHeight: 1.75,
                      color: INK, margin: 0,
                    }}>{meeting.publicComment}</p>
                  </div>
                )}


                {item.votes.map((v, vi) => (
                  <div key={vi} style={{
                    marginLeft: depth === 0 ? "0" : "28px",
                    marginBottom: "12px",
                    background: "#fff",
                    border: `1px solid ${RULE}`,
                    borderLeft: `4px solid ${v.passed ? "#1e5c2a" : "#7B2D2D"}`,
                  }}>

                    <div style={{
                      padding: "10px 14px 8px",
                      borderBottom: `1px solid ${RULE}`,
                      display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap",
                    }}>
                      <VoteChip passed={v.passed} />
                      <span style={{
                        fontFamily: FONT_BODY,
                        fontSize: "13px", fontWeight: 600, color: INK,
                        fontStyle: "italic", flex: 1,
                      }}>Motion: {v.motion}</span>
                    </div>

                    <div style={{
                      padding: "7px 14px 8px",
                      borderBottom: `1px solid ${RULE}`,
                      fontFamily: FONT_BODY,
                      fontSize: "12px", color: "#666",
                    }}>
                      Initiated by <strong style={{color: INK}}>{v.initiator}</strong>
                      {v.seconder && <>, seconded by <strong style={{color: INK}}>{v.seconder}</strong></>}
                    </div>

                    <div style={{ display: "flex", gap: "1px", padding: "12px 14px" }}>
                      <VoteBar votes={v.yes}     label="Yes"     color="#2D5A3D" />
                      {(v.no.length > 0 || v.yes.length > 0) && (
                        <VoteBar votes={v.no.length ? v.no : []} label="No" color="#7B2D2D" />
                      )}
                      {v.abstain && v.abstain.length > 0 && (
                        <VoteBar votes={v.abstain} label="Abstain" color="#8a7a2a" />
                      )}
                      {v.no.length === 0 && (
                        <div style={{ flex: 1, textAlign: "center" }}>
                          <div style={{ background: "#7B2D2D", padding: "5px 10px", marginBottom: "6px", fontFamily: FONT_DISPLAY, fontSize: "11px", letterSpacing: "0.12em", color: "#fff" }}>No</div>
                          <div style={{ fontFamily: FONT_BODY, fontSize: "28px", fontWeight: 700, color: "#666" }}>0</div>
                        </div>
                      )}
                    </div>
                  </div>
                ))}


                {hasDocs && (
                  <div style={{
                    marginLeft: depth === 0 ? "0" : "28px",
                    marginBottom: "8px",
                    display: "flex", flexWrap: "wrap", gap: "6px",
                  }}>
                    {item.docs.map((doc, di) => {
                      const docName = typeof doc === "string" ? doc : doc.name;
                      const docUrl  = typeof doc === "string" ? null : doc.url;
                      const chip = (
                        <span style={{
                          fontFamily: FONT_DISPLAY,
                          fontSize: "9px", letterSpacing: "0.1em",
                          background: docUrl ? "#fff" : CREAM,
                          color: docUrl ? src.accent : "#888",
                          border: `1px solid ${docUrl ? src.accent : RULE}`,
                          padding: "3px 8px",
                          display: "inline-flex", alignItems: "center", gap: "4px",
                          transition: "all 0.15s",
                        }}>
                           {docName}
                        </span>
                      );
                      return docUrl ? (
                        <a key={di} href={docUrl} target="_blank" rel="noreferrer"
                          onClick={() => track("Outbound Link", { source: meeting.source, kind: "AGENDA DOCUMENT" })}
                          style={{ textDecoration: "none" }}
                          onMouseEnter={e => e.currentTarget.querySelector("span").style.background = "#fef0ee"}
                          onMouseLeave={e => e.currentTarget.querySelector("span").style.background = "#fff"}
                        >{chip}</a>
                      ) : (
                        <span key={di}>{chip}</span>
                      );
                    })}
                  </div>
                )}


                {hasChildren && (
                  <div style={{ marginLeft: depth === 0 ? "14px" : "28px" }}>
                    {item.children.map(child => renderItem(child, depth + 1))}
                  </div>
                )}
              </div>
            );
          };

          return (
            <>
              <div style={{ ...labelStyle, marginBottom: "16px" }}>
                Motions & Votes - Sourced from{" "}
                <a href={`https://${src.docHost}`} target="_blank" rel="noreferrer"
                  style={{ color: src.accent, textDecoration: "none", fontWeight: 600 }}>
                  CivicClerk
                </a>
              </div>
              {meeting.civicItems.map(item => renderItem(item, 0))}
            </>
          );
        })()}

        {tab === "votes" && hasVotes && (
          <>
            <div style={{ ...labelStyle, marginBottom: "4px" }}>Motions & Votes</div>
            <p style={{ ...bodyStyle, color: "#777", marginBottom: "16px", fontSize: "12px" }}>
              Extracted from the meeting {meeting.isAgendaOnly ? "record" : "recording or official minutes"} — see the source documents for the authoritative record.
            </p>
            {meeting.votes.map((v, vi) => (
              <div key={vi} style={{
                background: "#fff", border: `1px solid ${RULE}`,
                borderLeft: `4px solid ${
                  /fail|denied|reject/i.test(v.outcome || "") ? "#7B2D2D"
                  : /tabl|postpon|refer/i.test(v.outcome || "") ? "#8B6914"
                  : "#2D5A3D"}`,
                padding: "12px 16px", marginBottom: "10px",
              }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: "10px", flexWrap: "wrap" }}>
                  <div style={{ fontFamily: FONT_HEADLINE, fontWeight: 700, fontSize: "14px", color: INK, flex: 1, minWidth: "200px" }}>
                    {v.item}
                  </div>
                  <div style={{ fontFamily: FONT_DISPLAY, fontSize: "12px", letterSpacing: "0.1em", whiteSpace: "nowrap",
                    color: /fail|denied|reject/i.test(v.outcome || "") ? "#7B2D2D"
                         : /tabl|postpon|refer/i.test(v.outcome || "") ? "#8B6914"
                         : "#2D5A3D" }}>
                    {(v.outcome || "").toUpperCase()}{v.tally ? ` · ${v.tally}` : ""}
                  </div>
                </div>
                {v.motion && (
                  <div style={{ ...bodyStyle, fontSize: "12.5px", marginTop: "6px", color: "#444" }}>{v.motion}</div>
                )}
                {(v.mover || v.second) && (
                  <div style={{ fontFamily: FONT_BODY, fontSize: "11px", color: "#888", marginTop: "6px", fontStyle: "italic" }}>
                    {v.mover ? `Moved by ${v.mover}` : ""}{v.mover && v.second ? " · " : ""}{v.second ? `Seconded by ${v.second}` : ""}
                  </div>
                )}
              </div>
            ))}
          </>
        )}

                {tab === "documents" && <>
          <div style={labelStyle}>Official Documents</div>
          <p style={{ ...bodyStyle, color: "#777", marginBottom: "18px", marginTop: "4px" }}>
            Published by {SOURCE_CONFIG[meeting.source].label} and linked from the meeting's YouTube description.
          </p>
          {meeting.docUrl ? (
            <a href={meeting.docUrl} target="_blank" rel="noreferrer" style={{ textDecoration: "none" }}>
              <div style={{
                display: "flex", alignItems: "center", gap: "16px",
                padding: "16px 18px", background: "#fff",
                border: `1px solid ${RULE}`, borderLeft: `4px solid ${src.accent}`,
                transition: "box-shadow 0.15s",
              }}
              onMouseEnter={e => e.currentTarget.style.boxShadow="0 2px 12px rgba(0,0,0,0.08)"}
              onMouseLeave={e => e.currentTarget.style.boxShadow="none"}
              >
                <span style={{ fontSize: "26px", lineHeight: 1 }}></span>
                <div>
                  <div style={{ fontFamily: FONT_HEADLINE, fontSize: "14px", fontWeight: 700, color: INK, marginBottom: "3px" }}>{"Meeting Agenda & Packet"}</div>
                  <div style={{ fontFamily: FONT_DISPLAY, fontSize: "10px", letterSpacing: "0.12em", color: src.accent }}>
                    {SOURCE_CONFIG[meeting.source].docHost.toUpperCase()} <span aria-hidden="true">{"→ VIEW PDF →"}</span>
                  </div>
                </div>
              </div>
            </a>
          ) : (
            <p style={{ ...bodyStyle, color: "#5a5a5a", fontStyle: "italic" }}>No documents linked for this meeting.</p>
          )}
        </>}
      </div>
    </div>
  );
}

const labelStyle = { fontFamily: FONT_DISPLAY, fontSize: "10px", letterSpacing: "0.18em", color: "#5a5a5a", marginBottom: "12px", paddingBottom: "5px", borderBottom: `1px solid ${RULE}` };
const bodyStyle  = { fontFamily: FONT_BODY, fontSize: "14px", lineHeight: 1.8, color: "#2A2015", margin: 0 };

function UpcomingMeetings({ isMobile }) {
  const [upFilter, setUpFilter]       = useState("all");
  const [calOpen,  setCalOpen]        = useState(null);  // ev key with open dropdown
  // Local date, not toISOString() — the UTC date rolls over at 7 PM Central,
  // which made today's (mostly evening!) meetings vanish from Upcoming.
  const today = new Date().toLocaleDateString("en-CA");

  const allUpcoming = JURISDICTIONS
    .filter(j => upFilter === "all" || upFilter === j.key)
    .flatMap(j => UPCOMING[j.key] || [])
    .filter(e => e.date >= today)
    .sort((a, b) => a.date.localeCompare(b.date) || a.time.localeCompare(b.time));

  const grouped = allUpcoming.reduce((acc, ev) => {
    const key = ev.date;
    if (!acc[key]) acc[key] = [];
    acc[key].push(ev);
    return acc;
  }, {});

  const tomorrow = new Date(Date.now() + 86400000).toLocaleDateString("en-CA");

  const dateLabel = (dateStr) => {
    if (dateStr === today) return "TODAY";
    if (dateStr === tomorrow) return "TOMORROW";
    const d = new Date(dateStr + "T00:00:00");
    return d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" }).toUpperCase();
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, overflow: "hidden" }}>


      <div style={{ padding: "12px 14px 10px", borderBottom: `2px solid ${RULE}`, background: CREAM }}>


        <div style={{ display: "flex", gap: "6px", flexWrap: "wrap", marginBottom: "10px" }}>
          {FILTER_OPTIONS.map(({ key, label, color, avatar }) => {
            const active = upFilter === key;
            return (
              <button
                key={key}
                aria-pressed={active}
                aria-label={`Filter upcoming meetings by ${label}`}
                onClick={() => { track("Filter", { source: key, panel: "upcoming" }); setUpFilter(key); }}
                onMouseEnter={e => { if (!active) { e.currentTarget.style.background = `${color}15`; e.currentTarget.style.borderColor = color; e.currentTarget.style.color = color; }}}
                onMouseLeave={e => { if (!active) { e.currentTarget.style.background = "transparent"; e.currentTarget.style.borderColor = "#d0ccc4"; e.currentTarget.style.color = "#888"; }}}
                style={{
                  background:    active ? color : "transparent",
                  border:        `1.5px solid ${active ? color : "#d0ccc4"}`,
                  color:         active ? "#fff" : "#888",
                  fontFamily:    FONT_DISPLAY,
                  fontSize:      "12px",
                  letterSpacing: "0.12em",
                  padding:       "6px 13px",
                  cursor:        "pointer",
                  transition:    "all 0.15s",
                  display:       "flex", alignItems: "center", gap: "7px",
                  whiteSpace:    "nowrap",
                }}
              >
                {avatar && (
                  <img
                    src={avatar}
                    alt={label}
                    onError={e => { e.currentTarget.style.visibility = "hidden"; }}
                    style={{
                      width: "18px", height: "18px",
                      borderRadius: "50%",
                      objectFit: "cover",
                      flexShrink: 0,
                      opacity: active ? 1 : 0.7,
                      border: active ? "1.5px solid rgba(255,255,255,0.5)" : "1.5px solid transparent",
                      transition: "opacity 0.15s",
                    }}
                  />
                )}
                {label}
              </button>
            );
          })}
        </div>


        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <div style={{
            fontFamily: FONT_DISPLAY,
            fontSize: "10px", letterSpacing: "0.12em",
            color: "#5a5a5a", display: "flex", alignItems: "center", gap: "6px",
          }}>
            <span style={{ color: TEAL, fontSize: "11px" }}></span>
            {allUpcoming.length} MEETINGS SCHEDULED
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: "14px" }}>
            <a
              href={`webcal://${window.location.host}${BASE_URL}meetings.ics`}
              onClick={() => track("Outbound Link", { source: "all", kind: "CALENDAR SUBSCRIBE" })}
              title={`Subscribe: every upcoming meeting from all ${GOV_COUNT_WORD} governments, auto-updating in your calendar app`}
              style={{
                fontFamily: FONT_DISPLAY,
                fontSize: "10px", letterSpacing: "0.12em",
                color: TEAL, textDecoration: "none",
                display: "flex", alignItems: "center", gap: "4px",
              }}>
              <span aria-hidden="true" style={{ fontSize: "11px" }}>📅</span> SUBSCRIBE
            </a>
            {(() => {
              // Calendar destination follows the source filter so the link
              // doesn't always drop the reader on the first jurisdiction.
              const calLabel = upFilter === "all" ? "FULL CALENDARS" : "FULL CALENDAR";
              return (
                <a href={CAL_URLS[upFilter] || CAL_URLS.all} target="_blank" rel="noreferrer"
                  style={{
                    fontFamily: FONT_DISPLAY,
                    fontSize: "10px", letterSpacing: "0.12em",
                    color: TEAL, textDecoration: "none",
                    display: "flex", alignItems: "center", gap: "4px",
                  }}>
                  {calLabel} <span aria-hidden="true" style={{ fontSize: "11px" }}>{"›"}</span>
                </a>
              );
            })()}
          </div>
        </div>
      </div>


      <div style={{ flex: 1, overflowY: "auto", WebkitOverflowScrolling: "touch" }} onClick={e => { if (calOpen) setCalOpen(null); }}>
        {Object.entries(grouped).slice(0, 12).map(([date, events]) => {
          const isToday    = date === today;
          const isTomorrow = date === tomorrow;
          const isImminent = isToday || isTomorrow;

          return (
            <div key={date}>

              <div style={{
                padding: "14px 14px 10px",
                background: CREAM,
                position: "relative",
                display: "flex", alignItems: "center", justifyContent: "center",
              }}>

                <div style={{
                  position: "absolute", left: "14px", right: "14px",
                  top: "50%", height: "1px",
                  background: isImminent ? INK : "#ccc",
                }} />

                <span style={{
                  position: "relative",
                  fontFamily:    FONT_DISPLAY,
                  fontSize:      isImminent ? "13px" : "12px",
                  letterSpacing: "0.2em",
                  color:         isImminent ? "#fff" : INK,
                  background:    isImminent ? INK : CREAM,
                  padding:       "3px 12px",
                }}>{dateLabel(date)}</span>
              </div>


              {events.map((ev, i) => {
                const src = SOURCE_CONFIG[ev.source];
                const evKey  = `${ev.date}-${i}`;
                const isOpen = calOpen === evKey;
                const evSrc  = SOURCE_CONFIG[ev.source];
                const evName = encodeURIComponent(ev.name);
                const evOrg  = evSrc ? evSrc.label : "";
                const evDesc = encodeURIComponent(ev.name + " - " + evOrg);
                const calcUrls = () => {
                  let h = 0; let m = 0;
                  if (ev.time) {
                    const tp = ev.time.split(":");
                    h = parseInt(tp[0], 10);
                    const ms = tp[1] ? tp[1].split(" ") : ["00","AM"];
                    m = parseInt(ms[0], 10);
                    const ap = (ms[1] || (ev.time.indexOf("PM") > -1 ? "PM" : "AM")).toUpperCase();
                    if (ap === "PM" && h !== 12) h += 12;
                    if (ap === "AM" && h === 12) h = 0;
                  }
                  const et = h * 60 + m + 90;
                  const eh = Math.floor(et / 60);
                  const em = et % 60;
                  const pad = (n) => (n < 10 ? "0" : "") + n;
                  const dd = ev.date.split("-").join("");
                  if (!ev.time) {
                    return {
                      google:  "https://calendar.google.com/calendar/render?action=TEMPLATE&text=" + evName + "&dates=" + dd + "/" + dd + "&details=" + evDesc,
                      outlook: "https://outlook.live.com/calendar/0/addevent?subject=" + evName + "&startdt=" + ev.date + "&enddt=" + ev.date + "&body=" + evDesc + "&allday=true",
                    };
                  }
                  const gS = dd + "T" + pad(h) + pad(m) + "00";
                  const gE = dd + "T" + pad(eh) + pad(em) + "00";
                  const oS = ev.date + "T" + pad(h) + ":" + pad(m) + ":00";
                  const oE = ev.date + "T" + pad(eh) + ":" + pad(em) + ":00";
                  return {
                    google:  "https://calendar.google.com/calendar/render?action=TEMPLATE&text=" + evName + "&dates=" + gS + "/" + gE + "&details=" + evDesc,
                    outlook: "https://outlook.live.com/calendar/0/addevent?subject=" + evName + "&startdt=" + oS + "&enddt=" + oE + "&body=" + evDesc,
                  };
                };
                const urls = calcUrls();
                return (
                      <div key={i} style={{ position: "relative" }}>

                        <a href={ev.url} target="_blank" rel="noreferrer"
                          style={{ textDecoration: "none", display: "block" }}>
                          <div
                            style={{
                              padding: "10px 14px",
                              borderBottom: `1px solid ${RULE}`,
                              background: isOpen ? `${src.accent}12` : "#fff",
                              display: "flex", alignItems: "center", gap: "10px",
                              transition: "background 0.12s",
                            }}
                            onMouseEnter={e => { e.currentTarget.style.background = `${src.accent}12`; }}
                            onMouseLeave={e => { if (!isOpen) e.currentTarget.style.background = "#fff"; }}
                          >

                            <img
                              src={src.avatar}
                              alt={src.label}
                              onError={e => { e.currentTarget.style.visibility = "hidden"; }}
                              style={{
                                width: "28px", height: "28px",
                                borderRadius: "50%",
                                objectFit: "cover",
                                flexShrink: 0,
                                border: `1.5px solid ${src.accent}`,
                              }}
                            />


                            <div style={{ flex: 1, minWidth: 0 }}>
                              <div style={{
                                fontFamily: FONT_HEADLINE,
                                fontSize: "13px", fontWeight: 600,
                                color: INK, lineHeight: 1.3,
                                // Wrap to up to two lines instead of clipping to
                                // one, so long committee names show in full.
                                display: "-webkit-box", WebkitLineClamp: 2,
                                WebkitBoxOrient: "vertical", overflow: "hidden",
                              }}>{ev.name}</div>
                              <div style={{
                                display: "flex", alignItems: "center", gap: "6px", marginTop: "3px",
                              }}>
                                {ev.time && (
                                  <span style={{
                                    fontFamily: FONT_DISPLAY,
                                    fontSize: "10px", letterSpacing: "0.1em", color: "#5a5a5a",
                                  }}> {ev.time}</span>
                                )}
                                <span style={{
                                  fontFamily: FONT_DISPLAY,
                                  fontSize: "10px", letterSpacing: "0.1em",
                                  color: src.accent, fontWeight: 600,
                                }}>{src.label}</span>
                              </div>
                            </div>


                            <span aria-hidden="true" style={{ color: "#5a5a5a", fontSize: "14px", flexShrink: 0 }}>{"›"}</span>
                          </div>
                        </a>


                        <button
                          onClick={e => { e.stopPropagation(); setCalOpen(isOpen ? null : evKey); }}
                          style={{
                            position: "absolute",
                            right: "32px", top: "50%", transform: "translateY(-50%)",
                            background: isOpen ? src.accent : "transparent",
                            border: `1px solid ${isOpen ? src.accent : "#ddd"}`,
                            color: isOpen ? "#fff" : "#666",
                            fontFamily: FONT_DISPLAY,
                            fontSize: "9px", letterSpacing: "0.1em",
                            padding: "3px 8px",
                            cursor: "pointer",
                            transition: "all 0.15s",
                            zIndex: 2,
                            display: "flex", alignItems: "center", gap: "4px",
                          }}
                          onMouseEnter={e => { if (!isOpen) { e.currentTarget.style.borderColor = src.accent; e.currentTarget.style.color = src.accent; }}}
                          onMouseLeave={e => { if (!isOpen) { e.currentTarget.style.borderColor = "#ddd"; e.currentTarget.style.color = "#666"; }}}
                          aria-label={`Add ${ev.name} to calendar`}
                          aria-expanded={isOpen}
                          title="Add to calendar"
                        >
                          + CAL
                        </button>


                        {isOpen && (
                          <div style={{
                            position: "absolute",
                            right: "14px", top: "calc(100% - 1px)",
                            background: "#fff",
                            border: `1px solid ${RULE}`,
                            borderTop: `2px solid ${src.accent}`,
                            zIndex: 100,
                            minWidth: "160px",
                            boxShadow: "0 4px 12px rgba(0,0,0,0.1)",
                          }}>
                            <div style={{
                              fontFamily: FONT_DISPLAY,
                              fontSize: "9px", letterSpacing: "0.14em",
                              color: "#5a5a5a", padding: "7px 12px 5px",
                              borderBottom: `1px solid ${RULE}`,
                            }}>ADD TO CALENDAR</div>
                            {[
                              { label: "Google Calendar",      icon: BASE_URL + "assets/cal-google.png",  href: urls.google  },
                              { label: "Outlook / Office 365", icon: BASE_URL + "assets/cal-outlook.png", href: urls.outlook },
                            ].map(opt => (
                              <a
                                key={opt.label}
                                href={opt.href}
                                target="_blank"
                                rel="noreferrer"
                                onClick={() => setCalOpen(null)}
                                style={{
                                  display: "flex", alignItems: "center", gap: "8px",
                                  padding: "9px 12px",
                                  textDecoration: "none",
                                  borderBottom: `1px solid ${RULE}`,
                                  transition: "background 0.1s",
                                }}
                                onMouseEnter={e => e.currentTarget.style.background = CREAM}
                                onMouseLeave={e => e.currentTarget.style.background = "#fff"}
                              >
                                <img src={opt.icon} alt={opt.label} style={{ width: "16px", height: "16px", flexShrink: 0 }} />
                                <span style={{
                                  fontFamily: FONT_DISPLAY,
                                  fontSize: "10px", letterSpacing: "0.1em",
                                  color: INK,
                                }}>{opt.label}</span>
                              </a>
                            ))}
                          </div>
                        )}
                      </div>
                );
              })}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// Deep-link helpers: keep `selected` in sync with the URL hash so individual
// meetings get shareable links (e.g. ".../#m/j64Gj2ean2k").
function _findByHash() {
  const m = window.location.hash.match(/^#m\/([\w-]+)$/);
  return m ? MEETINGS.find(x => x.id === m[1]) || null : null;
}

function _writeHash(meeting) {
  const next = meeting ? `#m/${meeting.id}` : "";
  if (window.location.hash !== next) {
    // history.replaceState avoids spamming the back-stack on each click.
    const url = window.location.pathname + window.location.search + next;
    window.history.replaceState(null, "", url);
  }
}

// ─── Sponsor CTA ─────────────────────────────────────────────────────────────
// Strip under the masthead inviting sponsorship inquiries. Clicking opens a
// dialog with a mailto link and a copy-email fallback so the reader can pick
// whichever path works in their environment. Rendered only when the instance
// defines a sponsor block.

function SponsorModal({ open, onClose, isMobile }) {
  const [copied, setCopied] = useState(false);

  // Reset the "copied" indicator each time the modal re-opens.
  useEffect(() => { if (open) setCopied(false); }, [open]);

  // Close on Escape.
  useEffect(() => {
    if (!open) return;
    const onKey = e => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const mailto = `mailto:${SPONSOR.email}?subject=${encodeURIComponent(SPONSOR.subject)}&body=${encodeURIComponent(SPONSOR.body)}`;

  const copyEmail = async () => {
    try {
      await navigator.clipboard.writeText(SPONSOR.email);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback for browsers without clipboard API (or insecure context).
      const ta = document.createElement("textarea");
      ta.value = SPONSOR.email;
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); setCopied(true); setTimeout(() => setCopied(false), 2000); } catch {}
      document.body.removeChild(ta);
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="sponsor-modal-title"
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, zIndex: 1000,
        background: "rgba(0,0,0,0.55)",
        display: "flex", alignItems: "center", justifyContent: "center",
        padding: "16px",
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          background: CREAM,
          maxWidth: "440px", width: "100%",
          border: `1px solid ${RULE}`,
          boxShadow: "0 10px 40px rgba(0,0,0,0.25)",
          padding: isMobile ? "20px 18px" : "26px 28px",
          position: "relative",
        }}
      >
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          style={{
            position: "absolute", top: "8px", right: "10px",
            background: "transparent", border: "none", cursor: "pointer",
            color: "#5a5a5a", fontSize: "22px", lineHeight: 1,
            width: "30px", height: "30px",
          }}
        >×</button>

        <div style={{
          fontFamily: FONT_DISPLAY,
          fontSize: "11px", letterSpacing: "0.18em",
          color: TEAL, marginBottom: "8px",
        }}>SPONSORSHIP</div>

        <h3 id="sponsor-modal-title" style={{
          fontFamily: FONT_HEADLINE,
          fontSize: isMobile ? "20px" : "22px",
          fontWeight: 700, color: INK, lineHeight: 1.25,
          margin: "0 0 12px",
        }}>Sponsor the Meeting Tracker</h3>

        <p style={{
          fontFamily: FONT_BODY,
          fontSize: "14px", lineHeight: 1.6, color: "#2A2015",
          margin: "0 0 18px",
        }}>
          Reach {INSTANCE.newsroom} readers who care about local
          government. Email us for placement options, audience details, and
          pricing.
        </p>

        <div style={{
          fontFamily: "'JetBrains Mono', Menlo, Consolas, monospace",
          fontSize: "13px", color: INK,
          background: "#fff", border: `1px solid ${RULE}`,
          padding: "8px 12px", marginBottom: "16px",
          wordBreak: "break-all",
        }}>{SPONSOR.email}</div>

        <div style={{ display: "flex", gap: "10px", flexWrap: "wrap" }}>
          <a
            href={mailto}
            onClick={() => onClose()}
            style={{
              flex: "1 1 0", minWidth: "150px",
              display: "inline-flex", alignItems: "center", justifyContent: "center",
              background: TEAL, color: "#fff", textDecoration: "none",
              fontFamily: FONT_DISPLAY,
              fontSize: "12px", letterSpacing: "0.14em",
              padding: "10px 14px",
              transition: "opacity 0.15s",
            }}
            onMouseEnter={e => e.currentTarget.style.opacity = "0.9"}
            onMouseLeave={e => e.currentTarget.style.opacity = "1"}
          >OPEN IN EMAIL CLIENT</a>
          <button
            type="button"
            onClick={copyEmail}
            style={{
              flex: "1 1 0", minWidth: "120px",
              border: `1px solid ${INK}`, background: "#fff", color: INK,
              fontFamily: FONT_DISPLAY,
              fontSize: "12px", letterSpacing: "0.14em",
              padding: "10px 14px",
              cursor: "pointer",
              transition: "all 0.15s",
            }}
            onMouseEnter={e => { e.currentTarget.style.background = INK; e.currentTarget.style.color = "#fff"; }}
            onMouseLeave={e => { e.currentTarget.style.background = "#fff"; e.currentTarget.style.color = INK; }}
          >{copied ? "COPIED!" : "COPY EMAIL"}</button>
        </div>
      </div>
    </div>
  );
}

function BookmarkButton({ isMobile }) {
  const [hint, setHint] = useState(false);
  // Browsers don't allow JS to add a bookmark directly (security). The reliable
  // path is to prompt the keyboard shortcut — and pressing it bookmarks the
  // TOP-LEVEL page (the newsroom page when embedded), which is what we want
  // for stickiness, since the shortcut targets the parent document, not this
  // iframe. Legacy IE's AddFavorite is tried first but is effectively dead.
  const isMac = /Mac|iPhone|iPad|iPod/.test(
    (typeof navigator !== "undefined" && (navigator.platform || navigator.userAgent)) || ""
  );
  const onClick = () => {
    track("Bookmark");
    try {
      if (typeof window !== "undefined" && window.external &&
          typeof window.external.AddFavorite === "function") {
        window.external.AddFavorite(window.location.href, document.title);
        return;
      }
    } catch (e) { /* fall through to the keyboard hint */ }
    setHint(true);
    setTimeout(() => setHint(false), 5000);
  };
  return (
    <div style={{ position: "relative" }}>
      <button
        onClick={onClick}
        aria-label="Bookmark this page"
        title="Bookmark this page"
        style={{
          display: "flex", alignItems: "center", gap: "5px",
          fontFamily: FONT_DISPLAY,
          fontSize: isMobile ? "11px" : "12px", letterSpacing: "0.1em",
          color: "#fff", background: "rgba(255,255,255,0.14)",
          border: "1px solid rgba(255,255,255,0.55)", borderRadius: "4px",
          padding: isMobile ? "4px 8px" : "5px 11px", cursor: "pointer",
          whiteSpace: "nowrap", lineHeight: 1,
        }}
      >
        <span aria-hidden="true" style={{ fontSize: isMobile ? "12px" : "13px" }}>★</span>
        {isMobile ? "SAVE" : "BOOKMARK"}
      </button>
      {hint && (
        <div role="status" style={{
          position: "absolute", top: "calc(100% + 6px)", right: 0, zIndex: 50,
          background: INK, color: "#fff",
          fontFamily: FONT_BODY, fontSize: "12px", lineHeight: 1.4,
          padding: "8px 11px", borderRadius: "5px", width: "max-content",
          maxWidth: "210px", boxShadow: "0 4px 14px rgba(0,0,0,0.28)",
        }}>
          Press <strong>{isMac ? "⌘ D" : "Ctrl + D"}</strong> to bookmark this page
          for quick access.
        </div>
      )}
    </div>
  );
}

function SponsorStrip({ isMobile }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <div style={{
        background: "#fff",
        borderBottom: `1px solid ${RULE}`,
        padding: isMobile ? "8px 16px" : "9px 24px",
        display: "flex", alignItems: "center", justifyContent: "center",
        gap: "10px", flexWrap: "wrap",
      }}>
        <span style={{
          fontFamily: FONT_BODY,
          fontSize: isMobile ? "12px" : "13px",
          color: "#3a3a3a", lineHeight: 1.4,
        }}>{SPONSOR.prompt}</span>
        <button
          type="button"
          onClick={() => setOpen(true)}
          style={{
            background: "transparent", border: "none", cursor: "pointer",
            fontFamily: FONT_DISPLAY,
            fontSize: isMobile ? "12px" : "13px", letterSpacing: "0.14em",
            color: TEAL, padding: "2px 4px",
            display: "inline-flex", alignItems: "center", gap: "4px",
          }}
          onMouseEnter={e => e.currentTarget.style.color = "#2f6660"}
          onMouseLeave={e => e.currentTarget.style.color = TEAL}
        >{SPONSOR.button} <span aria-hidden="true">{"→"}</span></button>
      </div>
      <SponsorModal open={open} onClose={() => setOpen(false)} isMobile={isMobile} />
    </>
  );
}

function Tracker() {
  const isMobile = useIsMobile();
  const [selected,    setSelected]    = useState(() => _findByHash());
  const [search,      setSearch]      = useState("");
  const [sourceFilter, setSourceFilter] = useState("all"); // "all" | jurisdiction key
  const [panelTab,     setPanelTab]     = useState("recent"); // "recent" | "upcoming"

  useEffect(() => {
    if (!isMobile && !selected) setSelected(MEETINGS[0]);
  }, [isMobile]);

  // Mirror selection into the URL hash.
  useEffect(() => { _writeHash(selected); }, [selected]);

  // Respond to external hash changes (back/forward, paste-link).
  useEffect(() => {
    const onHash = () => {
      const m = _findByHash();
      if (m) setSelected(m);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const parseDate = (d) => new Date(d);

  const filtered = MEETINGS
    .filter(m => {
      const matchSource = sourceFilter === "all" || m.source === sourceFilter;
      if (!matchSearch(m, search)) return false;
      return matchSource;
    })
    .sort((a, b) => parseDate(b.date) - parseDate(a.date));

  const showList   = !isMobile || !selected;
  const showDetail = !isMobile || !!selected;
  const newCount   = MEETINGS.filter(m => m.badge).length;

  return (
    <>
      <style>{`
        * { box-sizing: border-box; margin: 0; padding: 0; }
        html, body { height: 100%; }
        body { background: ${CREAM}; -webkit-tap-highlight-color: transparent; }
        /* Webkit + Firefox scrollbar styling */
        ::-webkit-scrollbar { width: 4px; height: 4px; }
        ::-webkit-scrollbar-thumb { background: #ccc; border-radius: 2px; }
        * { scrollbar-width: thin; scrollbar-color: #ccc transparent; }
        /* Visible keyboard focus ring on every interactive element. Inline
           styles override default outlines, so this needs !important. */
        button:focus-visible,
        a:focus-visible,
        input:focus-visible,
        [tabindex]:focus-visible {
          outline: 2px solid ${TEAL} !important;
          outline-offset: 2px;
          border-radius: 1px;
        }
        button:focus:not(:focus-visible),
        a:focus:not(:focus-visible),
        input:focus:not(:focus-visible) { outline: none; }
      `}</style>

      <div style={{ display: "flex", flexDirection: "column", height: "100vh" }}>

        <header style={{ background: TEAL, flexShrink: 0, borderBottom: `3px solid ${HEADER_BORDER}` }}>
          <div style={{
            padding: isMobile ? "10px 16px 10px" : "12px 24px 12px",
            display: "flex", alignItems: "center", justifyContent: "space-between",
            borderBottom: "1px solid rgba(255,255,255,0.15)",
          }}>
            <a href={INSTANCE.newsroom_url} target="_blank" rel="noreferrer"
              style={{ display: "flex", alignItems: "center", gap: "14px", textDecoration: "none" }}>

              <img
                src={BASE_URL + THEME.logo}
                alt={INSTANCE.newsroom}
                style={{
                  width: isMobile ? "38px" : "52px",
                  height: isMobile ? "38px" : "52px",
                  borderRadius: "50%",
                  flexShrink: 0,
                  display: "block",
                }}
              />

              <div style={{ display: "flex", flexDirection: "column", gap: "1px" }}>
                <span style={{
                  fontFamily: FONT_HEADLINE,
                  fontSize: isMobile ? "17px" : "24px",
                  fontWeight: 800, color: "#fff",
                  letterSpacing: "-0.01em", whiteSpace: "nowrap",
                  lineHeight: 1.1,
                }}>
                  {INSTANCE.newsroom}
                </span>
                {INSTANCE.tagline && (
                  <span style={{
                    fontFamily: FONT_DISPLAY,
                    fontSize: isMobile ? "9px" : "10px",
                    letterSpacing: "0.14em",
                    color: "rgba(255,255,255,0.5)",
                    whiteSpace: "nowrap",
                  }}>{INSTANCE.tagline}</span>
                )}
              </div>
            </a>
            <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: "6px" }}>
              <BookmarkButton isMobile={isMobile} />
              <div style={{ fontFamily: FONT_DISPLAY, fontSize: isMobile ? "13px" : "16px", letterSpacing: "0.12em", color: "#fff" }}>
                {new Date().toLocaleDateString("en-US",{month:"short",day:"numeric",year:"numeric"}).toUpperCase()}
              </div>
            </div>
          </div>

          <div style={{ padding: isMobile ? "7px 16px 10px" : "7px 24px 12px", display: "flex", alignItems: "baseline", gap: "10px", flexWrap: "wrap" }}>
            <span style={{ fontFamily: FONT_DISPLAY, fontSize: isMobile ? "20px" : "28px", color: "#fff", letterSpacing: "0.08em", lineHeight: 1 }}>{TITLE_LINE_1}</span>
            {TITLE_LINE_2 && (
              <span style={{ fontFamily: FONT_DISPLAY, fontSize: isMobile ? "20px" : "28px", color: "#fff", letterSpacing: "0.08em", lineHeight: 1 }}>{TITLE_LINE_2}</span>
            )}
          </div>
        </header>

        {SPONSOR && <SponsorStrip isMobile={isMobile} />}

        <main style={{ display: "flex", flex: 1, overflow: "hidden", flexDirection: isMobile ? "column" : "row" }}>


          {showList && (
            <nav aria-label="Meetings list" style={{
              width: isMobile ? "100%" : "420px",
              minWidth: isMobile ? "unset" : "420px",
              background: "#fff",
              borderRight: isMobile ? "none" : `1px solid ${RULE}`,
              display: "flex", flexDirection: "column",
              flex: isMobile ? 1 : "unset", overflow: "hidden",
            }}>

              <div role="tablist" aria-label="Meetings panel" style={{ display: "flex", borderBottom: `2px solid #000`, flexShrink: 0, background: INK }}>
                {[
                  { key: "recent",   label: "Recent Meetings"    },
                  { key: "upcoming", label: "Upcoming Meetings"  },
                ].map(({ key, label }) => {
                  const active = panelTab === key;
                  return (
                    <button
                      key={key}
                      role="tab"
                      aria-selected={active}
                      onClick={() => setPanelTab(key)}
                      style={{
                        flex: 1, border: "none", cursor: "pointer",
                        fontFamily: FONT_DISPLAY,
                        fontSize: "12px", letterSpacing: "0.16em",
                        padding: "11px 0",
                        background: active ? "rgba(255,255,255,0.18)" : "transparent",
                        color: active ? "#fff" : "rgba(255,255,255,0.45)",
                        borderBottom: active ? "3px solid #fff" : "3px solid transparent",
                        transition: "all 0.15s",
                      }}
                      onMouseEnter={e => { if (!active) e.currentTarget.style.color = "rgba(255,255,255,0.75)"; }}
                      onMouseLeave={e => { if (!active) e.currentTarget.style.color = "rgba(255,255,255,0.45)"; }}
                    >{label.toUpperCase()}</button>
                  );
                })}
              </div>

              {panelTab === "recent" && <>

              <div style={{ padding: "12px 14px 10px", borderBottom: `2px solid ${RULE}`, background: CREAM }}>
                <div style={{ fontFamily: FONT_DISPLAY, fontSize: "10px", letterSpacing: "0.18em", color: "#5a5a5a", marginBottom: "10px" }}>RECENT MEETINGS</div>
                <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
                  {FILTER_OPTIONS.map(({ key, label, color, avatar }) => {
                    const active = sourceFilter === key;
                    return (
                      <button
                        key={key}
                        aria-pressed={active}
                        aria-label={`Filter recent meetings by ${label}`}
                        onClick={() => { track("Filter", { source: key, panel: "recent" }); setSourceFilter(key); }}
                        onMouseEnter={e => { if (!active) { e.currentTarget.style.background = color + "15"; e.currentTarget.style.borderColor = color; e.currentTarget.style.color = color; }}}
                        onMouseLeave={e => { if (!active) { e.currentTarget.style.background = "transparent"; e.currentTarget.style.borderColor = "#d0ccc4"; e.currentTarget.style.color = "#888"; }}}
                        style={{
                          background:    active ? color : "transparent",
                          border:        "1.5px solid " + (active ? color : "#d0ccc4"),
                          color:         active ? "#fff" : "#888",
                          fontFamily:    FONT_DISPLAY,
                          fontSize:      "11px", letterSpacing: "0.12em",
                          padding:       "5px 12px",
                          cursor:        "pointer",
                          transition:    "all 0.15s",
                          display:       "flex", alignItems: "center", gap: "6px",
                          whiteSpace:    "nowrap",
                        }}
                      >
                        {avatar && (
                          <img
                            src={avatar}
                            alt={label}
                            onError={e => { e.currentTarget.style.visibility = "hidden"; }}
                            style={{
                              width: "16px", height: "16px",
                              borderRadius: "50%",
                              objectFit: "cover",
                              flexShrink: 0,
                              opacity: active ? 1 : 0.75,
                              border: active ? "1px solid rgba(255,255,255,0.5)" : "1px solid transparent",
                              transition: "opacity 0.15s",
                            }}
                          />
                        )}
                        {label}
                      </button>
                    );
                  })}
                </div>
                <div style={{ position: "relative" }}>
                  <input
                    type="search"
                    placeholder="Search topics, votes, projects..."
                    aria-label="Search meetings by topic, committee, or any text in the summaries"
                    value={search}
                    onChange={e => setSearch(e.target.value)}
                    style={{
                      width: "100%", padding: "8px 32px 8px 12px",
                      border: `1px solid ${RULE}`,
                      fontFamily: FONT_BODY,
                      fontSize: "16px", color: INK,
                      background: "#fff", outline: "none",
                    }}
                    onFocus={e => e.target.style.borderColor=TEAL}
                    onBlur={e => e.target.style.borderColor=RULE}
                  />
                  {search && (
                    <button
                      type="button"
                      onClick={() => setSearch("")}
                      aria-label="Clear search"
                      style={{
                        position: "absolute", right: "6px", top: "50%", transform: "translateY(-50%)",
                        width: "22px", height: "22px",
                        background: "transparent", border: "none", cursor: "pointer",
                        color: "#5a5a5a", fontSize: "18px", lineHeight: 1,
                        display: "flex", alignItems: "center", justifyContent: "center",
                      }}
                    >×</button>
                  )}
                </div>
              </div>


              <div style={{ flex: 1, overflowY: "auto", WebkitOverflowScrolling: "touch", minHeight: 0 }}>
                {filtered.length === 0
                  ? (
                    <div style={{ padding: "32px 18px", color: "#5a5a5a", fontFamily: FONT_BODY, fontSize: "14px", lineHeight: 1.55, textAlign: "center" }}>
                      <div style={{ fontFamily: FONT_DISPLAY, fontSize: "13px", letterSpacing: "0.14em", color: "#5a5a5a", marginBottom: "8px" }}>NO MATCHES</div>
                      {search
                        ? <>No meetings match <em>&ldquo;{search}&rdquo;</em>. Try a different search term or change the source filter.</>
                        : sourceFilter !== "all"
                          ? <>No recent meetings yet for this source. Check back after the next scheduled meeting.</>
                          : <>No recent meetings have been summarized yet.</>}
                    </div>
                  )
                  : filtered.map(m => (
                    <MeetingCard key={m.id} meeting={m} onClick={(mm) => { track("Meeting Opened", { source: mm.source, committee: mm.committee }); setSelected(mm); }} active={!isMobile && selected?.id === m.id} />
                  ))
                }
              </div>

              </>}

              {panelTab === "upcoming" && (
                <UpcomingMeetings isMobile={isMobile} />
              )}


              <div style={{ padding: "10px 14px 12px", borderTop: `1px solid ${RULE}`, background: CREAM }}>
                <div style={{ fontFamily: FONT_DISPLAY, fontSize: "9px", letterSpacing: "0.12em", color: "#5a5a5a", marginBottom: "5px" }}>
                  {newCount} NEW <span aria-hidden="true">·</span> {MEETINGS.length} TOTAL
                </div>
                <div style={{ fontFamily: FONT_BODY, fontSize: "10px", color: "#666", lineHeight: 1.55 }}>
                  Coverage of{" "}
                  {JURISDICTIONS.flatMap((j, i, arr) => {
                    const sc = SOURCE_CONFIG[j.key];
                    const link = (
                      <a key={j.key} href={sc.channel} target="_blank" rel="noreferrer"
                         style={{ color: sc.accent, textDecoration: "none", fontWeight: 600 }}>{sc.label}</a>
                    );
                    const sep = i < arr.length - 1 ? (i === arr.length - 2 ? ", and " : ", ") : "";
                    return sep ? [link, sep] : [link];
                  })}
                  {" "}meetings. Details are scraped from public agendas, minutes, and recordings — these summaries are not a substitute for official minutes.
                </div>
              </div>
            </nav>
          )}

          {showDetail && (
            <section aria-label="Meeting summary" style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column" }}>
              {selected
                ? <SummaryDetail meeting={selected} onBack={() => setSelected(null)} isMobile={isMobile}
                    onTopicClick={(t) => {
                      // Topic chip → filter the whole tracker by that topic via
                      // full-text search. Reset the source filter so matches
                      // from every jurisdiction show; on compact layouts jump
                      // back to the list so the results are visible.
                      setSearch(t);
                      setSourceFilter("all");
                      setPanelTab("recent");
                      if (isMobile) setSelected(null);
                    }} />
                : (
                  <div style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", background: CREAM, gap: "14px", padding: "0 24px", textAlign: "center" }}>
                    <div style={{ fontFamily: FONT_DISPLAY, fontSize: "42px", letterSpacing: "0.08em", color: "#9c9387", lineHeight: 1 }}>SELECT A MEETING</div>
                    <div style={{ fontFamily: FONT_BODY, fontSize: "15px", color: "#5a5a5a", maxWidth: "440px", lineHeight: 1.55 }}>
                      Pick a meeting from the list to read the agenda, key discussions, public comment, and recorded votes.
                    </div>
                  </div>
                )
              }
            </section>
          )}
        </main>
      </div>
    </>
  );
}

// ─── Boot ────────────────────────────────────────────────────────────────────
// Fetch instance.json + data/*.json in parallel, apply config, then mount the
// tracker. Fail loud: a missing or invalid required file renders a plain error
// screen naming it — no retries, no fallback data.

function BootLoading() {
  return (
    <div style={{
      height: "100%", minHeight: "100vh",
      display: "flex", alignItems: "center", justifyContent: "center",
      background: "var(--background, #fff)",
      fontFamily: "Georgia, serif", fontSize: "15px", color: "#5a5a5a",
    }}>Loading…</div>
  );
}

function BootError({ file }) {
  return (
    <div role="alert" style={{
      height: "100%", minHeight: "100vh",
      display: "flex", alignItems: "center", justifyContent: "center",
      background: "#fff", padding: "24px", textAlign: "center",
      fontFamily: "Georgia, serif", color: "#1a1a1a",
    }}>
      <div style={{ maxWidth: "520px" }}>
        <div style={{ fontSize: "18px", fontWeight: 700, marginBottom: "10px" }}>
          This tracker failed to start
        </div>
        <div style={{ fontSize: "14px", lineHeight: 1.6 }}>
          A required file could not be loaded:{" "}
          <code style={{ fontFamily: "monospace", background: "#f2f2f2", padding: "2px 6px" }}>{BASE_URL}{file}</code>
          <br />
          Check that the deployment includes this file at the site root.
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const [boot, setBoot] = useState({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    const load = async (path) => {
      let res;
      try { res = await fetch(BASE_URL + path); } catch { throw new Error(path); }
      if (!res.ok) throw new Error(path);
      try { return await res.json(); } catch { throw new Error(path); }
    };
    Promise.all([
      load("instance.json"),
      load("data/meetings.json"),
      load("data/upcoming.json"),
    ])
      .then(([config, meetings, upcoming]) => {
        if (cancelled) return;
        try {
          _applyConfig(config, meetings, upcoming);
          setBoot({ status: "ready" });
        } catch (e) {
          setBoot({ status: "error", file: e.message });
        }
      })
      .catch(e => { if (!cancelled) setBoot({ status: "error", file: e.message }); });
    return () => { cancelled = true; };
  }, []);

  if (boot.status === "error") return <BootError file={boot.file} />;
  if (boot.status === "loading") return <BootLoading />;
  return <Tracker />;
}
