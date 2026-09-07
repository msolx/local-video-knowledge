/**
 * Persistent Browser Runtime Sidecar Server (Node.js)
 * 
 * Manages dedicated Chromium instance with persistent profile (userDataDir) via puppeteer-core.
 * Communicates with parent Python process via newline-delimited JSON over stdio (stdin/stdout).
 * Diagnostics and logs are directed exclusively to stderr.
 */

const fs = require('fs');
const path = require('path');
const readline = require('readline');

// Dynamic resolution of puppeteer-core across standard project locations
function resolvePuppeteer() {
  const candidates = [
    'puppeteer-core',
    path.resolve(__dirname, '../../../../node_modules/puppeteer-core'),
    'G:/opencode_project/opencode-user/AppData/Local/Temp/opencode/dy/node_modules/puppeteer-core',
    'G:/antigravity-cli/node_modules/puppeteer-core',
  ];
  for (const cand of candidates) {
    try {
      return require(cand);
    } catch (e) {
      // Continue to next candidate
    }
  }
  throw new Error(`Unable to resolve puppeteer-core. Checked: ${candidates.join(', ')}`);
}

const puppeteer = resolvePuppeteer();

let browser = null;
let activePage = null;
let activeConfig = null;
let isShuttingDown = false;

function logDebug(msg) {
  process.stderr.write(`[sidecar:${process.pid}] ${new Date().toISOString()} ${msg}\n`);
}

function sendResponse(id, success, data = null, error = null) {
  const payload = {
    id: id || 'unknown',
    success: Boolean(success),
    data,
    error: error ? {
      code: error.code || 'RUNTIME_ERROR',
      message: error.message || String(error),
      details: error.details || null
    } : null
  };
  process.stdout.write(JSON.stringify(payload) + '\n');
}

async function getOrInitPage() {
  if (!browser) {
    throw new Error('Browser is not launched');
  }
  let page;
  if (activePage && !activePage.isClosed()) {
    page = activePage;
  } else {
    const pages = await browser.pages();
    if (pages.length > 0) {
      page = pages[0];
    } else {
      page = await browser.newPage();
    }
    activePage = page;
  }

  // Ensure standard user-agent and viewport to match known-good requests
  try {
    await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36');
    await page.setViewport({ width: 1440, height: 900 });
  } catch (e) {
    logDebug(`Notice setting userAgent/viewport: ${e.message}`);
  }

  return page;
}

