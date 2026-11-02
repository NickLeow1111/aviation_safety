/**
 * Who is flying, from the callsign.
 *
 * A callsign is the airline's ICAO designator followed by the flight number — SIA324 is
 * Singapore Airlines, AXM5227 is AirAsia — so the first three letters are the operator.
 * Only the operators this feed actually sees are named; an unknown prefix is shown as the
 * code rather than guessed at, because a wrong airline next to an event is worse than a
 * bare code an analyst can look up.
 */

const AIRLINES: Record<string, string> = {
  // Singapore and the neighbourhood
  SIA: "Singapore Airlines",
  SLK: "SilkAir",
  TGW: "Scoot",
  SCO: "Scoot",
  AXM: "AirAsia",
  XAX: "AirAsia X",
  MXD: "Batik Air Malaysia",
  MAS: "Malaysia Airlines",
  FFM: "Firefly",
  CEB: "Cebu Pacific",
  SEJ: "SpiceJet",
  IGO: "IndiGo",
  AIC: "Air India",
  GIA: "Garuda Indonesia",
  LNI: "Lion Air",
  BTK: "Batik Air",
  HVN: "Vietnam Airlines",
  VJC: "VietJet Air",
  BAV: "Bamboo Airways",
  THA: "Thai Airways",
  TAI: "Thai Airways",
  PAL: "Philippine Airlines",
  // Further afield, all of which appear on the sector
  DLH: "Lufthansa",
  QFA: "Qantas",
  QLK: "QantasLink",
  ANZ: "Air New Zealand",
  CPA: "Cathay Pacific",
  UAE: "Emirates",
  ETD: "Etihad Airways",
  QTR: "Qatar Airways",
  SVA: "Saudia",
  KAL: "Korean Air",
  AAR: "Asiana Airlines",
  ANA: "All Nippon Airways",
  JAL: "Japan Airlines",
  CES: "China Eastern",
  CSN: "China Southern",
  CCA: "Air China",
  CHH: "Hainan Airlines",
  CSZ: "Shenzhen Airlines",
  CXA: "XiamenAir",
  EVA: "EVA Air",
  CAL: "China Airlines",
  TUR: "Turkish Airlines",
  BAW: "British Airways",
  KLM: "KLM",
  AFR: "Air France",
  SWR: "Swiss",
  SIA_CARGO: "Singapore Airlines Cargo",
};

export interface Airline {
  /** ICAO designator, e.g. SIA. */
  code: string;
  /** Full name when known, otherwise the code itself. */
  name: string;
  /** False when the code was read off the callsign but is not in the table. */
  known: boolean;
}

/** The operator behind a flight, from its callsign(s). */
export function airlineOf(callsigns: string[] | null | undefined): Airline | null {
  const callsign = (callsigns ?? []).find((value) => typeof value === "string" && value.trim().length >= 3);
  if (!callsign) return null;

  const code = callsign.trim().slice(0, 3).toUpperCase();
  if (!/^[A-Z]{3}$/.test(code)) return null;

  const name = AIRLINES[code];
  return { code, name: name ?? code, known: Boolean(name) };
}

/** Sort alphabetically by operator, with unbranded flights last. */
export function airlineSortKey(callsigns: string[] | null | undefined): string {
  const airline = airlineOf(callsigns);
  return airline ? airline.name.toUpperCase() : "~";
}
