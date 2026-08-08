#!/usr/bin/env node
/** 通过 web-access CDP 代理采集 S1 岗位卡片，并保存可验证的滚动证据。 */

import http from 'node:http';

function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith('--') || value === undefined) throw new Error(`invalid argument near ${key ?? '<end>'}`);
    result[key.slice(2)] = value;
  }
  return result;
}

function request(method, path, body = '') {
  return new Promise((resolve, reject) => {
    const req = http.request({
      host: 'localhost',
      port: 3456,
      method,
      path,
      headers: body ? {
        'content-type': 'text/plain; charset=utf-8',
        'content-length': Buffer.byteLength(body),
      } : {},
    }, (response) => {
      let text = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => { text += chunk; });
      response.on('end', () => {
        if ((response.statusCode ?? 500) >= 400) reject(new Error(`proxy ${method} ${path} failed: ${response.statusCode} ${text}`));
        else resolve(text);
      });
    });
    req.on('error', reject);
    req.end(body);
  });
}

const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function parseProxyJson(text, operation) {
  let value;
  try { value = JSON.parse(text); } catch { throw new Error(`${operation} returned invalid JSON: ${text}`); }
  if (value?.error) throw new Error(`${operation} failed: ${text}`);
  return value;
}

async function evaluate(target, expression) {
  const response = parseProxyJson(
    await request('POST', `/eval?target=${encodeURIComponent(target)}`, expression),
    'eval',
  );
  if (typeof response.value !== 'string') throw new Error(`eval returned no string value: ${JSON.stringify(response)}`);
  return JSON.parse(response.value);
}

const pageExpression = `JSON.stringify((()=>{
  const blockedTerms=['安全验证','访问受限','账号登录','请完成验证','异常访问'];
  const endTerms=['没有更多职位','没有更多了','暂无更多','已加载全部'];
  const text=document.body?.innerText||'';
  const blocked=blockedTerms.find(term=>text.includes(term))||null;
  const endMarker=endTerms.find(term=>text.includes(term))||null;
  const cards=[...document.querySelectorAll('.job-card-wrap')].map((card)=>{
    const link=card.querySelector('a.job-name');
    const href=link?.href||'';
    const match=new URL(href||location.href,location.href).pathname.match(/^\\/job_detail\\/([A-Za-z0-9_~-]+)\\.html$/);
    return {
      job_id:match?.[1]||'',
      boss_job_url:href,
      job_title:link?.innerText?.trim()||'',
      brand_company_name:card.querySelector('.boss-name')?.innerText?.trim()||'',
      salary:card.querySelector('.job-salary')?.innerText||'',
      tags:[...card.querySelectorAll('.tag-list li')].map(item=>item.innerText.trim()),
      card_city:card.querySelector('.company-location')?.innerText?.trim()||'',
      posted_at:''
    };
  });
  return {
    title:document.title,
    url:location.href,
    blocked,
    end_marker:endMarker,
    scroll_y:Math.round(window.scrollY),
    viewport_height:Math.round(window.innerHeight),
    scroll_height:Math.max(document.body?.scrollHeight||0,document.documentElement?.scrollHeight||0),
    cards
  };
})())`;

function stableCards(cards) {
  const result = [];
  const seen = new Set();
  for (const card of cards) {
    if (!card.job_id || !card.boss_job_url || !card.job_title || !card.brand_company_name) {
      throw new Error(`invalid visible card: ${JSON.stringify(card)}`);
    }
    const key = `id:${card.job_id}`;
    if (!seen.has(key)) {
      seen.add(key);
      result.push(card);
    }
  }
  return result;
}

const args = parseArgs(process.argv.slice(2));
const searchUrl = args.url;
const city = args.city;
const term = args.term;
const mode = args.mode ?? 'sample';
const limit = mode === 'sample' ? Number.parseInt(args.limit ?? '20', 10) : null;
const existingTarget = args['existing-target'] ?? null;
const observedInitialCount = args['initial-visible-count'] === undefined
  ? null
  : Number.parseInt(args['initial-visible-count'], 10);
const observedScrollRounds = args['scroll-rounds'] === undefined
  ? null
  : Number.parseInt(args['scroll-rounds'], 10);
if (!searchUrl || !city || !term || !['sample', 'exhaustive'].includes(mode)
    || (mode === 'sample' && (!Number.isInteger(limit) || limit < 1))) {
  throw new Error('usage: boss_collect_s1.mjs --url URL --city CITY --term TERM --mode sample|exhaustive [--limit N] [--existing-target ID --initial-visible-count N --scroll-rounds N]');
}
if (existingTarget && (!Number.isInteger(observedInitialCount) || observedInitialCount < 0
    || !Number.isInteger(observedScrollRounds) || observedScrollRounds < 1)) {
  throw new Error('existing-target requires observed initial-visible-count and scroll-rounds');
}

