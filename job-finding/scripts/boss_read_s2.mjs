#!/usr/bin/env node
/** 通过 web-access CDP Proxy 读取一个岗位详情和对应 BOSS 公司页工商信息。 */

import http from 'node:http';

function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith('--') || value === undefined) throw new Error(`参数无效：${key ?? '<end>'}`);
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
        if ((response.statusCode ?? 500) >= 400) reject(new Error(`代理请求失败：${method} ${path} ${response.statusCode} ${text}`));
        else resolve(text);
      });
    });
    req.on('error', reject);
    req.end(body);
  });
}

function parseProxyJson(text, operation) {
  let value;
  try { value = JSON.parse(text); } catch { throw new Error(`${operation} 返回的不是有效 JSON：${text}`); }
  if (value?.error) throw new Error(`${operation} 失败：${text}`);
  return value;
}

async function evaluate(target, expression) {
  const response = parseProxyJson(
    await request('POST', `/eval?target=${encodeURIComponent(target)}`, expression),
    '页面读取',
  );
  if (typeof response.value !== 'string') throw new Error(`页面读取未返回字符串：${JSON.stringify(response)}`);
  return JSON.parse(response.value);
}

function normalizeCompanyUrl(raw) {
  if (!raw) return null;
  const parsed = new URL(raw, 'https://www.zhipin.com');
  if (parsed.protocol !== 'https:' || parsed.hostname !== 'www.zhipin.com') return null;
  if (!/^\/gongsi\/[A-Za-z0-9_~-]+\.html$/.test(parsed.pathname)) return null;
  return `https://www.zhipin.com${parsed.pathname}`;
}

const detailExpression = `JSON.stringify((()=>{
  const blockedTerms=['安全验证','访问受限','账号登录','请完成验证','异常访问'];
  const bodyText=document.body?.innerText||'';
  const blocked=blockedTerms.find(term=>bodyText.includes(term))||null;
  const primary=document.querySelector('.job-detail-section');
  const fallback=document.querySelector('.job-detail');
  const detailRoot=primary||fallback;
  const selector=primary?'.job-detail-section':(fallback?'.job-detail':null);
  const companyCandidates=[
    ...document.querySelectorAll('.job-detail-company a[href*="/gongsi/"], .company-info a[href*="/gongsi/"], .company-name a[href*="/gongsi/"], a.company-name[href*="/gongsi/"]')
  ];
  const exactCompanyLink=document.querySelector('a[ka="job-detail-company_custompage"][href*="/gongsi/"]');
  const companyLink=exactCompanyLink||companyCandidates.find(link=>/^\\/gongsi\\/[A-Za-z0-9_~-]+\\.html$/.test(new URL(link.href,location.href).pathname))||null;
  const companyRoot=companyLink?.closest('.job-detail-company,.company-info,.company-card')||null;
  const pathMatch=location.pathname.match(/^\\/job_detail\\/([A-Za-z0-9_~-]+)\\.html$/);
  return {
    blocked,
    final_url:location.href,
    page_job_id:pathMatch?.[1]||null,
    page_job_title:document.querySelector('h1')?.innerText?.trim()||'',
    selector,
    jd_text:detailRoot?.innerText?.trim()||'',
    boss_company_name:(companyRoot?.querySelector('.company-name')?.innerText||companyLink?.innerText||'').trim(),
    company_page_url:companyLink?.href||null
  };
})())`;

