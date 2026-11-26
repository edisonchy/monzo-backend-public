/** Convert decimal pounds without floating-point rounding or accepting exponents. */
export function budgetMinor(value: string): number | null {
  const match = /^(0|[1-9]\d{0,10})(?:\.(\d{1,2}))?$/.exec(value.trim());
  if (!match) return null;
  const minor =
    Number(match[1]) * 100 + Number((match[2] || "").padEnd(2, "0"));
  return minor <= 1000000000000 ? minor : null;
}
