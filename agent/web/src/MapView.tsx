import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Map as MapLibreMap,
  NavigationControl,
  LngLatBounds,
  setWorkerUrl,
  type GeoJSONSource,
  type MapLayerMouseEvent,
} from "maplibre-gl";
// MapLibre runs tile parsing in a web worker whose file the bundler will not copy
// by itself, so the URL is imported and set explicitly. Without this the map
// renders an empty canvas and the console reports "Worker failed to load".
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import "maplibre-gl/dist/maplibre-gl.css";
import { UploadPanel } from "./UploadPanel";
import { Chat } from "./Chat";
import { useAssistant } from "./useAssistant";

/** The map view: where safety events actually happened.
 *
 *  Deliberately separate from the chat canvas. The Canvas renders what a
 *  conversation produced; this renders what the store holds, for whoever is
 *  looking. Both read the same `safety_event` rows, so the two cannot disagree.
 *
 *  Tiles are plain OpenStreetMap raster: no key, no vendor account. That is fine
 *  for a POC and explicitly not fine for production traffic — OSM's tile policy
 *  forbids it — so the basemap URL is the one thing to swap before this ships.
 */

const BASEMAP_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";

const SEVERITY_COLOUR: Record<string, string> = {
  critical: "#d93a3a",
  high: "#e07b2a",
  medium: "#d9a72e",
  low: "#3f9a63",
  unknown: "#8a94a6",
};

const SEVERITY_ORDER = ["critical", "high", "medium", "low", "unknown"];

type EventRow = {
  id: number;
  event_time: string;
  event_type: string;
  severity: string;
  lat: number;
  lon: number;
  place_icao: string | null;
  place_name: string | null;
  registration: string | null;
  callsigns: string[] | null;
  source: string;
  provenance: string;
  /** Producer metadata. Derived events carry the arithmetic; `engine` marks OpenARIA. */
  metrics?: Record<string, unknown> | null;
};

type Encounter = {
  event_id: number;
  cpa: { lat: number; lon: number; lateral_nm: number | null; vertical_ft: number | null };
  tracks: { icao24: string | null; callsign: string | null; count: number; points: [number, number, number][] }[];
};

type Aircraft = {
  icao24: string;
  callsign: string | null;
  country: string | null;
  lat: number | null;
  lon: number | null;
  altitude_ft: number | null;
  ground_speed_kt: number | null;
  track_deg: number | null;
  on_ground: boolean;
  vertical_rate_fpm: number | null;
};

/** Speed limits are per altitude band, so the panel edits the low band and the
 *  ceiling above it rather than a single number that flags all of cruise. */
function bandEdit(rules: Rules, index: number, patch: Partial<SpeedBand>): Rules {
  const bands: SpeedBand[] =
    rules.speed_bands && rules.speed_bands.length
      ? rules.speed_bands.map((band) => ({ ...band }))
      : [
          { max_alt_ft: 10000, max_speed_kt: 250 },
          { max_alt_ft: null, max_speed_kt: rules.max_speed_kt },
        ];
  bands[index] = { ...bands[index], ...patch };
  return { ...rules, speed_bands: bands, max_speed_kt: bands[bands.length - 1].max_speed_kt };
}

type Detection = {
  type: string;
  limit_kt?: number;
  band?: string;
  a?: string;
  b?: string;
  icao24_a?: string;
  icao24_b?: string;
  callsign?: string;
  icao24?: string;
  separation_nm?: number;
  vertical_ft?: number | null;
  ground_speed_kt?: number;
  lat?: number | null;
  lon?: number | null;
};

type Detections = {
  enabled: boolean;
  rules?: Record<string, unknown>;
  checked?: number;
  conflicts: Detection[];
  overspeed: Detection[];
  summary: string;
  disclaimer?: string;
};

type SpeedBand = { max_alt_ft: number | null; max_speed_kt: number };

type Rules = {
  min_separation_nm: number;
  min_vertical_ft: number;
  max_speed_kt: number;
  ignore_ground: boolean;
  enabled: boolean;
  speed_bands?: SpeedBand[];
};

type Anomaly = {
  kind: string;
  callsign: string;
  icao24: string;
  detail: string;
  lat?: number | null;
  lon?: number | null;
};

type Anomalies = {
  flags: Anomaly[];
  summary: string;
  disclaimer?: string;
};

type PredictedPair = {
  a: string;
  b: string;
  current_nm: number;
  predicted_nm: number;
  time_to_cpa_s: number;
  vertical_ft: number | null;
};

type Predicted = { horizon_s: number; conflicts: PredictedPair[]; summary: string };

type MotionEvent = { kind: string; callsign: string; detail: string };

type Motion = { events: MotionEvent[]; summary: string };

type LivePayload = {
  aircraft: Aircraft[];
  rules?: Rules;
  detections?: Detections;
  anomalies?: Anomalies;
  predicted?: Predicted;
  motion?: Motion;
  credential_warning?: string;
  count: number;
  mode: string;
  credits_used: number;
  credits_budget: number;
  credits_remaining: number;
  age_seconds: number;
  cached: boolean;
};

type LiveStatus = {
  mode: string;
  credits_used: number;
  credits_budget: number;
  credits_remaining: number;
  cache_ttl_seconds: number;
  max_bbox_square_degrees: number;
};

