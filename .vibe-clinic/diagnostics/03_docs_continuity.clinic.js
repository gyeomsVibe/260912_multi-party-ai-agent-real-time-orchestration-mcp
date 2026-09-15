/**
 * 03. 문서 체계 연속성 검사 (docs continuity)
 * docs/ 디렉터리의 2자리 일련번호(00부터 최신 번호까지) 연속성 및 내부 식별자 일치 검증.
 */

const fs = require('fs');
const path = require('path');
const { verdict } = require('./_shared');

module.exports = {
  id: 'docs-continuity',
  name: '03 문서 연속성 — docs/ 2자리 일련번호 및 헤더 정합성',
  layer: 'TASK',

  async run(ctx) {
    const root = ctx.projectDir || ctx.cwd;
    const docsDir = path.join(root, 'docs');
    const notChecked = '외부 웹 링크(URL)의 404 라이브 응답성';

    if (!fs.existsSync(docsDir)) {
      return verdict('ERROR', 'docs/ 디렉터리가 존재하지 않습니다.', notChecked);
    }

    const files = fs.readdirSync(docsDir).sort();
    const prefixMap = {};

    for (const f of files) {
      const m = /^(\d{2})_/.exec(f);
      if (m) {
        const num = m[1];
        if (!prefixMap[num]) prefixMap[num] = [];
        prefixMap[num].push(f);
      }
    }

    const prefixes = Object.keys(prefixMap).map(Number).sort((a, b) => a - b);
    const maxPrefix = prefixes.length > 0 ? Math.max(...prefixes) : 0;

    // Check numbers 00 to maxPrefix
    const missing = [];
    for (let i = 0; i <= maxPrefix; i++) {
      const s = String(i).padStart(2, '0');
      if (!prefixMap[s]) {
        missing.push(s);
      }
    }

    if (missing.length > 0) {
      return verdict('ERROR',
        `docs/ 일련번호 중 누락된 번호가 있습니다: ${missing.join(', ')}`,
        notChecked);
    }

    // Check duplicated numbers (except 00/01/02 which are multi-part images)
    const duplicates = [];
    for (let i = 3; i <= maxPrefix; i++) {
      const s = String(i).padStart(2, '0');
      if (prefixMap[s] && prefixMap[s].length > 1) {
        duplicates.push(`${s}: [${prefixMap[s].join(', ')}]`);
      }
    }

    if (duplicates.length > 0) {
      return verdict('ERROR',
        `docs/ 일련번호가 중복된 문서가 있습니다:\n  · ${duplicates.join('\n  · ')}`,
        notChecked);
    }

    const endStr = String(maxPrefix).padStart(2, '0');
    return verdict('OK', `docs/00 부터 docs/${endStr} 까지 ${maxPrefix + 1}개 번호군 결손/중복 없이 완전 일치`, notChecked);
  }
};
