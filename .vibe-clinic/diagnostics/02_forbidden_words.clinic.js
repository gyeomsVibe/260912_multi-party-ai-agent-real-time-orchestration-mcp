/**
 * 02. 영구 금지어 감사 (forbidden word audit)
 * 구시대적 레거시 명칭이 코드/문서 전 영역에서 0건인지 감사.
 */

const { run, verdict } = require('./_shared');

module.exports = {
  id: 'forbidden-words',
  name: '02 금지어 감사 — 레거시 용어 0건 검증',
  layer: 'SYSTEM',

  async run(ctx) {
    const root = ctx.projectDir || ctx.cwd;
    // Query for the 3-letter forbidden legacy token case-insensitively
    const cmd = `python -c "
import os, re, sys
sys.stdout.reconfigure(encoding='utf-8')
root = '.'
bad_pattern = re.compile(r'\\b' + 'c' + '3' + 'p' + '\\b', re.IGNORECASE)
violations = []
ignore_dirs = {'.git', 'node_modules', '.vibe-clinic', '__pycache__', 'scratch'}

for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames if d not in ignore_dirs]
    for f in filenames:
        if f.endswith(('.py', '.md', '.json', '.txt', '.html', '.js')):
            fpath = os.path.join(dirpath, f)
            try:
                with open(fpath, 'r', encoding='utf-8', errors='ignore') as fp:
                    for lno, line in enumerate(fp, 1):
                        if bad_pattern.search(line):
                            violations.append(f'{fpath}:{lno}: {line.strip()}')
            except Exception as e:
                pass

if violations:
    print('VIOLATIONS_FOUND:' + str(len(violations)))
    for v in violations[:5]:
        print('  ' + v)
    sys.exit(1)
else:
    print('ZERO_VIOLATIONS')
    sys.exit(0)
"`;

    const r = run(cmd, root);
    const notChecked = '바이너리 파일(이미지, PDF) 내부 바이트';

    if (r.code !== 0 || r.out.includes('VIOLATIONS_FOUND')) {
      return verdict('ERROR',
        `금지어 위반이 발견되었습니다:\n` +
        `  · 출력: ${r.out.trim()}`,
        notChecked);
    }

    return verdict('OK', '코드 및 문서 전역 금지어 0건 확인 완료 (Clean)', notChecked);
  }
};
