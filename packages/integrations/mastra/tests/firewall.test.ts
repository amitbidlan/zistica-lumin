/**
 * Tests for the Mastra firewall integration — Slice 3 PR J.
 *
 * Covers:
 *   - LuminFirewallClient: decide, fail-mode, approval polling
 *   - buildAdminRules: admin classification, blocked-message
 *     generation
 *   - translateDecision: every decision verb path
 *   - wrapToolWithFirewall: end-to-end wrapper behavior on
 *     allow / block / rewrite / require_approval
 *
 * The fetch impl is fully mocked — no network. Mastra is not a
 * dependency: the wrapper takes anything structurally compatible.
 */

import { describe, expect, it, vi } from 'vitest';

import {
  buildAdminRules,
  LuminFirewallClient,
  translateDecision,
  type DecideResponseBody,
  type FirewallConfig,
} from '../src/firewall.js';
import {
  FirewallBlockedError,
  wrapToolWithFirewall,
  type MastraToolLike,
} from '../src/wrap.js';


// ---- helpers --------------------------------------------------------------

function makeFetchStub(
  responder: (url: string, init: RequestInit) => Response | Promise<Response>,
): typeof fetch {
  return (async (url: RequestInfo | URL, init?: RequestInit) => {
    const u = typeof url === 'string' ? url : url.toString();
    return responder(u, init || {});
  }) as typeof fetch;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function makeTool(execute: MastraToolLike['execute']): MastraToolLike {
  return { id: 'test_tool', description: 'test', execute };
}


// ---- LuminFirewallClient --------------------------------------------------

describe('LuminFirewallClient.decide', () => {
  it('round-trips a real allow response', async () => {
    let capturedUrl = '';
    const fetchImpl = makeFetchStub((url) => {
      capturedUrl = url;
      return jsonResponse({ decision: 'allow' });
    });
    const client = new LuminFirewallClient({ fetchImpl });
    const out = await client.decide({ lifecycle: 'before_tool_call' });
    expect(out.decision).toBe('allow');
    expect(capturedUrl).toContain('/v1/policy/decide');
  });

  it('fails open by default when fetch throws', async () => {
    const fetchImpl = makeFetchStub(() => {
      throw new Error('network down');
    });
    const client = new LuminFirewallClient({ fetchImpl });
    const out = await client.decide({ lifecycle: 'before_tool_call' });
    expect(out.decision).toBe('allow');
    expect(out.reason).toContain('error:');
  });

  it('fails closed when onFirewallError=deny', async () => {
    const fetchImpl = makeFetchStub(() => {
      throw new Error('network down');
    });
    const client = new LuminFirewallClient({
      fetchImpl,
      onFirewallError: 'deny',
    });
    const out = await client.decide({ lifecycle: 'before_tool_call' });
    expect(out.decision).toBe('block');
    expect(out.policy_name).toBe('_firewall_fail_closed');
  });

  it('fails open on non-2xx response', async () => {
    const fetchImpl = makeFetchStub(() =>
      new Response('boom', { status: 500 }),
    );
    const client = new LuminFirewallClient({ fetchImpl });
    const out = await client.decide({ lifecycle: 'before_tool_call' });
    expect(out.decision).toBe('allow');
    expect(out.reason).toContain('http_500');
  });

  it('forwards X-Lumin-Project header', async () => {
    let captured: Record<string, string> = {};
    const fetchImpl = makeFetchStub((_url, init) => {
      captured = (init.headers as Record<string, string>) ?? {};
      return jsonResponse({ decision: 'allow' });
    });
    const client = new LuminFirewallClient({
      fetchImpl,
      project: 'my-bot',
    });
    await client.decide({ lifecycle: 'before_tool_call' });
    expect(captured['X-Lumin-Project']).toBe('my-bot');
  });

  it('forwards Authorization header when apiKey present', async () => {
    let captured: Record<string, string> = {};
    const fetchImpl = makeFetchStub((_url, init) => {
      captured = (init.headers as Record<string, string>) ?? {};
      return jsonResponse({ decision: 'allow' });
    });
    const client = new LuminFirewallClient({
      fetchImpl,
      apiKey: 'sk-test',
    });
    await client.decide({ lifecycle: 'before_tool_call' });
    expect(captured.Authorization).toBe('Bearer sk-test');
  });
});


describe('LuminFirewallClient.waitForApproval', () => {
  it('returns "allowed" when the approval resolves allowed', async () => {
    let calls = 0;
    const fetchImpl = makeFetchStub(() => {
      calls++;
      if (calls < 2) return jsonResponse({ state: 'pending' });
      return jsonResponse({ state: 'allowed' });
    });
    const client = new LuminFirewallClient({ fetchImpl });
    const v = await client.waitForApproval('apv-1', 5000);
    expect(v).toBe('allowed');
  });

  it('returns "denied" when the approval is denied', async () => {
    const fetchImpl = makeFetchStub(() =>
      jsonResponse({ state: 'denied' }),
    );
    const client = new LuminFirewallClient({ fetchImpl });
    expect(await client.waitForApproval('apv-1', 5000)).toBe('denied');
  });

  it('returns "timed_out" when the deadline passes', async () => {
    const fetchImpl = makeFetchStub(() =>
      jsonResponse({ state: 'pending' }),
    );
    const client = new LuminFirewallClient({ fetchImpl });
    const v = await client.waitForApproval('apv-1', 250);
    expect(v).toBe('timed_out');
  });
});


// ---- buildAdminRules ------------------------------------------------------

describe('buildAdminRules', () => {
  it('classifies admins case-insensitively with whitespace tolerance', () => {
    const rules = buildAdminRules({
      adminSenders: ['Telegram:5706', 'slack:U02ABCD'],
    });
    expect(rules.isAdmin('telegram:5706')).toBe(true);
    expect(rules.isAdmin('TELEGRAM:5706 ')).toBe(true);
    expect(rules.isAdmin('slack:U02ABCD')).toBe(true);
    expect(rules.isAdmin('telegram:other')).toBe(false);
    expect(rules.isAdmin(null)).toBe(false);
    expect(rules.isAdmin('')).toBe(false);
    expect(rules.isAdmin(undefined)).toBe(false);
  });

  it('non-admins always get the canned message', () => {
    const rules = buildAdminRules({
      adminSenders: ['admin'],
      userBlockedMessage: 'access denied',
    });
    const decision: DecideResponseBody = {
      decision: 'block',
      reason: 'shell exec to /etc/passwd',
      policy_name: 'destructive_shell',
      agent_feedback: "don't fabricate /approve",
    };
    expect(rules.blockedMessageFor('non-admin', decision, {})).toBe(
      'access denied',
    );
  });

  it('admins see full reasoning by default', () => {
    const rules = buildAdminRules({
      adminSenders: ['admin'],
    });
    const decision: DecideResponseBody = {
      decision: 'block',
      reason: 'shell exec',
      policy_name: 'destructive_shell',
      agent_feedback: 'guard against /approve',
    };
    const msg = rules.blockedMessageFor('admin', decision, {});
    expect(msg).toContain('destructive_shell');
    expect(msg).toContain('shell exec');
    expect(msg).toContain('guard against /approve');
  });

  it('admins get the canned message when adminSeesFullResponse=false', () => {
    const rules = buildAdminRules({
      adminSenders: ['admin'],
      adminSeesFullResponse: false,
      userBlockedMessage: 'denied',
    });
    const decision: DecideResponseBody = {
      decision: 'block',
      reason: 'shell exec',
      policy_name: 'destructive_shell',
    };
    expect(rules.blockedMessageFor('admin', decision, {})).toBe('denied');
  });

  it('falls back to the canned message when admin reasoning is empty', () => {
    const rules = buildAdminRules({
      adminSenders: ['admin'],
      userBlockedMessage: 'denied',
    });
    const decision: DecideResponseBody = { decision: 'block' };
    expect(rules.blockedMessageFor('admin', decision, {})).toBe('denied');
  });
});


// ---- translateDecision ----------------------------------------------------

describe('translateDecision', () => {
  const cfg: FirewallConfig = { adminSenders: [] };
  const rules = buildAdminRules(cfg);

  it('allow + flag are pass-through', () => {
    expect(
      translateDecision({ decision: 'allow' }, rules, cfg).blocked,
    ).toBe(false);
    expect(
      translateDecision({ decision: 'flag' }, rules, cfg).blocked,
    ).toBe(false);
  });

  it('block sets reason and llmFeedback', () => {
    const r = translateDecision(
      { decision: 'block', reason: 'bad', decision_id: 'd1' },
      rules,
      cfg,
    );
    expect(r.blocked).toBe(true);
    expect(r.reason).toBe('bad');
    expect(r.decisionId).toBe('d1');
    expect(r.llmFeedback).toBeTruthy();
  });

  it('rewrite with params returns rewrittenParams', () => {
    const r = translateDecision(
      { decision: 'rewrite', rewritten: { params: { x: 1 } } },
      rules,
      cfg,
    );
    expect(r.blocked).toBe(false);
    expect(r.rewrittenParams).toEqual({ x: 1 });
  });

  it('rewrite without params downgrades to block', () => {
    const r = translateDecision(
      { decision: 'rewrite' } as DecideResponseBody,
      rules,
      cfg,
    );
    expect(r.blocked).toBe(true);
    expect(r.reason).toContain('rewrite without payload');
  });

  it('require_approval forwards approval_id', () => {
    const r = translateDecision(
      { decision: 'require_approval', approval_id: 'apv-1' },
      rules,
      cfg,
    );
    expect(r.blocked).toBe(true);
    expect(r.approvalId).toBe('apv-1');
    expect(r.decision).toBe('require_approval');
  });

  it('unknown verb degrades to allow (Rule 7)', () => {
    const r = translateDecision(
      { decision: 'mystery' as never } as DecideResponseBody,
      rules,
      cfg,
    );
    expect(r.blocked).toBe(false);
  });
});


// ---- wrapToolWithFirewall -------------------------------------------------

describe('wrapToolWithFirewall', () => {
  it('preserves id + description', () => {
    const original = makeTool(async () => 'ok');
    const wrapped = wrapToolWithFirewall(original, {
      fetchImpl: makeFetchStub(() => jsonResponse({ decision: 'allow' })),
    });
    expect(wrapped.id).toBe('test_tool');
    expect(wrapped.description).toBe('test');
  });

  it('runs the underlying tool when the firewall allows', async () => {
    const original = makeTool(async () => 'tool result');
    const wrapped = wrapToolWithFirewall(original, {
      fetchImpl: makeFetchStub(() => jsonResponse({ decision: 'allow' })),
    });
    const out = await wrapped.execute({ context: { x: 1 } });
    expect(out).toBe('tool result');
  });

  it('throws FirewallBlockedError when blocked', async () => {
    const original = makeTool(async () => 'tool result');
    const wrapped = wrapToolWithFirewall(original, {
      fetchImpl: makeFetchStub(() =>
        jsonResponse({
          decision: 'block',
          reason: 'destructive shell',
          policy_name: 'p1',
        }),
      ),
    });
    await expect(wrapped.execute({ context: {} })).rejects.toBeInstanceOf(
      FirewallBlockedError,
    );
  });

  it('substitutes rewritten params before running the tool', async () => {
    let seen: Record<string, unknown> = {};
    const original = makeTool(async (ctx) => {
      seen = ctx.context;
      return 'ok';
    });
    const wrapped = wrapToolWithFirewall(original, {
      fetchImpl: makeFetchStub(() =>
        jsonResponse({
          decision: 'rewrite',
          rewritten: { params: { redacted: true } },
        }),
      ),
    });
    await wrapped.execute({ context: { secret: 'leak' } });
    expect(seen).toEqual({ redacted: true });
  });

  it('runs the tool after an allowed approval', async () => {
    let approvalCalls = 0;
    const fetchImpl = makeFetchStub((url) => {
      if (url.includes('/v1/policy/decide')) {
        return jsonResponse({
          decision: 'require_approval',
          approval_id: 'apv-1',
        });
      }
      // approval long-poll
      approvalCalls++;
      return jsonResponse({ state: approvalCalls >= 1 ? 'allowed' : 'pending' });
    });

    const original = makeTool(async () => 'approved-result');
    const wrapped = wrapToolWithFirewall(original, {
      fetchImpl,
      approvalTimeoutMs: 5000,
    });

    const out = await wrapped.execute({ context: {} });
    expect(out).toBe('approved-result');
  });

  it('throws when approval is denied', async () => {
    const fetchImpl = makeFetchStub((url) => {
      if (url.includes('/v1/policy/decide')) {
        return jsonResponse({
          decision: 'require_approval',
          approval_id: 'apv-1',
        });
      }
      return jsonResponse({ state: 'denied' });
    });

    const original = makeTool(async () => 'should-not-run');
    const wrapped = wrapToolWithFirewall(original, {
      fetchImpl,
      approvalTimeoutMs: 5000,
    });

    await expect(wrapped.execute({ context: {} })).rejects.toBeInstanceOf(
      FirewallBlockedError,
    );
  });

  it('non-admin sender gets canned message; admin gets full reasoning', async () => {
    const fetchImpl = makeFetchStub(() =>
      jsonResponse({
        decision: 'block',
        reason: 'destructive shell',
        policy_name: 'p1',
      }),
    );

    const adminTool = wrapToolWithFirewall(
      makeTool(async () => 'x'),
      {
        fetchImpl,
        adminSenders: ['admin-id'],
      },
    );

    // Non-admin sender via runtimeContext.get
    const nonAdminCtx = {
      context: {},
      runtimeContext: {
        get: (k: string) => (k === 'sender_id' ? 'random-user' : undefined),
      },
    };
    await expect(adminTool.execute(nonAdminCtx))
      .rejects.toThrow(/security policy/);

    // Admin sender — full reasoning
    const adminCtx = {
      context: {},
      runtimeContext: {
        get: (k: string) => (k === 'sender_id' ? 'admin-id' : undefined),
      },
    };
    await expect(adminTool.execute(adminCtx))
      .rejects.toThrow(/destructive shell|p1/);
  });

  it('forwards trace_id / agent / project from runtime context', async () => {
    let captured: Record<string, unknown> = {};
    const fetchImpl = makeFetchStub((_url, init) => {
      const body = JSON.parse(String(init.body));
      captured = body;
      return jsonResponse({ decision: 'allow' });
    });

    const wrapped = wrapToolWithFirewall(makeTool(async () => 'x'), {
      fetchImpl,
      project: 'my-app',
      agent: 'researcher',
    });

    await wrapped.execute({
      context: { q: 'hello' },
      runtimeContext: {
        get: (k: string) =>
          ({ trace_id: 'tr-1', sessionId: 'sess-1' } as Record<string, string>)[
            k
          ],
      },
    });

    expect(captured.tool_name).toBe('test_tool');
    expect(captured.params).toEqual({ q: 'hello' });
    expect(captured.trace_id).toBe('tr-1');
    expect(captured.session_id).toBe('sess-1');
    expect(captured.agent).toBe('researcher');
    expect(captured.project).toBe('my-app');
  });

  it('onDecision callback is called even when blocked, errors are swallowed', async () => {
    const onDecision = vi.fn(() => {
      throw new Error('observer crashed');
    });
    const fetchImpl = makeFetchStub(() =>
      jsonResponse({ decision: 'block', reason: 'no' }),
    );
    const wrapped = wrapToolWithFirewall(makeTool(async () => 'x'), {
      fetchImpl,
      onDecision,
    });
    await expect(wrapped.execute({ context: {} })).rejects.toBeInstanceOf(
      FirewallBlockedError,
    );
    expect(onDecision).toHaveBeenCalledOnce();
  });

  it('preserves arbitrary tool fields on the wrapped object', () => {
    const tool: MastraToolLike & { customField: string } = {
      id: 'custom_tool',
      execute: async () => 'x',
      customField: 'preserved',
    };
    const wrapped = wrapToolWithFirewall(tool, {
      fetchImpl: makeFetchStub(() => jsonResponse({ decision: 'allow' })),
    });
    expect((wrapped as unknown as { customField: string }).customField).toBe(
      'preserved',
    );
  });
});
