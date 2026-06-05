import { describe, it, expect } from "vitest";
import { ago, compact } from "./format";

describe("compact", () => {
  it("formats magnitudes the way the dashboard cards expect", () => {
    expect(compact(0)).toBe("0");
    expect(compact(999)).toBe("999");
    expect(compact(1500)).toBe("1.5k");
    expect(compact(32430)).toBe("32k");
    expect(compact(1_780_505)).toBe("1.78M");
    expect(compact(null)).toBe("—");
    expect(compact(undefined)).toBe("—");
  });
});

describe("ago", () => {
  it("handles null and recent timestamps", () => {
    expect(ago(null)).toBe("never");
    expect(ago(new Date().toISOString())).toMatch(/just now|\ds ago/);
    expect(ago(new Date(Date.now() - 3 * 3600_000).toISOString())).toMatch(/h ago/);
  });
});
