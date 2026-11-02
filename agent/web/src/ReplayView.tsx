import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Map as MapLibreMap,
  LngLatBounds,
  setWorkerUrl,
  type GeoJSONSource,
} from "maplibre-gl";
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import "maplibre-gl/dist/maplibre-gl.css";
import { planeImage } from "./planeIcon";

setWorkerUrl(maplibreWorkerUrl);

/**
 * Replay: step through the stored positions either side of an event.
 *
 * The map draws an event as a dot with two lines, which asks to be believed. This pane
 * hands control of the clock to the analyst: scrub, play, change speed, and watch the
 * separation close and open again. Both aircraft are drawn from the samples the poller
 * actually stored, with the gaps left visible — the position between two samples is an
 * interpolation, and the pane says so rather than hiding it.
 */

const BASEMAP_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const TRACK_COLOURS = ["#1b2a4a", "#b45309"];
const TAIL_MS = 60_000;

interface Sample {
  t: string;
  lat: number;
  lon: number;
  altitude_ft: number | null;
  ground_speed_kt: number | null;
  track_deg: number | null;
  vertical_rate_fpm: number | null;
  on_ground: boolean;
}

interface Aircraft {
  icao24: string;
  callsign: string | null;
  samples: Sample[];
}

interface Replay {
  event_id: number;
  event_time: string;
  event_type: string;
  severity: string;
  place_icao: string | null;
  callsigns: string[] | null;
  lat: number | null;
  lon: number | null;
  metrics: Record<string, unknown>;
  provenance: string | null;
  aircraft: Aircraft[];
}

interface EventOption {
  id: number;
  event_time: string;
  event_type: string;
  severity: string;
  place_icao: string | null;
  callsigns: string[] | null;
  icao24: string[] | null;
}

const clock = (iso: string) => iso.slice(11, 19);
const niceType = (value: string) => value.replace(/_/g, " ");

