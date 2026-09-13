/**
 * 01. 회귀 검사 (regression)
 * unittest 전수 검사 (tests/ 디렉터리 내 전체 단위/동시성 테스트)
 */

const { run, verdict } = require('./_shared');

module.exports = {
  id: 'regression',
  name: '01 회귀 검사 — 전체 25개 단위/통합 테스트 무결성',
  layer: 'SYSTEM',

  async run(ctx) {
    const root = ctx.projectDir || ctx.cwd;
    const r = run('python -m unittest discover -s tests -q 2>&1', root);

    const ran = /Ran\s+(\d+)\s+tests?/.exec(r.out);
    const failures = /failures=(\d+)/.exec(r.out);
    const errors = /errors=(\d+)/.exec(r.out);
    const skipped = /skipped=(\d+)/.exec(r.out);
    const total = ran ? parseInt(ran[1], 10) : 0;
    const nFailed = failures ? parseInt(failures[1], 10) : 0;
    const nErrors = errors ? parseInt(errors[1], 10) : 0;
    const nSkipped = skipped ? parseInt(skipped[1], 10) : 0;
    const nPassed = Math.max(0, total - nFailed - nErrors - nSkipped);

    const notChecked = '외부 라이브 LLM API 실시간 과금/쿼터 호출';

    if (!ran || !/\b(?:OK|FAILED)\b/.test(r.out)) {
      return verdict('ERROR',
        `테스트 결과를 읽지 못했습니다.\n` +
        `  · 종료 코드: ${r.code}\n` +
        `  · 출력 끝부분: ${(r.out || '(없음)').trim().slice(-200)}`,
        notChecked);
    }

    if (r.code !== 0 || nFailed > 0 || nErrors > 0 || /\bFAILED\b/.test(r.out)) {
      return verdict('ERROR',
        `테스트 실패 ${nFailed}개, 오류 ${nErrors}개입니다 (통과 ${nPassed}개).\n` +
        `  · 출력 끝부분: ${r.out.trim().slice(-500)}`,
        notChecked);
    }

    if (total === 0) {
      return verdict('ERROR',
        '테스트가 0개 수집됐습니다. 검사기가 테스트를 찾지 못했습니다.',
        notChecked);
    }

    return verdict('OK', `전체 ${total}개 테스트 중 ${nPassed}개 통과 (종료 코드 0)`, notChecked);
  }
};
