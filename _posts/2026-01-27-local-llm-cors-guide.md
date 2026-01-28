---
title: "외부 웹사이트에서 로컬 Ollama 연동하기"
date: 2026-01-27 14:00:00 +0900
categories: [CodeLog]
tags: [ollama, qwen3, cors, local-llm, javascript]
author: dr_softkorea
---

## 왜 로컬 LLM인가?

의료 데이터 요약처럼 민감한 정보를 다룰 때, 외부 API로 데이터를 보내기 꺼려진다.
로컬 LLM을 쓰면 **데이터가 내 PC를 벗어나지 않는다**.

최근 8B급 모델 성능이 많이 좋아졌다. 직접 테스트해보니 **Qwen3 8B (4-bit 양자화)**로도 한글 요약이 충분히 쓸만하다. RTX 2080 8GB에서 문제없이 돌아간다.

## 아키텍처

```
┌─────────────────────────────────────────────────┐
│  브라우저                                        │
│  ┌───────────────┐       ┌───────────────────┐  │
│  │ 웹 앱 (JS)    │──────▶│ localhost:11434   │  │
│  │ (외부 HTTPS)  │◀──────│ (Ollama)          │  │
│  └───────────────┘       └───────────────────┘  │
└─────────────────────────────────────────────────┘
         ↑                          ↑
    외부 서버 없음           데이터가 PC 밖으로 안 나감
```

**핵심**: 브라우저 ↔ 로컬 Ollama 직접 통신. 서버 경유 없음.

## 설정 방법

### 1. Ollama 설치 & 모델 다운로드

```bash
# Ollama 설치 후
ollama pull qwen3:8b
```

### 2. CORS 허용 (필수)

외부 웹사이트에서 localhost를 호출하려면 CORS 설정이 필요하다.

**Windows (PowerShell 관리자 권한)**
```powershell
[System.Environment]::SetEnvironmentVariable('OLLAMA_ORIGINS', '*', 'User')
# Ollama 재시작 필요
```

**macOS / Linux**
```bash
export OLLAMA_ORIGINS="*"
# ~/.zshrc 또는 ~/.bashrc에 추가 후 재시작
```

**원클릭 설정 배치 파일** (사용자 배포용)

```batch
@echo off
chcp 65001 >nul
echo Ollama CORS 설정 중...

:: 1. 영구 설정 (재부팅/새 터미널용)
setx OLLAMA_ORIGINS "*"

:: 2. 현재 세션 임시 설정 (즉시 적용 필수)
set "OLLAMA_ORIGINS=*"

taskkill /f /im ollama.exe 2>nul
timeout /t 2 >nul
start "" "%LOCALAPPDATA%\Programs\Ollama\Ollama.exe"
echo 완료! 브라우저를 새로고침하세요.
pause
```

## 브라우저 보안 정책

HTTPS 사이트에서 `http://localhost`를 호출할 때 발생하는 이슈들:

| 정책 | 설명 | 해결 |
|------|------|------|
| **CORS** | 다른 origin 간 요청 차단 | `OLLAMA_ORIGINS` 환경 변수 |
| **Mixed Content** | HTTPS → HTTP 차단 | localhost는 예외 처리됨 (Chrome/Edge) |
| **Private Network Access** | 공용 → 사설 네트워크 차단 | Ollama 0.5.x+에서 지원 |

> Safari는 Mixed Content 정책이 엄격해서 안 될 수 있음. Chrome/Edge 권장.

## 구현 핵심 코드

### 연결 상태 확인

```javascript
async function checkOllama() {
  try {
    const res = await fetch('http://localhost:11434/api/tags', {
      signal: AbortSignal.timeout(3000)
    });
    const data = await res.json();
    return {
      ok: true,
      models: data.models?.map(m => m.name) || []
    };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}
```

### 스트리밍 요약 요청

```javascript
async function summarize(text, onChunk) {
  const res = await fetch('http://localhost:11434/api/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model: 'qwen3:8b',
      prompt: `다음 텍스트를 한국어로 간결하게 요약하세요:\n\n${text}`,
      stream: true,
      options: { temperature: 0.3 }
    })
  });

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let result = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    const lines = decoder.decode(value).split('\n').filter(Boolean);
    for (const line of lines) {
      const json = JSON.parse(line);
      if (json.response) {
        result += json.response;
        onChunk?.(json.response, result);
      }
    }
  }
  return result;
}
```

## 라이브 데모

아래 데모 페이지에서 직접 테스트해볼 수 있다:

👉 [Live Demo](/local-llm-demo.html)

데모 페이지는 GitHub Pages에 올려서 실제 HTTPS → localhost 연동을 시연한다.

## 프론트엔드 개발자 전달용 체크리스트

- [ ] `http://localhost:11434/api/tags` 호출로 연결 상태 확인 UI
- [ ] 모델 미설치 시 `ollama pull qwen3:8b` 안내
- [ ] CORS 에러 시 환경 변수 설정 가이드 표시
- [ ] 스트리밍 응답으로 실시간 출력 (UX 개선)
- [ ] Safari 사용자에게 Chrome/Edge 권장 안내

## 테스트 후 CORS 비활성화 (보안)

외부 웹사이트에서 로컬 Ollama에 접근할 수 있다는 건, 악성 사이트도 마찬가지라는 뜻이다.
테스트가 끝나면 CORS 설정을 꺼두는 게 안전하다.

