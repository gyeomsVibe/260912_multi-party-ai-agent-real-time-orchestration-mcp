/**
 * 04. Git 무결성 및 공백 검사 (git diff --check)
 * 파일 후행 공백(Trailing Whitespace) 및 캐리지 리턴 무결성 검증.
 */

const { run, verdict } = require('./_shared');

module.exports = {
  id: 'git-diff-clean',
  name: '04 포맷 무결성 — git diff --check 공백 오류 검증',
  layer: 'SYSTEM',

  async run(ctx) {
    const root = ctx.projectDir || ctx.cwd;
    const r = run('git diff --check', root);
    const notChecked = 'Git LFS 대용량 바이너리 포맷';

    if (r.code !== 0 || (r.out && r.out.trim().length > 0)) {
      return verdict('ERROR',
        `공백 또는 포맷 오류가 발견되었습니다:\n` +
        `  · 출력: ${r.out.trim().slice(0, 300)}`,
        notChecked);
    }

    return verdict('OK', 'git diff --check 공백/줄바꿈 위반 0건 (Clean)', notChecked);
  }
};