// Action / RPC Method Handlers
const methods = {
  ping: async () => ({
    status: 'pong',
    node_pid: process.pid,
    browser_pid: browser && browser.process() ? browser.process().pid : null,
    timestamp: Date.now()
  }),

  launch: async (params) => {
    if (browser && browser.isConnected()) {
      return {
        status: 'already_running',
        node_pid: process.pid,
        browser_pid: browser.process() ? browser.process().pid : null,
        userDataDir: activeConfig.userDataDir,
        headless: activeConfig.headless,
        version: await browser.version()
      };
    }

    const executablePath = params.executable_path || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
    const userDataDir = params.profile_path || 'G:/antigravity-cli/dy/runtime/chrome-profile';
    const headless = Boolean(params.headless);

    // Hardening: Strictly prohibit automatic creation of empty profile
    if (!fs.existsSync(userDataDir)) {
      throw {
        code: 'PROFILE_NOT_INITIALIZED',
        message: `userDataDir '${userDataDir}' does not exist. Automatic creation of empty profile is prohibited.`
      };
    }

    const defaultArgs = [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-blink-features=AutomationControlled',
      '--disable-infobars',
      '--window-size=1280,800'
    ];
    if (params.extra_args && Array.isArray(params.extra_args)) {
      defaultArgs.push(...params.extra_args);
    }

    logDebug(`Launching Chromium: executable=${executablePath}, profile=${userDataDir}, headless=${headless}`);

    browser = await puppeteer.launch({
      executablePath,
      userDataDir,
      headless: headless ? (params.headless_mode || true) : false,
      args: defaultArgs,
      ignoreDefaultArgs: ['--enable-automation'],
      defaultViewport: headless ? { width: 1280, height: 800 } : null
    });

    activeConfig = { executablePath, userDataDir, headless };
    const browserPid = browser.process() ? browser.process().pid : null;
    logDebug(`Chromium launched successfully (browser_pid=${browserPid})`);

    const page = await getOrInitPage();

    browser.on('disconnected', () => {
      logDebug('Browser disconnected event triggered');
      activePage = null;
    });

    return {
      status: 'launched',
      node_pid: process.pid,
      browser_pid: browserPid,
      userDataDir,
      headless,
      version: await browser.version()
    };
  },

  health: async () => {
    const isConnected = Boolean(browser && browser.isConnected());
    let pageResponsive = false;
    let currentUrl = null;
    let pagesCount = 0;

    if (isConnected) {
      try {
        const pages = await browser.pages();
        pagesCount = pages.length;
        const page = await getOrInitPage();
        currentUrl = page.url();
        const pingVal = await page.evaluate(() => 1 + 1);
        pageResponsive = (pingVal === 2);
      } catch (e) {
        logDebug(`Health check page evaluation error: ${e.message}`);
      }
    }

    return {
      healthy: isConnected && pageResponsive,
      connected: isConnected,
      page_responsive: pageResponsive,
      node_pid: process.pid,
      browser_pid: browser && browser.process() ? browser.process().pid : null,
      pages_count: pagesCount,
      current_url: currentUrl
    };
  },

  info: async () => ({
    node_pid: process.pid,
    browser_pid: browser && browser.process() ? browser.process().pid : null,
    userDataDir: activeConfig ? activeConfig.userDataDir : null,
    headless: activeConfig ? activeConfig.headless : null,
    executablePath: activeConfig ? activeConfig.executablePath : null,
    version: browser && browser.isConnected() ? await browser.version() : null,
    uptime_sec: process.uptime()
  }),

  navigate: async (params) => {
    if (!params.url) {
      throw { code: 'INVALID_PARAMS', message: 'Missing url parameter' };
    }
    const page = await getOrInitPage();
    const timeout = params.timeout_ms || 30000;
    const waitUntil = params.wait_until || 'domcontentloaded';

    logDebug(`Navigating to ${params.url} (timeout=${timeout}ms, waitUntil=${waitUntil})`);
    const resp = await page.goto(params.url, { timeout, waitUntil });

    return {
      url: page.url(),
      title: await page.title(),
      status: resp ? resp.status() : 200
    };
  },

  content: async () => {
    const page = await getOrInitPage();
    return {
      title: await page.title(),
      url: page.url()
    };
  },

  screenshot: async (params) => {
    const page = await getOrInitPage();
    const filePath = params.path || null;
    const options = {};
    if (filePath) {
      options.path = filePath;
    } else {
      options.encoding = params.encoding || 'base64';
    }
    const data = await page.screenshot(options);
    return {
      path: filePath,
      data: filePath ? null : data
    };
  },

  close: async () => {
    logDebug('Close command received. Shutting down browser...');
    if (browser) {
      try {
        await browser.close();
      } catch (e) {
        logDebug(`Error during browser.close: ${e.message}`);
      }
      browser = null;
      activePage = null;
    }
    return { status: 'closed' };
  },

  // Internal debug-only evaluate
  '_debug.evaluate': async (params) => {
    if (!params.expression) {
      throw { code: 'INVALID_PARAMS', message: 'Missing expression parameter' };
    }
    const page = await getOrInitPage();
    const result = await page.evaluate(async (expr) => {
      const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
      const code = expr.trim();
      const fn = new AsyncFunction(code.includes('return ') ? code : 'return (' + code + ')');
      return await fn();
    }, params.expression);
    return { result };
  },

  // C04: Allowlisted listcollection fetch
  'douyin.collection.fetch_page': async (params) => {
    const page = await getOrInitPage();
    const cursor = params.cursor !== undefined ? String(params.cursor) : '0';
    const count = params.count !== undefined ? parseInt(params.count, 10) : 10;
    const timeoutMs = params.timeout_ms || 20000;

    // 1. Ensure current page is on douyin user/self context
    const currentUrl = page.url();
    if (!currentUrl || !currentUrl.includes('douyin.com')) {
      logDebug(`Page not on douyin.com ('${currentUrl}'). Navigating to user/self...`);
      await page.goto('https://www.douyin.com/user/self', {
        waitUntil: 'networkidle2',
        timeout: 45000
      }).catch(e => logDebug(`Navigation notice: ${e.message}`));
      await new Promise(r => setTimeout(r, 2000));
    }

    // 2. Check for active Challenge or Captcha intermediate state
    const readiness = await page.evaluate(() => {
      const url = window.location.href;
      const title = document.title || '';
      const hasCaptcha = Boolean(
        document.querySelector('.captcha_verify_container') ||
        document.querySelector('#captcha_container') ||
        document.querySelector('iframe[src*="verifycenter/captcha"]') ||
        document.querySelector('iframe[src*="/captcha/"]') ||
        document.querySelector('iframe[src*="captcha_verify"]') ||
        url.includes('verify.snssdk.com') ||
        (title && (title.includes('安全验证') || title.includes('验证码') || title.includes('验证中间页')))
      );

      // Attempt to click 收藏 tab if visible and not already active
      const tabs = Array.from(document.querySelectorAll('[class*="semi-tabs-tab"]'));
      const collectTab = tabs.find(x => x.textContent.trim() === '收藏');
      if (collectTab && !collectTab.className.includes('active')) {
        try { collectTab.click(); } catch(e) {}
      }

      return {
        url,
        title,
        has_captcha: hasCaptcha,
        has_collect_tab: Boolean(collectTab)
      };
    });

    if (readiness.has_captcha) {
      logDebug(`Active captcha challenge detected on page ('${readiness.title}'). Returning AUTH_CHALLENGE_ACTIVE.`);
      return {
        http_status: 403,
        platform_status: null,
        raw_response: null,
        parse_error: 'AUTH_CHALLENGE_ACTIVE: ByteDance security verification challenge active on page',
        latency_ms: 0,
        page_url: readiness.url,
        challenge_active: true
      };
    }

    // 2. Perform in-page fetch using Douyin frontend context
    const fetchRes = await page.evaluate(async (reqCursor, reqCount, reqTimeout) => {
      const API_URL = 'https://www.douyin.com/aweme/v1/web/aweme/listcollection/';
      const COMMON_QUERY = 'device_platform=webapp&aid=6383&channel=channel_pc_web&pc_client_type=1&version_code=170400&version_name=17.4.0&cookie_enabled=true&screen_width=1920&screen_height=1080&browser_language=zh-CN&browser_platform=Win32&browser_name=Mozilla&browser_version=5.0%20(Windows%20NT%2010.0%3B%20Win64%3B%20x64)%20AppleWebKit%2F537.36%20(KHTML%2C%20like%20Gecko)%20Chrome%2F124.0.0.0%20Safari%2F537.36&browser_online=true&engine_name=Blink&engine_version=124.0.0.0&os_name=Windows&os_version=10&cpu_core_num=8&device_memory=8&platform=webapp';
      const fullUrl = `${API_URL}?${COMMON_QUERY}`;

      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), reqTimeout);
      const startTime = Date.now();

      try {
        const resp = await fetch(fullUrl, {
          method: 'POST',
          headers: {
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'accept': 'application/json, text/plain, */*',
            'referer': 'https://www.douyin.com/user/self?showTab=favorite_collection'
          },
          body: `cursor=${reqCursor}&count=${reqCount}`,
          credentials: 'include',
          signal: controller.signal
        });
        clearTimeout(timer);
        const latencyMs = Date.now() - startTime;
        const httpStatus = resp.status;

        let jsonBody = null;
        try {
          jsonBody = await resp.json();
        } catch (parseErr) {
          return {
            http_status: httpStatus,
            platform_status: null,
            raw_response: null,
            parse_error: String(parseErr),
            latency_ms: latencyMs,
            page_url: window.location.href
          };
        }

        return {
          http_status: httpStatus,
          platform_status: jsonBody ? jsonBody.status_code : null,
          raw_response: jsonBody,
          latency_ms: latencyMs,
          page_url: window.location.href,
          cursor_returned: jsonBody ? String(jsonBody.cursor) : null,
          has_more: jsonBody ? jsonBody.has_more : null,
          items_count: (jsonBody && Array.isArray(jsonBody.aweme_list)) ? jsonBody.aweme_list.length : 0
        };
      } catch (e) {
        clearTimeout(timer);
        return {
          http_status: null,
          platform_status: null,
          raw_response: null,
          fetch_error: e.name === 'AbortError' ? 'TIMEOUT' : String(e),
          latency_ms: Date.now() - startTime,
          page_url: window.location.href
        };
      }
    }, cursor, count, timeoutMs);

    return fetchRes;
  },

  // C03: Allowlisted auth signals inspection
  'douyin.auth.page_signals': async () => {
    const page = await getOrInitPage();
    const signals = await page.evaluate(() => {
      const url = window.location.href;
      const title = document.title;
      const isLoginModal = Boolean(
        document.querySelector('.login-mask') ||
        document.querySelector('[data-e2e="login-panel"]') ||
        document.getElementById('login-panel')
      );
      const avatarEl = document.querySelector('[data-e2e="user-avatar"]') ||
                       document.querySelector('.avatar-component img') ||
                       document.querySelector('.avatar-component-avatar-container') ||
                       document.querySelector('.semi-avatar img') ||
                       document.querySelector('img[src*="avatar"]');
      // Check visible captcha container
      const captchaEl = document.querySelector('.captcha_verify_container') ||
                        document.querySelector('#captcha_container') ||
                        document.querySelector('[class*="captcha_verify_container"]') ||
                        document.querySelector('.secsdk-captcha-drag-wrapper');
      let isCaptchaElVisible = false;
      if (captchaEl) {
        const style = window.getComputedStyle(captchaEl);
        isCaptchaElVisible = style.display !== 'none' && style.visibility !== 'hidden' && (captchaEl.offsetWidth > 0 || captchaEl.offsetHeight > 0);
      }

      // Check challenge iframe (excluding invisible nocaptcha)
      const challengeIframe = document.querySelector('iframe[src*="verifycenter/captcha"]') ||
                             document.querySelector('iframe[src*="/captcha/"]') ||
                             document.querySelector('iframe[src*="captcha_verify"]');
      let isIframeVisible = false;
      if (challengeIframe) {
        const style = window.getComputedStyle(challengeIframe);
        isIframeVisible = style.display !== 'none' && style.visibility !== 'hidden';
      }

      const hasCaptcha = Boolean(
        isCaptchaElVisible ||
        isIframeVisible ||
        url.includes('verify.snssdk.com') ||
        (title && (title.includes('安全验证') || title.includes('验证码') || title.includes('验证中间页')))
      );

      const hasIdentity = Boolean(
        avatarEl ||
        document.querySelector('[data-e2e="user-info"]') ||
        document.querySelector('[class*="user-info"]')
      );

      const isUserPage = Boolean(
        document.querySelector('[class*="semi-tabs"]') ||
        url.includes('/user/self')
      );

      const isRedirected = Boolean(
        url.includes('login') ||
        url.includes('passport') ||
        (!url.includes('douyin.com/user/self') && !url.includes('douyin.com'))
      );

      // Extract stable account identifier (sec_uid / user_unique_id)
      let accountIdentifier = null;
      const userLink = document.querySelector('a[href*="/user/MS4wLjABAAAA"]');
      if (userLink) {
        const m = (userLink.href || '').match(/\/user\/(MS4wLjABAAAA[a-zA-Z0-9_-]+)/);
        if (m) accountIdentifier = m[1];
      }
      if (!accountIdentifier) {
        const renderEl = document.getElementById('RENDER_DATA');
        if (renderEl) {
          try {
            const d = JSON.parse(decodeURIComponent(renderEl.innerText));
            accountIdentifier = d?.app?.user?.secUid || d?.app?.user?.uid || d?.app?.odin?.user_unique_id || null;
          } catch (e) {}
        }
      }
      if (!accountIdentifier) {
        const urlMatch = url.match(/\/user\/(MS4wLjABAAAA[a-zA-Z0-9_-]+)/);
        if (urlMatch) accountIdentifier = urlMatch[1];
      }

      return {
        url,
        title,
        is_login_modal_present: isLoginModal,
        login_modal_visible: isLoginModal,
        has_avatar: Boolean(avatarEl),
        has_captcha: hasCaptcha,
        challenge_visible: hasCaptcha,
        expected_user_identity_present: hasIdentity,
        normal_user_page_present: isUserPage,
        is_redirected: isRedirected,
        account_identifier: accountIdentifier
      };
    });
    return signals;
  },

  // D02: Allowlisted credential snapshot
  'douyin.credentials.snapshot': async (params) => {
    const page = await getOrInitPage();
    const urls = params.urls || ['https://www.douyin.com', 'https://douyin.com', 'https://live.douyin.com'];
    const rawCookies = await page.cookies(...urls);
    const ALLOWED_ROOTS = ['douyin.com', 'iesdouyin.com', 'live.douyin.com'];
    const isAllowedDomain = (domain) => {
      if (!domain) return false;
      const clean = String(domain).trim().toLowerCase().replace(/^\.+/, '');
      return ALLOWED_ROOTS.some(root => clean === root || clean.endsWith('.' + root));
    };
    const allowedCookies = rawCookies.filter(c => isAllowedDomain(c.domain));

    let accountIdentifier = null;
    try {
      const signals = await methods['douyin.auth.page_signals']();
      accountIdentifier = signals.account_identifier || null;
    } catch (e) {
      logDebug(`Could not extract page signals during credential snapshot: ${e.message}`);
    }

    logDebug(`Credential snapshot captured: count=${allowedCookies.length}, has_account_id=${Boolean(accountIdentifier)}`);

    return {
      cookies: allowedCookies.map(c => ({
        name: c.name,
        value: c.value,
        domain: c.domain,
        path: c.path || '/',
        expires: c.expires || null,
        http_only: Boolean(c.httpOnly),
        secure: Boolean(c.secure),
        same_site: c.sameSite || null
      })),
      account_identifier: accountIdentifier
    };
  }
};