let target = null;
let ownsTarget = false;
let preserveTarget = false;
try {
  if (existingTarget) {
    target = existingTarget;
  } else {
    const created = parseProxyJson(await request('POST', '/new', searchUrl), 'new tab');
    target = created.targetId ?? created.id ?? created.target;
    if (!target) throw new Error(`new tab returned no target id: ${JSON.stringify(created)}`);
    ownsTarget = true;
    await wait(2000);
  }

  let snapshot = await evaluate(target, pageExpression);
  if (snapshot.blocked) throw new Error(`page is blocked: ${snapshot.blocked}`);
  if (snapshot.url !== searchUrl) throw new Error(`target URL mismatch: ${snapshot.url}`);
  const collected = new Map();
  function mergeCards(rawCards) {
    const cards = stableCards(rawCards);
    let added = 0;
    for (const card of cards) {
      const key = `id:${card.job_id}`;
      if (!collected.has(key)) {
        collected.set(key, card);
        added += 1;
      }
    }
    return added;
  }
  mergeCards(snapshot.cards);
  const initialVisibleCount = existingTarget ? observedInitialCount : collected.size;
  const manualScrollRounds = existingTarget ? observedScrollRounds : 0;
  let automatedScrollRounds = 0;
  let scrollRounds = manualScrollRounds;
  let consecutiveNoNewRounds = 0;
  let successfulRefreshRounds = existingTarget && collected.size > initialVisibleCount ? 1 : 0;
  let endMarkerSeen = Boolean(snapshot.end_marker);
  const scrollTrace = [];
  const maximumAutomatedRounds = 200;

  while (((mode === 'sample' && collected.size < limit && consecutiveNoNewRounds < 10)
      || (mode === 'exhaustive' && consecutiveNoNewRounds < 10))
      && automatedScrollRounds < maximumAutomatedRounds) {
    const before = snapshot;
    await request('GET', `/scroll?target=${encodeURIComponent(target)}&y=2000`);
    scrollRounds += 1;
    automatedScrollRounds += 1;
    await wait(1500);
    snapshot = await evaluate(target, pageExpression);
    if (snapshot.blocked) throw new Error(`page is blocked: ${snapshot.blocked}`);
    let recoveryNudge = false;
    let recoveryPositionChanged = false;
    const directPositionChanged = snapshot.scroll_y !== before.scroll_y
      || snapshot.scroll_height !== before.scroll_height;
    if (!directPositionChanged && !snapshot.end_marker) {
      await request('GET', `/scroll?target=${encodeURIComponent(target)}&y=400&direction=up`);
      await wait(300);
      const recovery = await evaluate(target, pageExpression);
      if (recovery.blocked) throw new Error(`page is blocked: ${recovery.blocked}`);
      recoveryPositionChanged = recovery.scroll_y !== before.scroll_y
        || recovery.scroll_height !== before.scroll_height;
      await request('GET', `/scroll?target=${encodeURIComponent(target)}&y=2000`);
      await wait(1500);
      snapshot = await evaluate(target, pageExpression);
      if (snapshot.blocked) throw new Error(`page is blocked: ${snapshot.blocked}`);
      recoveryNudge = true;
    }
    const added = mergeCards(snapshot.cards);
    const effective = added > 0
      || snapshot.scroll_y !== before.scroll_y
      || snapshot.scroll_height !== before.scroll_height
      || recoveryPositionChanged
      || Boolean(snapshot.end_marker);
    endMarkerSeen ||= Boolean(snapshot.end_marker);
    if (added > 0) {
      successfulRefreshRounds += 1;
      consecutiveNoNewRounds = 0;
    } else if (effective) {
      consecutiveNoNewRounds += 1;
    } else {
      break;
    }
    scrollTrace.push({
      round: scrollRounds,
      before_unique_jobs: collected.size - added,
      after_unique_jobs: collected.size,
      added_unique_jobs: added,
      before_scroll_y: before.scroll_y,
      after_scroll_y: snapshot.scroll_y,
      before_scroll_height: before.scroll_height,
      after_scroll_height: snapshot.scroll_height,
      recovery_nudge: recoveryNudge,
      effective,
    });
  }

  const cards = [...collected.values()];
  const sampleIncomplete = mode === 'sample' && collected.size < limit;
  const exhaustiveIncomplete = mode === 'exhaustive' && consecutiveNoNewRounds < 10;
  const refreshUnproven = successfulRefreshRounds < 1
    && initialVisibleCount >= 15
    && !endMarkerSeen;
  if (sampleIncomplete || exhaustiveIncomplete || refreshUnproven) {
    preserveTarget = true;
    process.stdout.write(`${JSON.stringify({
      ok: false,
      status: 'manual_scroll_required',
      target_id: target,
      search_url: searchUrl,
      city,
      term,
      collection_mode: mode === 'sample' ? 'bounded_sample' : 'exhaustive',
      requested_limit: mode === 'sample' ? limit : null,
      initial_visible_count: initialVisibleCount,
      observed_unique_jobs: cards.length,
      manual_scroll_rounds: manualScrollRounds,
      automated_scroll_rounds: automatedScrollRounds,
      successful_refresh_rounds: successfulRefreshRounds,
      consecutive_no_new_rounds: consecutiveNoNewRounds,
      end_marker_seen: endMarkerSeen,
      scroll_trace: scrollTrace,
      reason: refreshUnproven
        ? '首屏达到15条但自动滚动没有证明岗位刷新'
        : '自动滚动未达到采集完成条件',
    })}\n`);
  } else {
    process.stdout.write(`${JSON.stringify({
      search_url: searchUrl,
      city,
      term,
      collection_mode: mode === 'sample' ? 'bounded_sample' : 'exhaustive',
      limit: mode === 'sample' ? limit : cards.length,
      initial_visible_count: initialVisibleCount,
      scroll_rounds: scrollRounds,
      manual_scroll_rounds: manualScrollRounds,
      automated_scroll_rounds: automatedScrollRounds,
      successful_refresh_rounds: successfulRefreshRounds,
      consecutive_no_new_rounds: consecutiveNoNewRounds,
      end_marker_seen: endMarkerSeen,
      scroll_trace: scrollTrace,
      unique_jobs_after_scroll: cards.length,
      stop_reason: mode === 'sample' ? 'sample_limit_reached' : 'natural_exhaustion',
      cards: mode === 'sample' ? cards.slice(0, limit) : cards,
    })}\n`);
  }
} finally {
  if (target && ownsTarget && !preserveTarget) {
    try { await request('GET', `/close?target=${encodeURIComponent(target)}`); } catch { /* Preserve the original error. */ }
  }
}
