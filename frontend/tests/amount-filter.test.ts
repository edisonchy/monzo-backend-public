import assert from "node:assert/strict";
import { test } from "node:test";
import { amountFilter } from "../src/lib/amount-filter.ts";

for (const [currency, whole, fractional, input, underFive, overHundred] of [
  ["GBP", "1000", "10.01", "1001", "4.99", "100.01"],
  ["JPY", "10", "11", "11", "4", "101"],
  ["KWD", "10000", "10.001", "10001", "4.999", "100.001"],
]) {
  test(`${currency} transaction filter round-trips amounts and uses inclusive preset boundaries`, () => {
    const filter = amountFilter(currency);
    assert.equal(filter.minor("10"), whole);
    assert.equal(filter.minor(fractional), input);
    assert.equal(filter.input(input), fractional);
    assert.equal(filter.underFive, underFive);
    assert.equal(filter.overHundred, overHundred);
    assert.equal(
      Number(filter.minor(underFive)),
      Number(filter.minor("5")) - 1,
    );
    assert.equal(
      Number(filter.minor(overHundred)),
      Number(filter.minor("100")) + 1,
    );
  });
}

test("filter precision, optional amounts and upper limits follow the API", () => {
  assert.throws(() => amountFilter("JPY").minor("1.5"));
  assert.throws(() => amountFilter("GBP").minor("1.001"));
  assert.throws(() => amountFilter("KWD").minor("1.0001"));
  assert.equal(amountFilter("JPY").minor("100000000000"), "100000000000");
  assert.throws(() => amountFilter("JPY").minor("100000000001"));
  assert.equal(amountFilter("GBP").minor(""), undefined);
  assert.equal(amountFilter("GBP").input(undefined), "");
});
