/**
 * Unit tests for the profile + override resolver.
 *
 * Pinned to spec §3 contracts: changing a profile's defaults is a
 * breaking change for operators who rely on its name as an audit
 * baseline. These tests are the gate.
 */

import { describe, it, expect } from "vitest";
import { resolveSecuritySettings, SECURITY_PROFILES } from "../src/index";

describe("SECURITY_PROFILES", () => {
  it("declares the four canonical profiles + three legacy aliases", () => {
    expect(Object.keys(SECURITY_PROFILES).sort()).toEqual([
      "balanced", "light", "logging-only",
      "observability", "permissive", "standard", "strict",
    ]);
  });

  it("legacy aliases point at the same object as the canonical profile", () => {
    expect(SECURITY_PROFILES.balanced).toBe(SECURITY_PROFILES.standard);
    expect(SECURITY_PROFILES.permissive).toBe(SECURITY_PROFILES.light);
    expect(SECURITY_PROFILES.observability).toBe(SECURITY_PROFILES["logging-only"]);
  });

  it("strict has every protection on", () => {
    const p = SECURITY_PROFILES.strict;
    expect(p.enforce).toBe(true);
    expect(p.failClosedOnMissingWorkspace).toBe(true);
    expect(p.l2HistoryResetMode).toBe("clear-on-switch");
    expect(p.l3Detectors.vault_exact).toBe(true);
    expect(p.l3Detectors.structural_pattern).toBe(true);
    expect(p.l3Detectors.presidio).toBe(true);
    expect(p.auditSamplingRate).toBe(1.0);
    expect(p.deniedTools.length).toBeGreaterThan(0);
  });

  it("logging-only never blocks", () => {
    const p = SECURITY_PROFILES["logging-only"];
    expect(p.enforce).toBe(false);
    expect(p.deniedTools).toEqual([]);
    expect(p.l2HistoryResetMode).toBe("off");
  });

  it("light samples audit at 10%", () => {
    expect(SECURITY_PROFILES.light.auditSamplingRate).toBe(0.1);
  });
});


describe("resolveSecuritySettings — profile baseline", () => {
  it("uses 'standard' as default when no profile set", () => {
    const r = resolveSecuritySettings({});
    expect(r.enforce).toBe(true);
    expect(r.l2HistoryResetMode).toBe("clear-on-switch");
    expect(r.l3Detectors.presidio).toBe(false);  // standard
  });

  it("applies named profile defaults", () => {
    const r = resolveSecuritySettings({ securityProfile: "strict" });
    expect(r.l3Detectors.presidio).toBe(true);
    expect(r.failClosedOnMissingWorkspace).toBe(true);
  });

  it("falls back to 'standard' when given an unknown profile name", () => {
    const r = resolveSecuritySettings({ securityProfile: "WAT" as never });
    expect(r.l3Detectors.presidio).toBe(false);  // standard
    expect(r.enforce).toBe(true);
  });
});


