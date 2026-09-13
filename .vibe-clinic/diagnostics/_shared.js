/**
 * 진단 공통 도구 (_shared.js) - Trinity-ACE Protocol Vibe Clinic
 */

const { execSync } = require('child_process');

function run(cmd, cwd) {
  try {
    const out = execSync(cmd, {
      cwd,
      encoding: 'utf8',
      timeout: 180000,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
    });
    return { ok: true, code: 0, out: out || '' };
  } catch (e) {
    return {
      ok: false,
      code: typeof e.status === 'number' ? e.status : -1,
      out: (e.stdout || '') + (e.stderr || ''),
      err: e.message,
    };
  }
}

function verdict(status, summary, notChecked) {
  const limit = notChecked ? `\n  · 검사하지 않은 것: ${notChecked}` : '';
  return { status, details: `${summary}${limit}` };
}

module.exports = { run, verdict };