// Aliases mapping for namespaced RPC and backward compatibility
const METHOD_MAP = {
  'ping': methods.ping,
  'runtime.ping': methods.ping,
  'launch': methods.launch,
  'runtime.launch': methods.launch,
  'health': methods.health,
  'runtime.health': methods.health,
  'info': methods.info,
  'runtime.info': methods.info,
  'close': methods.close,
  'runtime.close': methods.close,
  'navigate': methods.navigate,
  'page.navigate': methods.navigate,
  'content': methods.content,
  'page.info': methods.content,
  'screenshot': methods.screenshot,
  'page.screenshot': methods.screenshot,
  '_debug.evaluate': methods['_debug.evaluate'],
  'evaluate': methods['_debug.evaluate'],
  'douyin.collection.fetch_page': methods['douyin.collection.fetch_page'],
  'douyin.auth.page_signals': methods['douyin.auth.page_signals'],
  'douyin.credentials.snapshot': methods['douyin.credentials.snapshot'],
};

// Allowlist of allowed methods
const ALLOWED_METHODS = new Set(Object.keys(METHOD_MAP));

async function handleCommand(line) {
  let req;
  try {
    req = JSON.parse(line);
  } catch (err) {
    sendResponse('unknown', false, null, {
      code: 'PROTOCOL_ERROR',
      message: `Invalid JSON message: ${err.message}`
    });
    return;
  }

  const id = req.id || req.request_id || 'unknown';
  const method = req.method || req.action;

  if (!method || !ALLOWED_METHODS.has(method)) {
    sendResponse(id, false, null, {
      code: 'PROTOCOL_METHOD_NOT_ALLOWED',
      message: `Method '${method}' is not permitted by sidecar allowlist.`
    });
    return;
  }

  const handler = METHOD_MAP[method];
  try {
    const result = await handler(req.params || {});
    sendResponse(id, true, result);
    if (method === 'close' || method === 'runtime.close') {
      logDebug('Exiting process cleanly after close action');
      setTimeout(() => process.exit(0), 50);
    }
  } catch (err) {
    logDebug(`Method ${method} failed: ${err.message || err}`);
    sendResponse(id, false, null, {
      code: err.code || 'ACTION_FAILED',
      message: err.message || String(err),
      details: err.details || null
    });
  }
}

async function cleanExit(signal) {
  if (isShuttingDown) return;
  isShuttingDown = true;
  logDebug(`Sidecar exiting due to signal/event: ${signal}`);
  if (browser) {
    try {
      await browser.close();
    } catch (e) {
      // ignore
    }
  }
  process.exit(0);
}

// Lifecycle and stream event hooks
process.stdin.setEncoding('utf8');
const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
  terminal: false
});

rl.on('line', (line) => {
  const trimmed = line.trim();
  if (trimmed) {
    handleCommand(trimmed);
  }
});

rl.on('close', () => cleanExit('stdin_close'));
process.on('SIGINT', () => cleanExit('SIGINT'));
process.on('SIGTERM', () => cleanExit('SIGTERM'));

process.on('unhandledRejection', (reason, promise) => {
  logDebug(`Unhandled Rejection at: ${promise}, reason: ${reason}`);
});

process.on('uncaughtException', (err) => {
  logDebug(`Uncaught Exception: ${err.stack || err}`);
  cleanExit('uncaughtException');
});

logDebug('Persistent Browser Runtime Sidecar initialized and awaiting allowlisted commands on stdin.');