describe("resolveSecuritySettings — friendly toggles", () => {
  it("enableTenantIsolation: false flips enforce off", () => {
    const r = resolveSecuritySettings({ enableTenantIsolation: false });
    expect(r.enforce).toBe(false);
  });

  it("blockShellTools: false removes shell-class denies but keeps egress", () => {
    const r = resolveSecuritySettings({
      securityProfile: "standard",
      blockShellTools: false,
    });
    // Egress denies remain (web_fetch etc.)
    expect(r.deniedTools).toContain("web_fetch");
    expect(r.deniedTools).not.toContain("exec");
    expect(r.deniedTools).not.toContain("bash");
  });

  it("blockWebTools: false removes egress denies but keeps shells", () => {
    const r = resolveSecuritySettings({
      securityProfile: "standard",
      blockWebTools: false,
    });
    expect(r.deniedTools).toContain("exec");
    expect(r.deniedTools).not.toContain("web_fetch");
  });

  it("both blockShellTools and blockWebTools false → empty deny list", () => {
    const r = resolveSecuritySettings({
      blockShellTools: false,
      blockWebTools: false,
    });
    expect(r.deniedTools).toEqual([]);
  });

  it("resetMemoryBetweenUsers maps to l2HistoryResetMode", () => {
    expect(resolveSecuritySettings({ resetMemoryBetweenUsers: "between-users" }).l2HistoryResetMode).toBe("clear-on-switch");
    expect(resolveSecuritySettings({ resetMemoryBetweenUsers: "between-channels" }).l2HistoryResetMode).toBe("scope-by-channel");
    expect(resolveSecuritySettings({ resetMemoryBetweenUsers: "never" }).l2HistoryResetMode).toBe("off");
  });

  it("hideOtherUsersData: false zeroes all detectors", () => {
    const r = resolveSecuritySettings({ hideOtherUsersData: false });
    expect(r.l3Detectors.vault_exact).toBe(false);
    expect(r.l3Detectors.structural_pattern).toBe(false);
    expect(r.l3Detectors.presidio).toBe(false);
  });

  it("hideOtherUsersData: true enables all detectors", () => {
    const r = resolveSecuritySettings({
      securityProfile: "light",       // would normally have only vault_exact
      hideOtherUsersData: true,
    });
    expect(r.l3Detectors.vault_exact).toBe(true);
    expect(r.l3Detectors.structural_pattern).toBe(true);
    expect(r.l3Detectors.presidio).toBe(true);
  });

  it("recordSecurityEvents: boolean → 1.0 / 0.0", () => {
    expect(resolveSecuritySettings({ recordSecurityEvents: true }).auditSamplingRate).toBe(1.0);
    expect(resolveSecuritySettings({ recordSecurityEvents: false }).auditSamplingRate).toBe(0.0);
  });

  it("recordSecurityEvents: number is clamped to [0, 1]", () => {
    expect(resolveSecuritySettings({ recordSecurityEvents: 0.25 }).auditSamplingRate).toBe(0.25);
    expect(resolveSecuritySettings({ recordSecurityEvents: 5 }).auditSamplingRate).toBe(1);
    expect(resolveSecuritySettings({ recordSecurityEvents: -0.5 }).auditSamplingRate).toBe(0);
  });
});


describe("resolveSecuritySettings — technical fields override friendly", () => {
  it("explicit deniedTools wins over blockShellTools", () => {
    const r = resolveSecuritySettings({
      blockShellTools: true,
      deniedTools: ["only_this_one"],
    });
    expect(r.deniedTools).toEqual(["only_this_one"]);
  });

  it("l3Detectors per-key wins over hideOtherUsersData=true", () => {
    const r = resolveSecuritySettings({
      hideOtherUsersData: true,
      l3Detectors: { presidio: false },
    });
    expect(r.l3Detectors.vault_exact).toBe(true);     // from hideOtherUsersData
    expect(r.l3Detectors.structural_pattern).toBe(true);
    expect(r.l3Detectors.presidio).toBe(false);       // explicit override
  });

  it("auditSamplingRate wins over recordSecurityEvents", () => {
    const r = resolveSecuritySettings({
      recordSecurityEvents: true,
      auditSamplingRate: 0.05,
    });
    expect(r.auditSamplingRate).toBe(0.05);
  });

  it("enforce: false (legacy) wins over enableTenantIsolation: true", () => {
    const r = resolveSecuritySettings({
      enableTenantIsolation: true,
      enforce: false,
    });
    expect(r.enforce).toBe(false);
  });
});


describe("resolveSecuritySettings — profile + overrides composition", () => {
  it("strict profile with hideOtherUsersData=false disables all L3", () => {
    const r = resolveSecuritySettings({
      securityProfile: "strict",
      hideOtherUsersData: false,
    });
    expect(r.failClosedOnMissingWorkspace).toBe(true);  // from strict
    expect(r.l3Detectors.vault_exact).toBe(false);      // from override
    expect(r.l3Detectors.presidio).toBe(false);
  });

  it("logging-only with explicit blockShellTools=true keeps observability but blocks shells", () => {
    const r = resolveSecuritySettings({
      securityProfile: "logging-only",
      blockShellTools: true,
    });
    expect(r.enforce).toBe(false);                       // from logging-only
    expect(r.deniedTools).toContain("exec");             // from override
    expect(r.deniedTools).not.toContain("web_fetch");    // logging-only had no egress
  });
});
