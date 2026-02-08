---
title: "Claude Code와 Chrome 확장 프로그램 연결 안 될 때 (Windows)"
date: 2026-01-29 12:00:00 +0900
categories: [CodeLog]
tags: [claude-code, chrome, windows, 트러블슈팅]
author: dr_softkorea
description: "Claude Code에서 Chrome 확장 프로그램 연결이 안 되는 Windows 버그 해결기. Native Messaging Host 충돌 원인과 v2.1.19 다운그레이드 해결법, 트러블슈팅 체크리스트를 정리했다."
---

Claude Code에서 Chrome 확장 프로그램 연결이 안 됐다.

```
Browser extension is not connected
```

Claude Desktop은 잘 되는데 Claude Code만 안 되길래 한참 삽질했다.

## 원인

두 가지 문제가 겹쳐 있었다.

1. **Native Messaging Host 충돌**: Claude Desktop과 Claude Code가 같은 이름의 Host를 사용해서 서로 덮어쓴다.
2. **Claude Code v2.1.20 이후 Windows 버그**: GitHub 이슈에 올라와 있는 알려진 버그다.

## 해결

결론부터 말하면 **다운그레이드**로 해결했다.

```bash
claude install 2.1.19
```

v2.1.19에서는 정상 작동한다.

## 삽질 기록

나중에 또 헤맬까봐 기록해둔다.

### Native Messaging Host 파일 위치

| 항목 | 경로 |
|------|------|
| Claude Desktop | `%APPDATA%\Claude\ChromeNativeHost\com.anthropic.claude_browser_extension.json` |
| Claude Code | `%APPDATA%\Claude Code\ChromeNativeHost\com.anthropic.claude_code_browser_extension.json` |

### Claude Desktop 사용할 때 (원본)

```json
{
  "name": "com.anthropic.claude_browser_extension",
  "path": "C:\\Users\\{username}\\AppData\\Local\\AnthropicClaude\\app-1.1.1200\\resources\\chrome-native-host.exe",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://dihbgbndebgnbjfmelmegjepbnkhlgni/",
    "chrome-extension://fcoeoabgfenejglbffodgkkbkcdhcgfn/",
    "chrome-extension://dngcpimnedloihjnnfngkgjoidhnaolf/"
  ]
}
```

### Claude Code 사용할 때 (수정)

```json
{
  "name": "com.anthropic.claude_browser_extension",
  "path": "C:\\Users\\{username}\\.claude\\chrome\\chrome-native-host.bat",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://dihbgbndebgnbjfmelmegjepbnkhlgni/",
    "chrome-extension://fcoeoabgfenejglbffodgkkbkcdhcgfn/",
    "chrome-extension://dngcpimnedloihjnnfngkgjoidhnaolf/"
  ]
}
```

`path`만 바꿔주면 된다. 근데 이렇게 해도 v2.1.20 이후 버전에서는 안 됐다.

### 버전 관리 명령어

```bash
# 현재 버전 확인
claude --version

# 특정 버전 설치
claude install 2.1.19

# 최신 버전으로 업그레이드
claude install latest
```

## 트러블슈팅 체크리스트

안 되면 순서대로 해본다.

1. Chrome 완전 종료: `taskkill /F /IM chrome.exe`
2. 확장 프로그램 재로드: `chrome://extensions` → Claude 새로고침 버튼
3. Claude Code 재시작
4. 사이드패널 열기: `Ctrl+Shift+.`
5. MCP 재연결: Claude Code에서 `/mcp` 명령어

## 참고 링크

- [GitHub Issue #20887](https://github.com/anthropics/claude-code/issues/20887)
- [GitHub Issue #21300](https://github.com/anthropics/claude-code/issues/21300)
- [GitHub Issue #20341](https://github.com/anthropics/claude-code/issues/20341)

버그 픽스 기다리는 중. 일단 v2.1.19로 버틴다.
