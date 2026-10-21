// Display formatting. Money arrives as integer paise and is converted to rupees
// with integer arithmetic only, so no floating-point rounding can creep in.

const IST = "Asia/Kolkata";
const groupedInteger = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });

/** 482465000 -> "₹48,24,650.00"; -45000 -> "-₹450.00". */
export function formatINR(paise: number, decimals: 0 | 2 = 2): string {
  const negative = paise < 0;
  const absolute = Math.abs(paise);
  let rupees = Math.floor(absolute / 100);
  let fraction = absolute % 100;
  if (decimals === 0 && fraction >= 50) {
    rupees += 1; // Round half up to whole rupees.
  }
  if (decimals === 0) fraction = 0;
  const text = `₹${groupedInteger.format(rupees)}${
    decimals === 2 ? `.${String(fraction).padStart(2, "0")}` : ""
  }`;
  return negative ? `-${text}` : text;
}

/** Indian short form: ₹1.24Cr, ₹48.25L, ₹4.5K, ₹950. */
export function formatCompactINR(paise: number): string {
  const sign = paise < 0 ? "-" : "";
  const rupees = Math.abs(paise) / 100; // Display only; not used for arithmetic.
  if (rupees >= 1_00_00_000) return `${sign}₹${(rupees / 1_00_00_000).toFixed(2)}Cr`;
  if (rupees >= 1_00_000) return `${sign}₹${(rupees / 1_00_000).toFixed(2)}L`;
  if (rupees >= 1_000) return `${sign}₹${(rupees / 1_000).toFixed(1)}K`;
  return `${sign}₹${Math.round(rupees)}`;
}

/** Paise to a rupee number, for chart axes only (never for money arithmetic). */
export function paiseToRupeeNumber(paise: number): number {
  return paise / 100;
}

export function formatNumber(value: number): string {
  return groupedInteger.format(value);
}

export function formatPercent(value: number, decimals = 2): string {
  return `${value.toFixed(decimals)}%`;
}

const istDateTime = new Intl.DateTimeFormat("en-GB", {
  timeZone: IST,
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

/** "2026-10-17T03:30:00Z" -> "17 Oct 2026, 09:00:00 IST". */
export function formatIstDateTime(iso: string): string {
  return `${istDateTime.format(new Date(iso))} IST`;
}

const istClock = new Intl.DateTimeFormat("en-GB", {
  timeZone: IST,
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

export function formatIstClock(date: Date): string {
  return istClock.format(date);
}

// Calendar days ("YYYY-MM-DD") are IST days, as the backend's date filters are.

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function dayParts(day: string): [number, number, number] {
  const [year, month, date] = day.split("-").map(Number);
  return [year, month, date];
}

/** "2026-10-17" -> "17 Oct". */
export function formatDayShort(day: string): string {
  const [, month, date] = dayParts(day);
  return `${String(date).padStart(2, "0")} ${MONTHS[month - 1]}`;
}

/** "2026-10-17" -> "17 Oct 2026". */
export function formatDayLong(day: string): string {
  const [year] = dayParts(day);
  return `${formatDayShort(day)} ${year}`;
}

export function istToday(): string {
  return new Date().toLocaleDateString("en-CA", { timeZone: IST });
}

/** Shift a "YYYY-MM-DD" day by whole days. */
export function addDays(day: string, days: number): string {
  const [year, month, date] = dayParts(day);
  const shifted = new Date(Date.UTC(year, month - 1, date + days));
  return shifted.toISOString().slice(0, 10);
}

/** Inclusive number of days from `from` to `to`. */
export function daysBetween(from: string, to: string): number {
  const [y1, m1, d1] = dayParts(from);
  const [y2, m2, d2] = dayParts(to);
  return Math.round((Date.UTC(y2, m2 - 1, d2) - Date.UTC(y1, m1 - 1, d1)) / 86_400_000) + 1;
}

export function startOfIstMonth(): string {
  return `${istToday().slice(0, 8)}01`;
}

export function shortId(id: string): string {
  return id.slice(0, 8).toUpperCase();
}
