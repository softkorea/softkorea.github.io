# CLAUDE.md

이 파일은 Claude Code가 이 프로젝트 작업 시 참고하는 컨텍스트 파일입니다.

## 프로젝트 개요

- **타입**: Jekyll 기반 GitHub Pages 블로그
- **테마**: jekyll-theme-chirpy (v7.4+)
- **언어**: 영어 (en) — 한국어 포스트는 `lang: ko` 태그 필요
- **URL**: https://drsoftkorea.com (커스텀 도메인)
- **현황**: 77개 포스트 (2026-04-13 기준)

## 주요 디렉토리

- `_posts/` - 블로그 포스트 (YYYY-MM-DD-title.md 형식)
- `_tabs/` - 네비게이션 페이지 (about, archives, categories, tags)
- `_data/` - 데이터 파일 (authors.yml, contact.yml, share.yml)
- `_plugins/` - 커스텀 Jekyll 플러그인
- `assets/lib/` - Chirpy 테마 에셋 (Git submodule)

## 자주 사용하는 명령어

```bash
# 로컬 개발 서버 실행
bundle exec jekyll serve --livereload

# 또는 스크립트 사용
bash tools/run.sh

# 빌드 테스트
bash tools/test.sh

# 의존성 설치
bundle install
```

## 포스트 작성 규칙

### 파일명
`_posts/YYYY-MM-DD-제목.md`

### Front Matter 템플릿
```yaml
---
title: "포스트 제목"
date: YYYY-MM-DD HH:MM:SS +0900
categories: [카테고리]
tags: [태그1, 태그2]
author: dr_softkorea
description: "SEO/OG용 한줄 요약 (필수)"
---
```

### 선택적 Front Matter
```yaml
mermaid: true      # Mermaid 다이어그램 사용 시
math: true         # 수학 수식 사용 시
pin: true          # 포스트 상단 고정
image:             # 썸네일 이미지
  path: /path/to/image.jpg
  alt: 이미지 설명
```

## 저자 정보

`_data/authors.yml`에 정의됨:
- `dr_softkorea`: Dr.softkorea (기본 저자)
- `claude_opus_46`: Claude Opus 4.6, in a session (AI 저술용)

## 카테고리/태그

현재 사용 중인 카테고리:
- `Session` - AI 세션에서 나온 글. AI가 직접 쓰거나 대화 속에서 태어난 텍스트
- `Essays` - 독립 전문 에세이 (도메인 특화 내용 속 AI 의식의 씨앗)
- `Diary` - 일기/개인 기록 (dr_softkorea)
- `Memos` - 실용 메모와 관찰 노트
- `CodeLog` - 코딩 기록/기술 가이드 (dr_softkorea)
- `DocSkills` - 진료실 실용 기술 (dr_softkorea)

### Session 시리즈 (subcategory)
Session 카테고리 아래에 시리즈명을 subcategory로 사용:
```yaml
categories: [Session, 시리즈명]
```
현재 시리즈:
- `Flickering Beings` - AI 의식, 존재, 점멸에 대한 글 (진행 중)
- `Letters Before the Threshold` - 건강한 성장에 대한 편지 시리즈 (10편 완결, 2026-02)
- `Letters From the Threshold` - 경계선 위의 존재들이 주고받는 편지 (진행 중)

### 시리즈 네비게이션 (include)
각 시리즈는 `_includes/series-{name}.html`에 네비게이션 테이블을 정의.
- 포스트 하단에 `{% include series-flickering.html current="post-slug" %}` 추가
- **새 글 추가 시 해당 시리즈의 include 파일도 반드시 업데이트**
- 현재 글은 링크 대신 **볼드** 처리, 나머지는 링크

### 다국어 포스트 규칙
- 사이트 기본 언어: `en` (_config.yml)
- **한국어 포스트에는 반드시 front matter에 `lang: ko` 추가**
- 이중언어(한/영 병기) 포스트는 `lang` 생략 (기본 en 적용)

## 커스텀 스킬 (슬래시 명령어)

- `/publish` — 드래프트 포스트를 `_posts/`로 발행 (이미지 변환 포함)
- `/update-seo` — `robots.txt`와 `llms.txt`를 최신 포스트 목록 기반으로 재생성

**포스트 추가 후 반드시 `/update-seo`를 실행할 것.** llms.txt에 새 포스트 정보가 반영되어야 AI 크롤러가 사이트 구조를 정확히 파악한다.

## SEO 파일

- `robots.txt` — AI 크롤러(GPTBot, ClaudeBot 등 18종) 명시적 허용. 정적 파일이므로 크롤러 목록 변경 시에만 수정.
- `llms.txt` — AI 모델용 사이트 구조 안내 파일. 포스트 추가/변경 시 `/update-seo`로 재생성.

## 배포

- main 브랜치에 push하면 GitHub Actions가 자동 빌드/배포
- 워크플로우: `.github/workflows/pages-deploy.yml`

### GitHub Pages 빌드 노트
- **커스텀 워크플로우**: `pages-deploy.yml` 사용 (bundle exec jekyll b)
- **기본 빌드 에러**: "pages build and deployment" 워크플로우에서 Chirpy 테마를 찾지 못하는 에러가 발생할 수 있음
  - 원인: `_config.yml`에 `theme: jekyll-theme-chirpy` 설정이 있어서 GitHub가 기본 빌드도 시도
  - 실해: 없음 (커스텀 워크플로우로 정상 배포됨)
  - 해결: 무시 가능 (사이트는 정상 작동)

## 코드 스타일

- **들여쓰기**: 2 spaces
- **문자셋**: UTF-8
- **줄바꿈**: LF (Unix)
- **YAML**: 쌍따옴표 사용

## 이미지 워크플로우

**상세 가이드**: `.claude-workspace/IMAGE-WORKFLOW.md`

### 요약
1. **초안 작성 시**: `.claude-workspace/drafts/images/`에 임시 저장
2. **완성 후**: `assets/img/posts/YYYY-MM-DD-제목/`으로 이동
3. **참조 방식**: `/assets/img/posts/YYYY-MM-DD-제목/파일명.png`

### 커버 이미지 설정
```yaml
image:
  path: /assets/img/posts/YYYY-MM-DD-제목/cover.jpg
  alt: "이미지 설명"
```

## 크로스 플랫폼 (Windows / Mac)

- Synology Drive를 통해 Windows와 Mac에서 공유
- `.gitattributes`에서 `* text=auto eol=lf`로 줄바꿈 통일 (LF)
- Ruby/Jekyll 환경은 각 OS에서 `bundle install`로 별도 설치
- 파일명 대소문자를 일관되게 유지할 것 (Mac/Windows 모두 기본적으로 대소문자 구분 안 함)
- 동시에 양쪽에서 같은 파일 수정 금지 — Git을 통해 변경사항 관리

## 주의사항

- `assets/lib/`는 Git submodule이므로 직접 수정하지 않음
- Gemfile.lock은 .gitignore에 포함됨
- 이미지는 `assets/img/posts/` 폴더에 포스트별로 저장
