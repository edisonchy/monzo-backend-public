export function amountFilter(currency: string) {
  const formatter = new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency,
  });
  const precision = formatter.resolvedOptions().maximumFractionDigits ?? 2;
  const scale = 10 ** precision;
  const symbol =
    formatter.formatToParts(0).find((part) => part.type === "currency")
      ?.value ?? currency;
  const input = (value?: string) =>
    value === undefined ? "" : (Number(value) / scale).toFixed(precision);
  function minor(value: string): string | undefined {
    const text = value.trim();
    if (!text) return undefined;
    const match = /^(\d+)(?:\.(\d*))?$/.exec(text);
    if (
      !match ||
      (match[2] !== undefined && (!precision || match[2].length > precision))
    )
      throw new Error(
        precision
          ? `Enter an amount with up to ${precision} decimal places.`
          : "Enter a whole amount.",
      );
    const amount =
      Number(match[1]) * scale +
      Number((match[2] || "").padEnd(precision, "0"));
    if (amount > 100000000000) throw new Error("Amount is too large.");
    return String(amount);
  }
  return {
    precision,
    symbol,
    input,
    minor,
    underFive: input(String(5 * scale - 1)),
    overHundred: input(String(100 * scale + 1)),
  };
}