/** Great-circle distance in nautical miles. */
function distanceNm(a: { lat: number; lon: number }, b: { lat: number; lon: number }): number {
  const toRad = (value: number) => (value * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLon = toRad(b.lon - a.lon);
  const lat1 = toRad(a.lat);
  const lat2 = toRad(b.lat);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 2 * 3440.065 * Math.asin(Math.min(1, Math.sqrt(h)));
}

/**
 * Split a track where the data stops being a flown path.
 *
 * A feed that repeats a position (a stale fix) or jumps several miles between two
 * samples thirteen seconds apart does not describe an aircraft flying — it describes a
 * gap. Drawing a straight line across it would be inventing a route, so the line is cut
 * and the count is reported instead.
 */
const MAX_PLAUSIBLE_KT = 900;
const MAX_GAP_S = 180;

function splitTrack(samples: Sample[]): { segments: number[][][]; jumps: number; stale: number } {
  const segments: number[][][] = [];
  let current: number[][] = [];
  let jumps = 0;
  let stale = 0;

  samples.forEach((sample, index) => {
    const point: number[] = [sample.lon, sample.lat];
    if (index === 0) {
      current = [point];
      return;
    }
    const previous = samples[index - 1];
    const seconds = Math.max(1, (Date.parse(sample.t) - Date.parse(previous.t)) / 1000);
    const nm = distanceNm(previous, sample);
    const impliedKt = (nm / seconds) * 3600;

    if (nm < 0.01) {
      stale += 1; // the same fix twice: no movement to draw
      return;
    }
    if (impliedKt > MAX_PLAUSIBLE_KT || seconds > MAX_GAP_S) {
      jumps += 1;
      if (current.length > 1) segments.push(current);
      current = [point];
      return;
    }
    current.push(point);
  });

  if (current.length > 1) segments.push(current);
  return { segments, jumps, stale };
}

/** Where an aircraft is at a moment, interpolated between the two samples around it. */
function positionAt(aircraft: Aircraft, ms: number): Sample | null {
  const samples = aircraft.samples;
  if (!samples.length) return null;
  const first = Date.parse(samples[0].t);
  const last = Date.parse(samples[samples.length - 1].t);
  if (ms <= first) return samples[0];
  if (ms >= last) return samples[samples.length - 1];

  for (let index = 1; index < samples.length; index += 1) {
    const before = samples[index - 1];
    const after = samples[index];
    const start = Date.parse(before.t);
    const end = Date.parse(after.t);
    if (ms > end) continue;
    const span = Math.max(1, end - start);
    const ratio = (ms - start) / span;
    const blend = (left: number | null, right: number | null) =>
      left === null && right === null ? null : (left ?? 0) + ((right ?? 0) - (left ?? 0)) * ratio;
    return {
      t: new Date(ms).toISOString(),
      lat: before.lat + (after.lat - before.lat) * ratio,
      lon: before.lon + (after.lon - before.lon) * ratio,
      altitude_ft: blend(before.altitude_ft, after.altitude_ft),
      ground_speed_kt: blend(before.ground_speed_kt, after.ground_speed_kt),
      track_deg: blend(before.track_deg, after.track_deg),
      vertical_rate_fpm: blend(before.vertical_rate_fpm, after.vertical_rate_fpm),
      on_ground: before.on_ground && after.on_ground,
    };
  }
  return samples[samples.length - 1];
}

export function ReplayView({ focusEventId = null }: { focusEventId?: number | null }) {
  const mapNode = useRef<HTMLDivElement | null>(null);
  const map = useRef<MapLibreMap | null>(null);
  const cleanupObserver = useRef<() => void>(() => {});
  // State, not a ref: the draw effects have to re-run once the map has loaded, otherwise
  // a replay that arrives first draws nothing and never retries.
  const [mapReady, setMapReady] = useState(false);

  const [events, setEvents] = useState<EventOption[]>([]);
  const [replay, setReplay] = useState<Replay | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(focusEventId);
  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(6);
  const [loading, setLoading] = useState(false);
  // Distinct from "no events": the list is slow enough that saying there are none while
  // it is still arriving is simply untrue.
  const [loadingEvents, setLoadingEvents] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // ---- the list of things worth replaying --------------------------------
  useEffect(() => {
    let cancelled = false;
    fetch("/api/events?limit=400")
      .then((response) => response.json())
      .then((body) => {
        if (cancelled) return;
        const rows: EventOption[] = (body.events ?? []).filter(
          (row: EventOption) => Array.isArray(row.icao24) && row.icao24.length >= 2,
        );
        setEvents(rows);
        setSelectedId((current) => current ?? rows[0]?.id ?? null);
      })
      .catch(() => {
        if (!cancelled) setError("Could not load the event list.");
      })
      .finally(() => {
        if (!cancelled) setLoadingEvents(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (focusEventId !== null) setSelectedId(focusEventId);
  }, [focusEventId]);

  // ---- load one replay ----------------------------------------------------
  useEffect(() => {
    if (selectedId === null) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetch(`/api/events/${selectedId}/replay?before_s=150&after_s=150`)
      .then(async (response) => {
        const body = await response.json();
        if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status}`);
        return body as Replay;
      })
      .then((body) => {
        if (cancelled) return;
        setReplay(body);
        const times = body.aircraft.flatMap((craft) => craft.samples.map((sample) => Date.parse(sample.t)));
        setCursor(times.length ? Math.min(...times) : 0);
        setPlaying(false);
      })
      .catch((cause) => {
        if (!cancelled) {
          setReplay(null);
          setError(cause instanceof Error ? cause.message : String(cause));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  // ---- the timeline -------------------------------------------------------
  const span = useMemo(() => {
    if (!replay) return { start: 0, end: 0 };
    const times = replay.aircraft.flatMap((craft) => craft.samples.map((sample) => Date.parse(sample.t)));
    return { start: Math.min(...times), end: Math.max(...times) };
  }, [replay]);

  const positions = useMemo(() => {
    if (!replay) return [];
    return replay.aircraft.map((craft) => positionAt(craft, cursor));
  }, [replay, cursor]);

  const separation = useMemo(() => {
    if (positions.length < 2 || !positions[0] || !positions[1]) return null;
    const lateral = distanceNm(positions[0], positions[1]);
    const vertical =
      positions[0].altitude_ft !== null && positions[1].altitude_ft !== null
        ? Math.abs(positions[0].altitude_ft - positions[1].altitude_ft)
        : null;
    return { lateral, vertical };
  }, [positions, mapReady, replay, cursor]);

  // ---- map ---------------------------------------------------------------
  useEffect(() => {
    if (!mapNode.current || map.current) return;
    const instance = new MapLibreMap({
      container: mapNode.current,
      style: {
        version: 8,
        sources: {
          basemap: { type: "raster", tiles: [BASEMAP_TILES], tileSize: 256, attribution: "© OpenStreetMap contributors" },
        },
        layers: [
          { id: "bg", type: "background", paint: { "background-color": "#dfe7f1" } },
          { id: "basemap", type: "raster", source: "basemap" },
        ],
      },
      center: [103.9, 1.36],
      zoom: 8,
    });
    instance.on("load", () => {
      if (!instance.getSource("track-a")) {
        instance.addSource("track-a", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
        instance.addSource("track-b", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
        instance.addSource("tail-a", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
        instance.addSource("tail-b", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
        instance.addSource("dot-a", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
        instance.addSource("dot-b", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
        instance.addSource("cpa", { type: "geojson", data: { type: "FeatureCollection", features: [] } });

        instance.addLayer({ id: "track-a-line", type: "line", source: "track-a", paint: { "line-color": TRACK_COLOURS[0], "line-width": 2, "line-opacity": 0.65 } });
        instance.addLayer({ id: "track-b-line", type: "line", source: "track-b", paint: { "line-color": TRACK_COLOURS[1], "line-width": 2, "line-opacity": 0.65 } });
        instance.addLayer({ id: "tail-a-line", type: "line", source: "tail-a", paint: { "line-color": TRACK_COLOURS[0], "line-width": 4, "line-opacity": 0.45 } });
        instance.addLayer({ id: "tail-b-line", type: "line", source: "tail-b", paint: { "line-color": TRACK_COLOURS[1], "line-width": 4, "line-opacity": 0.45 } });

        // An aircraft, not a dot: the silhouette points where the aircraft is pointing,
        // which is the part a circle cannot tell you.
        TRACK_COLOURS.forEach((colour, index) => {
          instance.addImage(`replay-plane-${index}`, planeImage(colour), { pixelRatio: 2 });
        });
        for (const sourceId of ["dot-a", "dot-b"] as const) {
          instance.addLayer({
            id: `${sourceId}-icon`,
            type: "symbol",
            source: sourceId,
            layout: {
              "icon-image": ["get", "icon"],
              "icon-size": 0.85,
              "icon-rotate": ["get", "heading"],
              "icon-rotation-alignment": "map",
              "icon-allow-overlap": true,
              "icon-ignore-placement": true,
            },
          });
        }
        instance.addLayer({
          id: "cpa-circle",
          type: "circle",
          source: "cpa",
          paint: { "circle-radius": 5, "circle-color": "#d93a3a", "circle-opacity": 0.9 },
        });
      }
      setMapReady(true);
      instance.resize();
      // MapLibre does not watch its container: without this the canvas keeps whatever size
      // it had when the pane was first laid out.
      const observer = new ResizeObserver(() => instance.resize());
      observer.observe(mapNode.current as HTMLDivElement);
      cleanupObserver.current = () => observer.disconnect();
    });
    map.current = instance;
    return () => {
      cleanupObserver.current();
      setMapReady(false);
      instance.remove();
      map.current = null;
    };
  }, []);

  // ---- draw the tracks whenever the event changes -------------------------
  useEffect(() => {
    const instance = map.current;
    if (!instance || !mapReady || !replay) return;

    const lineFor = (craft: Aircraft) => ({
      type: "FeatureCollection" as const,
      features: splitTrack(craft.samples).segments.map((coordinates) => ({
        type: "Feature" as const,
        geometry: { type: "LineString" as const, coordinates },
        properties: {},
      })),
    });

    (instance.getSource("track-a") as GeoJSONSource | undefined)?.setData(lineFor(replay.aircraft[0]));
    (instance.getSource("track-b") as GeoJSONSource | undefined)?.setData(lineFor(replay.aircraft[1]));

    const cpa =
      replay.lat !== null && replay.lon !== null
        ? {
            type: "FeatureCollection" as const,
            features: [
              { type: "Feature" as const, geometry: { type: "Point" as const, coordinates: [replay.lon, replay.lat] }, properties: {} },
            ],
          }
        : { type: "FeatureCollection" as const, features: [] };
    (instance.getSource("cpa") as GeoJSONSource | undefined)?.setData(cpa);

    const bounds = new LngLatBounds();
    replay.aircraft.forEach((craft) => craft.samples.forEach((sample) => bounds.extend([sample.lon, sample.lat])));
    if (!bounds.isEmpty()) instance.fitBounds(bounds, { padding: 60, duration: 0 });
  }, [replay, mapReady]);

  // ---- move the dots with the clock ---------------------------------------
  useEffect(() => {
    const instance = map.current;
    if (!instance || !mapReady) return;
    const tagged = (sample: Sample | null, index: number) =>
      sample
        ? {
            type: "FeatureCollection" as const,
            features: [
              {
                type: "Feature" as const,
                geometry: { type: "Point" as const, coordinates: [sample.lon, sample.lat] },
                properties: { icon: `replay-plane-${index}`, heading: sample.track_deg ?? 0 },
              },
            ],
          }
        : { type: "FeatureCollection" as const, features: [] };

    (instance.getSource("dot-a") as GeoJSONSource | undefined)?.setData(tagged(positions[0] ?? null, 0));
    (instance.getSource("dot-b") as GeoJSONSource | undefined)?.setData(tagged(positions[1] ?? null, 1));

    // The last minute of flight, so direction reads at a glance instead of from the icon
    // angle alone.
    if (replay) {
      const tails = replay.aircraft.slice(0, 2).map((craft) => {
        const points = craft.samples
          .filter((sample) => {
            const moment = Date.parse(sample.t);
            return moment <= cursor && moment >= cursor - TAIL_MS;
          })
          .map((sample) => [sample.lon, sample.lat]);
        return {
          type: "FeatureCollection" as const,
          features:
            points.length > 1
              ? [{ type: "Feature" as const, geometry: { type: "LineString" as const, coordinates: points }, properties: {} }]
              : [],
        };
      });
      (instance.getSource("tail-a") as GeoJSONSource | undefined)?.setData(tails[0] ?? { type: "FeatureCollection", features: [] });
      (instance.getSource("tail-b") as GeoJSONSource | undefined)?.setData(tails[1] ?? { type: "FeatureCollection", features: [] });
    }
  }, [positions, mapReady, replay, cursor]);

  // ---- the clock ----------------------------------------------------------
  useEffect(() => {
    if (!playing || !span.end) return;
    let frame = 0;
    let previous = performance.now();
    const tick = (now: number) => {
      const delta = now - previous;
      previous = now;
      setCursor((current) => {
        const next = current + delta * speed;
        if (next >= span.end) {
          setPlaying(false);
          return span.end;
        }
        return next;
      });
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [playing, speed, span.end]);

  const togglePlay = useCallback(() => {
    if (cursor >= span.end) setCursor(span.start);
    setPlaying((value) => !value);
  }, [cursor, span.end, span.start]);

  const quality = useMemo(() => {
    if (!replay) return { jumps: 0, stale: 0 };
    return replay.aircraft.reduce(
      (total, craft) => {
        const split = splitTrack(craft.samples);
        return { jumps: total.jumps + split.jumps, stale: total.stale + split.stale };
      },
      { jumps: 0, stale: 0 },
    );
  }, [replay]);

  /** Separation against time, sampled once per second of the window. */
  const separationSeries = useMemo(() => {
    if (!replay || replay.aircraft.length < 2 || !span.end) return [];
    const step = Math.max(1000, (span.end - span.start) / 260);
    const points: { t: number; nm: number }[] = [];
    for (let moment = span.start; moment <= span.end; moment += step) {
      const first = positionAt(replay.aircraft[0], moment);
      const second = positionAt(replay.aircraft[1], moment);
      if (first && second) points.push({ t: moment, nm: distanceNm(first, second) });
    }
    return points;
  }, [replay, span]);

  const closest = useMemo(() => {
    if (!separationSeries.length) return null;
    return separationSeries.reduce((best, point) => (point.nm < best.nm ? point : best), separationSeries[0]);
  }, [separationSeries]);

  const sampleMoments = useMemo(() => {
    if (!replay) return [];
    const set = new Set<number>();
    replay.aircraft.forEach((craft) => craft.samples.forEach((sample) => set.add(Date.parse(sample.t))));
    return Array.from(set).sort((left, right) => left - right);
  }, [replay]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!span.end) return;
      const target = event.target as HTMLElement | null;
      if (target && ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName)) return;
      if (event.code === "Space") {
        event.preventDefault();
        setPlaying((value) => !value);
      } else if (event.code === "ArrowRight" || event.code === "ArrowLeft") {
        event.preventDefault();
        setPlaying(false);
        const direction = event.code === "ArrowRight" ? 1 : -1;
        setCursor((current) => {
          if (direction > 0) {
            const next = sampleMoments.find((moment) => moment > current + 1);
            return next ?? span.end;
          }
          const previous = [...sampleMoments].reverse().find((moment) => moment < current - 1);
          return previous ?? span.start;
        });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [span, sampleMoments]);

  const sampleTicks = useMemo(() => {
    if (!replay || !span.end) return [];
    const width = span.end - span.start || 1;
    return replay.aircraft.flatMap((craft, index) =>
      craft.samples.map((sample) => ({
        key: `${index}-${sample.t}`,
        left: ((Date.parse(sample.t) - span.start) / width) * 100,
        colour: TRACK_COLOURS[index] ?? TRACK_COLOURS[0],
      })),
    );
  }, [replay, span]);

  return (
    <div className="replay-shell" data-replay>
      <aside className="replay-list">
        <div className="replay-list-head">
          <strong>Replayable events</strong>
          <span className="ops-muted">{loadingEvents ? "…" : events.length}</span>
        </div>
        <p className="replay-hint">
          Events that name two aircraft. Each is replayed from the positions the poller
          stored — nothing is redrawn from a live feed that has since moved on.
        </p>
        <div className="replay-list-scroll">
          {events.map((event) => (
            <button
              key={event.id}
              type="button"
              className={`replay-item ${event.id === selectedId ? "is-on" : ""}`}
              data-replay-event={event.id}
              onClick={() => setSelectedId(event.id)}
            >
              <span className="replay-item-time mono">{clock(event.event_time)}</span>
              <span className="replay-item-type">{niceType(event.event_type)}</span>
              <span className="replay-item-who mono">{(event.callsigns ?? []).join(" / ") || "—"}</span>
              <span className="replay-item-place">{event.place_icao ?? "—"}</span>
            </button>
          ))}
          {!events.length && (
            <p className="ops-empty">
              {loadingEvents ? "Loading events…" : "No event names two aircraft yet."}
            </p>
          )}
        </div>
      </aside>

      <div className="replay-main">
        <div className="replay-map-frame">
          <div className="replay-map" ref={mapNode} data-replay-map />
        </div>

        {error && <p className="replay-error">{error}</p>}

        {replay && (
          <>
            <div className="replay-readout">
              <span className="mono">{clock(new Date(cursor).toISOString())}Z</span>
              {replay.aircraft.map((craft, index) => {
                const sample = positions[index];
                return (
                  <span key={craft.icao24} className="replay-craft" style={{ borderColor: TRACK_COLOURS[index] }}>
                    <span className="replay-dot" style={{ background: TRACK_COLOURS[index] }} />
                    <strong>{craft.callsign ?? craft.icao24}</strong>
                    {sample && (
                      <>
                        <span className="mono">{sample.altitude_ft !== null ? `${Math.round(sample.altitude_ft)} ft` : "alt n/a"}</span>
                        <span className="mono">{sample.ground_speed_kt !== null ? `${Math.round(sample.ground_speed_kt)} kt` : "gs n/a"}</span>
                      </>
                    )}
                  </span>
                );
              })}
              {separation && (
                <span className={`replay-sep ${separation.lateral < 3 ? "is-close" : ""}`}>
                  {separation.lateral.toFixed(2)} NM
                  {separation.vertical !== null ? ` · ${Math.round(separation.vertical)} ft` : ""}
                </span>
              )}
            </div>

            <div className="replay-controls">
              <button type="button" className="map-btn" data-replay-play onClick={togglePlay}>
                {playing ? "Pause" : cursor >= span.end ? "Replay" : "Play"}
              </button>
              <button
                type="button"
                className="map-btn map-btn--ghost"
                onClick={() => {
                  setPlaying(false);
                  setCursor(Date.parse(replay.event_time));
                }}
              >
                Jump to event
              </button>
              <label className="replay-speed">
                speed
                <select value={speed} onChange={(event) => setSpeed(Number(event.target.value))}>
                  {[1, 2, 6, 15, 40].map((value) => (
                    <option key={value} value={value}>
                      {value}×
                    </option>
                  ))}
                </select>
              </label>

              <div className="replay-scrub-wrap">
                <input
                  type="range"
                  className="replay-scrub"
                  data-replay-scrub
                  min={span.start}
                  max={span.end}
                  step={250}
                  value={cursor}
                  onChange={(event) => {
                    setPlaying(false);
                    setCursor(Number(event.target.value));
                  }}
                />
                <div className="replay-ticks" aria-hidden>
                  {sampleTicks.map((tick) => (
                    <span key={tick.key} style={{ left: `${tick.left}%`, background: tick.colour }} />
                  ))}
                </div>
              </div>
            </div>

            {separationSeries.length > 1 && (
              <div className="replay-graph" aria-label="separation against time">
                <svg viewBox="0 0 100 24" preserveAspectRatio="none" data-replay-graph>
                  {(() => {
                    const spanMs = span.end - span.start || 1;
                    const top = Math.max(3.4, ...separationSeries.map((point) => point.nm));
                    const toX = (moment: number) => ((moment - span.start) / spanMs) * 100;
                    const toY = (nm: number) => 24 - (nm / top) * 22 - 1;
                    const path = separationSeries.map((point) => `${toX(point.t).toFixed(2)},${toY(point.nm).toFixed(2)}`).join(" ");
                    const threshold = toY(3);
                    return (
                      <>
                        <line x1="0" y1={threshold} x2="100" y2={threshold} stroke="#d93a3a" strokeWidth="0.35" strokeDasharray="1 1" vectorEffect="non-scaling-stroke" />
                        <polyline points={path} fill="none" stroke="#1b2a4a" strokeWidth="1" vectorEffect="non-scaling-stroke" />
                        {closest && <circle cx={toX(closest.t)} cy={toY(closest.nm)} r="1.1" fill="#d93a3a" vectorEffect="non-scaling-stroke" />}
                        <line x1={toX(cursor)} y1="0" x2={toX(cursor)} y2="24" stroke="#b45309" strokeWidth="0.8" vectorEffect="non-scaling-stroke" />
                      </>
                    );
                  })()}
                </svg>
                <span className="replay-graph-label">
                  separation · min {closest ? `${closest.nm.toFixed(2)} NM` : "—"} · dashed line is 3 NM
                </span>
              </div>
            )}

            <p className="replay-note">
              {niceType(replay.event_type)} · {replay.severity} · {replay.place_icao ?? "unplaced"} · the ticks
              under the scrubber are the samples that actually exist. Between two ticks the position is
              interpolated, not observed.
              {quality.jumps > 0 &&
                ` ${quality.jumps} jump${quality.jumps === 1 ? "" : "s"} faster than ${MAX_PLAUSIBLE_KT} kt: the line is cut there rather than drawn across a gap.`}
              {quality.stale > 0 && ` ${quality.stale} repeated fix${quality.stale === 1 ? "" : "es"} ignored.`}
            </p>
          </>
        )}

        {loading && <p className="replay-loading">Loading the tracks…</p>}
      </div>
    </div>
  );
}