**Windows (PowerShell 관리자 권한)**
```powershell
# 환경 변수 삭제
[System.Environment]::SetEnvironmentVariable('OLLAMA_ORIGINS', $null, 'User')

# Ollama 재시작
taskkill /f /im ollama.exe
Start-Process "$env:LOCALAPPDATA\Programs\Ollama\Ollama.exe"
```

**macOS / Linux**
```bash
# ~/.zshrc 또는 ~/.bashrc에서 해당 줄 삭제
# export OLLAMA_ORIGINS="*"  ← 이 줄 삭제 또는 주석 처리

# 적용
source ~/.zshrc

# Ollama 재시작
pkill ollama && ollama serve &
```

**원클릭 비활성화 배치 파일** (Windows)

```batch
@echo off
chcp 65001 >nul
echo Ollama CORS 설정 해제 중...
reg delete "HKCU\Environment" /v OLLAMA_ORIGINS /f 2>nul
taskkill /f /im ollama.exe 2>nul
timeout /t 2 >nul
start "" "%LOCALAPPDATA%\Programs\Ollama\Ollama.exe"
echo 완료! CORS가 비활성화되었습니다.
pause
```

> **팁**: 자주 사용한다면 특정 도메인만 허용하는 게 낫다.
> `OLLAMA_ORIGINS="https://mindchart.com"` 처럼 신뢰할 수 있는 사이트만 지정.

## Qwen3 Thinking 모드 주의사항

Qwen3는 기본적으로 **thinking 모드**가 활성화되어 있다. 응답 전에 `<think>...</think>` 블록에서 내부 추론을 수행한다:

```
<think>
사용자가 요약을 요청했다. 핵심 내용을 추출해야 한다.
1번 증상은 수면장애이고...
2번은 불안 증상이며...
</think>

실제 요약: 환자는...
```

### 문제점
- thinking에서 토큰을 많이 사용 → `num_predict`가 작으면 실제 요약 전에 끊김
- `<think>` 태그가 출력에 노출될 수 있음

### 해결 방법

**thinking 모드는 요약 품질을 높여주므로 유지하되, 토큰을 충분히 확보한다.**

```javascript
options: {
  num_ctx: 32768,     // 컨텍스트 윈도우 32K
  num_predict: 8192   // 출력 토큰 (thinking + 실제 응답 포함)
}
```

| VRAM | 권장 num_ctx | 비고 |
|------|-------------|------|
| 8GB | 32768 (32K) | RTX 2080, 3060, 4060 |
| 12GB | 65536 (64K) | RTX 3060 12GB, 4070 |
| 16GB+ | 131072 (128K) | RTX 4080, 4090 |

> Qwen3 8B 4-bit 양자화 기준. 모델 크기나 양자화 방식에 따라 달라질 수 있음.

**`<think>` 블록 필터링 (UI 노출 방지)**
```javascript
let isInsideThinkBlock = false;

// 응답 처리 시
if (response.includes('<think>')) {
  isInsideThinkBlock = true;
}
if (response.includes('</think>')) {
  isInsideThinkBlock = false;
}
if (isInsideThinkBlock) continue; // 출력 스킵
```

> **참고**: `/no_think` 옵션으로 thinking을 끌 수 있지만,
> 테스트 결과 thinking을 켠 상태가 요약 품질이 더 좋았음.

## 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| `ERR_CONNECTION_REFUSED` | Ollama 미실행 | Ollama 앱 실행 |
| `CORS error` | OLLAMA_ORIGINS 미설정 | 환경 변수 설정 후 재시작 |
| `404 Not Found` | 모델 미설치 | `ollama pull qwen3:8b` |
| Safari에서만 안 됨 | Mixed Content | Chrome/Edge 사용 |
| CORS 설정했는데도 차단 | Private Network Access | 아래 PNA 설정 참고 |
| 요약이 중간에 끊김 | 토큰 부족 (thinking 포함) | `num_predict: 4096` 이상 |
| `<think>` 태그 출력됨 | 필터링 누락 | 필터링 코드 추가 |

### 배포 환경(HTTPS)에서 연결 안 될 때 (Chrome 124+)

Chrome 124+ 버전은 보안 정책(PNA)으로 HTTPS → localhost 연결이 차단된다.

에러 메시지:
```
Permission was denied for this request to access the 'loopback' address space
```

**해결: Chrome 특수 모드로 실행**

1. Chrome 완전 종료 (시스템 트레이 포함)
2. 아래 명령어로 Chrome 실행:

```cmd
"C:\Program Files\Google\Chrome\Application\chrome.exe" --disable-features=BlockInsecurePrivateNetworkRequests,PrivateNetworkAccessSendPreflights --disable-web-security --user-data-dir="C:\temp\chrome-dev"
```

> 이 모드는 보안이 비활성화되므로, Ollama 테스트 용도로만 사용할 것.

**바로가기 만들기 (편의용)**
1. 바탕화면 우클릭 → 새로 만들기 → 바로가기
2. 위 명령어 전체를 붙여넣기
3. 이름: "Chrome (Ollama Dev)"

**함께 확인할 것**
- `OLLAMA_ORIGINS="*"` 환경 변수 설정
- Ollama 재시작 (환경 변수 변경 후 필수)

## 결론

- **Qwen3 8B**면 한글 요약에 충분 (RTX 2080 8GB OK)
- CORS 설정만 하면 외부 웹사이트에서 로컬 LLM 호출 가능
- 민감한 데이터를 외부로 보내지 않고 AI 기능 구현 가능
