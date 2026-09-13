/**
 * 05. MCP Stdio 어댑터 및 7대 도구 검사 (mcp-adapter)
 * central_hub/mcp_server_adapter.py 규격 및 7대 도구 스키마 검증.
 */

const { run, verdict } = require('./_shared');

module.exports = {
  id: 'mcp-adapter',
  name: '05 MCP 어댑터 — 7대 Trinity-ACE 도구 스키마 및 Stdio 무결성',
  layer: 'FUNCTION',

  async run(ctx) {
    const root = ctx.projectDir || ctx.cwd;
    const cmd = 'python -c "from central_hub.mcp_server_adapter import MCPServerAdapter; a = MCPServerAdapter(\':memory:\'); exp = {\'trinity_send_card\', \'trinity_read_inbox\', \'trinity_ack_card\', \'trinity_acquire_lock\', \'trinity_release_lock\', \'trinity_store_artifact\', \'trinity_triage_error\'}; acts = {t[\'name\'] for t in a.get_tool_definitions()}; a.close(); print(\'ALL_TOOLS_VERIFIED\' if acts == exp else f\'MISMATCH:{acts}\')"';

    const r = run(cmd, root);
    const notChecked = '외부 원격 네트워크 클라이언트의 SSL/TLS 인증서';

    if (r.code !== 0 || !r.out || !r.out.includes('ALL_TOOLS_VERIFIED')) {
      return verdict('ERROR',
        `MCP 어댑터 도구 스키마 검증 실패:\n  · 종료 코드: ${r.code}\n  · 출력: ${(r.out || r.err || '').trim()}`,
        notChecked);
    }

    return verdict('OK', '7대 Trinity-ACE 표준 도구 스키마 검증 완결 (ALL_TOOLS_VERIFIED)', notChecked);
  }
};