const companyExpression = `JSON.stringify((()=>{
  const blockedTerms=['安全验证','访问受限','账号登录','请完成验证','异常访问'];
  const bodyText=document.body?.innerText||'';
  const blocked=blockedTerms.find(term=>bodyText.includes(term))||null;
  const root=document.querySelector('.job-sec.company-business');
  if(!root) return {blocked,url:location.href,status:'not_found',source_selector:null,fields:{}};
  const wanted=['企业名称','统一社会信用代码','法定代表人','成立时间','注册资本','注册地址'];
  const fields={};
  for(const item of root.querySelectorAll('.business-detail li')){
    const parts=(item.innerText||'').split(/\\n+/).map(value=>value.trim()).filter(Boolean);
    const labelNode=item.querySelector('.business-detail-name,.t,.label');
    const valueNode=item.querySelector('.business-detail-id,.business-detail-value,.v,.value');
    let label=(labelNode?.innerText||parts[0]||'').replace(/[：:]$/,'').trim();
    let value=(valueNode?.innerText||parts.slice(1).join(' ')||'').trim();
    if(!wanted.includes(label)){
      const joined=parts.join(' ');
      const matched=wanted.find(name=>joined.startsWith(name));
      if(matched){
        label=matched;
        value=joined.slice(matched.length).replace(/^[：:\\s]+/,'').trim();
      }
    }
    if(wanted.includes(label)&&value) fields[label]=value;
  }
  return {blocked,url:location.href,status:fields['企业名称']?'acquired':'not_found',source_selector:'.job-sec.company-business',fields};
})())`;

// 在访问浏览器前先验证要发送到页面的表达式语法。
new Function(`return (${detailExpression});`);
new Function(`return (${companyExpression});`);

const args = parseArgs(process.argv.slice(2));
const jobUrl = args.url;
const jobId = args['job-id'];
const jobKey = args['job-key'];
let knownCompanyUrls;
try { knownCompanyUrls = new Set(JSON.parse(args['known-company-urls'] ?? '[]')); } catch { throw new Error('known-company-urls 必须是 JSON 数组'); }
if (!jobUrl || !jobId || !jobKey || !Array.isArray([...knownCompanyUrls])) {
  throw new Error('用法：boss_read_s2.mjs --url URL --job-id ID --job-key KEY --known-company-urls JSON');
}

let target = null;
try {
  const created = parseProxyJson(await request('POST', '/new', jobUrl), '创建详情标签页');
  target = created.targetId ?? created.id ?? created.target;
  if (!target) throw new Error(`创建标签页后没有 target ID：${JSON.stringify(created)}`);

  const detail = await evaluate(target, detailExpression);
  if (detail.blocked) throw new Error(`岗位页面触发访问限制：${detail.blocked}`);
  if (detail.page_job_id !== jobId) throw new Error(`岗位身份不一致：期望 ${jobId}，页面为 ${detail.page_job_id ?? '空'}`);

  const normalizedCompanyUrl = normalizeCompanyUrl(detail.company_page_url);
  const result = {
    job_key: jobKey,
    job_id: jobId,
    requested_url: jobUrl,
    detail: {
      status: detail.selector && detail.jd_text ? 'ok' : 'failed',
      final_url: detail.final_url,
      selector: detail.selector,
      jd_text: detail.jd_text,
      page_job_title: detail.page_job_title,
      boss_company_name: detail.boss_company_name,
      company_page_url: normalizedCompanyUrl,
      failure: detail.selector && detail.jd_text ? null : {
        code: 'detail_selector_missing',
        message: '未读取到当前岗位详情区域',
      },
    },
    company: {status: 'not_applicable'},
  };

  if (normalizedCompanyUrl && knownCompanyUrls.has(normalizedCompanyUrl)) {
    result.company = {status: 'reused', company_page_url: normalizedCompanyUrl};
  } else if (normalizedCompanyUrl) {
    await request('POST', `/navigate?target=${encodeURIComponent(target)}`, normalizedCompanyUrl);
    const company = await evaluate(target, companyExpression);
    if (company.blocked) {
      result.company = {
        status: 'failed',
        company_page_url: normalizedCompanyUrl,
        failure: {code: 'company_page_blocked', message: `公司页触发访问限制：${company.blocked}`},
      };
    } else {
      result.company = {
        status: company.status,
        company_page_url: normalizedCompanyUrl,
        source_selector: company.source_selector,
        fields: company.fields,
      };
    }
  }

  process.stdout.write(`${JSON.stringify(result)}\n`);
} finally {
  if (target) {
    try { await request('GET', `/close?target=${encodeURIComponent(target)}`); } catch { /* 保留原始错误。 */ }
  }
}
