# ERR_001 — Windows Node execSync Multiline Command Syntax Error

## Summary
When running `child_process.execSync` on Windows, multi-line commands passed into `python -c "..."` cause `cmd.exe` to fail with syntax errors or truncate lines after the first newline.

## Symptoms
- Diagnostic node returns `ERROR` with empty or truncated output.
- `r.code !== 0` but running the same Python logic directly in PowerShell passes.
- Output contains `SyntaxError` or no output at all.

## Root Cause
Windows `child_process.execSync` uses `cmd.exe /d /s /c "..."` under the hood. Unlike Bash or PowerShell, `cmd.exe` does not preserve newlines inside double quotes in arguments, causing string truncation.

## Solution
1. Compress inline Python commands into a clean single-line semicolon-separated string:
   `python -c "from foo import bar; a = bar(); print('OK' if a.check() else 'ERR')"`
2. Escape internal single quotes appropriately or write a temporary script file.

## Prevention
- Never write multiline string literals in `execSync` command strings targeted for Windows execution.
- Always use single-line Python invocations or dedicated test scripts in `.clinic.js` diagnostics.

## Related
- Related diagnostic: `05_mcp_adapter.clinic.js`
- Layer: `FUNCTION`