type Filters = {
  types: string[];
  severities: string[];
  source: string | null;
  days: number | null;
};

const EMPTY_FILTERS: Filters = { types: [], severities: [], source: null, days: null };

const DAY_PRESETS: { label: string; days: number | null }[] = [
  { label: "All time", days: null },
  { label: "Last 30 days", days: 30 },
  { label: "Last 7 days", days: 7 },
];

function niceType(value: string): string {
  return value.replace(/_/g, " ");
}

/** Which engine produced this event, if it says. Only OpenARIA events carry tracks. */
function engineOf(row: EventRow): string | null {
  const engine = (row.metrics as { engine?: unknown } | null | undefined)?.engine;
  return typeof engine === "string" && engine ? engine : null;
}

/** A numeric metric, or null when the producer did not record one. */
function numberOf(row: EventRow, key: string): number | null {
  const value = (row.metrics as Record<string, unknown> | null | undefined)?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function when(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toISOString().replace("T", " ").replace(":00.000Z", "Z").replace(".000Z", "Z");
}

setWorkerUrl(maplibreWorkerUrl);

/** Draw an airliner silhouette pointing north, so a symbol layer can rotate it by
 *  the reported track. Canvas rather than a sprite sheet: no extra asset to ship,
 *  and the colour variants stay in one place. */
function planeImage(colour: string, outline = "#ffffff"): ImageData {
  const size = 48;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("2d canvas unavailable");

  ctx.beginPath();
  ctx.moveTo(24, 3);                                   // nose
  ctx.bezierCurveTo(26.4, 8, 27.6, 13, 27.6, 19);      // fuselage, right
  ctx.lineTo(45, 31);                                  // right wing leading edge
  ctx.lineTo(45, 35);
  ctx.lineTo(27.6, 28);                                // wing trailing edge
  ctx.lineTo(27.6, 38);
  ctx.lineTo(34, 42);                                  // right tailplane
  ctx.lineTo(34, 45);
  ctx.lineTo(24, 42.5);
  ctx.lineTo(14, 45);                                  // left tailplane
  ctx.lineTo(14, 42);
  ctx.lineTo(20.4, 38);
  ctx.lineTo(20.4, 28);
  ctx.lineTo(3, 35);                                   // left wing
  ctx.lineTo(3, 31);
  ctx.lineTo(20.4, 19);
  ctx.bezierCurveTo(20.4, 13, 21.6, 8, 24, 3);         // fuselage, left
  ctx.closePath();

  ctx.fillStyle = colour;
  ctx.strokeStyle = outline;
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.fill();
  return ctx.getImageData(0, 0, size, size);
}

export function MapView({
  onLiveChange,
}: {
  onLiveChange?: (state: { enabled: boolean; bbox: number[] | null }) => void;
} = {}) {
  const mapContainer = useRef<HTMLDivElement | null>(null);
  const map = useRef<MapLibreMap | null>(null);
  const [ready, setReady] = useState(false);
  const [events, setEvents] = useState<EventRow[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [severityCounts, setSeverityCounts] = useState<Record<string, number>>({});
  const [summary, setSummary] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<EventRow | null>(null);
  // The tracks behind a selected encounter, when the producer kept them.
  const [encounter, setEncounter] = useState<Encounter | null>(null);
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [showUpload, setShowUpload] = useState(false);
  const [live, setLive] = useState(false);
  const [liveData, setLiveData] = useState<LivePayload | null>(null);
  const [liveStatus, setLiveStatus] = useState<LiveStatus | null>(null);
  const [liveError, setLiveError] = useState<string | null>(null);
  const [liveNote, setLiveNote] = useState<string | null>(null);
  const [rules, setRules] = useState<Rules | null>(null);
  const [rulesBusy, setRulesBusy] = useState(false);
  const [flagged, setFlagged] = useState<Detections | null>(null);
  const [unusual, setUnusual] = useState<Anomalies | null>(null);
  const [predicted, setPredicted] = useState<Predicted | null>(null);
  const [motion, setMotion] = useState<Motion | null>(null);
  const [assistantOpen, setAssistantOpen] = useState(true);
  const [aircraft, setAircraft] = useState<Aircraft | null>(null);

  // ---- map lifecycle -----------------------------------------------------
  useEffect(() => {
    if (!mapContainer.current || map.current) return;

    const instance = new MapLibreMap({
      container: mapContainer.current,
      style: {
        version: 8,
        sources: {
          basemap: {
            type: "raster",
            tiles: [BASEMAP_TILES],
            tileSize: 256,
            attribution: "© OpenStreetMap contributors",
          },
        },
        layers: [
          { id: "bg", type: "background", paint: { "background-color": "#dfe7f1" } },
          { id: "basemap", type: "raster", source: "basemap" },
        ],
      },
      center: [103.9, 1.36], // Singapore: the data is mostly here and it makes the first paint useful
      zoom: 8,
    });

    instance.addControl(new NavigationControl({ showCompass: false }), "top-right");
    instance.on("load", () => {
      instance.addSource("events", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      instance.addLayer({
        id: "event-halo",
        type: "circle",
        source: "events",
        paint: {
          "circle-radius": 14,
          "circle-color": ["get", "colour"],
          "circle-opacity": 0.18,
        },
      });
      instance.addLayer({
        id: "event-aria-ring",
        type: "circle",
        source: "events",
        // OpenARIA events get a ring: they were computed over the whole track history,
        // not read off a single poll, and the map should not make those look identical.
        filter: ["==", ["get", "aria"], true],
        paint: {
          "circle-radius": 11,
          "circle-color": "rgba(255,255,255,0)",
          "circle-stroke-width": 2,
          "circle-stroke-color": "#b45309",
          "circle-stroke-opacity": 0.9,
        },
      });
      instance.addLayer({
        id: "event-dot",
        type: "circle",
        source: "events",
        paint: {
          "circle-radius": 7,
          "circle-color": ["get", "colour"],
          "circle-stroke-width": 2,
          "circle-stroke-color": "#ffffff",
        },
      });
      instance.on("click", "event-dot", (event: MapLayerMouseEvent) => {
        const feature = event.features?.[0];
        if (!feature) return;
        const id = Number(feature.properties?.id);
        setSelected((current) => eventsRef.current.find((row) => row.id === id) ?? current);
      });

      // The encounter a detector kept: two tracks that came close, drawn as evidence.
      // Everything else on this map is a dot that asks to be believed.
      instance.addSource("encounter", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      instance.addLayer({
        id: "encounter-line-a",
        type: "line",
        source: "encounter",
        filter: ["==", ["get", "slot"], 0],
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": "#1b2a4a", "line-width": 2, "line-opacity": 0.85 },
      });
      instance.addLayer({
        id: "encounter-line-b",
        type: "line",
        source: "encounter",
        filter: ["==", ["get", "slot"], 1],
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": "#c2410c", "line-width": 2, "line-opacity": 0.85 },
      });
      instance.addLayer({
        id: "encounter-cpa",
        type: "circle",
        source: "encounter",
        filter: ["==", ["get", "kind"], "cpa"],
        paint: {
          "circle-radius": 6,
          "circle-color": "rgba(255,255,255,0)",
          "circle-stroke-width": 2,
          "circle-stroke-color": "#b45309",
        },
      });
      instance.addSource("aircraft", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      // Aircraft icons, drawn once per colour variant. Conflict and overspeed keep
      // their own colour so the eye finds them without reading the panel.
      for (const [name, colour] of [
        ["plane", "#1b2a4a"],
        ["plane-conflict", "#d93a3a"],
        ["plane-overspeed", "#e07b2a"],
        ["plane-ground", "#7d8898"],
      ] as const) {
        if (!instance.hasImage(name)) {
          instance.addImage(name, planeImage(colour), { pixelRatio: 2 });
        }
      }
      if (!instance.hasImage("plane-shadow")) {
        instance.addImage("plane-shadow", planeImage("#0b1524", "#0b1524"), { pixelRatio: 2 });
      }
      instance.addLayer({
        id: "aircraft-shadow",
        type: "symbol",
        source: "aircraft",
        layout: {
          "icon-image": "plane-shadow",
          "icon-size": ["interpolate", ["linear"], ["zoom"], 4, 0.74, 8, 0.96, 12, 1.22],
          "icon-rotate": ["coalesce", ["get", "track_deg"], 0],
          "icon-rotation-alignment": "map",
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "icon-offset": [1.2, 1.2],
        },
        paint: { "icon-opacity": 0.22 },
      });
      instance.addLayer({
        id: "aircraft-plane",
        type: "symbol",
        source: "aircraft",
        layout: {
          "icon-image": [
            "case",
            ["==", ["get", "status"], "conflict"], "plane-conflict",
            ["==", ["get", "status"], "overspeed"], "plane-overspeed",
            ["get", "on_ground"], "plane-ground",
            "plane",
          ],
          // Sized like a traffic display: legible at regional zoom, not a speck.
          "icon-size": ["interpolate", ["linear"], ["zoom"], 4, 0.62, 8, 0.82, 12, 1.05],
          "icon-rotate": ["coalesce", ["get", "track_deg"], 0],
          "icon-rotation-alignment": "map",
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
        },
      });
      instance.on("click", "aircraft-plane", (event: MapLayerMouseEvent) => {
        const feature = event.features?.[0];
        if (!feature) return;
        const properties = feature.properties as Record<string, unknown>;
        const id = String(properties.icao24 ?? "");
        setAircraft(liveRef.current?.aircraft.find((item) => item.icao24 === id) ?? null);
      });
      instance.on("mouseenter", "aircraft-plane", () => {
        instance.getCanvas().style.cursor = "pointer";
      });
      instance.on("mouseleave", "aircraft-plane", () => {
        instance.getCanvas().style.cursor = "";
      });
      instance.on("mouseenter", "event-dot", () => {
        instance.getCanvas().style.cursor = "pointer";
      });
      instance.on("mouseleave", "event-dot", () => {
        instance.getCanvas().style.cursor = "";
      });
      setReady(true);
    });

    map.current = instance;
    return () => {
      instance.remove();
      map.current = null;
    };
  }, []);

  const eventsRef = useRef<EventRow[]>([]);
  eventsRef.current = events;

  // ---- data --------------------------------------------------------------
  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    const params = new URLSearchParams();
    if (filters.types.length) params.set("types", filters.types.join(","));
    if (filters.severities.length) params.set("severities", filters.severities.join(","));
    if (filters.source) params.set("source", filters.source);
    if (filters.days) {
      const since = new Date(Date.now() - filters.days * 86_400_000).toISOString();
      params.set("since", since);
    }
    params.set("limit", "2000");
    try {
      const response = await fetch(`/api/events?${params.toString()}`);
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status}`);
      setEvents(body.events ?? []);
      setCounts(body.counts ?? {});
      setSeverityCounts(body.severities ?? {});
      setSummary(body.summary ?? "");
    } catch (cause) {
      setError(String(cause));
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    void load();
  }, [load]);

  // ---- live traffic ------------------------------------------------------
  const liveRef = useRef<LivePayload | null>(null);
  liveRef.current = liveData;
  const liveStatusRef = useRef<LiveStatus | null>(null);
  liveStatusRef.current = liveStatus;
  const onLiveChangeRef = useRef(onLiveChange);
  onLiveChangeRef.current = onLiveChange;

  // The panel asks about what is on screen: current bounds, and only when the live
  // layer is actually on — otherwise each question would spend OpenSky credits.
  const assistant = useAssistant(() => {
    const instance = map.current;
    if (!instance || !liveRef.current) return { enabled: false, bbox: null };
    const bounds = instance.getBounds();
    return {
      enabled: true,
      bbox: [bounds.getSouth(), bounds.getWest(), bounds.getNorth(), bounds.getEast()],
    };
  });

  const fetchLive = useCallback(async () => {
    const instance = map.current;
    if (!instance) return;

    // OpenSky bills by area, so a world view is not a view we are allowed to ask
    // for. Rather than refuse and leave the user staring at an error, narrow the
    // map to a local box and say that we did.
    const maxArea = liveStatusRef.current?.max_bbox_square_degrees ?? 25;
    let bounds = instance.getBounds();
    const area = (bounds.getNorth() - bounds.getSouth()) * (bounds.getEast() - bounds.getWest());
    if (area > maxArea) {
      // Narrow around the events, not around whatever patch of ocean the world
      // view happened to be centred on: a live layer that opens on empty water
      // looks broken even when it is working.
      const points = eventsRef.current;
      const center = points.length
        ? points.reduce(
            (acc, row) => ({ lng: acc.lng + row.lon / points.length, lat: acc.lat + row.lat / points.length }),
            { lng: 0, lat: 0 },
          )
        : instance.getCenter();
      const span = Math.min(4, Math.sqrt(maxArea) * 0.6);
      bounds = new LngLatBounds(
        [center.lng - span / 2, center.lat - span / 2],
        [center.lng + span / 2, center.lat + span / 2],
      );
      instance.fitBounds(bounds, { duration: 600, maxZoom: 12 });
      setLiveNote(
        `narrowed to a ${span.toFixed(1)}° box — OpenSky bills by area, so live traffic is a local view`,
      );
    }

    const bbox = [bounds.getSouth(), bounds.getWest(), bounds.getNorth(), bounds.getEast()]
      .map((value) => value.toFixed(4))
      .join(",");
    try {
      const response = await fetch(`/api/live?bbox=${bbox}`);
      // A tunnel or proxy failure answers with an HTML page, and JSON.parse then throws
      // something like "Unexpected token '<'" — which tells the reader nothing.
      const text = await response.text();
      let body: any = null;
      try {
        body = JSON.parse(text);
      } catch {
        throw new Error(
          response.ok
            ? "the live feed answered with something that was not data — the tunnel or proxy is probably down"
            : `the live feed is unreachable (HTTP ${response.status})`,
        );
      }
      if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status}`);
      setLiveData(body as LivePayload);
      const liveBody = body as LivePayload;
      if (liveBody.rules) setRules(liveBody.rules);
      setFlagged(liveBody.detections ?? null);
      setUnusual(liveBody.anomalies ?? null);
      setPredicted(liveBody.predicted ?? null);
      setMotion(liveBody.motion ?? null);
      onLiveChangeRef.current?.({
        enabled: true,
        bbox: bbox.split(",").map(Number),
      });
      setLiveStatus((current) =>
        current
          ? { ...current, mode: body.mode, credits_used: body.credits_used, credits_remaining: body.credits_remaining }
          : current,
      );
      setLiveError(null);
    } catch (cause) {
      // Never mangle the message: an old version stripped "Error: " from anywhere in
      // the string, turning "SyntaxError: …" into "Syntax …" and hiding the cause.
      const message =
        cause instanceof Error ? cause.message : typeof cause === "string" ? cause : "the live feed failed";
      setLiveError(message);
    }
  }, []);

  useEffect(() => {
    fetch("/api/live/status")
      .then((response) => response.json())
      .then((body) => setLiveStatus(body as LiveStatus))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready || !live) return;
    const onMoveEnd = () => void fetchLive();
    instance.on("moveend", onMoveEnd);
    return () => {
      instance.off("moveend", onMoveEnd);
    };
  }, [live, ready, fetchLive]);

  useEffect(() => {
    if (!live) {
      setLiveNote(null);
      setFlagged(null);
      setUnusual(null);
      setPredicted(null);
      setMotion(null);
      onLiveChangeRef.current?.({ enabled: false, bbox: null });
      const instance = map.current;
      const source = instance?.getSource("aircraft") as GeoJSONSource | undefined;
      source?.setData({ type: "FeatureCollection", features: [] });
      setLiveData(null);
      return;
    }
    void fetchLive();
    // 15s with a 10s server-side cache: several viewers share one billed request.
    const timer = setInterval(() => void fetchLive(), 15_000);
    return () => clearInterval(timer);
  }, [live, fetchLive]);

  const statusFor = (icao24: string): string => {
    if (!flagged) return "";
    if ((flagged.conflicts ?? []).some((item) => item.icao24_a === icao24 || item.icao24_b === icao24)) {
      return "conflict";
    }
    if ((flagged.overspeed ?? []).some((item) => item.icao24 === icao24)) return "overspeed";
    return "";
  };

  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready) return;
    const source = instance.getSource("aircraft") as GeoJSONSource | undefined;
    if (!source) return;
    const features = (liveData?.aircraft ?? [])
      .filter((item) => item.lat !== null && item.lon !== null)
      .map((item) => ({
        type: "Feature" as const,
        geometry: { type: "Point" as const, coordinates: [item.lon as number, item.lat as number] },
        properties: {
          icao24: item.icao24,
          on_ground: item.on_ground,
          callsign: item.callsign ?? item.icao24,
          status: statusFor(item.icao24),
          track_deg: item.track_deg ?? 0,
        },
      }));
    source.setData({ type: "FeatureCollection", features });
  }, [liveData, ready]);

  // ---- push events into the map -----------------------------------------
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready) return;
    const source = instance.getSource("events") as GeoJSONSource | undefined;
    if (!source) return;

    source.setData({
      type: "FeatureCollection",
      features: events.map((row) => ({
        type: "Feature" as const,
        geometry: { type: "Point" as const, coordinates: [row.lon, row.lat] },
        properties: {
          id: row.id,
          colour: SEVERITY_COLOUR[row.severity] ?? SEVERITY_COLOUR.unknown,
          aria: Boolean((row.metrics as { engine?: string } | null | undefined)?.engine),
        },
      })),
    });
  }, [events, ready]);

  // Fetch the tracks behind a selected encounter. Only events whose producer kept them
  // have any; anything else answers 404 and the panel simply shows no tracks.
  useEffect(() => {
    if (!selected) {
      setEncounter(null);
      return;
    }
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await fetch(`/api/events/${selected.id}/encounter`, { signal: controller.signal });
        if (!response.ok) {
          setEncounter(null);
          return;
        }
        setEncounter((await response.json()) as Encounter);
      } catch {
        setEncounter(null);
      }
    })();
    return () => controller.abort();
  }, [selected]);

  // Push the encounter tracks into the map: two lines and a marked closest approach.
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready) return;
    const source = instance.getSource("encounter") as GeoJSONSource | undefined;
    if (!source) return;

    const features: GeoJSON.Feature[] = [];
    if (encounter) {
      encounter.tracks.forEach((track, index) => {
        features.push({
          type: "Feature",
          geometry: { type: "LineString", coordinates: track.points.map(([lon, lat]) => [lon, lat]) },
          properties: { slot: index, callsign: track.callsign, kind: "track" },
        });
      });
      if (encounter.cpa?.lat != null && encounter.cpa?.lon != null) {
        features.push({
          type: "Feature",
          geometry: { type: "Point", coordinates: [encounter.cpa.lon, encounter.cpa.lat] },
          properties: { kind: "cpa" },
        });
      }
    }
    source.setData({ type: "FeatureCollection", features });

    // Frame the encounter, or the two tracks sit behind the detail panel and the claim
    // stays unverifiable in practice. flyTo rather than fitBounds: the panel covers the
    // left of the map, so centring on the closest approach keeps the crossing visible.
    if (encounter?.cpa?.lat != null && encounter.cpa?.lon != null) {
      console.info("[encounter] centring on the closest approach", encounter.cpa.lateral_nm);
      instance.flyTo({
        center: [encounter.cpa.lon, encounter.cpa.lat],
        zoom: 12.2,
        duration: 800,
      });
    }
  }, [encounter, ready]);

  const fitToEvents = useCallback(
    (animate = true) => {
      const instance = map.current;
      if (!instance || !events.length) return;
      const bounds = new LngLatBounds();
      events.forEach((row) => bounds.extend([row.lon, row.lat]));
      instance.fitBounds(bounds, { padding: 70, maxZoom: 12, duration: animate ? 700 : 0 });
    },
    [events],
  );

  // Fit once the first batch of events arrives.
  const fittedOnce = useRef(false);
  useEffect(() => {
    if (fittedOnce.current || !ready || !events.length) return;
    fitToEvents(false);
    fittedOnce.current = true;
  }, [events, ready, fitToEvents]);

  const focusEvent = useCallback((row: EventRow) => {
    setSelected(row);
    map.current?.flyTo({ center: [row.lon, row.lat], zoom: Math.max(map.current.getZoom(), 11), duration: 600 });
  }, []);

  const typeList = useMemo(() => Object.keys(counts).sort(), [counts]);
  const totalShown = events.length;

  const toggleType = (type: string) =>
    setFilters((current) => ({
      ...current,
      types: current.types.includes(type)
        ? current.types.filter((item) => item !== type)
        : [...current.types, type],
    }));

  const toggleSeverity = (severity: string) =>
    setFilters((current) => ({
      ...current,
      severities: current.severities.includes(severity)
        ? current.severities.filter((item) => item !== severity)
        : [...current.severities, severity],
    }));

  return (
    <div className={`map-view ${assistantOpen ? "map-view--assistant" : ""}`}>
      <aside className="map-rail">
        <div className="map-rail-head">
          <h2>Safety events</h2>
          <span className="map-count">{totalShown}</span>
        </div>
        <p className="map-sub" data-map-summary>
          {loading ? "Loading…" : summary || "no events"}
        </p>

        <div className="map-actions">
          <button className="map-btn" onClick={() => fitToEvents()} disabled={!events.length}>
            Fit to events
          </button>
          <button className="map-btn map-btn--ghost" onClick={() => setShowUpload(true)}>
            Upload file
          </button>
        </div>

        <section className="map-block">
          <div className="map-live-head">
            <h3>Live traffic</h3>
            <button
              className={`map-toggle ${live ? "is-on" : ""}`}
              data-live-toggle
              onClick={() => setLive((value) => !value)}
            >
              {live ? "On" : "Off"}
            </button>
          </div>
          {live && (
            <p className="map-live-status" data-live-status>
              {liveData
                ? `${liveData.count} aircraft · ${liveData.mode} · ${liveData.credits_used}/${liveData.credits_budget} credits today · ${Math.round(liveData.age_seconds)}s old`
                : liveError
                  ? "no contact"
                  : "contacting OpenSky…"}
            </p>
          )}
          {liveNote && live && <p className="map-live-hint">{liveNote}</p>}
          {liveData?.credential_warning && live && (
            <p className="map-warning" data-cred-warning>
              {liveData.credential_warning} Showing the anonymous allowance meanwhile.
            </p>
          )}
          {liveError && live && <p className="map-error">{liveError}</p>}
          {!live && liveStatus && (
            <p className="map-live-hint">
              OpenSky ADS-B · {liveStatus.mode} · {liveStatus.credits_used}/{liveStatus.credits_budget} credits used today
            </p>
          )}
        </section>

        {live && rules && (
          <section className="map-block">
            <h3>Detection rules</h3>
            <div className="rule-grid">
              <label>
                <span>separation NM</span>
                <input
                  type="number"
                  step="0.5"
                  min="0.5"
                  data-rule="min_separation_nm"
                  value={rules.min_separation_nm}
                  onChange={(event) =>
                    setRules({ ...rules, min_separation_nm: Number(event.target.value) })
                  }
                />
              </label>
              <label>
                <span>vertical ft</span>
                <input
                  type="number"
                  step="100"
                  min="0"
                  data-rule="min_vertical_ft"
                  value={rules.min_vertical_ft}
                  onChange={(event) => setRules({ ...rules, min_vertical_ft: Number(event.target.value) })}
                />
              </label>
              <label>
                <span>low band alt ft</span>
                <input
                  type="number"
                  step="500"
                  min="0"
                  data-rule="band_alt"
                  value={rules.speed_bands?.[0]?.max_alt_ft ?? 10000}
                  onChange={(event) => setRules(bandEdit(rules, 0, { max_alt_ft: Number(event.target.value) }))}
                />
              </label>
              <label>
                <span>low band kt</span>
                <input
                  type="number"
                  step="10"
                  min="50"
                  data-rule="band_low_kt"
                  value={rules.speed_bands?.[0]?.max_speed_kt ?? 250}
                  onChange={(event) => setRules(bandEdit(rules, 0, { max_speed_kt: Number(event.target.value) }))}
                />
              </label>
              <label>
                <span>above kt</span>
                <input
                  type="number"
                  step="10"
                  min="50"
                  data-rule="band_high_kt"
                  value={rules.speed_bands?.[rules.speed_bands.length - 1]?.max_speed_kt ?? rules.max_speed_kt}
                  onChange={(event) => {
                    const bands = rules.speed_bands ?? [
                      { max_alt_ft: 10000, max_speed_kt: 250 },
                      { max_alt_ft: null, max_speed_kt: rules.max_speed_kt },
                    ];
                    setRules(bandEdit({ ...rules, speed_bands: bands }, bands.length - 1, {
                      max_speed_kt: Number(event.target.value),
                    }));
                  }}
                />
              </label>
            </div>
            <button
              className="map-btn"
              data-apply-rules
              disabled={rulesBusy}
              onClick={async () => {
                setRulesBusy(true);
                try {
                  await fetch("/api/detect/rules", {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                      min_separation_nm: rules.min_separation_nm,
                      min_vertical_ft: rules.min_vertical_ft,
                      speed_bands: rules.speed_bands,
                    }),
                  });
                  await fetchLive();
                } finally {
                  setRulesBusy(false);
                }
              }}
            >
              {rulesBusy ? "Applying…" : "Apply rules"}
            </button>

            {flagged && (
              <p className="map-live-status" data-detection-summary>
                {flagged.summary}
              </p>
            )}
            {flagged && flagged.conflicts.length > 0 && (
              <ul className="detection-list" data-detection-conflicts>
                {flagged.conflicts.slice(0, 5).map((item) => (
                  <li key={`${item.icao24_a}-${item.icao24_b}`} className="is-conflict">
                    <strong>
                      {item.a} / {item.b}
                    </strong>
                    <span>
                      {item.separation_nm} NM
                      {item.vertical_ft !== null && item.vertical_ft !== undefined
                        ? ` · ${item.vertical_ft} ft`
                        : ""}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {predicted && predicted.conflicts.length > 0 && (
              <ul className="detection-list" data-predicted-list>
                {predicted.conflicts.slice(0, 4).map((item) => (
                  <li key={`${item.a}-${item.b}`} className="is-predicted">
                    <strong>
                      {item.a} / {item.b}
                    </strong>
                    <span>
                      {item.predicted_nm} NM in {item.time_to_cpa_s}s (now {item.current_nm})
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {motion && motion.events.length > 0 && (
              <ul className="detection-list" data-motion-list>
                {motion.events.slice(0, 4).map((item) => (
                  <li key={`${item.callsign}-${item.kind}`} className="is-motion">
                    <strong>{item.callsign}</strong>
                    <span>
                      {item.kind.replace(/_/g, " ")} — {item.detail}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {unusual && unusual.flags.length > 0 && (
              <ul className="detection-list" data-unusual-list>
                {unusual.flags.slice(0, 5).map((item) => (
                  <li key={`${item.icao24}-${item.kind}`} className="is-unusual">
                    <strong>{item.callsign}</strong>
                    <span>{item.detail}</span>
                  </li>
                ))}
              </ul>
            )}
            {flagged && flagged.overspeed.length > 0 && (
              <ul className="detection-list" data-detection-overspeed>
                {flagged.overspeed.slice(0, 5).map((item) => (
                  <li key={item.icao24} className="is-overspeed">
                    <strong>{item.callsign}</strong>
                    <span>
                      {item.ground_speed_kt} kt
                      {item.limit_kt ? ` (limit ${item.limit_kt})` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}

        <section className="map-block">
          <h3>Time</h3>
          <div className="map-chips">
            {DAY_PRESETS.map((preset) => (
              <button
                key={preset.label}
                data-day-preset={preset.days ?? "all"}
                className={`map-chip ${filters.days === preset.days ? "is-on" : ""}`}
                onClick={() => setFilters((current) => ({ ...current, days: preset.days }))}
              >
                {preset.label}
              </button>
            ))}
          </div>
        </section>

        <section className="map-block">
          <h3>Severity</h3>
          <div className="map-chips">
            {SEVERITY_ORDER.filter(
              (severity) => severityCounts[severity] || filters.severities.includes(severity),
            ).map(
              (severity) => (
                <button
                  key={severity}
                  data-severity-chip={severity}
                  className={`map-chip ${filters.severities.includes(severity) ? "is-on" : ""}`}
                  onClick={() => toggleSeverity(severity)}
                >
                  <span className="map-dot" style={{ background: SEVERITY_COLOUR[severity] }} />
                  {severity}
                  <span className="map-chip-count">{severityCounts[severity] ?? 0}</span>
                </button>
              ),
            )}
          </div>
        </section>

        <section className="map-block">
          <h3>Type</h3>
          <div className="map-chips">
            {typeList.map((type) => (
              <button
                key={type}
                data-type-chip={type}
                className={`map-chip ${filters.types.includes(type) ? "is-on" : ""}`}
                onClick={() => toggleType(type)}
              >
                {niceType(type)}
                <span className="map-chip-count">{counts[type]}</span>
              </button>
            ))}
          </div>
        </section>

        <section className="map-block">
          <h3>Source</h3>
          <div className="map-chips">
            {[null, "report", "upload", "derived"].map((value) => (
              <button
                key={value ?? "all"}
                data-source-chip={value ?? "all"}
                className={`map-chip ${filters.source === value ? "is-on" : ""}`}
                onClick={() => setFilters((current) => ({ ...current, source: value }))}
              >
                {value ?? "all"}
              </button>
            ))}
          </div>
        </section>

        {(filters.types.length || filters.severities.length || filters.source || filters.days) && (
          <button className="map-btn map-btn--ghost" onClick={() => setFilters(EMPTY_FILTERS)}>
            Clear filters
          </button>
        )}

        {error && <p className="map-error">{error}</p>}
      </aside>

      <div className="map-main">
        <div className="map-canvas" ref={mapContainer} data-map-canvas />

        <div className="map-table-wrap">
          <table className="map-table" data-event-table>
            <thead>
              <tr>
                <th>When (UTC)</th>
                <th>Type</th>
                <th>Severity</th>
                <th>Place</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {events.slice(0, 200).map((row) => (
                <tr
                  key={row.id}
                  data-event-row={row.id}
                  className={selected?.id === row.id ? "is-selected" : ""}
                  onClick={() => focusEvent(row)}
                >
                  <td className="mono">{when(row.event_time)}</td>
                  <td>
                    {niceType(row.event_type)}
                    {engineOf(row) && (
                      <span className="map-aria-tag" title="Detected by MITRE OpenARIA over the stored tracks">
                        ARIA
                      </span>
                    )}
                  </td>
                  <td>
                    <span className="map-dot" style={{ background: SEVERITY_COLOUR[row.severity] }} />
                    {row.severity}
                  </td>
                  <td>{row.place_icao ?? "—"}</td>
                  <td>{row.source}</td>
                </tr>
              ))}
              {!events.length && !loading && (
                <tr>
                  <td colSpan={5} className="map-empty">
                    Nothing matches these filters.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {aircraft && (
          <div className="map-detail map-detail--aircraft" data-aircraft-detail>
            <div className="map-detail-head">
              <strong>{aircraft.callsign ?? aircraft.icao24}</strong>
              <button className="map-detail-close" onClick={() => setAircraft(null)} aria-label="Close aircraft">
                ×
              </button>
            </div>
            <dl>
              <dt>Callsign</dt>
              <dd>{aircraft.callsign ?? "—"}</dd>
              <dt>State</dt>
              <dd>{aircraft.on_ground ? "on the ground" : "airborne"}</dd>
              <dt>Altitude</dt>
              <dd>{aircraft.altitude_ft !== null ? `${aircraft.altitude_ft} ft` : "—"}</dd>
              <dt>Ground speed</dt>
              <dd>{aircraft.ground_speed_kt !== null ? `${aircraft.ground_speed_kt} kt` : "—"}</dd>
              <dt>Vertical rate</dt>
              <dd>{aircraft.vertical_rate_fpm !== null ? `${aircraft.vertical_rate_fpm} ft/min` : "—"}</dd>
              <dt>Country</dt>
              <dd>{aircraft.country ?? "—"}</dd>
              <dt>ICAO24</dt>
              <dd className="map-provenance">{aircraft.icao24} · live ADS-B, not surveillance grade</dd>
            </dl>
          </div>
        )}

        {selected && (
          <div className="map-detail" data-event-detail>
            <div className="map-detail-head">
              <strong>{niceType(selected.event_type)}</strong>
              <button className="map-detail-close" onClick={() => setSelected(null)} aria-label="Close details">
                ×
              </button>
            </div>
            <dl>
              <dt>When</dt>
              <dd>{when(selected.event_time)}</dd>
              <dt>Where</dt>
              <dd>
                {selected.place_name ?? selected.place_icao ?? "—"} ({selected.lat.toFixed(4)},{" "}
                {selected.lon.toFixed(4)})
              </dd>
              <dt>Severity</dt>
              <dd>{selected.severity}</dd>
              {selected.registration && (
                <>
                  <dt>Aircraft</dt>
                  <dd>{selected.registration}</dd>
                </>
              )}
              <dt>Source</dt>
              <dd>{selected.source}</dd>
              {engineOf(selected) && (
                <>
                  <dt>Detected by</dt>
                  <dd>
                    OpenARIA
                    {numberOf(selected, "score") !== null && ` · score ${numberOf(selected, "score")?.toFixed(2)}`}
                  </dd>
                  <dt>Closest approach</dt>
                  <dd>
                    {numberOf(selected, "lateral_nm")?.toFixed(2)} NM
                    {numberOf(selected, "vertical_ft") !== null &&
                      ` · ${Math.abs(numberOf(selected, "vertical_ft") as number).toFixed(0)} ft`}
                    {typeof (selected.metrics as { conflict_angle?: string })?.conflict_angle === "string" &&
                      ` · ${String((selected.metrics as { conflict_angle?: string }).conflict_angle).toLowerCase()}`}
                  </dd>
                </>
              )}
              <dt>Provenance</dt>
              <dd className="map-provenance">{selected.provenance}</dd>
              {encounter && (
                <dd className="map-encounter-note" data-encounter-note>
                  Both tracks are drawn on the map — A in navy
                  {encounter.tracks[0]?.callsign ? ` (${encounter.tracks[0].callsign})` : ""}, B in orange
                  {encounter.tracks[1]?.callsign ? ` (${encounter.tracks[1].callsign})` : ""} — with the
                  closest approach ringed.
                </dd>
              )}
            </dl>
          </div>
        )}
      </div>

      {assistantOpen ? (
        <aside className="map-assistant" data-map-assistant>
          <div className="map-assistant-head">
            <span>Ask the traffic</span>
            <button
              className="map-assistant-close"
              onClick={() => setAssistantOpen(false)}
              aria-label="Hide the assistant"
              data-assistant-toggle
            >
              ×
            </button>
          </div>
          <Chat
            messages={assistant.messages}
            busy={assistant.busy}
            onSend={(text) => void assistant.send(text)}
            ready="ready"
            liveGrounding={live}
            suggestions={[
              "Any unusual flight paths right now?",
              "Which aircraft are inside the separation minimum?",
              "What is the fastest aircraft in view?",
            ]}
          />
        </aside>
      ) : (
        <button
          className="map-assistant-open"
          onClick={() => setAssistantOpen(true)}
          data-assistant-toggle
        >
          Ask
        </button>
      )}

      {showUpload && (
        <UploadPanel onClose={() => setShowUpload(false)} onImported={() => void load()} />
      )}
    </div>
  );
}
